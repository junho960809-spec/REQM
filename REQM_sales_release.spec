# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files

python_runtime_dir = Path(sys.base_prefix)
runtime_binaries = [
    (str(python_runtime_dir / dll_name), ".")
    for dll_name in ("vcruntime140.dll", "vcruntime140_1.dll")
    if (python_runtime_dir / dll_name).exists()
]

a = Analysis(
    ["ecount_sales_app.py"],
    pathex=[],
    binaries=runtime_binaries,
    datas=[("supabase/ecount_migration/data", "supabase/ecount_migration/data")] + collect_data_files("playwright"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# 번들 런타임의 ICU DLL이 Qt6Core보다 먼저 로드되면 일부 배포 PC에서
# 프로그램이 시작되지 않으므로 Qt 충돌 DLL을 배포 목록에서 제외한다.
conflicting_icu_dlls = {"icuuc.dll", "icudt78.dll"}
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
    name="REQM_판매전표_ESM",
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
