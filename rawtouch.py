"""Global touch capture via Raw Input from the digitizer (touchscreen).

Why this and not the alternatives (all tested on this device and rejected):
- WH_MOUSE_LL global hook: Windows delivers touch-promoted mouse messages
  straight to the window under the touch point, bypassing the low-level
  input pipeline, so a background hook misses most touches.
- Full-screen capture overlay + SendInput replay: replaying a tap after
  toggling WS_EX_TRANSPARENT does not reliably keep the synthetic click off
  our own window, producing a reproducible infinite click-storm.
- Raw Input on the *mouse* usage page (0x01/0x02): confirmed over a 45s live
  test that touch generates zero RIM_TYPEMOUSE reports here.

Raw Input on the *digitizer* usage page (0x0D/0x04) reads the touchscreen's
HID reports directly. It purely observes - it never blocks, swallows or
replays input - so normal touch keeps working everywhere and there is no
feedback-loop risk. Contact position comes from the HID report parsed with
hid.dll's HidP_* functions, and is scaled from the digitizer's logical
range to screen pixels.
"""
import ctypes
import json
import logging
import os
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("gesture_hud.rawtouch")

CALIBRATION_FILE = Path(os.environ.get("LOCALAPPDATA", ".")) / "GestureHud" / "calibration.json"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
hid = ctypes.windll.hid

WM_INPUT = 0x00FF
WM_QUIT = 0x0012
WM_DESTROY = 0x0002

RIM_TYPEHID = 2
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDEV_INPUTSINK = 0x00000100
WS_POPUP = 0x80000000

HIDP_STATUS_SUCCESS = 0x00110000
HidP_Input = 0

USAGE_PAGE_DIGITIZER = 0x0D
USAGE_TOUCHSCREEN = 0x04
USAGE_CONTACT_ID = 0x51
USAGE_CONTACT_COUNT = 0x54
USAGE_TIP_SWITCH = 0x42
USAGE_PAGE_GENERIC = 0x01
USAGE_X = 0x30
USAGE_Y = 0x31

ACTIVATE_THRESHOLD = 14  # px of mostly-vertical movement before we claim the drag
IDLE, PENDING, ACTIVE = "idle", "pending", "active"

# Three-finger tap (opens an external app). A tap, not a swipe: it must stay
# put and be brief, so it cannot be confused with a three-finger drag when
# the swipe gesture itself is set to three fingers.
TAP_FINGERS = 3
TAP_MAX_MOVE = 55        # px the centroid may wander and still count as a tap
TAP_MAX_SECONDS = 0.9    # a longer hold is not a tap
# Only long enough to swallow a finger bouncing back onto the panel as a
# second "tap". It used to be 1.5s, on the theory that it stopped one tap
# firing twice as the fingers came up - but _fire_tap already disarms for
# that, so all the long window really did was eat deliberate repeat taps:
# live capture showed identical [2,3,2] taps firing one moment and being
# dropped the next, purely on timing.
TAP_COOLDOWN = 0.4


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class HIDP_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", wintypes.USHORT), ("UsageMax", wintypes.USHORT),
        ("StringMin", wintypes.USHORT), ("StringMax", wintypes.USHORT),
        ("DesignatorMin", wintypes.USHORT), ("DesignatorMax", wintypes.USHORT),
        ("DataIndexMin", wintypes.USHORT), ("DataIndexMax", wintypes.USHORT),
    ]


class HIDP_NOTRANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT), ("Reserved1", wintypes.USHORT),
        ("StringIndex", wintypes.USHORT), ("Reserved2", wintypes.USHORT),
        ("DesignatorIndex", wintypes.USHORT), ("Reserved3", wintypes.USHORT),
        ("DataIndex", wintypes.USHORT), ("Reserved4", wintypes.USHORT),
    ]


