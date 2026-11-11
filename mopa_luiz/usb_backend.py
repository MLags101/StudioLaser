"""Make sure pyusb can find a libusb library on macOS, Windows and Linux.

pyusb is only a Python wrapper; it needs the native libusb-1.0 library.
Homebrew or the Linux package manager may provide one, but a fresh machine
usually has none, and then no laser is ever found. The `libusb-package`
dependency ships a libusb binary for every platform, so we fall back to it.

pyusb caches the first libusb backend it loads, so priming it here also
covers galvoplotter's own `usb.core.find()` calls.
"""

from __future__ import annotations

import logging
from typing import Any

_STATUS: dict[str, Any] | None = None


def ensure_libusb_backend() -> dict[str, Any]:
    """Load a libusb-1.0 backend once; report where it came from. Never raises."""
    global _STATUS
    if _STATUS is not None:
        return _STATUS
    try:
        import usb.backend.libusb1 as libusb1
    except ImportError:
        _STATUS = {"available": False, "source": None, "note": "pyusb is not installed."}
        return _STATUS

    # A failed search is expected on a fresh machine; keep pyusb from printing
    # an alarming "Error loading libusb" line before the fallback succeeds.
    pyusb_logger = logging.getLogger("usb.backend.libusb1")
    previous_level = pyusb_logger.level
    pyusb_logger.setLevel(logging.CRITICAL)
    try:
        backend = libusb1.get_backend()
        source = "system"
        if backend is None:
            try:
                import libusb_package
            except ImportError:
                libusb_package = None
            if libusb_package is not None:
                backend = libusb1.get_backend(find_library=libusb_package.find_library)
                source = "bundled (libusb-package)"
    finally:
        pyusb_logger.setLevel(previous_level)

    if backend is None:
        _STATUS = {
            "available": False,
            "source": None,
            "note": "No libusb-1.0 library found. Run the StudioLaser installer again "
                    "so the libusb-package dependency is installed.",
        }
    else:
        _STATUS = {"available": True, "source": source}
    return _STATUS
