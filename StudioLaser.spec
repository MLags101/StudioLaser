# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata, get_module_file_attribute

root = Path(SPECPATH)
icon = root / "assets" / "icons" / "openmopa.ico"

a = Analysis(
    [str(root / "scripts" / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("libusb_package"),
    datas=[(str(icon), "assets/icons")] + sum(
        (copy_metadata(name) for name in ("studiolaser", "galvoplotter", "pyusb", "libusb-package")), []
    ),
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "tkinter", "pytest", "IPython",
        "matplotlib", "scipy", "pandas", "torch", "quart", "flask",
    ],
)

# Qt links to the Windows ICU exports. A third-party icuuc.dll from PATH can
# have the same filename but only versioned exports, so it breaks QtCore.
# Keep Windows system libraries out of the bundle.
windows_dlls = {"ucrtbase.dll", "icu.dll", "icuin.dll", "icuuc.dll"}
qt = Path(get_module_file_attribute("PySide6")).parent
qt_crt = ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
bundled_crt = [(name, str(qt / name), "BINARY") for name in qt_crt if (qt / name).is_file()]
replace_names = {name.casefold() for name, _, _ in bundled_crt}
a.binaries = [
    item for item in a.binaries
    if not item[0].casefold().startswith(("api-ms-win-", "ext-ms-win-"))
    and Path(item[0]).name.casefold() not in replace_names | windows_dlls | {"libusb0.dll"}
    and not (Path(item[0]).name.casefold().startswith("icudt") and item[0].casefold().endswith(".dll"))
] + bundled_crt

pyz = PYZ(a.pure)
version = str(root / "scripts" / "version_info.txt")
# STUDIOLASER_ONEDIR=1 builds dist/StudioLaser/ (the exe plus its files) instead of one self-extracting exe.
# The folder build does not unpack itself into %TEMP% on every start, which antivirus heuristics
# (for example Avast's IDP detections) are much less likely to flag.
if os.environ.get("STUDIOLASER_ONEDIR") == "1":
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="StudioLaser",
        icon=str(icon),
        version=version,
        console=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="StudioLaser", upx=False)
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas,
        name="StudioLaser",
        icon=str(icon),
        version=version,
        console=False,
        upx=False,
    )
