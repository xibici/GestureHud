"""Gesture HUD: swipe left half of the screen to adjust brightness, right
half to adjust volume - like Bilibili's mobile player, but for a Windows
tablet/handheld (touch is promoted to mouse events by Windows, so this also
works with a regular mouse for testing on a desktop)."""
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import singleton


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_elevated() -> bool:
    """Re-run this process elevated via a UAC prompt. True if that was accepted."""
    SW_SHOWNORMAL = 1
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        exe, args = sys.executable, [__file__, *sys.argv[1:]]
    params = " ".join(f'"{a}"' for a in args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, SW_SHOWNORMAL)
    return rc > 32


# Raising the window of an app that needs elevation (MotionAssistant, opened
# by the three-finger tap - see launcher.py) requires this process to be at
# the same integrity level or higher: Windows' UIPI unconditionally blocks
# SetForegroundWindow/AttachThreadInput from a lower-integrity caller, with no
# workaround available from this side. Confirmed live: every tap on that app
# silently fell through to a no-op relaunch (MotionAssistant is single
# instance, so re-running it does nothing - see launcher.py). Hence this
# always runs elevated, checked and fixed up before anything else -
# including the singleton mutex below, which lives in the session's shared
# namespace regardless of integrity level, so acquiring it here first would
# make the elevated relaunch see itself as "already running" and quit.
if not _is_elevated():
    if not _relaunch_elevated():
        ctypes.windll.user32.MessageBoxW(
            None,
            "手势HUD需要管理员权限才能运行(用于三指点击呼出需要提权的应用),"
            "但未能获得授权,程序将退出。",
            "GestureHud",
            0x30,  # MB_ICONWARNING
        )
    sys.exit(0)

# Checked before anything else - including logging setup - so a second
# instance never even opens the shared log file, let alone creates windows
# or hooks that would fight with the first instance's.
if not singleton.acquire():
    singleton.notify_already_running()
    sys.exit(0)

