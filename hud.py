"""HUD panel shown while a gesture is active.

Built to match Windows' own volume OSD - the small light panel with an icon
and a slider that pops up just above the taskbar when a volume key is
pressed - because that is exactly what the user sees for volume gestures
(MediaKeyVolume taps the real volume keys), and a brightness panel in a
different place and style next to it looks broken.

There is no public API to summon either the OSD or the tray's Quick
Settings flyout on demand - both are Explorer/Shell-internal, only open on
their own trigger, and cannot be auto-dismissed the way a gesture-driven
overlay needs - so this repaints our own panel to look and sit the same way
instead.

Every constant below was measured off a real screenshot of the native OSD
on this display (see PANEL_* / layout comments), not guessed.
"""
import ctypes
import math
import tkinter as tk
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageTk

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SPI_GETWORKAREA = 0x0030
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUNDSMALL = 3
GA_ROOT = 2

# --- Measured from the native volume OSD (1920x1200 @150%) ---------------
PANEL_W = 285
PANEL_H = 71
CORNER_RADIUS = 8
BOTTOM_GAP = 22          # px between panel bottom and the work area's bottom

PANEL_BG = "#f6f7fb"     # sampled (246, 247, 251)
TRACK_COLOR = "#898a8b"  # sampled (137, 137, 139)
FILL_COLOR = "#0067c0"   # sampled (0, 103, 192)
ICON_COLOR = "#292929"   # sampled (41, 41, 41)

ICON_X = 18              # icon's left edge, offset from panel left
# The native OSD's own glyph measures 15x16. These are drawn a little larger
# than that on purpose - at 16 the shapes only inked 12-14px of their box and
# read as noticeably smaller than the system's. There is room: the track does
# not start until offset 61.
ICON_SIZE = 24
TRACK_X1 = 61            # track spans offsets 61..225
TRACK_X2 = 226
TRACK_H = 6
VALUE_RIGHT_MARGIN = 19  # the number is right-aligned this far from the panel's right edge
VALUE_FONT = ("Segoe UI", -20)
# The native OSD draws no slider knob - the blue fill simply stops at the
# value (verified: blue runs to x=901, grey starts at 902) - so neither
# does this.


def _panel_image(w: int, h: int, fill: str) -> ImageTk.PhotoImage:
    # A plain filled rectangle, not a rounded one: SetWindowRgn is what
    # actually produces the rounded shape by clipping the window, so a
    # rounded_rectangle here would anti-alias its own corners against this
    # image's transparent background - a second, slightly different curve
    # than the region's - leaving a mismatched sliver at each corner
    # (visible in testing as a small dark triangle). One rounding source.
    return ImageTk.PhotoImage(Image.new("RGBA", (w, h), fill))


def _sun_image(size: int, color: str, dim: bool) -> Image.Image:
    # Drawn with PIL rather than the "Segoe UI Emoji" glyph: Tk labels
    # render that font's colour emoji as a hollow outline glyph once a
    # foreground colour is set (confirmed in testing - it showed as an
    # illegible smudge against the light panel), so a hand-drawn icon
    # sidesteps the font entirely.
    # Drawn at 4x and downsampled - PIL has no anti-aliased stroking, and
    # the rays look ragged at 16px otherwise.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s / 2
    r = s * 0.17 if dim else s * 0.20
    stroke = max(2, round(s / 22))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=stroke)
    ray_len = s * 0.10 if dim else s * 0.15
    gap = s * 0.07
    for i in range(8):
        angle = math.radians(i * 45)
        x1 = cx + (r + gap) * math.cos(angle)
        y1 = cy + (r + gap) * math.sin(angle)
        x2 = cx + (r + gap + ray_len) * math.cos(angle)
        y2 = cy + (r + gap + ray_len) * math.sin(angle)
        d.line([x1, y1, x2, y2], fill=color, width=stroke)
    return img.resize((size, size), Image.LANCZOS)


