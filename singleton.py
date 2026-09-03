"""Enforces a single running instance of GestureHud.

A second instance is not a cosmetic annoyance here - it is a proven source of
real bugs: two sets of edge-capture windows fighting over the same touches,
two HUD windows racing each other, two calibration states stepping on each
other. A named mutex is the standard Windows mechanism for this: the OS
tracks the handle itself and releases it automatically when the process
exits, even on a crash, unlike a lock file which can be left stale.
"""
import ctypes

MUTEX_NAME = "GestureHud_SingleInstance_7F3B2C1A"
ERROR_ALREADY_EXISTS = 183

_mutex_handle = None


def acquire() -> bool:
    """Try to become the one running instance.

    Returns True if this process now holds the lock (safe to proceed), False
    if another instance already holds it. The handle is kept open for the
    life of the process - do not close it early - so it doesn't need calling
    again.
    """
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        # Could not create the mutex at all (very unusual) - fail open rather
        # than permanently block the app from ever starting.
        return True
    _mutex_handle = handle
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def notify_already_running():
    """Tell the user a copy is already running, since a --windowed app that
    just exits silently on a second launch looks like it did nothing."""
    try:
        MB_ICONINFORMATION = 0x40
        ctypes.windll.user32.MessageBoxW(
            None,
            "手势HUD已经在运行了,请检查系统托盘图标。",
            "GestureHud",
            MB_ICONINFORMATION,
        )
    except Exception:
        pass