# Must run before any window/monitor query (including Tk init) so that
# GetSystemMetrics, Tk's screen size, and the low-level mouse hook's raw
# coordinates all agree on the same (physical-pixel) coordinate space.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import autostart
from brightness import Brightness
from calibrate import CalibrationWindow
from capture import CaptureOverlay
from hud import Hud
from launcher import AppLauncher
from rawtouch import RawTouchListener
from tray import build_tray
from volume import MediaKeyVolume, Volume
import settings as settings_store

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "GestureHud"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "gesture_hud.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gesture_hud")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()

        self.hud = Hud(self.root, self.screen_w, self.screen_h)

        self.settings = settings_store.load()

        self.brightness = None
        self.volume = None
        try:
            self.brightness = Brightness()
        except Exception:
            log.exception("Brightness control unavailable")
        try:
            # Media keys make Windows raise its own volume OSD; the direct
            # Core Audio path is the fallback when that is turned off.
            self.volume = (MediaKeyVolume() if self.settings["native_volume_osd"]
                           else Volume())
        except Exception:
            log.exception("Volume control unavailable")

        self.events: "queue.Queue" = queue.Queue()

        # Primary input: global raw touch from the digitizer - works anywhere
        # on screen and only observes, so it never interferes with normal
        # touch. The edge strips stay as a fallback for devices where no
        # touchscreen is present (or raw input is unavailable), and they also
        # cover mouse users on a desktop.
        self.rawtouch = None
        try:
            self.rawtouch = RawTouchListener(
                self.events, self.screen_w, self.screen_h,
                required_fingers=self.settings["required_fingers"],
                three_finger_tap=self.settings["three_finger_tap"],
            )
            self.rawtouch.start()
        except Exception:
            log.exception("Raw touch listener unavailable; edge strips only")
            self.rawtouch = None

        self.capture = CaptureOverlay(self.root, self.events, self.screen_w, self.screen_h,
                                      required_fingers=self.settings["required_fingers"])

        self.launcher = AppLauncher(self.settings["three_finger_apps"],
                                    self.settings["three_finger_selected"])

        # A rebuild or a move leaves the logon entry pointing at the old path.
        # Also doubles as the tray's initial autostart state below, so that
        # doesn't need its own separate (and slower - each is a schtasks.exe
        # spawn) round trip through the scheduled task.
        autostart_enabled = autostart.refresh_if_stale()

        # Some devices (this one included) implement WMI brightness SET
        # correctly but never update the CurrentBrightness readback, so we
        # cannot trust a hardware re-read at every gesture start. Track the
        # level ourselves instead, seeded once from a best-effort read.
        self._level = {"left": self._safe_get(self.brightness), "right": self._safe_get(self.volume)}
        self._gesture_baseline = 0.0
        self._anchor_y = 0
        self.range_fraction = self.settings["range_fraction"]
        self.show_hud = self.settings["show_hud"]
        self._native_volume_osd = isinstance(self.volume, MediaKeyVolume)
        self._strips_retired = False
        self._display_check = 0

        # Cheap insurance against hammering WMI/COM with 100+ calls/sec
        # during a fast drag; the HUD still updates every event so the
        # visual feedback stays smooth regardless.
        self._last_set_time = {"left": 0.0, "right": 0.0}
        # Media-key volume needs a beat between updates: each set() reads the
        # current level to work out how many steps to send, and the keys take
        # a moment to land, so firing every frame would overshoot.
        self._set_intervals = {"left": 0.03, "right": 0.05 if self._native_volume_osd else 0.0}

        # Saved state has to be put into effect, not merely shown in the tray:
        # starting with gestures switched off must actually leave them off.
        # Placed here rather than earlier because _apply_enabled reads
        # _strips_retired, which is only set a few lines above.
        if not self.settings["enabled"]:
            self._apply_enabled(False)

        self.tray = build_tray(self._on_toggle_enabled, self._on_toggle_hud,
                               self._on_recalibrate, self._on_quit,
                               on_set_fingers=self._on_set_fingers,
                               initial_fingers=self.settings["required_fingers"],
                               initial_show_hud=self.show_hud,
                               on_toggle_native_osd=self._on_toggle_native_osd,
                               initial_native_osd=self._native_volume_osd,
                               on_set_sensitivity=self._on_set_sensitivity,
                               initial_range_fraction=self.range_fraction,
                               on_toggle_three_finger=self._on_toggle_three_finger,
                               initial_three_finger=self.settings["three_finger_tap"],
                               three_finger_apps=self.settings["three_finger_apps"],
                               initial_three_finger_app=self.settings["three_finger_selected"],
                               on_set_three_finger_app=self._on_set_three_finger_app,
                               on_toggle_autostart=self._on_toggle_autostart,
                               initial_autostart=autostart_enabled,
                               initial_enabled=self.settings["enabled"])
        threading.Thread(target=self.tray.run, daemon=True).start()

        self._calibration_win = None
        self.root.after(15, self._poll_events)
        # No calibration prompt on first run: the digitizer mapping is derived
        # from the display rotation, which is right on normal hardware. The
        # calibration screen stays available from the tray for the odd panel
        # where that rule does not hold.

    @staticmethod
    def _safe_get(controller) -> float:
        if controller is None:
            return 0.5
        try:
            return controller.get()
        except Exception:
            log.exception("Failed to read initial level")
            return 0.5

    def _controller_for(self, zone: str):
        return self.brightness if zone == "left" else self.volume

    def _kind_for(self, zone: str) -> str:
        return "brightness" if zone == "left" else "volume"

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass

        if self.rawtouch is not None:
            self.rawtouch.check_stale()
            self._display_check += 1
            if self._display_check >= 60:  # ~1s at the 15ms poll interval
                self._display_check = 0
                self.rawtouch.refresh_display()

        # Once global raw touch has produced a calibrated gesture it covers the
        # whole screen, so retire the edge strips rather than let both fire for
        # the same swipe.
        if (not self._strips_retired and self.rawtouch is not None
                and self.rawtouch.has_calibrated()):
            self._strips_retired = True
            log.info("global raw touch active; retiring edge strips")
            self.capture.hide()

        self.root.after(15, self._poll_events)

    def _apply(self, controller, zone, fraction, force=False):
        now = time.monotonic()
        if not force and (now - self._last_set_time[zone]) < self._set_intervals[zone]:
            return
        try:
            controller.set(fraction)
        except Exception:
            log.exception("Failed to set %s level", zone)
        self._last_set_time[zone] = now

    def _handle_event(self, event):
        kind = event[0]
        # Not a zone gesture - no brightness/volume controller involved, so
        # this has to come before the zone lookup below.
        if kind == "tap":
            self.launcher.open()
            return

        zone = event[1]
        controller = self._controller_for(zone)
        if controller is None:
            return

        # Windows raises its own OSD for media-key volume changes, so showing
        # our panel too would just stack two indicators on screen.
        want_hud = self.show_hud and not (zone == "right" and self._native_volume_osd)

        if kind == "start":
            _, _, x, y = event
            self._gesture_baseline = self._level[zone]
            self._anchor_y = y
            if want_hud:
                self.hud.update(self._kind_for(zone), self._level[zone])

        elif kind == "move":
            _, _, x, y = event
            delta = (self._anchor_y - y) / (self.screen_h * self.range_fraction)
            fraction = max(0.0, min(1.0, self._gesture_baseline + delta))
            self._apply(controller, zone, fraction)
            self._level[zone] = fraction
            if want_hud:
                self.hud.update(self._kind_for(zone), fraction)

        elif kind == "end":
            # Always commit the final value even if the last move was throttled.
            self._apply(controller, zone, self._level[zone], force=True)
            if want_hud:
                self.hud.schedule_hide()

    def _maybe_calibrate(self, forced=False):
        """Show the calibration screen unless this display is already learnt."""
        if self.rawtouch is None or self._calibration_win is not None:
            return
        if not forced and self.rawtouch.has_calibrated():
            return
        if not forced and not self.rawtouch.saw_touch():
            # No touchscreen has reported yet - do not interrupt with a
            # calibration prompt on a mouse-only machine. Check again later
            # in case a touch happens; the edge strips work meanwhile.
            self.root.after(4000, self._maybe_calibrate)
            return
        log.info("showing touch calibration screen")
        self._calibration_win = CalibrationWindow(
            self.root, self.rawtouch, self.screen_w, self.screen_h,
            on_done=self._on_calibrated,
        )

    def _on_calibrated(self):
        self._calibration_win = None

    def _on_recalibrate(self):
        def start():
            if self.rawtouch is None or self._calibration_win is not None:
                return
            # Re-open buffering on already-locked devices, otherwise the
            # calibration screen would never receive any raw contacts.
            self.rawtouch.begin_recalibration()
            self._maybe_calibrate(forced=True)
        self.root.after(0, start)

    def _on_toggle_enabled(self, enabled: bool):
        self.settings["enabled"] = enabled
        settings_store.save(self.settings)
        self._apply_enabled(enabled)

    def _apply_enabled(self, enabled: bool):
        if self.rawtouch is not None:
            self.rawtouch.set_enabled(enabled)
        if not self._strips_retired:
            self.capture.set_enabled(enabled)

    def _on_toggle_hud(self, show_hud: bool):
        self.show_hud = show_hud
        self.settings["show_hud"] = show_hud
        settings_store.save(self.settings)
        if not show_hud:
            # Tray callbacks run on pystray's own thread, and Tkinter may only
            # be touched from the thread running the main loop - calling
            # schedule_hide() directly raised "RuntimeError: main thread is
            # not in main loop" and aborted the menu action. Hand it to the Tk
            # thread, as _on_recalibrate and _on_quit already do.
            self.root.after(0, lambda: self.hud.schedule_hide(delay_ms=0))

    def _on_toggle_native_osd(self, native: bool):
        # Swapping the volume controller mid-gesture would be fiddly; the
        # setting is read at startup, so just persist it and say so.
        self.settings["native_volume_osd"] = native
        settings_store.save(self.settings)
        log.info("native volume OSD set to %s (applies on next start)", native)

    def _on_toggle_three_finger(self, enabled: bool):
        self.settings["three_finger_tap"] = enabled
        settings_store.save(self.settings)
        if self.rawtouch is not None:
            self.rawtouch.set_three_finger_tap(enabled)

    def _on_toggle_autostart(self, enabled: bool) -> bool:
        """Returns what the scheduled task actually says afterwards, so the
        tray checkmark reflects reality rather than the attempted change."""
        autostart.enable() if enabled else autostart.disable()
        return autostart.is_enabled()

    def _on_set_three_finger_app(self, index: int):
        self.settings["three_finger_selected"] = index
        settings_store.save(self.settings)
        self.launcher.set_selected(index)

    def _on_set_sensitivity(self, range_fraction: float):
        self.range_fraction = range_fraction
        self.settings["range_fraction"] = range_fraction
        settings_store.save(self.settings)
        log.info("swipe range set to %.2f (screen-height fraction for full range)", range_fraction)

    def _on_set_fingers(self, count: int):
        self.settings["required_fingers"] = count
        settings_store.save(self.settings)
        if self.rawtouch is not None:
            self.rawtouch.set_required_fingers(count)
        # The edge-strip fallback can only ever see one finger (Tk's mouse
        # bindings carry just the primary touch point), so it must go inert
        # rather than silently honour a lower finger count than requested.
        self.capture.set_required_fingers(count)

    def _on_quit(self):
        self.root.after(0, self._shutdown)

    def _shutdown(self):
        if self.rawtouch is not None:
            try:
                self.rawtouch.stop()
            except Exception:
                log.exception("Error stopping raw touch listener")
        try:
            self.tray.stop()
        except Exception:
            log.exception("Error stopping tray icon")
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    try:
        App().run()
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()
