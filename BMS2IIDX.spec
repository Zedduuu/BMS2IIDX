# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files


a = Analysis(
    ['run_gui.py'],
    pathex=['.', 'core/_vendor'],
    binaries=[
        ('work/ffbin/ffmpeg.exe', 'ffmpeg'),
        ('work/ffbin/ffprobe.exe', 'ffmpeg'),
        ('vendor/ifs_hook/ifs_hook.dll', 'vendor/ifs_hook'),
        ('vendor/ifs_hook/ifs_hook_32bit.dll', 'vendor/ifs_hook'),
        ('vendor/ifs_hook/LICENSE.txt', 'vendor/ifs_hook'),
    ],
    datas=collect_data_files('pypinyin'),
    hiddenimports=['core', 'gui', 'musicdata_tool', 'pypinyin', 'encodings.cp949'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BMS2IIDX',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='BMS2IIDX',
)
