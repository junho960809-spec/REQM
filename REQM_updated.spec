# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

python_runtime_dir = Path(sys.base_prefix)
runtime_binaries = [
    (str(python_runtime_dir / dll_name), '.')
    for dll_name in ('vcruntime140.dll', 'vcruntime140_1.dll')
    if (python_runtime_dir / dll_name).exists()
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=runtime_binaries,
    datas=[('assets/direct_conversion_reference.xlsx', 'assets')],
    hiddenimports=[],
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
    name='REQM',
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
