# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['..\\_hipot_staging\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\kacper.urbanowicz\\AppData\\Local\\Programs\\Python\\Python313\\tcl\\tcl8.6', '_tcl_data'), ('C:\\Users\\kacper.urbanowicz\\AppData\\Local\\Programs\\Python\\Python313\\tcl\\tk8.6', '_tk_data')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.filedialog', 'serial', 'serial.tools.list_ports', 'serial.tools.list_ports_windows', 'hashlib', 'hmac', 'secrets', 'main', 'gui', 'test_screen', 'admin_panel', 'station_config', 'settings_manager', 'product_profile', 'scpi_dialect', 'hipot_device', 'interlock', 'report_writer', 'safety_rules', 'security', 'profile_integrity', 'runtime_logging'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['C:\\Users\\kacper.urbanowicz\\PycharmProjects\\hipot_main_units\\build\\_hipot_staging\\runtime_hook_hipot.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Reconext Hi-Pot Main Units',
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
    version='C:\\Users\\kacper.urbanowicz\\PycharmProjects\\hipot_main_units\\build\\_hipot_staging\\version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Reconext Hi-Pot Main Units',
)
