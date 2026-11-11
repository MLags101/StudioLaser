"""Machine setup (factory files, source, lens, axes) and measured lens calibration."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea, QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..correction import fit_field_map, grid_points, points_from_spans
from ..device_profile import BASE_LABELS, BASES, DELAYS, DeviceProfile, import_ezcad
from ..patterns import calibration_grid, crosshair
from ..markparam import guess_kind, read_markparam
from ..presets import Preset, starting_layer
from ..sources import DEFAULT_SOURCE, SOURCES, parse_limits
from ..workspace import COPPER_FILL
from .panels import button, heading, hint, number, row, whole

DELAY_LABELS = {
    "delay_laser_on": "Laser-on delay", "delay_laser_off": "Laser-off delay", "delay_polygon": "Corner delay",
    "delay_jump_short": "Short-jump settle", "delay_jump_long": "Long-jump settle",
}
DELAY_DEFAULTS = {"delay_laser_on": 100, "delay_laser_off": 100, "delay_polygon": 100,
                  "delay_jump_short": 8, "delay_jump_long": 200}


def scrolled(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(widget)
    return area


class MachineSetupDialog(QDialog):
    """One place for everything that describes the machine. Nothing is typed as free text."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Machine Setup")
        self.resize(640, 680)
        self.controller = controller
        self.profile = controller.profile
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # ---- Factory files
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(hint(
            "ComMarker ships two files with each machine, on its USB stick in the EZCAD folder:\n"
            "  • markcfg7 — EZCAD's machine settings (in the folder beside EzCad2.exe, or its “plug” subfolder)\n"
            "  • a lens correction file such as jcz15.cor — measured for your lens\n"
            "  • MarkParam.lib, if you used EZCAD — its timing delays and your saved parameter sets\n"
            "Load both here. Nothing is sent to the laser."))
        self.cfg_path = QLineEdit()
        self.cor_path = QLineEdit()
        self.lib_path = QLineEdit()
        self.cfg_path.setPlaceholderText("markcfg7")
        self.cor_path.setPlaceholderText("lens correction .cor (optional but recommended)")
        self.lib_path.setPlaceholderText("MarkParam.lib — EZCAD's pen library (optional): timing and your saved settings")
        self.pending_sets = ()
        box.addLayout(row(self.cfg_path, button("Browse…", lambda: self._browse(self.cfg_path, "EZCAD configuration (markcfg*);;All files (*)"))))
        box.addLayout(row(self.cor_path, button("Browse…", lambda: self._browse(self.cor_path, "Lens correction (*.cor);;All files (*)"))))
        box.addLayout(row(self.lib_path, button("Browse…", lambda: self._browse(self.lib_path, "EZCAD pen library (*.lib);;All files (*)"))))
        box.addWidget(button("Read factory files", self.import_factory, primary=True))
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("What was found in the files appears here.")
        box.addWidget(self.report, 1)
        box.addLayout(row(button("Open saved profile…", self.open_profile), button("Save profile as…", self.save_profile_as),
                          stretch=True))
        tabs.addTab(page, "1 · Factory files")

        # ---- Machine
        page = QWidget()
        form = QFormLayout(page)
        self.name = QLineEdit()
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.addItems(list(SOURCES))
        self.source.setToolTip("Sets the list of valid pulse widths and their frequency limits")
        self.lens = QComboBox()
        self.lens.setEditable(True)
        self.lens.addItems(["70 mm", "110 mm", "150 mm", "200 mm", "300 mm"])
        self.width = number(150, 10, 1000, 2, "mm")
        self.height = number(150, 10, 1000, 2, "mm")
        self.width.setToolTip("Full galvo travel (EZCAD's field size). It is larger than the lens's rated area.")
        self.min_khz = number(1, 0.001, 4000, 3, "kHz")
        self.max_khz = number(4000, 0.001, 4000, 3, "kHz")
        self.swap = QCheckBox("Swap X and Y galvos")
        self.invert_x = QCheckBox("Reverse X")
        self.invert_y = QCheckBox("Reverse Y")
        form.addRow(heading("Identity"))
        form.addRow("Machine name", self.name)
        form.addRow("Laser source", self.source)
        form.addRow("Lens", self.lens)
        form.addRow(heading("Field"))
        form.addRow("Field width", self.width)
        form.addRow("Field height", self.height)
        form.addRow("Lowest frequency", self.min_khz)
        form.addRow("Highest frequency", self.max_khz)
        form.addRow(heading("Axes"))
        form.addRow(row(self.swap, self.invert_x, self.invert_y, stretch=True))
        form.addRow(hint("If a test square marks mirrored or turned a quarter turn, change these."))
        tabs.addTab(page, "2 · Machine")

        # ---- Source limits
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(hint(
            "Pulse widths this source accepts, with the cut-off and maximum frequency for each. Below the "
            "cut-off the source keeps full pulse energy but average power drops. Values come from the "
            "source's manual; edit them if yours differs."))
        self.limits = QTableWidget(0, 3)
        self.limits.setHorizontalHeaderLabels(["Pulse width (ns)", "Cut-off (kHz)", "Maximum (kHz)"])
        self.limits.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.limits.verticalHeader().hide()
        box.addWidget(self.limits, 1)
        box.addLayout(row(button("Add row", lambda: self.limits.insertRow(self.limits.rowCount())),
                          button("Remove row", lambda: self.limits.removeRow(self.limits.currentRow())),
                          button("Reset to source table", self._reset_limits), stretch=True))
        tabs.addTab(page, "3 · Pulse widths")

        # ---- Motion
        page = QWidget()
        form = QFormLayout(page)
        self.frame_speed = number(3000, 100, 15000, 0, "mm/s", 100)
        self.jump_speed = number(3000, 100, 15000, 0, "mm/s", 100)
        form.addRow(heading("Speeds"))
        form.addRow("Red-light frame speed", self.frame_speed)
        form.addRow("Jump speed (laser off)", self.jump_speed)
        form.addRow(heading("Controller delays"))
        form.addRow(hint("Leave at defaults unless line starts or corners look burnt or bent."))
        self.delays = {}
        for key, label in DELAY_LABELS.items():
            low = -32767 if key in ("delay_laser_on", "delay_laser_off") else 0
            self.delays[key] = number(DELAY_DEFAULTS[key], low, 32767, 0, "µs", 10)
            form.addRow(label, self.delays[key])
        form.addRow(heading("Red light"))
        self.red_x = number(0, -50, 50, 3, "mm", 0.05)
        self.red_y = number(0, -50, 50, 3, "mm", 0.05)
        self.red_sx = number(1, 0.5, 2, 4, "", 0.001)
        self.red_sy = number(1, 0.5, 2, 4, "", 0.001)
        form.addRow("Offset X / Y", row(self.red_x, self.red_y))
        form.addRow("Scale X / Y", row(self.red_sx, self.red_sy))
        form.addRow(hint("The red pointer and the laser beam land in slightly different places. "
                         "Machine → Calibrate Lens has a guided way to set these."))
        tabs.addTab(page, "4 · Motion and red light")

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.enabled = QCheckBox("This profile describes my machine — allow it to drive the laser")
        self.enabled.toggled.connect(self._status)
        layout.addWidget(self.enabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.name, ):
            widget.textChanged.connect(self._status)
        self.source.currentTextChanged.connect(self._status)
        self.lens.currentTextChanged.connect(self._status)
        self.load(self.profile)
        if self.profile.enabled or self.profile.limits:
            tabs.setCurrentIndex(1)

    def _browse(self, target: QLineEdit, pattern: str):
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", target.text(), pattern)
        if path:
            target.setText(path)

    def load(self, profile: DeviceProfile):
        self.profile = profile
        self.name.setText(profile.name if profile.name != "Offline preview" else "ComMarker B6 MOPA")
        self.source.setCurrentText(profile.source or DEFAULT_SOURCE)
        self.lens.setCurrentText(profile.lens or "150 mm")
        self.width.setValue(profile.width)
        self.height.setValue(profile.height)
        self.min_khz.setValue((profile.min_hz or 1000) / 1000)
        self.max_khz.setValue((profile.max_hz or 4_000_000) / 1000)
        self.swap.setChecked(profile.swap_xy)
        self.invert_x.setChecked(profile.invert_x)
        self.invert_y.setChecked(profile.invert_y)
        self._fill_limits(profile.limits or SOURCES.get(self.source.currentText(), ()))
        self.frame_speed.setValue(profile.frame_speed)
        self.jump_speed.setValue(profile.jump_speed)
        for key, box in self.delays.items():
            box.setValue(profile.delays.get(key, DELAY_DEFAULTS[key]))
        self.red_x.setValue(profile.red_x)
        self.red_y.setValue(profile.red_y)
        self.red_sx.setValue(profile.red_scale_x)
        self.red_sy.setValue(profile.red_scale_y)
        self.enabled.setChecked(profile.enabled)
        self._status()

    def _fill_limits(self, limits):
        self.limits.setRowCount(len(limits))
        for r, values in enumerate(limits):
            for c, value in enumerate(values):
                self.limits.setItem(r, c, QTableWidgetItem(f"{value:g}"))

    def _reset_limits(self):
        limits = SOURCES.get(self.source.currentText())
        if limits is None:
            QMessageBox.information(self, "Pulse widths", "There is no built-in table for this source. "
                                                         "Enter the values from its manual.")
        else:
            self._fill_limits(limits)

    def import_factory(self):
        if not self.cfg_path.text().strip():
            QMessageBox.information(self, "Factory files", "Choose the markcfg7 file first.")
            return
        cor = self.cor_path.text().strip()
        try:
            profile, report = import_ezcad(Path(self.cfg_path.text().strip()), Path(cor) if cor else None,
                                           source=self.source.currentText() or DEFAULT_SOURCE,
                                           lens=self.lens.currentText())
        except Exception as exc:
            self.report.setPlainText(f"Could not read the files:\n{exc}")
            return
        lib = self.lib_path.text().strip()
        beside = Path(self.cfg_path.text().strip()).with_name("MarkParam.lib")
        if not lib and beside.is_file():
            lib = str(beside)
            self.lib_path.setText(lib)
        self.pending_sets = ()
        if lib:
            try:
                library = read_markparam(Path(lib))
            except Exception as exc:
                report += f"\n{Path(lib).name} could not be read: {exc}"
            else:
                profile = replace(profile, delays={**profile.delays, **library.delays},
                                  jump_speed=library.jump_speed or profile.jump_speed)
                self.pending_sets = library.sets
                timing = ", ".join(f"{DELAY_LABELS[key].lower()} {value:g} µs" for key, value in library.delays.items())
                report += (f"\nFrom {Path(lib).name}: {timing or 'no timing'}"
                           + (f", jump speed {library.jump_speed:g} mm/s." if library.jump_speed else ".")
                           + (f"\nYour EZCAD parameter sets ({', '.join(s.name for s in library.sets)}) will be "
                              "added to Laser Presets when you save." if library.sets else ""))
        self.load(profile)
        self.report.setPlainText(report + "\n\nReview tabs 2–4, then tick the box below and Save. "
                                          "Next: Machine → Calibrate Lens.")

    def collect(self) -> DeviceProfile:
        rows = []
        for r in range(self.limits.rowCount()):
            cells = [self.limits.item(r, c).text().strip() if self.limits.item(r, c) else "" for c in range(3)]
            if any(cells):
                if not all(cells):
                    raise ValueError(f"Pulse width row {r + 1} is incomplete")
                rows.append([float(cell) for cell in cells])
        profile = replace(
            self.profile, name=self.name.text().strip(), source=self.source.currentText().strip(),
            lens=self.lens.currentText().strip(), width=self.width.value(), height=self.height.value(),
            min_hz=int(round(self.min_khz.value() * 1000)), max_hz=int(round(self.max_khz.value() * 1000)),
            limits=parse_limits(rows), swap_xy=self.swap.isChecked(), invert_x=self.invert_x.isChecked(),
            invert_y=self.invert_y.isChecked(), frame_speed=self.frame_speed.value(),
            jump_speed=self.jump_speed.value(), delays={key: box.value() for key, box in self.delays.items()},
            red_x=self.red_x.value(), red_y=self.red_y.value(), red_scale_x=self.red_sx.value(),
            red_scale_y=self.red_sy.value(), enabled=self.enabled.isChecked(),
        )
        if profile.min_hz > profile.max_hz:
            raise ValueError("The lowest frequency is above the highest")
        return profile

    def _status(self):
        try:
            gaps = self.collect().missing()
        except ValueError as exc:
            gaps = [str(exc)]
        if gaps:
            self.status.setText("Still needed before live use: " + ", ".join(gaps) + ".")
        elif not self.enabled.isChecked():
            self.status.setText("Complete. Tick the box below to allow framing and marking.")
        else:
            self.status.setText("Complete and enabled.")

    def save(self):
        try:
            profile = self.collect()
            profile.validate(live=profile.enabled)
        except Exception as exc:
            QMessageBox.warning(self, "Machine Setup", str(exc))
            return
        if self.controller.set_profile(profile):
            added = self.add_ezcad_presets(profile)
            if added:
                self.controller.say(f"Added {added} preset(s) from your EZCAD parameter library.", "success")
            self.accept()

    def add_ezcad_presets(self, profile) -> int:
        """Turn EZCAD's named parameter sets into presets, once each."""
        library = self.controller.presets
        existing = {p.name for p in library.user}
        added = 0
        for pen in self.pending_sets:
            name = f"{pen.name} (from EZCAD)"
            if name in existing:
                continue
            kind = guess_kind(pen.name)
            layer = replace(pen.layer(starting_layer(kind or COPPER_FILL)), layer_id=kind or "ezcad", name=pen.name,
                            output=True)
            try:
                library.add(Preset("", name, kind, layer, profile.name, profile.lens, profile.source,
                                   note="Imported from EZCAD's MarkParam.lib. Hatch spacing is StudioLaser's default: "
                                        "EZCAD keeps it on each drawing object, not in the pen library."))
            except Exception as exc:
                self.controller.say(f"EZCAD set {pen.name} was not added: {exc}", "warning")
                continue
            added += 1
        if added:
            self.controller.presets_changed.emit()
        self.pending_sets = ()
        return added

    def open_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open machine profile", filter="StudioLaser machine (*.json)")
        if path:
            try:
                self.load(DeviceProfile.open(Path(path)))
                self.report.setPlainText(f"Loaded {Path(path).name}. Save to make it the active machine.")
            except Exception as exc:
                QMessageBox.warning(self, "Machine profile", str(exc))

    def save_profile_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save machine profile", "commarker-b6.studiolaser-machine.json",
                                              "StudioLaser machine (*.json)")
        if path:
            try:
                self.collect().save(Path(path))
            except Exception as exc:
                QMessageBox.warning(self, "Machine profile", str(exc))


