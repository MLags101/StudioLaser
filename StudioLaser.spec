# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, get_module_file_attribute

root = Path(SPECPATH)
icon = root / "assets" / "icons" / "openmopa.ico"

a = Analysis(
    [str(root / "scripts" / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("libusb_package"),
    datas=[(str(icon), "assets/icons")],
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "tkinter", "pytest", "IPython",
        "matplotlib", "scipy", "pandas", "torch", "quart", "flask",
    ],
)

# Windows 10 and 11 provide the UCRT and API sets. Do not copy app-local
# versions from unrelated software on the build machine's DLL search path.
qt = Path(get_module_file_attribute("PySide6")).parent
qt_crt = ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
bundled_crt = [(name, str(qt / name), "BINARY") for name in qt_crt if (qt / name).is_file()]
replace_names = {name.casefold() for name, _, _ in bundled_crt}
a.binaries = [
    item for item in a.binaries
    if not item[0].casefold().startswith(("api-ms-win-", "ext-ms-win-"))
    and item[0].casefold() not in replace_names | {"ucrtbase.dll", "libusb0.dll"}
] + bundled_crt

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="StudioLaser",
    icon=str(icon),
    console=False,
    upx=False,
)
