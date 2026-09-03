"""One-off touch calibration screen.

The digitizer reports contacts in its own panel coordinates, which can be
rotated or mirrored relative to the desktop (this device's panel is natively
portrait behind a landscape desktop). Deriving that mapping from the mouse
cursor proved unreliable - a brief tap never moves the cursor at all, and a
sustained one lags it by roughly 0.4s.

So instead we ask for one swipe on a foreground window. Its touch events
carry coordinates Windows has already mapped to the desktop, i.e. exact
ground truth, and the raw HID listener sees the same contacts at the same
moments. Pairing the two by timestamp solves the orientation outright. The
result is saved, so this screen appears once per display configuration.
"""
import logging
import time
import tkinter as tk

log = logging.getLogger("gesture_hud.calibrate")

BG = "#101014"
FG = "#f5f5f7"
ACCENT = "#3a8fe0"

TITLE = "触摸校准"
BODY = "请用手指斜着划一条长线\n(比如从左上划到右下)"
DONE = "校准完成"
RETRY = "没采集到足够数据,请再划一次"
RETRY_AMBIGUOUS = "这条线太规整,分不出方向\n请斜着划,或者划一条拐弯的线"
SKIP = "跳过(继续用边缘滑动)"


class CalibrationWindow:
    def __init__(self, root: tk.Tk, listener, screen_w: int, screen_h: int, on_done=None):
        self.listener = listener
        self.on_done = on_done
        self.ui_events = []
        self._finished = False

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        self.win.geometry(f"{screen_w}x{screen_h}+0+0")

        # Canvas first so the later-created labels stack above it; otherwise
        # the full-size canvas hides the instructions entirely.
        self.trace = tk.Canvas(self.win, bg=BG, highlightthickness=0)
        self.trace.place(x=0, y=0, relwidth=1, relheight=1)

        self.title = tk.Label(self.win, text=TITLE, bg=BG, fg=FG,
                              font=("Microsoft YaHei UI", 34, "bold"))
        self.title.place(relx=0.5, rely=0.34, anchor="center")
        self.body = tk.Label(self.win, text=BODY, bg=BG, fg="#a1a1a6",
                             font=("Microsoft YaHei UI", 18), justify="center")
        self.body.place(relx=0.5, rely=0.45, anchor="center")

        # An opaque full-screen window with only an Escape binding would trap a
        # tablet user: an overrideredirect window usually never gets keyboard
        # focus, and a handheld has no keyboard anyway. Always offer a target
        # that can be dismissed by touch.
        self.skip = tk.Label(self.win, text=SKIP, bg="#2c2c2e", fg="#a1a1a6",
                             font=("Microsoft YaHei UI", 15), padx=28, pady=12)
        self.skip.place(relx=0.5, rely=0.72, anchor="center")
        self.skip.bind("<ButtonPress-1>", self._on_skip)
        self.skip.bind("<ButtonRelease-1>", lambda e: "break")

        # Tk routes events to whichever specific widget is under the pointer,
        # not to the window generally - a touch landing on the title/body text
        # (which sit dead centre, squarely on a natural swipe path) would
        # otherwise be swallowed by a widget with no bindings at all: no dot
        # drawn, no retry message, nothing. The whole stroke silently no-ops
        # and the screen looks stuck. Bind every widget the swipe could touch.
        interactive = (self.win, self.trace, self.title, self.body)
        for widget in interactive:
            widget.bind("<ButtonPress-1>", self._on_point)
            widget.bind("<B1-Motion>", self._on_point)
            widget.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", lambda e: self._finish(False))

    def _on_point(self, event):
        if self._finished:
            return
        # Absolute screen coordinates throughout, not event.x/event.y: this
        # fires from whichever widget the touch happened to land on (win,
        # trace, title or body), each with its own local origin, but the
        # window sits at screen (0,0) so the canvas's own coordinate space
        # coincides with absolute screen space - drawing with x_root/y_root
        # lands the dot correctly regardless of which widget triggered it.
        x, y = event.x_root, event.y_root
        self.ui_events.append((x, y, time.monotonic()))
        r = 6
        self.trace.create_oval(x - r, y - r, x + r, y + r, fill=ACCENT, outline="")

    def _on_release(self, event):
        if self._finished:
            return
        log.info("calibration stroke ended with %d UI points", len(self.ui_events))
        if self.listener.calibrate_from_ui(self.ui_events):
            self._finish(True)
        else:
            # Tell the user *why*, so they vary the stroke instead of
            # repeating the same ambiguous one forever.
            reason = getattr(self.listener, "last_failure", None)
            self.ui_events.clear()
            self.trace.delete("all")
            self.body.configure(
                text=RETRY_AMBIGUOUS if reason == "ambiguous" else RETRY, fg="#ff9f0a")

    def _on_skip(self, event):
        if self._finished:
            return "break"
        log.info("calibration skipped by user")
        self.listener.cancel_recalibration()
        self._finish(False)
        return "break"

    def _finish(self, success: bool):
        self._finished = True
        if success:
            self.title.configure(text=DONE)
            self.body.configure(text="", fg="#a1a1a6")
            self.win.after(700, self._close)
        else:
            self._close()

    def _close(self):
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_done:
            self.on_done()
