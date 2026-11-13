"""Machine → Connection Test, and the Help menu's log folder and problem report."""

from __future__ import annotations

import platform

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..desktop_hardware import TestOptions
from ..device_profile import BASE_LABELS, DRIVER_LABELS
from ..hardware_log import build_id, log_folder, recent_lines
from .panels import button, hint, number, row


def open_log_folder(controller) -> None:
    folder = log_folder()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
        controller.say(f"The log folder is {folder}", "info")


def build_report(controller) -> str:
    try:
        from importlib.metadata import version

        app_version = version("studiolaser")
    except Exception:
        app_version = "unknown"
    profile = controller.profile
    lines = [
        "StudioLaser problem report",
        f"Version {app_version} · Build {build_id()} · {platform.system()} {platform.version()} "
        f"· Python {platform.python_version()}",
        f"Machine: {profile.name} · {profile.source} · lens {profile.lens} · field {profile.width:g} mm",
        f"Lens correction: {BASE_LABELS.get(profile.base, profile.base)}"
        + (f" ({profile.correction_name}, direction {profile.cor_sign:+d})" if profile.base == "cor" else ""),
        f"Driver settings: {DRIVER_LABELS.get(profile.driver, profile.driver)}",
        f"Laser link: {controller.link} — {controller.link_note}",
        f"Log folder: {log_folder()}",
        "",
        *recent_lines(200),
    ]
    return "\n".join(lines)


def copy_problem_report(controller) -> None:
    QGuiApplication.clipboard().setText(build_report(controller))
    controller.say("Problem report copied. Paste it into your message.", "success")


class ConnectionTestDialog(QDialog):
    """Connect, light, frame and reconnect, each step timed, to show whether the link and board are sound."""

    STEPS = ("Find the laser", "Claim and handshake", "Red dot at the centre for 3 s",
             "Frame a 20 mm square", "Verify stop", "Reconnect and handshake")

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Test")
        self.resize(600, 520)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "Runs the laser's connection through a fixed routine using only the red light: connect, a red dot, "
            "a 20 mm square traced continuously, a verified stop, and a fresh handshake. "
            "The test uses OpenLaser's motion settings and records each step in the log."))
        form = QFormLayout()
        self.seconds = number(60, 10, 600, 0, "s", 10)
        form.addRow("Frame the square for", self.seconds)
        layout.addLayout(form)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Step", "Result", "Detail"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table, 1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.start = button("Run test", self.run, primary=True)
        layout.addLayout(row(self.start, button("Stop", controller.stop),
                             button("Copy problem report", lambda: copy_problem_report(controller)),
                             button("Open log folder", lambda: open_log_folder(controller)), stretch=True))
        layout.addWidget(button("Close", self.close))
        controller.test_step.connect(self.show_step)
        controller.state_changed.connect(self.refresh)
        self._rows: dict[str, int] = {}
        self._ran = False
        self.refresh()

    def run(self):
        options = TestOptions(self.seconds.value())
        self.table.setRowCount(0)
        self._rows.clear()
        self.summary.setText("")
        if self.controller.connection_test(options):
            self._ran = True
        self.refresh()

    def show_step(self, name, ok, detail):
        if name not in self._rows:
            self._rows[name] = self.table.rowCount()
            self.table.insertRow(self.table.rowCount())
        r = self._rows[name]
        text = "running…" if ok is None else "passed" if ok else "FAILED"
        for c, value in enumerate((name, text, detail)):
            item = QTableWidgetItem(value)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(r, c, item)
        if ok is False:
            self.summary.setText(f"Failed at {name}. Use Copy problem report to capture the controller log.")

    def refresh(self):
        busy = self.controller.busy
        self.start.setEnabled(not busy and not self.controller.state_unknown)
        if self._ran and not busy and self.controller.state_unknown:
            self.summary.setText("Machine state unknown. Use the machine's stop and confirm it is idle.")
        elif self._ran and not busy and not self.summary.text():
            done = [self.table.item(r, 1).text() for r in range(self.table.rowCount()) if self.table.item(r, 1)]
            if done and all(value == "passed" for value in done) and len(done) == len(self.STEPS):
                self.summary.setText("All steps passed: the connection and the controller are working.")

    def closeEvent(self, event):
        try:
            self.controller.test_step.disconnect(self.show_step)
            self.controller.state_changed.disconnect(self.refresh)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
