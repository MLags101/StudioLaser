"""Entry point for the StudioLaser desktop application."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


def windowless_workers() -> None:
    """Start hardware worker processes without a console window.

    Launched from a shortcut the app has no console, but each hardware
    operation runs in a child process; under python.exe that child would open
    a console window that flashes up and closes.
    """
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    import ctypes
    import multiprocessing

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.is_file() and not ctypes.windll.kernel32.GetConsoleWindow():
        multiprocessing.set_executable(str(pythonw))


def main(argv: list[str] | None = None) -> int:
    windowless_workers()
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .gui import theme
    from .gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv if argv is None else ["studiolaser", *argv])
    app.setApplicationName("StudioLaser")
    app.setOrganizationName("StudioLaser")
    saved = QSettings("StudioLaser", "Desktop").value("theme", None)
    if saved is None:   # first start after the rename
        saved = QSettings("OpenLaser", "Desktop").value("theme", "")
    theme.apply(app, saved or None)
    for base in (Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parents[1]):
        icon = base / "assets" / "icons" / "openmopa.ico"
        if icon.is_file():
            app.setWindowIcon(QIcon(str(icon)))
            break

    window = MainWindow()

    def report(kind, value, trace):
        # The packaged app has no console, so an unexpected error must be shown, and must stop the laser.
        text = "".join(traceback.format_exception(kind, value, trace))
        try:
            window.controller.stop()
            window.controller.say(f"Unexpected error: {value}", "error")
        except Exception:
            pass
        box = QMessageBox(QMessageBox.Icon.Critical, "StudioLaser", f"An unexpected error occurred:\n{value}")
        box.setDetailedText(text)
        box.exec()

    sys.excepthook = report
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
