"""Run at logon, via the per-user Run key.

HKCU\\...\\Run rather than a scheduled task or the all-users key: it needs no
administrator rights, and Windows lists it in Task Manager's Startup tab so
the user can turn it off from outside this app too.

The registry is the single source of truth - deliberately not mirrored into
settings.json, so that switching it off in Task Manager (or anywhere else)
cannot drift out of sync with what the tray menu shows.
"""
import logging
import sys
import winreg
from pathlib import Path

log = logging.getLogger("gesture_hud.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "GestureHud"


def command() -> str:
    """The command line that should be registered for this install."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}"'
    # Running from source: prefer pythonw.exe so a console does not flash up
    # at every logon.
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else exe
    return f'"{runner}" "{Path(__file__).with_name("main.py")}"'


def registered() -> "str | None":
    """The command currently registered, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("Could not read the Run key")
        return None


def is_enabled() -> bool:
    return registered() is not None


def enable() -> bool:
    try:
        # CreateKey, not OpenKey: the Run key normally exists, but a fresh
        # profile is not guaranteed to have it.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        log.info("autostart enabled: %s", command())
        return True
    except OSError:
        log.exception("Could not enable autostart")
        return False


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.info("autostart disabled")
        return True
    except FileNotFoundError:
        return True  # already gone
    except OSError:
        log.exception("Could not disable autostart")
        return False


def refresh_if_stale() -> None:
    """Re-point an existing entry at this executable if it has moved.

    Without this, moving or re-building the exe elsewhere leaves a Run entry
    aimed at a path that no longer starts anything, and the tray would still
    report autostart as on.
    """
    current = registered()
    if current is not None and current != command():
        log.info("autostart entry was stale (%s); updating", current)
        enable()