def _speaker_image(size: int, color: str, muted: bool) -> Image.Image:
    # Outline (not filled) speaker, matching the native OSD's stroked icon.
    # Drawn at 4x and downsampled because PIL has no anti-aliased stroking.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(2, round(s / 19))
    body_w, body_h = s * 0.17, s * 0.32
    x0, y0 = s * 0.06, s / 2 - body_h / 2
    cone_x = x0 + body_w
    cone_w, cone_h = s * 0.24, s * 0.70
    d.line([
        (x0, y0), (cone_x, y0), (cone_x + cone_w, s / 2 - cone_h / 2),
        (cone_x + cone_w, s / 2 + cone_h / 2), (cone_x, y0 + body_h),
        (x0, y0 + body_h), (x0, y0),
    ], fill=color, width=w, joint="curve")
    cx, cy = cone_x + cone_w, s / 2
    if muted:
        a, b = s * 0.08, s * 0.30
        d.line([cx + a, cy - a * 2, cx + b, cy + a * 2], fill=color, width=w)
        d.line([cx + a, cy + a * 2, cx + b, cy - a * 2], fill=color, width=w)
    else:
        for rad in (s * 0.22, s * 0.40):
            d.arc([cx - rad, cy - rad, cx + rad, cy + rad], start=-52, end=52,
                  fill=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


def _sun_icon(size: int, color: str, dim: bool) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(_sun_image(size, color, dim))


def _speaker_icon(size: int, color: str, muted: bool) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(_speaker_image(size, color, muted))


def _work_area(screen_w: int, screen_h: int):
    """Desktop rect excluding the taskbar, so the panel sits where the
    native OSD does no matter how tall the taskbar is or which edge it is
    docked to."""
    try:
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, screen_w, screen_h


class Hud:
    def __init__(self, root: tk.Tk, screen_w: int, screen_h: int):
        self.root = root
        self._hide_job = None
        self._fade_job = None
        self._kind = "brightness"

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=PANEL_BG)
        self.win.attributes("-alpha", 0.0)

        wa_left, _, wa_right, wa_bottom = _work_area(screen_w, screen_h)
        x = (wa_left + wa_right - PANEL_W) // 2
        y = wa_bottom - BOTTOM_GAP - PANEL_H
        self.win.geometry(f"{PANEL_W}x{PANEL_H}+{x}+{y}")

        self._bg_img = _panel_image(PANEL_W, PANEL_H, PANEL_BG)
        tk.Label(self.win, image=self._bg_img, bd=0, highlightthickness=0).place(x=0, y=0)

        row_y = PANEL_H // 2
        # Cache one image per (kind, state) pair up front so update() just
        # swaps a reference - no PIL work while a gesture is in flight.
        self._icon_imgs = {
            ("brightness", False): _sun_icon(ICON_SIZE, ICON_COLOR, dim=False),
            ("brightness", True): _sun_icon(ICON_SIZE, ICON_COLOR, dim=True),
            ("volume", False): _speaker_icon(ICON_SIZE, ICON_COLOR, muted=False),
            ("volume", True): _speaker_icon(ICON_SIZE, ICON_COLOR, muted=True),
        }
        self.icon_label = tk.Label(self.win, bg=PANEL_BG, bd=0, highlightthickness=0)
        self.icon_label.place(x=ICON_X, y=row_y, anchor="w")

        self.bar_track = tk.Frame(self.win, bg=TRACK_COLOR, width=TRACK_X2 - TRACK_X1, height=TRACK_H)
        self.bar_track.place(x=TRACK_X1, y=row_y, anchor="w")

        self.bar_fill = tk.Frame(self.win, bg=FILL_COLOR, width=0, height=TRACK_H)
        self.bar_fill.place(x=TRACK_X1, y=row_y, anchor="w")

        self.value_label = tk.Label(self.win, text="", bg=PANEL_BG, fg=ICON_COLOR, font=VALUE_FONT)
        self.value_label.place(x=PANEL_W - VALUE_RIGHT_MARGIN, y=row_y, anchor="e")

        self.win.update_idletasks()
        self._apply_window_styles()
        self._visible = False

    def _apply_window_styles(self):
        user32 = ctypes.windll.user32
        # winfo_id() is Tk's *child* HWND ("TkChild"); on Windows Tk wraps it
        # in a real top-level ("TkTopLevel"). Window-level attributes have to
        # go on the latter - applying the corner rounding to the child failed
        # outright (ERROR_INVALID_HANDLE), and clipping the child's region
        # rounded only the child while its square parent showed through
        # behind it as a black wedge in every corner.
        hwnd = user32.GetAncestor(self.win.winfo_id(), GA_ROOT) or self.win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        self._round_corners(hwnd)

    @staticmethod
    def _round_corners(hwnd):
        """Round the panel's corners the way Windows 11 rounds its own.

        DWM does this itself, anti-aliased, and composites the corner
        against whatever is behind the window. Clipping with SetWindowRgn
        instead (the first attempt here) left a hard-edged black wedge in
        each corner, since the clipped-away pixels render as unpainted
        rather than as background.
        """
        try:
            pref = ctypes.c_int(DWMWCP_ROUNDSMALL)
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref))
            if hr == 0:
                return
        except Exception:
            pass
        # Pre-Windows-11 (no corner preference attribute): fall back to a
        # clipped region, which is squarer but still better than a hard
        # rectangle.
        d = CORNER_RADIUS * 2
        region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, PANEL_W + 1, PANEL_H + 1, d, d)
        ctypes.windll.user32.SetWindowRgn(hwnd, region, True)

    def _update_bar(self, fraction: float):
        fraction = max(0.0, min(1.0, fraction))
        fill_w = round((TRACK_X2 - TRACK_X1) * fraction)
        if fill_w <= 0:
            self.bar_fill.place_forget()
        else:
            self.bar_fill.place(x=TRACK_X1, y=PANEL_H // 2, anchor="w",
                                width=fill_w, height=TRACK_H)

        # The native OSD shows a bare number, no percent sign.
        self.value_label.configure(text=str(round(fraction * 100)))
        kind = "brightness" if self._kind == "brightness" else "volume"
        img = self._icon_imgs[(kind, fraction <= 0.12)]
        self.icon_label.configure(image=img)
        self.icon_label.image = img

    def update(self, kind: str, fraction: float):
        self._kind = kind
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        if self._fade_job is not None:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None
        # Fully opaque while shown: a partial alpha blends the *whole*
        # window with what is behind it, text included, which showed as
        # visibly bled-through text rather than a clean panel.
        self.win.attributes("-alpha", 1.0)
        self.win.lift()
        self._visible = True
        self._update_bar(fraction)

    def schedule_hide(self, delay_ms: int = 550):
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(delay_ms, self._fade_out)

    def _fade_out(self, alpha: float = 1.0):
        alpha -= 0.15
        if alpha <= 0:
            self.win.attributes("-alpha", 0.0)
            self._visible = False
            self._fade_job = None
            return
        self.win.attributes("-alpha", alpha)
        self._fade_job = self.root.after(25, lambda: self._fade_out(alpha))