class CalibrationDialog(QDialog):
    """Measure a marked grid and correct the field so sizes and positions come out true.

    A tool window: the grid is an ordinary job, armed and run from the main window.
    """

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibrate Lens")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(False)
        self.resize(560, 700)
        self.controller = controller
        self.fit = None
        self.red_points: dict[str, list[float]] = {}
        self.red_aim = ""
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # ---- 1 Mark
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(heading("Lens correction to start from"))
        self.base = QComboBox()
        for key in BASES:
            self.base.addItem(BASE_LABELS[key], key)
        self.base.currentIndexChanged.connect(self._base_changed)
        box.addWidget(self.base)
        self.base_hint = hint()
        box.addWidget(self.base_hint)
        box.addWidget(heading("Mark a grid"))
        box.addWidget(hint(
            "Focus on a flat piece of card, painted metal or anodised aluminium. Add the grid, arm and run it "
            "from the main window like any job, at a power that just marks. A small flag marks the top-left cross."))
        form = QFormLayout()
        self.span = number(100, 10, 500, 1, "mm", 5)
        self.span.setToolTip("Distance between the outer crosses. Use about 85 % of the area you engrave in.")
        self.count = QComboBox()
        self.count.addItem("3 × 3 — measure 8 distances with calipers", 3)
        self.count.addItem("5 × 5 — enter each cross's position", 5)
        self.count.addItem("7 × 7 — enter each cross's position", 7)
        self.count.currentIndexChanged.connect(self._count_changed)
        form.addRow("Grid size", self.span)
        form.addRow("Crosses", self.count)
        box.addLayout(form)
        box.addWidget(button("Add calibration grid to the job list", self.add_grid, primary=True))
        box.addStretch(1)
        self.current = hint()
        box.addWidget(self.current)
        box.addWidget(button("Remove the measured calibration", self.clear_calibration))
        tabs.addTab(page, "1 · Mark")

        # ---- 2 Measure
        page = QWidget()
        box = QVBoxLayout(page)
        self.stack = QStackedWidget()
        quick = QWidget()
        form = QFormLayout(quick)
        form.addRow(hint("Measure centre-to-centre between crosses. Keep the flag at the top-left."))
        self.spans = {}
        for key, label in (("wt", "Top row, left ↔ right"), ("wm", "Middle row, left ↔ right"),
                           ("wb", "Bottom row, left ↔ right"), ("hl", "Left column, bottom ↕ top"),
                           ("hm", "Middle column, bottom ↕ top"), ("hr", "Right column, bottom ↕ top"),
                           ("d1", "Diagonal, bottom-left ↗ top-right"), ("d2", "Diagonal, top-left ↘ bottom-right")):
            self.spans[key] = number(100, 1, 1000, 3, "mm", 0.01)
            form.addRow(label, self.spans[key])
        self.stack.addWidget(quick)
        precise = QWidget()
        pbox = QVBoxLayout(precise)
        pbox.addWidget(hint(
            "Enter where each cross actually is, in mm from the centre cross (X right, Y up). Rows run top to "
            "bottom, left to right, starting at the flag. Paste from a spreadsheet with Paste measurements."))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Target X", "Target Y", "Measured X", "Measured Y"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        pbox.addWidget(self.table, 1)
        pbox.addWidget(button("Paste measurements (two columns: X, Y)", self.paste))
        self.stack.addWidget(precise)
        box.addWidget(self.stack, 1)
        box.addWidget(button("Calculate correction", self.calculate, primary=True))
        self.result = QLabel("Mark the grid, measure it, then calculate.")
        self.result.setWordWrap(True)
        box.addWidget(self.result)
        self.apply = button("Apply and save calibration", self.apply_calibration, primary=True)
        self.apply.setEnabled(False)
        box.addWidget(self.apply)
        box.addWidget(hint("Afterwards, mark the grid again and re-measure to confirm. Repeating refines it."))
        tabs.addTab(page, "2 · Measure")

        # ---- 3 Red light
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(hint(
            "The red pointer does not land exactly where the laser marks. With the marked grid still in place:\n"
            "1. Aim at the centre cross and steer the red dot onto the burnt cross.\n"
            "2. Aim at the top-right cross and do the same.\n"
            "3. Save. Framing and hole registration then match the beam."))
        self.aim_centre = button("Aim at centre cross", lambda: self.aim("centre"))
        self.aim_corner = button("Aim at top-right cross", lambda: self.aim("corner"))
        box.addLayout(row(self.aim_centre, self.aim_corner))
        self.red_step = QComboBox()
        for value in (1.0, 0.1, 0.02):
            self.red_step.addItem(f"{value:g} mm per press", value)
        self.red_step.setCurrentIndex(1)
        pad = QGridLayout()
        for text, dx, dy, r, c in (("▲", 0, 1, 0, 1), ("◀", -1, 0, 1, 0), ("▶", 1, 0, 1, 2), ("▼", 0, -1, 2, 1)):
            arrow = button(text, lambda _=False, dx=dx, dy=dy: self.red_jog(dx, dy))
            arrow.setFixedSize(46, 34)
            pad.addWidget(arrow, r, c)
        line = QHBoxLayout()
        line.addLayout(pad)
        line.addWidget(self.red_step)
        line.addStretch(1)
        box.addLayout(line)
        self.red_status = hint()
        box.addWidget(self.red_status)
        self.red_save = button("Save red-light alignment", self.save_red, primary=True)
        box.addWidget(self.red_save)
        box.addStretch(1)
        tabs.addTab(page, "3 · Red light")

        layout.addWidget(button("Close", self.close))
        controller.profile_changed.connect(self.refresh)
        self.refresh()
        self._count_changed()

    # ---- shared

    def refresh(self):
        profile = self.controller.profile
        self.base.blockSignals(True)
        self.base.setCurrentIndex(self.base.findData(profile.base))
        self.base.blockSignals(False)
        self.span.setMaximum(min(profile.width, profile.height))
        notes = {
            "cor": "The factory .cor table is uploaded to the controller." if profile.correction else
                   "No .cor file is stored in the profile. Load it in Machine Setup first.",
            "ezcad": "Uses EZCAD's bulge, skew and trapezoid numbers from markcfg7. EZCAD's formula is not "
                     "published, so this is an approximation: measure before trusting it.",
            "none": "No correction. Lines will bow towards the edges of the field until you calibrate.",
        }
        self.base_hint.setText(notes[profile.base] + "\nNot sure which is right? Mark the same grid with each "
                               "and keep the one whose spans measure closest to the grid size.")
        self.current.setText(f"Measured calibration: {profile.calibration_note}" if profile.calibration
                             else "No measured calibration is applied yet.")
        self._red_status()

    def _base_changed(self):
        profile = self.controller.profile
        base = self.base.currentData()
        if base == profile.base:
            return
        if base == "cor" and not profile.correction:
            QMessageBox.information(self, "Calibrate Lens", "Load the lens .cor file in Machine Setup first.")
            self.refresh()
            return
        # A measured calibration belongs to the correction it was measured on top of.
        if self.controller.set_profile(replace(profile, base=base, calibration=None, calibration_note="")):
            self.controller.say(f"Lens correction base: {BASE_LABELS[base]}. Any measured calibration was cleared; "
                                "mark and measure a grid again.", "warning")
        self.refresh()

    def targets(self):
        return grid_points(self.span.value(), self.count.currentData())

    def _count_changed(self):
        count = self.count.currentData()
        self.stack.setCurrentIndex(0 if count == 3 else 1)
        for box in self.spans.values():
            box.setValue(self.span.value())
        diagonal = self.span.value() * 2 ** 0.5
        self.spans["d1"].setValue(diagonal)
        self.spans["d2"].setValue(diagonal)
        points = self.targets()
        self.table.setRowCount(len(points))
        for r, (x, y) in enumerate(points):
            for c, value in enumerate((x, y, x, y)):
                item = QTableWidgetItem(f"{value:.3f}")
                if c < 2:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(r, c, item)

    def add_grid(self):
        try:
            job = calibration_grid(self.span.value(), self.count.currentData())
        except ValueError as exc:
            QMessageBox.warning(self, "Calibrate Lens", str(exc))
            return
        self._count_changed()
        self.controller.add_jobs([job])
        self.controller.say("Calibration grid added. Set its power in the inspector, then arm and run it.", "success")

    def paste(self):
        text = QGuiApplication.clipboard().text()
        rows = [line.replace(",", " ").replace(";", " ").split() for line in text.strip().splitlines() if line.strip()]
        try:
            values = [(float(r[-2]), float(r[-1])) for r in rows]
        except (ValueError, IndexError):
            QMessageBox.warning(self, "Paste measurements", "Copy rows of two numbers (measured X and Y in mm).")
            return
        if len(values) != self.table.rowCount():
            QMessageBox.warning(self, "Paste measurements",
                                f"Expected {self.table.rowCount()} rows, found {len(values)}.")
            return
        for r, (x, y) in enumerate(values):
            self.table.item(r, 2).setText(f"{x:.3f}")
            self.table.item(r, 3).setText(f"{y:.3f}")

    def measured(self):
        if self.count.currentData() == 3:
            s = {key: box.value() for key, box in self.spans.items()}
            return points_from_spans(self.span.value(), (s["wt"], s["wm"], s["wb"]), (s["hl"], s["hm"], s["hr"]),
                                     (s["d1"], s["d2"]))
        return [(float(self.table.item(r, 2).text()), float(self.table.item(r, 3).text()))
                for r in range(self.table.rowCount())]

    def calculate(self):
        profile = self.controller.profile
        targets = self.targets()
        try:
            measured = self.measured()
            # What was commanded for each target when the grid was marked: the calibration active now.
            commanded = [profile.calibration.apply(t) if profile.calibration else t for t in targets]
            self.fit = fit_field_map(commanded, measured, targets, profile.half)
        except ValueError as exc:
            self.fit = None
            self.apply.setEnabled(False)
            self.result.setText(f"✖ {exc}")
            return
        worst = max(self.fit.before)
        self.result.setText(
            f"The marks are off by {self.fit.rms_before * 1000:.0f} µm on average, {worst * 1000:.0f} µm at worst.\n"
            f"The correction fits them to within {self.fit.rms * 1000:.0f} µm."
            + ("\nThat is already close; applying will only trim it." if worst < 0.05 else ""))
        self.apply.setEnabled(True)

    def apply_calibration(self):
        if self.fit is None:
            return
        note = (f"{date.today().isoformat()}, {self.count.currentData()} × {self.count.currentData()} grid over "
                f"{self.span.value():g} mm; error before {self.fit.rms_before * 1000:.0f} µm RMS")
        profile = self.controller.profile.with_calibration(self.fit.field_map, note)
        try:
            for point in self.targets():      # the corrected grid must still be reachable
                profile.galvo_point(point)
        except ValueError as exc:
            QMessageBox.warning(self, "Calibrate Lens", f"This correction pushes the grid outside the field: {exc}")
            return
        if self.controller.set_profile(profile):
            self.controller.say("Lens calibration saved. Mark the grid again and re-measure to confirm.", "success")
            self.fit = None
            self.apply.setEnabled(False)
            self.result.setText("Saved. Mark the grid again to confirm; the spans should now match the grid size.")

    def clear_calibration(self):
        if self.controller.profile.calibration and self.controller.set_profile(
                self.controller.profile.with_calibration(None)):
            self.controller.say("Measured lens calibration removed.", "warning")

    # ---- red light

    def aim(self, which: str):
        half = self.span.value() / 2
        self.red_aim = which
        self.red_points.setdefault(which, [0.0, 0.0] if which == "centre" else [half, half])
        self.controller.show_target(crosshair(tuple(self.red_points[which]), 1.5))
        self._red_status()

    def red_jog(self, dx, dy):
        if not self.red_aim:
            return
        point = self.red_points[self.red_aim]
        step = self.red_step.currentData()
        point[0] = round(point[0] + dx * step, 3)
        point[1] = round(point[1] + dy * step, 3)
        self.controller.show_target(crosshair(tuple(point), 1.5))
        self._red_status()

    def keyPressEvent(self, event):
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1)}
        if event.key() in moves and self.red_aim:
            self.red_jog(*moves[event.key()])
        elif event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            super().keyPressEvent(event)

    def _red_status(self):
        profile = self.controller.profile
        lines = [f"Saved: offset {profile.red_x:.3f}, {profile.red_y:.3f} mm · scale {profile.red_scale_x:.4f}, "
                 f"{profile.red_scale_y:.4f}"]
        for key, label in (("centre", "Centre"), ("corner", "Top-right")):
            if key in self.red_points:
                x, y = self.red_points[key]
                lines.append(f"{label} cross found at {x:.3f}, {y:.3f} mm")
        reason = self.controller.blockers()["frame"] if not self.controller.framing else ""
        if reason and self.red_aim:
            lines.append(f"Red light is off: {reason}")
        self.red_status.setText("\n".join(lines))
        self.red_save.setEnabled("centre" in self.red_points)

    def save_red(self):
        """With the old alignment, aiming at p put the dot on the centre cross and aiming at q put it on
        the corner cross at c. The new offset and scale make aiming at 0 and c land there instead."""
        profile = self.controller.profile
        p = self.red_points["centre"]
        offset = (profile.red_scale_x * p[0] + profile.red_x, profile.red_scale_y * p[1] + profile.red_y)
        scale = [profile.red_scale_x, profile.red_scale_y]
        if "corner" in self.red_points:
            q, half = self.red_points["corner"], self.span.value() / 2
            scale = [profile.red_scale_x * (q[0] - p[0]) / half, profile.red_scale_y * (q[1] - p[1]) / half]
            if not all(0.8 <= value <= 1.25 for value in scale):
                QMessageBox.warning(self, "Red light", "The two crosses imply a red-light scale outside 0.8–1.25. "
                                                       "Check that you aimed at the right crosses.")
                return
        if self.controller.marking:
            QMessageBox.information(self, "Red light", "Wait for the engraving to finish, then save.")
            return
        self.controller.clear_target()
        if self.controller.framing:
            self.controller.stop()
        self._save_attempts = 0
        updated = replace(profile, red_x=round(offset[0], 4), red_y=round(offset[1], 4),
                          red_scale_x=round(scale[0], 5), red_scale_y=round(scale[1], 5))
        self._pending_red = updated
        self._try_save_red()

    def _try_save_red(self):
        # The frame loop takes a moment to stop; the profile cannot change while the laser is busy.
        from PySide6.QtCore import QTimer

        if self.controller.busy:
            self._save_attempts += 1
            if self._save_attempts > 60 or self.controller.marking:
                self.controller.say("The red-light alignment was not saved: the laser stayed busy.", "warning")
                return
            QTimer.singleShot(150, self._try_save_red)
            return
        if self.controller.set_profile(self._pending_red):
            self.red_points.clear()
            self.red_aim = ""
            self.controller.say("Red-light alignment saved.", "success")
        self._red_status()

    def closeEvent(self, event):
        self.controller.clear_target()
        super().closeEvent(event)