class HIDP_UNION(ctypes.Union):
    _fields_ = [("Range", HIDP_RANGE), ("NotRange", HIDP_NOTRANGE)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("u", HIDP_UNION),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# A digitizer panel can be mounted at any rotation relative to the desktop
# (this device's panel is natively portrait 1600x2560 behind a 1920x1200
# landscape desktop, i.e. rotated 90 degrees). Rather than hard-coding a
# guess, every orientation in the dihedral group is a candidate and the
# right one is picked at runtime - see _DeviceInfo.calibrate.
TRANSFORMS = {
    "identity":        lambda nx, ny: (nx, ny),
    "rot90":           lambda nx, ny: (ny, 1.0 - nx),
    "rot180":          lambda nx, ny: (1.0 - nx, 1.0 - ny),
    "rot270":          lambda nx, ny: (1.0 - ny, nx),
    "flip_x":          lambda nx, ny: (1.0 - nx, ny),
    "flip_y":          lambda nx, ny: (nx, 1.0 - ny),
    "transpose":       lambda nx, ny: (ny, nx),
    "anti_transpose":  lambda nx, ny: (1.0 - ny, 1.0 - nx),
}

# The mouse cursor follows touch, but measured ~0.4s behind - during a swipe
# it is still near where the finger started, so mid-drag readings are useless
# as ground truth. Sampling instead happens after the finger lifts: motion has
# stopped, so once the cursor settles it sits exactly on the last contact
# point. One clean sample per gesture, and a handful at different spots pins
# the orientation down.
CALIBRATION_MIN_SAMPLES = 6
CALIBRATION_MAX_ERR = 60  # px, summed |dx|+|dy|
CALIBRATION_MARGIN = 3.0  # winner must beat runner-up by this factor
# ...and by this many px outright. A ratio alone is not enough: a stroke that
# runs straight down the middle of the screen fits `identity` and `flip_x`
# equally well (both zero error), and 0 >= 0 * 3 would "pass", locking an
# arbitrary one of the two - a coin flip on whether left/right end up swapped.
CALIBRATION_MIN_SEPARATION = 120

# A digitizer is bonded to the panel, so its axes follow the panel's native
# orientation while the desktop may be rotated on top of it. The mapping is
# therefore fully determined by the display rotation - no user calibration
# needed. This table follows GestureSign (GPL-2.0, TransposonY/GestureSign,
# GestureSign.Daemon/Input/HidDevice.cs), whose GetCurrentScreenOrientation /
# GetCoordinate pair has been exercised on real Windows tablets for years:
# at 90 and 270 degrees the axes swap, and each rotation inverts a different
# axis. Note the naming looks "backwards" - Windows' 90 corresponds to a 270
# degree transform of the contact point - which is exactly the ambiguity that
# cannot be settled from geometry alone.
ORIENTATION_TRANSFORM = {
    0: "identity",   # DMDO_DEFAULT
    1: "rot270",     # DMDO_90
    2: "rot180",     # DMDO_180
    3: "rot90",      # DMDO_270
}


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


ENUM_CURRENT_SETTINGS = -1


def derive_transform(dx, dy, screen_w, screen_h, orientation):
    """Choose the contact-to-screen mapping for a digitizer.

    The display rotation determines it for a panel-bonded digitizer, which is
    the normal case. But not every digitizer reports in panel-native axes -
    Windows' virtual injection digitizer, for one, already reports desktop
    coordinates. Those two cases are told apart by aspect ratio: a
    panel-native device under a 90/270 rotation has its long axis across the
    desktop's short one, whereas an already-aligned device does not. When the
    rotation and the aspect disagree, the device is already desktop-aligned.
    """
    derived = ORIENTATION_TRANSFORM.get(orientation, "identity")
    if dx <= 0 or dy <= 0 or screen_h <= 0:
        return derived

    screen_aspect = screen_w / screen_h
    direct = dx / dy
    swapped = dy / dx
    # Ambiguous for a square-ish digitizer; then just trust the rotation.
    if abs(direct - swapped) < 0.05:
        return derived

    aspect_wants_swap = abs(swapped - screen_aspect) < abs(direct - screen_aspect)
    if aspect_wants_swap != (orientation in (1, 3)):
        log.info("digitizer %dx%d looks desktop-aligned at rot%d; using identity",
                 dx, dy, orientation * 90)
        return "identity"
    return derived


def display_orientation():
    """Current desktop rotation as 0/1/2/3 (DMDO_DEFAULT/90/180/270)."""
    try:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            return int(dm.dmDisplayOrientation)
    except Exception:
        log.exception("Could not read display orientation")
    return 0
CALIBRATION_PAIR_WINDOW = 0.08  # s; how closely a raw report must match a UI event
STALE_GESTURE_SECONDS = 1.0  # close a gesture if contacts stop arriving


def _load_calibration():
    try:
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_calibration(data):
    try:
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        log.exception("Could not persist calibration")


class _DeviceInfo:
    """Cached HID preparsed data, logical ranges, and orientation mapping."""

    def __init__(self, preparsed, x_min, x_max, y_min, y_max):
        self.preparsed = preparsed
        self.x_min, self.x_max = x_min, x_max
        self.y_min, self.y_max = y_min, y_max
        self.samples = []  # (nx, ny, cursor_x, cursor_y) gathered while moving
        self.locked = False
        self.transform_name = None
        self._transform = None
        self.key = None
        self.contact_slots = []  # HID link collections, one per touch contact
        self.source = None  # "derived" from display rotation, or "user" calibrated
        self.last_failure = None  # "ambiguous" | "poor_fit", to guide the user

    def apply_transform(self, name, source):
        self.transform_name = name
        self._transform = TRANSFORMS[name]
        self.source = source
        self.locked = True

    def normalize(self, x_raw, y_raw):
        nx = (x_raw - self.x_min) / (self.x_max - self.x_min)
        ny = (y_raw - self.y_min) / (self.y_max - self.y_min)
        return min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)

    def to_screen(self, nx, ny, screen_w, screen_h):
        fx, fy = self._transform(nx, ny)
        return int(fx * (screen_w - 1)), int(fy * (screen_h - 1))

    def fit(self, pairs, screen_w, screen_h):
        """Solve the orientation from (nx, ny, screen_x, screen_y) pairs.

        The screen coordinates come from real UI touch events, so they are
        exact - no cursor lag to work around. Pairs must span more than one
        line on screen, because some orientations coincide along a line (e.g.
        rot90 and flip_x agree wherever nx + ny == 1). Returns True on lock.
        """
        self.samples = list(pairs)
        if len(self.samples) < CALIBRATION_MIN_SAMPLES:
            return False

        # Score on the 75th percentile rather than the worst case: pairing raw
        # reports to UI events by timestamp mismatches occasionally, and a
        # single bad pair should not veto an otherwise perfect fit.
        scored = []
        for name, fn in TRANSFORMS.items():
            errs = []
            for nx_s, ny_s, cx, cy in self.samples:
                fx, fy = fn(nx_s, ny_s)
                errs.append(abs(fx * (screen_w - 1) - cx) + abs(fy * (screen_h - 1) - cy))
            errs.sort()
            scored.append((errs[int(len(errs) * 0.75)], name))
        scored.sort()

        best_err, best_name = scored[0]
        runner_err = scored[1][0] if len(scored) > 1 else float("inf")

        separated = (runner_err >= best_err * CALIBRATION_MARGIN
                     and runner_err - best_err >= CALIBRATION_MIN_SEPARATION)
        if best_err <= CALIBRATION_MAX_ERR and separated:
            self.apply_transform(best_name, source="user")
            log.info("digitizer orientation locked: %s (worst err %.0fpx, next best %s %.0fpx)",
                     best_name, best_err, scored[1][1], runner_err)
            self.samples = []
            return True

        log.info("orientation not decisive from %d pairs (best %s %.0fpx, next %s %.0fpx)",
                 len(self.samples), best_name, best_err, scored[1][1], runner_err)
        self.last_failure = (
            "ambiguous" if best_err <= CALIBRATION_MAX_ERR else "poor_fit"
        )
        return False


