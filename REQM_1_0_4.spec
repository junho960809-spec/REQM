# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

pdf_datas, pdf_binaries, pdf_hidden = collect_all("pdfplumber")
miner_datas, miner_binaries, miner_hidden = collect_all("pdfminer")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=pdf_binaries + miner_binaries,
    datas=pdf_datas + miner_datas,
    hiddenimports=pdf_hidden + miner_hidden,
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
    name="REQM_1.0.4",
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
)
