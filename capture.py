"""Edge-strip gesture capture.

Testing ruled out two approaches for this device's touchscreen:
- A global WH_MOUSE_LL hook: touch-to-legacy-mouse promotion is delivered
  straight to the window under the touch point (PostMessage), not through
  the low-level input pipeline, so a background hook misses most touches.
- A full-half-screen capture-and-replay overlay: replaying a tap via
  SendInput after toggling WS_EX_TRANSPARENT click-through does not
  reliably keep the replayed click off our own window (Tk's own mouse
  capture on press appears to override the transparency), which produced a
  real, reproducible infinite click-storm loop in testing.
- Raw Input (RIDEV_INPUTSINK) for the mouse device class: confirmed via a
  45s live test that this device's touch does not generate any RIM_TYPEMOUSE
  raw reports at all, so that path sees nothing.

So instead of covering the whole left/right half of the screen, two narrow
strips are pinned to the very left and right screen edges. A real foreground
window's standard mouse bindings reliably see every touch (confirmed with a
live diagnostic), and because the strips are narrow they don't meaningfully
get in the way of normal use - no click-through/replay trickery is needed at
all, so there is no loop risk. The trade-off: a tap that happens to land
inside a strip is simply swallowed (not forwarded to whatever's underneath).
"""
import ctypes
import queue
import tkinter as tk

GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

ACTIVATE_THRESHOLD = 14  # px of mostly-vertical movement needed to claim a drag as our gesture
STRIP_WIDTH = 140  # px, pinned to each screen edge
# Faint tint so the swipe zones are discoverable without being distracting.
STRIP_COLOR = "#3a8fe0"
STRIP_ALPHA = 0.07

IDLE, PENDING, ACTIVE = "idle", "pending", "active"


class _EdgeStrip:
    def __init__(self, root: tk.Tk, events: "queue.Queue", zone: str, x: int, width: int, screen_h: int,
                 required_fingers: int = 1):
        self.events = events
        self.zone = zone
        self.enabled = True
        # Tk's mouse bindings only ever see one pointer - touch-to-mouse
        # promotion carries just the primary finger, so there is no way for
        # this fallback to tell one finger from two. Rather than silently
        # ignore a 2+ finger setting (which exists specifically so a game
        # doesn't misfire on a one-finger drag), a single touch here simply
        # does nothing once more than one finger is required.
        self.required_fingers = required_fingers
        self._state = IDLE
        self._down_x = 0
        self._down_y = 0

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", STRIP_ALPHA)
        self.win.configure(bg=STRIP_COLOR)
        self.win.geometry(f"{width}x{screen_h}+{x}+0")
        self.win.update_idletasks()

        user32 = ctypes.windll.user32
        # winfo_id() hands back Tk's *child* HWND ("TkChild"), which Windows
        # wraps in the real top-level ("TkTopLevel"). Setting the styles on
        # the child left the actual window without WS_EX_NOACTIVATE
        # (confirmed by reading the flags back off both handles), so a strip
        # could take focus from whatever was in front - the strips were
        # observed stealing the foreground during testing.
        hwnd = user32.GetAncestor(self.win.winfo_id(), GA_ROOT) or self.win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        self.win.bind("<ButtonPress-1>", self._on_press)
        self.win.bind("<B1-Motion>", self._on_motion)
        self.win.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        if not self.enabled or self.required_fingers > 1:
            return
        self._state = PENDING
        self._down_x, self._down_y = event.x_root, event.y_root

    def _on_motion(self, event):
        if not self.enabled or self._state == IDLE:
            return
        x, y = event.x_root, event.y_root

        if self._state == PENDING:
            dy = y - self._down_y
            dx = x - self._down_x
            if abs(dy) >= ACTIVATE_THRESHOLD and abs(dy) >= abs(dx):
                self._state = ACTIVE
                self.events.put(("start", self.zone, self._down_x, self._down_y))
                self.events.put(("move", self.zone, x, y))
        elif self._state == ACTIVE:
            self.events.put(("move", self.zone, x, y))

    def _on_release(self, event):
        if self.enabled and self._state == ACTIVE:
            self.events.put(("end", self.zone))
        self._state = IDLE

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self._state = IDLE

    def set_required_fingers(self, count: int):
        self.required_fingers = count
        self._state = IDLE

    def hide(self):
        self.enabled = False
        self._state = IDLE
        try:
            self.win.withdraw()
        except Exception:
            pass


class CaptureOverlay:
    def __init__(self, root: tk.Tk, events: "queue.Queue", screen_w: int, screen_h: int,
                 required_fingers: int = 1):
        self.left = _EdgeStrip(root, events, "left", 0, STRIP_WIDTH, screen_h,
                               required_fingers=required_fingers)
        self.right = _EdgeStrip(root, events, "right", screen_w - STRIP_WIDTH, STRIP_WIDTH, screen_h,
                                required_fingers=required_fingers)

    def set_enabled(self, enabled: bool):
        self.left.set_enabled(enabled)
        self.right.set_enabled(enabled)

    def set_required_fingers(self, count: int):
        self.left.set_required_fingers(count)
        self.right.set_required_fingers(count)

    def hide(self):
        """Retire the strips once global raw touch has proven it works."""
        self.left.hide()
        self.right.hide()
