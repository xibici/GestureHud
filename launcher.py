"""Opening an external app from a gesture.

Two apps are configured for the three-finger tap and they need genuinely
different handling, which is why this is not just a Popen call. Both were
probed live on this machine:

- LegionSettingMenu.exe keeps a *hidden* full-screen window and is summoned
  by running the exe again; the second process signals the resident one and
  exits. Running it once more hides it again (same PID, window visible
  0 -> 1 -> 0), so re-launching gives tap-to-open / tap-to-close for free.
- MotionAssistant.exe was probed as single-instance with a minimized window
  that needed restoring and raising directly, back when GestureHud itself
  ran unelevated - launching it (it needs elevation) failed silently every
  time, which is what made re-running it look like a no-op. Under an
  elevated GestureHud the launch actually succeeds, repeatedly: live
  diagnostics (see _main_window's logging) showed every tap spawning a new
  MotionAssistant.exe process that piled up alongside the earlier ones,
  none of them ever gaining a visible top-level window - not even one
  started directly (outside GestureHud) shows one on this machine. So
  METHOD_RAISE's fallback path below only ever launches once per still-
  running process; see open().

Hence the rule in open(): restore/raise an existing window if there is one,
otherwise run the exe and let the app decide what that means. Legion's
hidden window is deliberately *not* treated as raisable - showing it with
ShowWindow reports success but paints nothing on screen (verified by
screenshot), so it must go through the re-launch path.

Process lookup goes through Toolhelp32 rather than shelling out to
tasklist - the packaged app is built --windowed, and spawning a console
program would flash a console window on every tap.
"""
import ctypes
import logging
import os
import re
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("gesture_hud.launcher")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_RESTORE = 0xF120
SMTO_ABORTIFHUNG = 0x0002

# How an entry is opened. They are not interchangeable - each was chosen from
# what the app actually does when probed (see the module docstring).
METHOD_LAUNCH = "launch"  # run the exe; the app itself decides what that means
METHOD_RAISE = "raise"    # restore/raise its existing window, launch only if absent
METHODS = (METHOD_LAUNCH, METHOD_RAISE)

LEGION_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Lenovo" / "LegionSpace"
LEGION_EXE = "LegionSettingMenu.exe"

# A path of "" means "find the Legion menu at runtime": its install directory
# carries the Legion Space version (this machine has both 1.4.4.21 and
# 1.4.4.31), so a fixed path would break at the next update.
AUTO_LEGION = ""


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _version_key(name: str):
    """Sort '1.4.4.31' above '1.4.4.21' numerically, not as text."""
    parts = re.findall(r"\d+", name)
    return tuple(int(p) for p in parts) if parts else (0,)


def find_legion_menu():
    """Newest installed LegionSettingMenu.exe, or None."""
    try:
        if not LEGION_ROOT.is_dir():
            return None
        candidates = [d / LEGION_EXE for d in LEGION_ROOT.iterdir() if d.is_dir()]
        candidates = [c for c in candidates if c.is_file()]
        return max(candidates, key=lambda p: _version_key(p.parent.name)) if candidates else None
    except Exception:
        log.exception("Could not scan for %s", LEGION_EXE)
        return None


