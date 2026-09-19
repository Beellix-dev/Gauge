# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from runpy import run_path
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
    VarFileInfo, VarStruct,
)

ROOT = Path(SPECPATH).parent
VERSION = run_path(str(ROOT / 'src' / '__init__.py'))['__version__']
VERSION_TUPLE = tuple(int(part) for part in VERSION.split('.')) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE,
                     mask=0x3f, flags=0, OS=0x40004, fileType=1,
                     subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'Gauge contributors'),
        StringStruct('FileDescription', 'Gauge — AI quota at a glance'),
        StringStruct('FileVersion', VERSION),
        StringStruct('InternalName', 'Gauge'),
        StringStruct('OriginalFilename', 'Gauge.exe'),
        StringStruct('ProductName', 'Gauge'),
        StringStruct('ProductVersion', VERSION),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 Gauge contributors'),
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])],
)

a = Analysis(
    [str(ROOT / 'gauge.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / 'assets' / name), 'assets') for name in ('gauge.ico', 'openai.svg', 'claude.svg')]
          + [(str(ROOT / 'docs' / 'licenses'), 'licenses'), (str(ROOT / 'LICENSE'), 'licenses')],
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
    name='Gauge',
    version=version_info,
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
    icon=[str(ROOT / 'assets' / 'gauge.ico')],
)
