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
    ['ecount_sales_app.py'],
    pathex=[],
    binaries=runtime_binaries,
    datas=[('supabase/ecount_migration/data', 'supabase/ecount_migration/data')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Codex/Poppler 런타임의 ICU DLL이 Qt6Core보다 먼저 수집되면
# 배포 PC에서 QtCore가 로드되지 않는다. Qt는 Windows 기본 ICU를 사용한다.
conflicting_icu_dlls = {'icuuc.dll', 'icudt78.dll'}
a.binaries = [
    binary for binary in a.binaries
    if Path(binary[0]).name.lower() not in conflicting_icu_dlls
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='REQM_판매전표_테스트',
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