class RawTouchListener:
    """Observes global touch contacts and emits gesture events on a queue.

    Emits the same ("start"|"move"|"end", zone, x, y) tuples the edge-strip
    capture used, so the rest of the app is unchanged.
    """

    def __init__(self, events: "queue.Queue", screen_w: int, screen_h: int,
                 zone_for_x=None, activate_threshold: int = ACTIVATE_THRESHOLD,
                 required_fingers: int = 2, three_finger_tap: bool = True):
        self.events = events
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.activate_threshold = activate_threshold
        # How many fingers must be down for a swipe to count. Two is the safe
        # default: a one-finger drag is what games use for aiming, virtual
        # sticks and swipe controls, so single-finger mode misfires constantly
        # while playing. Two fingers moving together is not something a game
        # asks for by accident.
        #
        # Clamped here, not just in set_required_fingers(): this value can
        # arrive straight from settings.json, and an unvalidated 0 makes
        # _on_frame divide by zero on every single finger-lift report -
        # confirmed by reproducing it directly.
        self.required_fingers = self._clamp_fingers(required_fingers)
        self.enabled = True
        self.orientation = display_orientation()
        self._zone_for_x = zone_for_x or (lambda x: "left" if x < self.screen_w / 2 else "right")

        self._devices = {}   # hDevice -> _DeviceInfo (or False if unusable)
        self._by_key = {}    # panel geometry key -> _DeviceInfo
        self._saved = _load_calibration()
        self._state = IDLE
        self._zone = None
        self._down_x = 0
        self._down_y = 0
        self._active_contact = None
        self._last_contact_time = 0.0
        # Three-finger tap tracking, kept separate from the swipe state
        # machine above so both can watch the same contacts.
        self.three_finger_tap = three_finger_tap
        self._tap_peak = 0          # most fingers seen so far this touch
        self._tap_armed = False
        self._tap_start = 0.0
        self._tap_origin = (0.0, 0.0)
        self._tap_last_fired = 0.0
        # Raw contacts seen while uncalibrated, for the calibration screen.
        self.raw_buffer = []
        self.recalibrating = False
        self.last_failure = None

        self._hwnd = None
        self._thread = None
        self._thread_id = None
        self._proc_ref = WNDPROC(self._wnd_proc)

    # ---- HID parsing -------------------------------------------------

    def _device_info(self, hDevice):
        info = self._devices.get(hDevice)
        if info is not None:
            return info

        size = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
        if size.value == 0:
            self._devices[hDevice] = False
            return False
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, buf, ctypes.byref(size)) <= 0:
            self._devices[hDevice] = False
            return False

        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(buf, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            self._devices[hDevice] = False
            return False

        n = wintypes.USHORT(caps.NumberInputValueCaps)
        if n.value == 0:
            self._devices[hDevice] = False
            return False
        vcaps = (HIDP_VALUE_CAPS * n.value)()
        if hid.HidP_GetValueCaps(HidP_Input, vcaps, ctypes.byref(n), buf) != HIDP_STATUS_SUCCESS:
            self._devices[hDevice] = False
            return False

        # A multi-touch digitizer publishes one link collection per contact
        # slot (10 on this panel), each carrying its own ContactID/X/Y, with
        # ContactCount at the top level. Collect the slots so every finger can
        # be read, not just the first.
        x_min = x_max = y_min = y_max = None
        has_x, has_y = set(), set()
        for vc in vcaps[: n.value]:
            usage = vc.u.Range.UsageMin if vc.IsRange else vc.u.NotRange.Usage
            if vc.UsagePage == USAGE_PAGE_GENERIC and usage == USAGE_X:
                x_min, x_max = vc.LogicalMin, vc.LogicalMax
                has_x.add(vc.LinkCollection)
            elif vc.UsagePage == USAGE_PAGE_GENERIC and usage == USAGE_Y:
                y_min, y_max = vc.LogicalMin, vc.LogicalMax
                has_y.add(vc.LinkCollection)

        if x_max is None or y_max is None or x_max <= x_min or y_max <= y_min:
            self._devices[hDevice] = False
            return False
        contact_slots = sorted(has_x & has_y)

        # Key on the panel geometry rather than the device handle: a handle can
        # be recreated (Windows hands out a fresh one per touch-injection
        # session, for instance), and calibration samples must survive that.
        # The desktop size is part of the key so rotating the display relearns.
        key = f"{x_max - x_min}x{y_max - y_min}@rot{self.orientation}"
        info = self._by_key.get(key)
        if info is None:
            info = _DeviceInfo(buf, x_min, x_max, y_min, y_max)
            info.key = key
            info.contact_slots = contact_slots
            log.info("digitizer has %d contact slot(s)", len(contact_slots))
            saved = self._saved.get(key)
            if saved in TRANSFORMS:
                # A mapping the user calibrated by hand always wins.
                info.apply_transform(saved, source="user")
                log.info("digitizer ready: X %d..%d Y %d..%d, orientation %s (calibrated)",
                         x_min, x_max, y_min, y_max, saved)
            else:
                derived = derive_transform(x_max - x_min, y_max - y_min,
                                           self.screen_w, self.screen_h, self.orientation)
                info.apply_transform(derived, source="derived")
                log.info("digitizer ready: X %d..%d Y %d..%d, orientation %s "
                         "(derived from %d degree display rotation)",
                         x_min, x_max, y_min, y_max, derived, self.orientation * 90)
            self._by_key[key] = info
        else:
            # Same panel, new handle - keep the calibration, but everything
            # derived from the HID descriptor belongs to this handle and must
            # be refreshed together. Keeping stale contact slots here silently
            # broke multi-finger parsing after the descriptor changed.
            info.preparsed = buf
            if info.contact_slots != contact_slots:
                log.info("digitizer descriptor changed: %d -> %d contact slot(s)",
                         len(info.contact_slots), len(contact_slots))
                info.contact_slots = contact_slots

        self._devices[hDevice] = info
        return info

    def _read_contacts(self, info, report, report_len):
        """Return [(contact_id, norm_x, norm_y)] for every finger currently down.

        Each contact lives in its own HID link collection, so the usages are
        queried per collection rather than globally - querying collection 0
        would merge all fingers and report "some finger is down" with only the
        first one's coordinates.
        """
        value = ctypes.c_ulong(0)

        def get_value(usage_page, usage, collection):
            status = hid.HidP_GetUsageValue(
                HidP_Input, usage_page, collection, usage, ctypes.byref(value),
                info.preparsed, report, report_len,
            )
            return value.value if status == HIDP_STATUS_SUCCESS else None

        # ContactCount says how many slots in *this* report are meaningful;
        # slots past it hold stale data from an earlier frame.
        reported = get_value(USAGE_PAGE_DIGITIZER, USAGE_CONTACT_COUNT, 0)
        slots = info.contact_slots
        if reported and 0 < reported <= len(slots):
            slots = slots[:reported]

        max_usages = hid.HidP_MaxUsageListLength(HidP_Input, USAGE_PAGE_DIGITIZER, info.preparsed)
        contacts = []
        for collection in slots:
            tip_down = False
            if max_usages > 0:
                usages = (wintypes.USHORT * max_usages)()
                length = ctypes.c_ulong(max_usages)
                status = hid.HidP_GetUsages(
                    HidP_Input, USAGE_PAGE_DIGITIZER, collection, usages,
                    ctypes.byref(length), info.preparsed, report, report_len,
                )
                if status == HIDP_STATUS_SUCCESS:
                    tip_down = USAGE_TIP_SWITCH in usages[: length.value]
            if not tip_down:
                continue

            x_raw = get_value(USAGE_PAGE_GENERIC, USAGE_X, collection)
            y_raw = get_value(USAGE_PAGE_GENERIC, USAGE_Y, collection)
            if x_raw is None or y_raw is None:
                continue
            cid = get_value(USAGE_PAGE_DIGITIZER, USAGE_CONTACT_ID, collection)
            nx, ny = info.normalize(x_raw, y_raw)
            contacts.append((cid if cid is not None else collection, nx, ny))
        return contacts

    # ---- gesture state machine ---------------------------------------

    def refresh_display(self):
        """Track desktop rotation/resolution changes.

        Both the cached screen size and the derived orientation go stale when
        the desktop rotates - on a handheld that happens whenever the device
        is turned. Devices are re-keyed so each rotation keeps its own
        mapping (and its own hand calibration, if any).
        """
        orientation = display_orientation()
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if orientation == self.orientation and w == self.screen_w and h == self.screen_h:
            return
        log.info("display changed: %dx%d rot%d -> %dx%d rot%d",
                 self.screen_w, self.screen_h, self.orientation * 90, w, h, orientation * 90)
        self.orientation = orientation
        self.screen_w, self.screen_h = w, h
        self._zone_for_x = lambda x: "left" if x < w / 2 else "right"
        # Force re-resolution of every device against the new configuration.
        self._devices.clear()
        self._by_key.clear()
        self.raw_buffer.clear()
        self._state = IDLE
        self._zone = None
        self._active_contact = None
        self._reset_tap()

    def check_stale(self):
        """Close a gesture whose finger-up report never arrived.

        Not every digitizer emits a final tip-up report. Without this the
        state machine would sit in ACTIVE forever: the HUD would never hide
        and the next touch would be treated as a continuation of the old
        gesture, measured from a stale anchor point.
        """
        if time.monotonic() - self._last_contact_time <= STALE_GESTURE_SECONDS:
            return
        # Drop any half-seen tap. Checked ahead of the IDLE bail-out below,
        # not after it: a three-finger touch leaves the swipe machine IDLE
        # whenever swipes are set to a different finger count, so an armed
        # tap would otherwise survive the gap and fire off a stale contact
        # the next time any finger touched down.
        self._reset_tap()
        if self._state == IDLE:
            return
        log.info("no contact report for %.1fs; closing stuck gesture", STALE_GESTURE_SECONDS)
        if self._state == ACTIVE:
            self.events.put(("end", self._zone))
        self._state = IDLE
        self._zone = None
        self._active_contact = None

    def _reset_tap(self):
        self._tap_peak = 0
        self._tap_armed = False

    def _track_tap(self, points, count):
        """Detect a three-finger tap and emit a ("tap", 3) event.

        Runs alongside the swipe state machine on the same contact stream,
        and deliberately keys off the *peak* finger count of the whole touch
        rather than a single frame: fingers neither land nor lift at the same
        instant, so a three-finger tap actually reports 1, 2, 3, 2, 1, 0
        contacts in sequence. Watching the peak means the ascending 1 and 2
        frames do not look like a one- or two-finger touch, and a four-finger
        rest (a palm) is rejected outright rather than being mistaken for a
        tap as it lifts back through three.

        Firing happens on the first finger *lifting* (3 -> fewer) instead of
        waiting for a zero-contact report, because not every digitizer sends
        one - the same gap check_stale() exists to cover.
        """
        if not self.three_finger_tap:
            return
        now = time.monotonic()
        if count == 0:
            # Every contact released within a single report. Handled here as
            # well as at the 3 -> fewer transition below, because a quick tap
            # can go straight from three contacts to none with no descending
            # frames in between - which the transition check alone misses.
            if self._tap_armed and self._tap_peak == TAP_FINGERS:
                self._fire_tap(now)
            self._reset_tap()
            return

        cx = sum(p[0] for p in points) / count
        cy = sum(p[1] for p in points) / count

        if count > self._tap_peak:
            self._tap_peak = count
            if count == TAP_FINGERS:
                self._tap_armed = True
                self._tap_start = now
                self._tap_origin = (cx, cy)
            elif count > TAP_FINGERS:
                self._tap_armed = False  # palm or four fingers: never a tap
            return

        if not self._tap_armed:
            return

        if count == TAP_FINGERS:
            moved = abs(cx - self._tap_origin[0]) + abs(cy - self._tap_origin[1])
            if moved > TAP_MAX_MOVE or now - self._tap_start > TAP_MAX_SECONDS:
                # A drag or a hold, not a tap - and if the swipe gesture is
                # also set to three fingers, this is that swipe.
                self._tap_armed = False
            return

        # count < TAP_FINGERS with peak == TAP_FINGERS: a finger just lifted.
        self._fire_tap(now)

    def _fire_tap(self, now):
        self._tap_armed = False
        if now - self._tap_start > TAP_MAX_SECONDS:
            return  # a hold, not a tap
        if now - self._tap_last_fired < TAP_COOLDOWN:
            return  # one tap must not fire twice as the fingers come up
        self._tap_last_fired = now
        log.info("three-finger tap")
        self.events.put(("tap", TAP_FINGERS))

    def _on_frame(self, points):
        """Handle one report's worth of contacts, in screen coordinates.

        `points` is [(x, y)] for the fingers currently down. A gesture only
        runs while exactly `required_fingers` are touching, and tracks their
        centroid, so a two-finger setting ignores everything a game does with
        one finger.
        """
        if not self.enabled:
            return
        self._last_contact_time = time.monotonic()
        count = len(points)

        self._track_tap(points, count)

        if count != self.required_fingers:
            # Wrong number of fingers: end any gesture in progress and wait.
            if self._state == ACTIVE:
                self.events.put(("end", self._zone))
            self._state = IDLE
            self._zone = None
            return

        cx = sum(p[0] for p in points) / count
        cy = sum(p[1] for p in points) / count

        if self._state == IDLE:
            self._state = PENDING
            self._down_x, self._down_y = cx, cy
            return

        if self._state == PENDING:
            dx, dy = cx - self._down_x, cy - self._down_y
            if abs(dy) >= self.activate_threshold and abs(dy) >= abs(dx):
                self._state = ACTIVE
                self._zone = self._zone_for_x(self._down_x)
                self.events.put(("start", self._zone, self._down_x, self._down_y))
                self.events.put(("move", self._zone, cx, cy))
        elif self._state == ACTIVE:
            self.events.put(("move", self._zone, cx, cy))

    # ---- win32 plumbing ----------------------------------------------

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                                 ctypes.sizeof(RAWINPUTHEADER))
                    if got == size.value:
                        header = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                        if header.dwType == RIM_TYPEHID:
                            self._handle_hid(header, buf)
            except Exception:
                log.exception("Error handling WM_INPUT")
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def calibrate_from_ui(self, ui_events):
        """Pair buffered raw contacts with UI touch events and solve.

        `ui_events` is [(screen_x, screen_y, monotonic_time)] captured by a
        foreground window, whose coordinates Windows has already mapped to the
        desktop - exact ground truth. Returns True if an orientation locked.
        """
        raw = list(self.raw_buffer)
        if not raw or not ui_events:
            log.info("calibration: no data (raw=%d ui=%d)", len(raw), len(ui_events))
            return False

        by_device = {}
        for sx, sy, t in ui_events:
            best = min(raw, key=lambda r: abs(r[3] - t))
            if abs(best[3] - t) > CALIBRATION_PAIR_WINDOW:
                continue
            info, nx, ny, _ = best
            by_device.setdefault(id(info), (info, []))[1].append((nx, ny, sx, sy))

        locked_any = False
        for info, pairs in by_device.values():
            if info.locked and not self.recalibrating:
                continue
            if info.fit(pairs, self.screen_w, self.screen_h):
                locked_any = True
                if info.key:
                    self._saved[info.key] = info.transform_name
                    _save_calibration(self._saved)
        if locked_any:
            self.raw_buffer.clear()
            self.recalibrating = False
        else:
            self.last_failure = next(
                (i.last_failure for i, _ in by_device.values() if i.last_failure), None
            )
        return locked_any

    def begin_recalibration(self):
        """Re-open calibration for devices that already have a mapping.

        Without this a locked device never buffers raw contacts again, so the
        calibration screen would collect UI points, pair them with nothing,
        and fail forever.
        """
        self.recalibrating = True
        self.raw_buffer.clear()
        for info in self._by_key.values():
            info.locked = False
        log.info("recalibration requested for %d digitizer(s)", len(self._by_key))

    def cancel_recalibration(self):
        """Abandon a recalibration, restoring whatever mapping was in use."""
        if not self.recalibrating:
            return
        self.recalibrating = False
        self.raw_buffer.clear()
        for info in self._by_key.values():
            if info._transform is not None:
                info.locked = True
        log.info("recalibration cancelled")

    def _handle_hid(self, header, buf):
        info = self._device_info(header.hDevice)
        if not info:
            return
        base = ctypes.sizeof(RAWINPUTHEADER)
        size_hid = int.from_bytes(buf[base:base + 4], "little")
        count = int.from_bytes(buf[base + 4:base + 8], "little")
        data_off = base + 8
        for i in range(count):
            report = ctypes.cast(
                ctypes.byref(buf, data_off + i * size_hid), ctypes.POINTER(ctypes.c_char)
            )
            contacts = self._read_contacts(info, report, size_hid)

            if not info.locked or self.recalibrating:
                # Until the orientation is known we emit no gestures at all -
                # acting on a half-guessed mapping would drive the wrong
                # control. Raw contacts are buffered with timestamps so the
                # calibration screen can pair them against real UI touch
                # events, which carry exact screen coordinates. Calibration is
                # single-finger, so only take a lone contact.
                if len(contacts) == 1:
                    _, nx, ny = contacts[0]
                    self.raw_buffer.append((info, nx, ny, time.monotonic()))
                    if len(self.raw_buffer) > 600:
                        del self.raw_buffer[:300]
                continue

            self._on_frame([
                info.to_screen(nx, ny, self.screen_w, self.screen_h)
                for _, nx, ny in contacts
            ])

    def _thread_main(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        hInstance = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSW(
            style=0, lpfnWndProc=self._proc_ref, cbClsExtra=0, cbWndExtra=0,
            hInstance=hInstance, hIcon=None, hCursor=None, hbrBackground=None,
            lpszMenuName=None, lpszClassName="GestureHudRawTouch",
        )
        if not user32.RegisterClassW(ctypes.byref(cls)):
            log.error("RegisterClassW failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return
        self._hwnd = user32.CreateWindowExW(
            0, "GestureHudRawTouch", "gesture-hud", WS_POPUP, 0, 0, 0, 0,
            None, None, hInstance, None,
        )
        if not self._hwnd:
            log.error("CreateWindowExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return

        rid = RAWINPUTDEVICE(usUsagePage=USAGE_PAGE_DIGITIZER, usUsage=USAGE_TOUCHSCREEN,
                             dwFlags=RIDEV_INPUTSINK, hwndTarget=self._hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
            log.error("RegisterRawInputDevices failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return
        log.info("raw touch listener started")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="raw-touch")
        self._thread.start()

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    def has_calibrated(self) -> bool:
        """True once some digitizer's orientation is definitively locked."""
        return any(d.locked for d in self._by_key.values())

    def saw_touch(self) -> bool:
        """True once any touchscreen has actually reported a contact."""
        return bool(self._by_key)

    @staticmethod
    def _clamp_fingers(count) -> int:
        try:
            return max(1, min(3, int(count)))
        except (TypeError, ValueError):
            log.warning("invalid required_fingers value %r; using default 2", count)
            return 2

    def set_required_fingers(self, count: int):
        self.required_fingers = self._clamp_fingers(count)
        if self._state == ACTIVE:
            self.events.put(("end", self._zone))
        self._state = IDLE
        self._zone = None
        log.info("gesture now requires %d finger(s)", self.required_fingers)

    def set_three_finger_tap(self, enabled: bool):
        self.three_finger_tap = enabled
        self._reset_tap()
        log.info("three-finger tap %s", "enabled" if enabled else "disabled")

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self._state = IDLE
        self._zone = None
        self._active_contact = None
        self._reset_tap()
