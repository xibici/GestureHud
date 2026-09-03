"""Small persisted settings store."""
import json
import logging
import os
from pathlib import Path

import launcher

log = logging.getLogger("gesture_hud.settings")

SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", ".")) / "GestureHud" / "settings.json"

DEFAULTS = {
    # Master on/off for the gestures themselves. Persisted like everything
    # else in the tray: without a key here the toggle had nowhere to be
    # stored, so switching gestures off silently came back on at the next
    # start.
    "enabled": True,
    # Two fingers by default: a one-finger drag is exactly what games use for
    # aiming and virtual sticks, so single-finger gestures would fire while
    # playing. Two fingers moving together is effectively never accidental.
    "required_fingers": 2,
    "show_hud": True,
    # Drive volume with media keys so Windows shows its own volume OSD
    # instead of our panel. There is no equivalent for brightness - the
    # native brightness OSD is only raised by OEM ACPI hotkeys - so
    # brightness keeps using the built-in HUD either way.
    "native_volume_osd": True,
    # Fraction of screen height a swipe needs to cover to swing the value
    # 0% -> 100%. Smaller = more sensitive (a short swipe maxes out fast).
    # 0.55 (the original default) turned out too twitchy - a small swipe
    # already pinned the value at 0 or 100 - so the shipped default asks for
    # a longer, more deliberate swipe for the same full-range change.
    "range_fraction": 0.9,
    # Three-finger tap opens one of these; which one is picked in the tray.
    # An empty path means "find the Legion menu at runtime" - its install
    # directory carries the Legion Space version, so a fixed path would
    # break at the next update (see launcher.py).
    "three_finger_tap": True,
    # "method" is per app because the two behave differently when probed:
    # re-running the Legion menu is how it is shown and hidden, while
    # MotionAssistant ignores a re-run, sits minimized, and needs elevation
    # to start - so it gets restored/raised instead. See launcher.py.
    "three_finger_apps": [
        {"name": "Legion 设置菜单", "path": "", "method": "launch"},
        {"name": "MotionAssistant", "method": "raise",
         "path": r"C:\MotionAssistant_1.2.0.7\MotionAssistant\MotionAssistant.exe"},
    ],
    "three_finger_selected": 0,
}

# Presets offered in the tray menu; "range_fraction" must be one of these.
SENSITIVITY_PRESETS = {
    "high": 0.5,     # short swipe -> full range, closest to the old default
    "medium": 0.9,   # shipped default
    "low": 1.4,      # long, deliberate swipe needed -> finer control
}
RANGE_FRACTION_BOUNDS = (0.2, 3.0)


def load():
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not read settings; using defaults")

    # A hand-edited or corrupted file can put anything in required_fingers -
    # in particular 0, which makes the gesture state machine divide by zero
    # on every finger-lift report (reproduced directly). Every consumer reads
    # through this loader, so validate once here rather than at each call site.
    try:
        fingers = int(data.get("required_fingers", 2))
        data["required_fingers"] = fingers if 1 <= fingers <= 3 else 2
    except (TypeError, ValueError):
        data["required_fingers"] = 2

    try:
        lo, hi = RANGE_FRACTION_BOUNDS
        rf = float(data.get("range_fraction", DEFAULTS["range_fraction"]))
        data["range_fraction"] = rf if lo <= rf <= hi else DEFAULTS["range_fraction"]
    except (TypeError, ValueError):
        data["range_fraction"] = DEFAULTS["range_fraction"]

    data["enabled"] = bool(data.get("enabled", True))
    data["three_finger_tap"] = bool(data.get("three_finger_tap", True))

    # Keep only well-formed entries; an empty or corrupted list falls back to
    # the defaults rather than leaving the tap wired to nothing.
    apps = data.get("three_finger_apps")
    if isinstance(apps, list):
        # dict(a), not a: data starts as a shallow copy of DEFAULTS, so when
        # the file supplies no list of its own these entries *are* DEFAULTS'
        # dicts, and anything editing a loaded entry would write straight
        # back into the module-level defaults.
        apps = [dict(a) for a in apps
                if isinstance(a, dict) and isinstance(a.get("path", ""), str)]
    else:
        # Anything that is not a list at all (a stray string, say) has to be
        # discarded outright: a truthy non-list would otherwise sail through
        # the `or` below and get used as the app list, and "nope" would read
        # back as four one-character entries.
        apps = []
    # Copy the default entries rather than aliasing them, so nothing that
    # edits an entry later writes through into DEFAULTS.
    apps = apps or [dict(a) for a in DEFAULTS["three_finger_apps"]]
    # Fill in a missing/invalid method from the default entry for the same
    # path before falling back to "launch". Settings files written before
    # "method" existed list these very apps without one, and blanket-defaulting
    # them to "launch" put MotionAssistant back on the path that needs
    # elevation - a UAC prompt per tap, for anyone with an existing config.
    known = {a.get("path", ""): a["method"] for a in DEFAULTS["three_finger_apps"]}
    for entry in apps:
        if entry.get("method") not in launcher.METHODS:
            entry["method"] = known.get(entry.get("path", ""), launcher.METHOD_LAUNCH)
    data["three_finger_apps"] = apps

    try:
        index = int(data.get("three_finger_selected", 0))
    except (TypeError, ValueError):
        index = 0
    data["three_finger_selected"] = index if 0 <= index < len(data["three_finger_apps"]) else 0
    return data


def save(data):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        log.exception("Could not persist settings")
