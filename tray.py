"""System tray icon: toggle gestures on/off, quit the app."""
from PIL import Image, ImageDraw
import pystray

from settings import SENSITIVITY_PRESETS

SENSITIVITY_LABELS = [
    ("high", "灵敏(小滑动就到底)"),
    ("medium", "适中(推荐)"),
    ("low", "精细(需要长滑动)"),
]


def _make_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.pieslice([2, 2, size - 2, size - 2], 90, 270, fill=(245, 197, 66, 255))   # sun half
    d.pieslice([2, 2, size - 2, size - 2], 270, 90, fill=(66, 148, 245, 255))   # speaker half
    d.ellipse([2, 2, size - 2, size - 2], outline=(28, 28, 30, 255), width=2)
    return img


def build_tray(on_toggle_enabled, on_toggle_hud, on_recalibrate, on_quit,
               on_set_fingers=None, initial_fingers=2, initial_show_hud=True,
               on_toggle_native_osd=None, initial_native_osd=True,
               on_set_sensitivity=None, initial_range_fraction=0.9,
               on_toggle_three_finger=None, initial_three_finger=True,
               three_finger_apps=None, initial_three_finger_app=0,
               on_set_three_finger_app=None,
               on_toggle_autostart=None, initial_autostart=False,
               initial_enabled=True) -> pystray.Icon:
    # Match the closest preset even if the stored value doesn't land exactly
    # on one (e.g. an older settings.json from before presets existed).
    initial_preset = min(
        SENSITIVITY_PRESETS, key=lambda k: abs(SENSITIVITY_PRESETS[k] - initial_range_fraction)
    )
    state = {"enabled": initial_enabled, "show_hud": initial_show_hud, "fingers": initial_fingers,
             "native_osd": initial_native_osd, "sensitivity": initial_preset,
             "three_finger": initial_three_finger,
             "three_finger_app": initial_three_finger_app,
             "autostart": initial_autostart}
    apps = list(three_finger_apps or [])

    def toggle_enabled(icon, item):
        state["enabled"] = not state["enabled"]
        on_toggle_enabled(state["enabled"])

    def toggle_hud(icon, item):
        state["show_hud"] = not state["show_hud"]
        on_toggle_hud(state["show_hud"])

    def set_fingers(count):
        def handler(icon, item):
            state["fingers"] = count
            if on_set_fingers:
                on_set_fingers(count)
        return handler

    def toggle_native_osd(icon, item):
        state["native_osd"] = not state["native_osd"]
        if on_toggle_native_osd:
            on_toggle_native_osd(state["native_osd"])

    def set_sensitivity(preset_key):
        def handler(icon, item):
            state["sensitivity"] = preset_key
            if on_set_sensitivity:
                on_set_sensitivity(SENSITIVITY_PRESETS[preset_key])
        return handler

    def toggle_three_finger(icon, item):
        state["three_finger"] = not state["three_finger"]
        if on_toggle_three_finger:
            on_toggle_three_finger(state["three_finger"])

    def set_three_finger_app(index):
        # Bound via a factory, not a closure over the loop variable: a plain
        # lambda in the comprehension below would capture the variable, so
        # every entry would select the last app.
        def handler(icon, item):
            state["three_finger_app"] = index
            if on_set_three_finger_app:
                on_set_three_finger_app(index)
        return handler

    def toggle_autostart(icon, item):
        want = not state["autostart"]
        # Trust what the callback reports the registry now holds, not `want`:
        # if the write failed the checkmark must not claim it succeeded.
        state["autostart"] = on_toggle_autostart(want) if on_toggle_autostart else want

    def recalibrate(icon, item):
        on_recalibrate()

    def quit_app(icon, item):
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(
            "手势已启用",
            toggle_enabled,
            checked=lambda item: state["enabled"],
        ),
        pystray.MenuItem(
            "屏幕提示",
            toggle_hud,
            checked=lambda item: state["show_hud"],
        ),
        pystray.MenuItem("触发方式", pystray.Menu(
            pystray.MenuItem(
                "双指上下滑(推荐,玩游戏不误触)",
                set_fingers(2),
                checked=lambda item: state["fingers"] == 2,
                radio=True,
            ),
            pystray.MenuItem(
                "单指上下滑(灵敏,游戏中易误触)",
                set_fingers(1),
                checked=lambda item: state["fingers"] == 1,
                radio=True,
            ),
        )),
        pystray.MenuItem(
            "音量用系统原生提示(需重启生效)",
            toggle_native_osd,
            checked=lambda item: state["native_osd"],
        ),
        pystray.MenuItem("灵敏度", pystray.Menu(*(
            pystray.MenuItem(
                label,
                set_sensitivity(key),
                checked=(lambda k: lambda item: state["sensitivity"] == k)(key),
                radio=True,
            )
            for key, label in SENSITIVITY_LABELS
        ))),
        pystray.MenuItem(
            "三指轻点打开程序",
            toggle_three_finger,
            checked=lambda item: state["three_finger"],
        ),
        pystray.MenuItem("三指轻点打开哪个", pystray.Menu(*(
            pystray.MenuItem(
                app.get("name", f"程序 {i + 1}"),
                set_three_finger_app(i),
                checked=(lambda k: lambda item: state["three_finger_app"] == k)(i),
                radio=True,
            )
            for i, app in enumerate(apps)
        ))),
        pystray.MenuItem(
            "开机自启",
            toggle_autostart,
            checked=lambda item: state["autostart"],
        ),
        pystray.MenuItem("重新校准触摸", recalibrate),
        pystray.MenuItem("退出", quit_app),
    )
    return pystray.Icon("gesture_hud", _make_icon_image(), "Gesture HUD", menu)
