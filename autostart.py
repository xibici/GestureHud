"""Run at logon, via a Scheduled Task.

Used to be the per-user HKCU\\...\\Run key, which needed no administrator
rights. That stopped working once GestureHud started requiring elevation
(see main.py): Windows does not start a Run-key entry whose target's
manifest demands elevation - it is silently skipped at logon, with no error
and no prompt, because the interactive UAC dialog cannot be shown that
early in the logon sequence.

A Scheduled Task with "run with highest privileges" and a logon trigger is
the documented way around this: creating such a task requires admin rights
(which this code always has by the time it runs, since GestureHud
self-elevates before anything else - see main.py), and Windows trusts a
task registered that way to run elevated at logon without prompting again.
"""
import logging
import re
import subprocess
import sys
import winreg
from pathlib import Path

log = logging.getLogger("gesture_hud.autostart")

TASK_NAME = "GestureHud"
CREATE_NO_WINDOW = 0x08000000

# Where autostart used to live, before GestureHud required elevation.
_LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LEGACY_VALUE_NAME = "GestureHud"


def _current_exe() -> Path:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return exe
    # Running from source: prefer pythonw.exe so a console does not flash up
    # at every logon.
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.is_file() else exe


def _current_args() -> str:
    """The argument portion of command(), for comparison against what's registered."""
    if getattr(sys, "frozen", False):
        return ""
    return f'"{Path(__file__).with_name("main.py")}"'


def command() -> str:
    """The command line that should be registered for this install."""
    args = _current_args()
    return f'"{_current_exe()}"' + (f" {args}" if args else "")


def _run_schtasks(args: list) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(
            ["schtasks", *args], capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        log.exception("Could not run schtasks %s", args)
        return None


def _registered_parts() -> "tuple[str, str] | None":
    """(exe path, arguments) currently registered, or None if there is no task."""
    result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    if result is None or result.returncode != 0:
        return None
    xml = result.stdout
    cmd_match = re.search(r"<Command>(.*?)</Command>", xml, re.S)
    if not cmd_match:
        return None
    args_match = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S)
    exe = cmd_match.group(1).strip().strip('"')
    args = args_match.group(1).strip() if args_match else ""
    return exe, args


def registered() -> "str | None":
    """The executable path currently registered, or None."""
    parts = _registered_parts()
    return parts[0] if parts else None


def is_enabled() -> bool:
    return registered() is not None


def enable() -> bool:
    tr = command()
    # /F: overwrite silently if the task already exists, rather than
    # prompting. /RL HIGHEST + /SC ONLOGON with no /RU/RP runs it elevated
    # for the current user at their own logon, no stored password and no
    # extra prompt beyond the one implied by /RL HIGHEST itself being
    # pre-approved (this code only runs already elevated).
    result = _run_schtasks(["/Create", "/TN", TASK_NAME, "/TR", tr,
                            "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"])
    if result is None:
        return False
    if result.returncode != 0:
        log.error("schtasks /Create failed: %s", result.stderr.strip())
        return False
    log.info("autostart enabled: %s", tr)
    return True


def disable() -> bool:
    # Always attempt the delete rather than pre-checking registered() first:
    # a query failure for a reason other than "task not found" (Task
    # Scheduler service hiccup, policy restriction, ...) would otherwise be
    # indistinguishable from "already gone" and skip a delete that needed to
    # happen, silently leaving the task registered.
    result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if result is None:
        return False
    if result.returncode == 0:
        log.info("autostart disabled")
        return True
    if registered() is None:
        return True  # wasn't there to begin with
    log.warning("schtasks /Delete failed: %s", result.stderr.strip())
    return False


def _migrate_legacy_run_key() -> None:
    """One-time migration from the old HKCU Run-key autostart.

    The Run key cannot start an elevated app at logon (see module
    docstring), so a leftover entry from before GestureHud required
    elevation would sit there silently doing nothing at every logon, while
    the tray checkbox (now backed by the scheduled task) reads "off" -
    looking like the user's autostart preference got reset. Carry it across
    once, and remove the dead entry so it stops appearing in Task Manager's
    Startup tab.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LEGACY_RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.QueryValueEx(key, _LEGACY_VALUE_NAME)
            winreg.DeleteValue(key, _LEGACY_VALUE_NAME)
    except OSError:
        return
    log.info("migrating autostart from the old Run key to a scheduled task")
    enable()


def refresh_if_stale() -> bool:
    """Re-point an existing entry at this executable if it has moved.

    Without this, moving or re-building the exe elsewhere leaves the task
    aimed at a path that no longer starts anything, and the tray would still
    report autostart as on.

    Returns whether autostart is enabled after this call, so a caller that
    already needs that (main.py, for the tray's initial checkbox state)
    doesn't have to make a second round trip through schtasks for it.
    """
    _migrate_legacy_run_key()
    parts = _registered_parts()
    if parts is None:
        return False
    exe, args = parts
    if exe != str(_current_exe()) or args != _current_args():
        log.info("autostart entry was stale (%s %s); updating", exe, args)
        return enable()
    return True
