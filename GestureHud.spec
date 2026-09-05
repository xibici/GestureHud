# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH (injected by PyInstaller into this file's exec namespace) rather
# than __file__, which .spec files cannot rely on.
sys.path.insert(0, SPECPATH)
from icon import make_icon_image

# Rendered from the exact same drawing as the tray icon (tray.py), so the
# exe's icon - File Explorer, taskbar, Task Manager, the UAC prompt - can
# never drift apart from what shows up in the tray at runtime.
ICON_PATH = os.path.join(SPECPATH, "icon.ico")
make_icon_image(256).save(
    ICON_PATH, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)

hiddenimports = []
hiddenimports += collect_submodules('wmi')
hiddenimports += collect_submodules('pycaw')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GestureHud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Raising a window owned by an app that needs elevation (MotionAssistant,
    # via the three-finger tap - see launcher.py) requires this process to be
    # elevated too, since Windows blocks a lower-integrity caller from doing
    # that outright. main.py also self-elevates at runtime as a fallback (e.g.
    # running from source), but embedding it in the manifest means Windows
    # elevates before the process even starts, with no relaunch round-trip.
    uac_admin=True,
    icon=ICON_PATH,
)
