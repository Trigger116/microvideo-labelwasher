# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir、console=False、static/config 资源随包分发。"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("static", "static"),
        ("config.json", "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="microvideo-labelwasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="microvideo-labelwasher",
)