def _pids_for(exe_name: str):
    """PIDs of running processes with this executable name."""
    pids = set()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        target = exe_name.lower()
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                pids.add(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def _main_window(pids):
    """A visible, real top-level window owned by one of `pids`, or None."""
    if not pids:
        return None
    found = []
    candidates = []  # every top-level window owned by `pids`, for diagnosis

    def cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        visible = bool(user32.IsWindowVisible(hwnd))
        has_text = bool(user32.GetWindowTextLengthW(hwnd))
        iconic = bool(user32.IsIconic(hwnd))
        if not has_text:
            candidates.append((hwnd, pid.value, "visible" if visible else "hidden", "untitled", None))
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        size = (rect.right - rect.left, rect.bottom - rect.top)
        candidates.append((hwnd, pid.value, "visible" if visible else "hidden", "titled",
                           ("iconic" if iconic else "normal", size)))
        if iconic:
            # A minimized window's rect is a stub (measured 356x59 for
            # MotionAssistant), so the size filter below would throw away
            # exactly the window being looked for - which is what made the tap
            # fall through to launching it instead of restoring it. Ranked
            # below any real window rather than above: scoring it by a huge
            # constant made a minimized *dialog* beat the actual main window
            # (reproduced with a 900x700 window plus a minimized 300x250 one).
            #
            # Deliberately not gated on `visible` here either: live inspection
            # of MotionAssistant's actual main window (title "Motion
            # Assistant 体感助手 V1.2.0.7") found it carries WS_MINIMIZE but
            # WS_VISIBLE was never set at all - a "start minimized to the
            # tray" app whose form is never Show()'n at startup, not merely
            # parked off-screen.
            found.append((hwnd, 0, 0))
        elif size[0] >= 200 and size[1] >= 200:
            # Also not gated on `visible`: the same window, after being
            # raised once, was later found titled/normal-sized (1944x1152)
            # but hidden again with no WS_MINIMIZE - MotionAssistant hides
            # rather than minimizes on its own (closing it, losing focus,
            # ...), so a tap after that point has to treat "hidden but
            # sized like a real window" as raisable too, not just the
            # iconic case above. else: tooltips, IME, helper windows - all
            # measured at 0x0 or well under this threshold (an AMD ADLX
            # helper window came in at 204x59).
            found.append((hwnd, 1, size[0] * size[1]))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(cb)
    user32.EnumWindows(proc, 0)
    if not found:
        # Not a failure by itself (the app may genuinely not be running), but
        # when it IS running (pids non-empty) and still nothing qualifies,
        # this is the only record of why - there is no other way to tell
        # "no window at all" apart from "a window that got filtered out".
        log.info("no raisable window among %d candidate(s) for pid(s) %s: %s",
                 len(candidates), sorted(pids), candidates)
    return max(found, key=lambda f: (f[1], f[2]))[0] if found else None


def _sys_command(hwnd, command) -> bool:
    """Post a WM_SYSCOMMAND and wait briefly for the app to act on it.

    SendMessageTimeoutW rather than SendMessageW: this runs on the Tk event
    loop, and a plain send into another process's UI thread blocks until that
    thread answers - an unresponsive target would freeze the gestures.
    """
    result = ctypes.c_void_p()
    return bool(user32.SendMessageTimeoutW(
        hwnd, WM_SYSCOMMAND, command, 0, SMTO_ABORTIFHUNG, 1000,
        ctypes.byref(result)))


def _restore(hwnd):
    """Un-minimize a window, including tray apps that ignore ShowWindow.

    MotionAssistant parks its window at (-48000, -48000) when minimized and
    stays iconic through ShowWindow(SW_RESTORE) - measured: the rect and the
    iconic flag both came back unchanged, so "raising" it put nothing on
    screen. Routing SC_RESTORE through the app's own message loop restores it
    properly (to 1944x1152, confirmed by screenshot).
    """
    if _sys_command(hwnd, SC_RESTORE) and not user32.IsIconic(hwnd):
        return
    user32.ShowWindow(hwnd, SW_RESTORE)


def _bring_to_front(hwnd) -> bool:
    """Raise someone else's window to the foreground.

    SetForegroundWindow normally refuses when the caller is not itself the
    foreground process, so the calling thread is briefly attached to the
    target's input queue - the standard workaround, and needed here because
    the tap arrives while a game or the desktop has focus.
    """
    try:
        if user32.IsIconic(hwnd):
            _restore(hwnd)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        fg = user32.GetForegroundWindow()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_thread and fg_thread != target_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, target_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, target_thread, False)
        return ok
    except Exception:
        log.exception("Could not raise window %s", hwnd)
        return False


class AppLauncher:
    """Opens whichever configured app is currently selected."""

    def __init__(self, apps=None, selected: int = 0):
        self.apps = list(apps or [])
        self.selected = selected if 0 <= selected < len(self.apps) else 0

    def set_selected(self, index: int):
        if 0 <= index < len(self.apps):
            self.selected = index
            log.info("three-finger tap now opens %s", self.apps[index].get("name", index))

    def current(self):
        return self.apps[self.selected] if self.apps else None

    @staticmethod
    def resolve(entry) -> "Path | None":
        path = (entry or {}).get("path", AUTO_LEGION)
        if not path:
            return find_legion_menu()
        candidate = Path(path)
        return candidate if candidate.is_file() else None

    def open(self) -> bool:
        entry = self.current()
        target = self.resolve(entry)
        if target is None:
            log.warning("nothing to open for %r", (entry or {}).get("name"))
            return False

        if (entry or {}).get("method") == METHOD_RAISE:
            pids = _pids_for(target.name)
            if pids:
                hwnd = _main_window(pids)
                if hwnd:
                    if hwnd == user32.GetForegroundWindow():
                        # Tap again with it already in front: put it away. It
                        # must NOT fall through to the launch below -
                        # MotionAssistant needs elevation, so that path pops a
                        # UAC prompt on every single tap while the app is
                        # focused (reproduced).
                        # Through the app's own message loop, so a tray app
                        # puts itself away the way it means to - same reason
                        # as _restore().
                        if not _sys_command(hwnd, SC_MINIMIZE):
                            user32.ShowWindow(hwnd, SW_MINIMIZE)
                        log.info("minimized %s", target.name)
                        return True
                    if _bring_to_front(hwnd):
                        log.info("raised existing window of %s", target.name)
                        return True
                    log.warning("found %s but could not raise its window", target.name)
                    return False
                # It is running (>=1 process) but has no window that
                # qualifies as raisable - do NOT fall through to relaunch:
                # reproduced live that this piles up a fresh process on every
                # single tap (MotionAssistant does not dedupe itself the way
                # it seemed to before elevation), none of them ever gaining a
                # visible window either. Repeating an already-failing launch
                # cannot fix that; it only wastes a process each time.
                log.warning("%s is running (%d process(es)) but has no visible window; not relaunching",
                           target.name, len(pids))
                return False
            log.info("%s not running; launching", target.name)

        # ShellExecuteW rather than subprocess.Popen: MotionAssistant.exe
        # requires elevation (it loads a kernel driver), and CreateProcess -
        # which Popen uses - simply fails that with "WinError 740: the
        # requested operation requires elevation" instead of elevating.
        # ShellExecuteW honours the target's manifest and raises the UAC
        # prompt. The working directory is the app's own, since both of
        # these load DLLs and profiles sitting beside them.
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "open", str(target), None, str(target.parent), SW_SHOW)
            if rc > 32:
                log.info("launched %s", target)
                return True
            log.warning("ShellExecute of %s failed (code %s)", target, rc)
            return False
        except Exception:
            log.exception("Failed to launch %s", target)
            return False
