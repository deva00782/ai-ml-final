# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

datas_sel, binaries_sel, hidden_sel = collect_all('selenium')

a = Analysis(
    ['receiver.py'],
    pathex=[],
    binaries=binaries_sel,
    datas=[
        ('agents/config.json', 'agents'),
    ] + datas_sel,
    hiddenimports=[
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'serial.tools.list_ports_windows',
        'serial.win32',
        'pygetwindow',
        'pyscreeze',
        'pymsgbox',
        'pytweening',
        'mouseinfo',
        'pyperclip',
        'PIL',
        'PIL.Image',
        'psutil',
        'ctypes',
        'ctypes.wintypes',
    ] + hidden_sel,
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
    name='Receiver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
