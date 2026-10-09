# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

# Locate project root
project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(project_root, "src", "native", "rnnoise.dll"), os.path.join("src", "native")),
    (os.path.join(project_root, "drivers", "vbcable"), os.path.join("drivers", "vbcable")),
    (os.path.join(project_root, "assets", "fonts"), os.path.join("assets", "fonts")),
    (os.path.join(project_root, "getsu-icon.png"), "."),
    (os.path.join(project_root, "getsu.ico"), "."),
    (os.path.join(project_root, "CHANGELOG.md"), "."),
    (os.path.join(project_root, "config.json"), "."),
]

binaries = []

hiddenimports = [
    "sounddevice",
    "dearpygui",
    "dearpygui.dearpygui",
    "pystray",
    "PIL",
    "cffi",
    "miniaudio",
    "_miniaudio",
    "src.router",
    "src.config_migration_v126",
    "src.host_api_resolver",
    "src.cli_cleaner",
    "logging.handlers",
]

excludes = [
    "tkinter",
    "scipy",
    "matplotlib",
    "pandas",
    "torch",
    "torchaudio",
    "unittest",
    "pydoc",
    "sqlite3",
]

a = Analysis(
    [os.path.join(project_root, "src", "main.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a_cli = Analysis(
    [os.path.join(project_root, "src", "main_cli.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='getsu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # Windowed / System Tray app (no black console box)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "getsu.ico"),
    version=os.path.join(project_root, "build", "version_info.txt"),
)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='getsu-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Console subsystem for synchronous CLI / terminal usage
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "getsu.ico"),
    version=os.path.join(project_root, "build", "version_info_cli.txt"),
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries + a_cli.binaries,
    a.zipfiles + a_cli.zipfiles,
    a.datas + a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='getsu',
)
