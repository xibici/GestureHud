# GestureHud

Windows 手持设备/平板的触屏手势工具:**屏幕左半边上下滑调亮度,右半边上下滑调音量**,像 B 站手机播放器那样。为 Lenovo Legion Go 开发和实测。

- **双指**上下滑触发(默认)。单指滑动正是游戏里瞄准和虚拟摇杆在做的事,所以单指模式玩游戏必然误触;两根手指一起移动几乎不会是无意的。
- **三指轻点**呼出 Legion 设置菜单或 MotionAssistant(托盘里切换)。
- 提示面板照着 Windows 自带的音量 OSD 做,位置和配色都是从截图上量出来的。
- 托盘可调:灵敏度、触发手指数、屏幕提示、开机自启。

## 安装运行

```bash
pip install -r requirements.txt
python main.py
```

打包:

```bash
pyinstaller --noconfirm --onefile --windowed --name GestureHud \
    --collect-submodules wmi --collect-submodules pycaw main.py
```

设置与校准数据存在 `%LOCALAPPDATA%\GestureHud\`。

## 实现要点

这些都是踩过坑之后定下来的,换掉任何一个都会退回到不工作的状态。

**全局触摸捕获用 Raw Input 读数字化仪(`rawtouch.py`)。** 试过并否掉的方案:`WH_MOUSE_LL` 全局钩子——Windows 把触摸提升成的鼠标消息直接 `PostMessage` 给触点下的窗口,绕过低级输入管线,后台钩子收不到;全屏覆盖层捕获后重放——`SendInput` 重放的点击挡不住自己的窗口,实测会造成无限点击循环;鼠标用途页的 Raw Input——45 秒实测该设备触摸完全不产生 `RIM_TYPEMOUSE` 报告。数字化仪用途页(0x0D/0x04)直接读 HID 报告,纯旁观,不拦截不重放,所以正常触摸完全不受影响。

**每个触点在自己的 HID link collection 里。** 只查 collection 0 会把所有手指合并,只报出第一个的坐标。多指必须逐 collection 查询。

**触摸坐标到屏幕坐标的映射由显示旋转推导**(思路参考 GPL-2.0 的 [GestureSign](https://github.com/TransposonY/GestureSign))。数字化仪绑在物理面板上,本机面板是 1600×2560 竖屏,桌面是 1920×1200 横屏。正常硬件不需要手动校准;个别面板可以用托盘里的校准界面,靠时间戳把原始触点和真实 UI 触摸事件配对来解方向。

**三指轻点按整次触摸的峰值手指数判定。** 手指不可能同时落下或抬起,真实的三指轻点上报序列是 1、2、3、2、1、0,按单帧判断会把上升过程当成单指/双指。看峰值还顺带让手掌误放(4 指以上)直接被排除。

**两个外部程序的呼出方式完全不同,都是实测出来的:**

| | 实测状态 | 采用方式 |
|---|---|---|
| LegionSettingMenu.exe | 窗口隐藏,常驻 | 重新启动 exe(它自己的显示/收起机制) |
| MotionAssistant.exe | 窗口最小化,需要提权 | `WM_SYSCOMMAND`/`SC_RESTORE` 恢复并置前 |

对 Legion 用 `ShowWindow` 显示会返回成功但屏幕上什么都不画(WinUI 窗口);对 MotionAssistant 用 `ShowWindow(SW_RESTORE)` 则被完全忽略(窗口一直停在 -48000)。所以两边分别走各自验证过的路径。

**Tk 在本机的几个坑:** `Canvas.create_text`/`create_rectangle` 渲染全白(改用 `Label`/`Frame`);`overrideredirect` 配合 `withdraw()`/`deiconify()` 会让窗口再也显示不出来(改成常驻映射、只切 `-alpha`);`winfo_id()` 返回的是 Tk 子窗口而不是真正的顶层窗口,窗口级属性(`WS_EX_NOACTIVATE`、DWM 圆角)必须用 `GetAncestor(..., GA_ROOT)` 拿到的句柄才生效。

**亮度走 WMI,音量走媒体键。** 模拟音量键能让 Windows 自己弹原生音量 OSD;亮度没有对应的公开接口,所以亮度用自绘面板。`brightness.py` 全程用 `comtypes`——`pythoncom`/`win32com` 和 `comtypes` 混用会让同一线程上的 WMI 调用静默失败。

## 许可

MIT
