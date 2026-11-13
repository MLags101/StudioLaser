"""Dialogs: PCB import, hole registration, presets and material tests."""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..patterns import PARAMETERS, focus_test, hole_targets, kerf_ladder, steps, test_matrix
from ..presets import Preset, layer_summary, starting_layer
from ..workspace import CLEANUP, COPPER_CONTOUR, COPPER_FILL, KIND_LABELS, KINDS, fit_placement, fit_to_outline
from . import theme
from .canvas import painter_path
from .panels import button, heading, hint, number, row, whole

ROLE_LABELS = {
    "top_copper": "Top copper", "bottom_copper": "Bottom copper",
    "top_mask": "Top solder mask", "bottom_mask": "Bottom solder mask",
    "top_silk": "Top silkscreen", "bottom_silk": "Bottom silkscreen",
    "outline": "Board outline", "drill": "Drill / slots", "other": "Ignore",
}


class PcbImportDialog(QDialog):
    """Review the exact Gerber geometry before adding it to the workspace."""

    def __init__(self, paths: list[Path], controller, parent=None, *, target=None):
        super().__init__(parent)
        self.setWindowTitle("Import Gerber / PCB")
        self.resize(730, 740)
        self.paths = paths
        self.controller = controller
        self.jobs = None
        self.target = target
        self.generation = 0
        self._reviewed = -1
        self._pending = None
        self._detected_roles = False
        layout = QVBoxLayout(self)
        layout.addWidget(hint("Confirm the file roles and preview the removal. Use files exported at 1:1 "
                              "from the same origin. StudioLaser mirrors the bottom side."))
        self.table = QTableWidget(len(paths), 2)
        self.table.setHorizontalHeaderLabels(["File", "Use as"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setMinimumHeight(110)
        self.table.setMaximumHeight(180)
        self.roles = []
        for index, path in enumerate(paths):
            item = QTableWidgetItem(path.name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setToolTip(str(path))
            self.table.setItem(index, 0, item)
            combo = QComboBox()
            combo.addItem("Choose a role…", "")
            for role, label in ROLE_LABELS.items():
                combo.addItem(label, role)
            combo.setEnabled(False)
            combo.currentIndexChanged.connect(self.invalidate)
            self.table.setCellWidget(index, 1, combo)
            self.roles.append(combo)
        layout.addWidget(self.table)

        form = QFormLayout()
        self.destination = QComboBox()
        self.destination.addItem("Create a separate PCB job", False)
        if (target is not None and target.side and target.origin is not None
                and target.work_area and target.status == "ready"):
            self.destination.addItem(f"Add operations to {target.name}", True)
            self.destination.setCurrentIndex(1)
        elif target is not None:
            layout.addWidget(hint("To add Gerbers in place, select a ready PCB side with a saved origin and work area. "
                                  "Reimport older boards to preserve their hole and slot exclusions."))
        form.addRow("Destination", self.destination)
        self.mode = QComboBox()
        self.mode.addItem("Isolate traces", "isolation")
        self.mode.addItem("Clear all non-trace copper", "full")
        self.mode.addItem("Engrave drawn shapes", "drawn")
        form.addRow("Copper output", self.mode)
        self.width = number(0.25, 0.05, 5, 3, "mm", 0.05)
        form.addRow("Isolation width", self.width)
        self.kerf = number(controller.presets.kerf(), 0, 1, 3, "mm", 0.01)
        form.addRow("Kerf compensation", self.kerf)
        self.flip = QComboBox()
        self.flip.addItem("Left ↔ right", "vertical")
        self.flip.addItem("Top ↔ bottom", "horizontal")
        form.addRow("Flip axis", self.flip)
        self.first = QComboBox()
        self.first.addItem("Top", "top")
        self.first.addItem("Bottom", "bottom")
        form.addRow("Run first", self.first)
        layout.addLayout(form)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setMinimumHeight(190)
        self.view.setAccessibleName("Gerber geometry preview")
        self.view.setBackgroundBrush(theme.color("canvas"))
        layout.addWidget(self.view, 1)
        self.side = QComboBox()
        self.side.currentIndexChanged.connect(self._draw)
        layout.addWidget(self.side)
        layout.addWidget(hint("Green: retained copper   ·   Orange: copper removal or drawn shapes   ·   "
                              "Frame: work area   ·   Mask and silkscreen use their operation colors"))
        self.coverage = QCheckBox("The stock covers the inferred work area shown above")
        self.coverage.toggled.connect(self._check)
        self.coverage.hide()
        layout.addWidget(self.coverage)
        self.problem = hint("Reading layer roles…")
        layout.addWidget(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.add_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.add_button.setText("Add to workspace")
        self.review_button = self.buttons.addButton("Preview geometry", QDialogButtonBox.ButtonRole.ActionRole)
        self.review_button.clicked.connect(self.review)
        self.buttons.accepted.connect(self._commit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for widget in (self.mode, self.flip, self.first, self.destination):
            widget.currentIndexChanged.connect(self.invalidate)
        for widget in (self.width, self.kerf):
            widget.valueChanged.connect(self.invalidate)
        self._future = controller.executor.submit(self._detect, list(paths))
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_result)
        self._poll.start(60)
        self._check()

    def done(self, result):
        self._poll.stop()
        super().done(result)

    @staticmethod
    def _detect(paths):
        from ..pcb.board import detect_role

        found = []
        for path in paths:
            try:
                found.append(detect_role(path))
            except Exception:
                found.append("other")
        return found

    @property
    def add_target(self):
        return self.target if self.destination.currentData() else None

    def inputs(self):
        result = {}
        for path, combo in zip(self.paths, self.roles):
            if combo.currentData() not in {"", "other"}:
                result.setdefault(combo.currentData(), []).append(path)
        return result

    def invalidate(self):
        self.generation += 1
        self.jobs = None
        self.side.clear()
        self.scene.clear()
        self.coverage.hide()
        self.coverage.setChecked(False)
        self._check()

    def _check(self):
        inputs = self.inputs()
        roles = {role.split("_")[0] for role in inputs if "_" in role}
        self.first.setEnabled(len(roles) == 2 and self.add_target is None)
        self.flip.setEnabled("bottom" in roles and self.add_target is None)
        self.width.setEnabled(self.mode.currentData() == "isolation")
        reason = ""
        if not self._detected_roles:
            reason = "Reading layer roles…"
        elif any(not combo.currentData() for combo in self.roles):
            reason = "Choose a role or Ignore for every unrecognized file."
        elif not roles:
            reason = "Select at least one copper, solder mask or silkscreen layer."
        elif any(role != "drill" and len(files) > 1 for role, files in inputs.items()):
            reason = "Assign each role once. Multiple drill files are allowed."
        elif self.add_target is None and not inputs.get("outline") and (
            len(roles) == 2 or (self.mode.currentData() == "full" and any(r.endswith("_copper") for r in inputs))
        ):
            reason = "Add a closed outline for two sides or full clear."
        self.review_button.setEnabled(not reason and self._pending is None)
        self.kerf.setEnabled(self.mode.currentData() != "drawn" or any(r.endswith("_mask") for r in inputs))
        self.add_button.setEnabled(bool(self.jobs) and self._reviewed == self.generation
                                   and (self.coverage.isHidden() or self.coverage.isChecked()))
        if reason:
            self.problem.setText(reason)
        elif not self.jobs and self._pending is None and self._reviewed != self.generation:
            self.problem.setText("Preview the geometry to review it before adding the job.")

    def settings(self):
        from ..pcb.inverter import InversionMode

        mode = {"isolation": InversionMode.ISOLATION, "full": InversionMode.FULL_INVERT,
                "drawn": InversionMode.POSITIVE}[self.mode.currentData()]
        return dict(mode=mode, isolation_width=self.width.value(), flip_axis=self.flip.currentData(),
                    kerf_mm=self.kerf.value(), first_side=self.first.currentData(), allow_inferred=True)

    def review(self):
        from ..pcb.board import prepare_jobs

        self.invalidate()
        self._pending = self.generation
        self.problem.setText("Preparing geometry…")
        self._future = self.controller.executor.submit(
            prepare_jobs, self.inputs(), self.controller.presets.defaults(), target=self.add_target, **self.settings())
        self._check()

    def _poll_result(self):
        if not self._future.done():
            return
        if not self._detected_roles:
            found = self._future.result()
            for combo, role in zip(self.roles, found):
                index = combo.findData(role) if role != "other" else 0
                combo.setCurrentIndex(max(0, index))
                combo.setEnabled(True)
            self._detected_roles = True
            self._check()
            return
        if self._pending is None:
            return
        generation, self._pending = self._pending, None
        if generation != self.generation:
            self._check()
            return
        self._reviewed = generation
        try:
            self.jobs = self._future.result()
        except Exception as exc:
            self.problem.setText(str(exc))
        else:
            self.side.clear()
            for job in self.jobs:
                self.side.addItem(job.name)
            self.side.setVisible(len(self.jobs) > 1)
            self.coverage.setVisible(any(job.inferred for job in self.jobs))
            self.problem.setText("Inferred work area. Check that it fits the stock." if not self.coverage.isHidden()
                                 else "Check the removal and registration before adding the job.")
            self._draw()
        self._check()

    def _draw(self):
        if not self.jobs or self.side.currentIndex() < 0:
            return
        job = self.jobs[self.side.currentIndex()]
        self.scene.clear()

        def draw(paths, color, *, fill=True, dashed=False):
            if not paths:
                return
            shape = painter_path(paths, closed=fill)
            pen = QPen(QColor(color), 0)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            self.scene.addPath(shape, pen, QBrush(QColor(color)) if fill else QBrush())

        draw(job.retained, "#38866a")
        for obj in job.objects:
            if obj.kind in {COPPER_CONTOUR, CLEANUP}:
                continue
            color = "#dc843b" if obj.kind in {COPPER_FILL, ""} else obj.layer.color
            draw(obj.fills or obj.paths, color, fill=bool(obj.fills))
        draw(job.boundary, "#8794a8", fill=False, dashed=job.inferred)
        bounds = self.scene.itemsBoundingRect()
        if not bounds.isEmpty():
            self.scene.setSceneRect(bounds.adjusted(-1, -1, 1, 1))
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "view") and not self.scene.sceneRect().isEmpty():
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _commit(self):
        if self.add_button.isEnabled():
            self.accept()


class RegistrationDialog(QDialog):
    """Fit the board's position from drilled holes or from its edges, found with the red light.

    A tool window, not a modal one: the canvas stays live so points are chosen
    by clicking them, and Stop stays reachable.
    """

    TOLERANCE = 0.15   # mm; above this a residual is flagged

    def __init__(self, controller, canvas, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Register board")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(False)
        self.resize(450, 600)
        self.controller = controller
        self.canvas = canvas
        job = controller.job
        self.job_id = job.job_id
        self.start = job.placement
        # Each point: [hole index or None, design point, machine point [x, y]].
        self.points: list[list] = []
        self.active = -1
        self.result = None
        layout = QVBoxLayout(self)
        self.mode = QComboBox()
        if job.references:
            self.mode.addItem("Drilled holes" if job.holes else "Board corners", "holes")
        if job.boundary and not job.inferred:
            self.mode.addItem("Board edge (edge cuts)", "edge")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        layout.addLayout(row(QLabel("Register on"), self.mode, stretch=True))
        self.instructions = hint()
        layout.addWidget(self.instructions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Point", "Machine X", "Machine Y", "Error"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda r, *_: self._activate(r))
        layout.addWidget(self.table, 1)

        layout.addWidget(heading("Steer the red dot"))
        self.step = QComboBox()
        for value in (1.0, 0.1, 0.02):
            self.step.addItem(f"{value:g} mm per press", value)
        self.step.setCurrentIndex(1)
        pad = QGridLayout()
        for text, dx, dy, r, c in (("▲", 0, 1, 0, 1), ("◀", -1, 0, 1, 0), ("▶", 1, 0, 1, 2), ("▼", 0, -1, 2, 1)):
            arrow = button(text, lambda _=False, dx=dx, dy=dy: self.jog(dx, dy))
            arrow.setFixedSize(46, 34)
            arrow.setAutoRepeat(True)
            arrow.setStyleSheet("font-size: 14pt; padding: 0;")
            pad.addWidget(arrow, r, c)
        pad_box = QHBoxLayout()
        pad_box.addLayout(pad)
        side = QVBoxLayout()
        side.addWidget(self.step)
        side.addWidget(hint("Arrow keys work too while this window is active."))
        self.light = QLabel()
        self.light.setObjectName("hint")
        self.light.setWordWrap(True)
        side.addWidget(self.light)
        pad_box.addLayout(side, 1)
        layout.addLayout(pad_box)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.remove = button("Remove point", self.remove_point)
        self.check = button("Check with red light", self.show_check,
                            tip="Traces the fitted result with the red light: rings at the holes, or the board outline")
        self.apply = button("Apply registration", self.accept, primary=True)
        layout.addLayout(row(self.remove, self.check))
        layout.addLayout(row(button("Cancel", self.reject), self.apply))

        canvas.picking = True
        canvas.marks = []
        canvas.reference_picked.connect(self.add_hole)
        canvas.edge_picked.connect(self.add_edge)
        controller.selection_changed.connect(self.reject)
        controller.state_changed.connect(self._light_state)
        self._mode_changed()
        self._light_state()

    @property
    def edge_mode(self) -> bool:
        return self.mode.currentData() == "edge"

    def _mode_changed(self):
        self.points.clear()
        self.active = -1
        self.canvas.pick_mode = self.mode.currentData()
        if self.edge_mode:
            self.instructions.setText(
                "1. Frame first so the outline is roughly on the board.\n"
                "2. Click a point on the board outline (edge cuts) on the canvas.\n"
                "3. Steer the red dot onto that edge of the real board. Any spot along the edge will do.\n"
                "4. Add points on at least two different edges, three or more in total. Corners and opposite "
                "edges give the best fit.")
        else:
            what = "drilled hole" if self.controller.job.holes else "board corner"
            self.instructions.setText(
                f"1. Click a {what} on the canvas (yellow markers).\n"
                f"2. Steer the red dot onto that {what} on the real board.\n"
                f"3. Repeat for a second {what} far from the first. A third or fourth checks the fit.")
        self.controller.clear_target()
        self._fill()

    def _light_state(self):
        reason = self.controller.blockers()["frame"] if not self.controller.framing else ""
        self.light.setText(f"Red light is off: {reason}. You can still type positions measured another way."
                           if reason else "")

    def _current_job(self):
        job = self.controller.job
        return job if job is not None and job.job_id == self.job_id else None

    def add_hole(self, index: int):
        job = self._current_job()
        if job is None or self.edge_mode:
            return
        existing = next((r for r, point in enumerate(self.points) if point[0] == index), None)
        if existing is None:
            if len(self.points) >= 6:
                return
            design = job.references[index]
            self.points.append([index, design, list(job.placement.apply(design))])
            existing = len(self.points) - 1
        self._select(existing)

    def add_edge(self, x: float, y: float):
        job = self._current_job()
        if job is None or not self.edge_mode or len(self.points) >= 8:
            return
        self.points.append([None, (x, y), list(job.placement.apply((x, y)))])
        self._select(len(self.points) - 1)

    def _select(self, row_index):
        self.active = row_index
        self._fill()
        self._activate(row_index)

    def remove_point(self):
        if 0 <= self.active < len(self.points):
            del self.points[self.active]
            self.active = min(self.active, len(self.points) - 1)
            self._fill()
            self._activate(self.active)

    def _activate(self, row_index: int):
        if not 0 <= row_index < len(self.points):
            self.active = -1
            self.controller.clear_target()
            return
        self.active = row_index
        self.controller.show_point(tuple(self.points[row_index][2]))

    def jog(self, dx: float, dy: float):
        if not 0 <= self.active < len(self.points):
            return
        step = self.step.currentData()
        point = self.points[self.active][2]
        half_w, half_h = self.controller.profile.width / 2, self.controller.profile.height / 2
        point[0] = round(max(-half_w, min(half_w, point[0] + dx * step)), 3)
        point[1] = round(max(-half_h, min(half_h, point[1] + dy * step)), 3)
        self.controller.show_point(tuple(point))
        self._fill()

    def keyPressEvent(self, event):
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1)}
        if event.key() in moves:
            self.jog(*moves[event.key()])
        elif event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):   # Enter must not apply by accident
            super().keyPressEvent(event)

    def _fill(self):
        self._refit()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.points))
        label = "Edge" if self.edge_mode else "Hole" if self.controller.job.holes else "Corner"
        for row_index, (_, _, (x, y)) in enumerate(self.points):
            error = ""
            if self.result and row_index < len(self.result.residuals):
                error = f"{self.result.residuals[row_index] * 1000:.0f} µm"
            for column, text in enumerate((f"{label} {row_index + 1}", f"{x:.3f}", f"{y:.3f}", error)):
                self.table.setItem(row_index, column, QTableWidgetItem(text))
        if 0 <= self.active < len(self.points):
            self.table.selectRow(self.active)
        self.table.blockSignals(False)
        job = self._current_job()
        self.canvas.marks = [job.placement.apply(design) for _, design, _ in self.points] if job else []
        self.canvas.viewport().update()

    def _refit(self):
        job = self._current_job()
        self.result = None
        need = 3 if self.edge_mode else 2
        text = f"Add {need - len(self.points)} more point(s) to fit." if len(self.points) < need else ""
        if job is not None and len(self.points) >= need:
            measured = [tuple(machine) for _, _, machine in self.points]
            try:
                if self.edge_mode:
                    self.result = fit_to_outline(job.boundary, measured, self.start,
                                                 picked=[design for _, design, _ in self.points])
                else:
                    self.result = fit_placement([design for _, design, _ in self.points], measured)
            except ValueError as exc:
                text = str(exc)
            else:
                p, worst = self.result.placement, self.result.worst
                text = f"Fitted position X {p.x:.3f}, Y {p.y:.3f} mm, rotation {p.angle:.3f}°.\n"
                if self.edge_mode:
                    text += f"Worst point is {worst * 1000:.0f} µm from the board edge."
                    if not self.result.well_posed:
                        self.result = None
                        text += ("\n⚠ These points cannot fix both position and rotation (for example, all on one "
                                 "straight edge). Add a point on a neighbouring edge.")
                else:
                    spacing = self.result.spacing_error
                    text += (f"Hole spacing differs from the design by {spacing * 1000:.0f} µm"
                             + (f"; worst hole is {worst * 1000:.0f} µm off the fit." if len(self.points) > 2 else "."))
                    worst = max(worst, spacing)
                if self.result is not None and worst > self.TOLERANCE:
                    text += ("\n⚠ That is more than expected. Check the points, that the board is the right way "
                             "up, and that the lens is calibrated.")
        self.summary.setText(text)
        self.apply.setEnabled(self.result is not None)
        self.check.setEnabled(self.result is not None)
        self.remove.setEnabled(bool(self.points))

    def show_check(self):
        job = self._current_job()
        if not (self.result and job):
            return
        if self.edge_mode:
            self.controller.show_target(self.result.placement.paths(job.boundary))
        else:
            placed = [self.result.placement.apply(design) for _, design, _ in self.points]
            self.controller.show_target(hole_targets(placed, radius=0.8))

    def done(self, code):
        self.canvas.picking = False
        self.canvas.marks = []
        try:
            self.canvas.reference_picked.disconnect(self.add_hole)
            self.canvas.edge_picked.disconnect(self.add_edge)
            self.controller.selection_changed.disconnect(self.reject)
            self.controller.state_changed.disconnect(self._light_state)
        except (RuntimeError, TypeError):
            pass
        self.controller.clear_target()
        job = self._current_job()
        if code == QDialog.DialogCode.Accepted and self.result and job:
            self.controller.set_placement(self.result.placement, merge="")
            self.controller.say(self.summary.text().splitlines()[0] + " Check the frame, then tick the alignment box.",
                                "success")
        self.canvas.viewport().update()
        super().done(code)


class SavePresetDialog(QDialog):
    """Name a preset and record what it was tuned for, in one form."""

    def __init__(self, layer, kind, controller, parent=None, preset: Preset | None = None):
        super().__init__(parent)
        self.setWindowTitle("Save preset" if preset is None else "Edit preset")
        self.layer = layer
        self.preset = preset
        profile = controller.profile
        form = QFormLayout(self)
        form.addRow(hint(layer_summary(layer)))
        self.name = QLineEdit(preset.name if preset else KIND_LABELS.get(kind, "Engrave"))
        self.kind = QComboBox()
        self.kind.addItem("Any operation", "")
        for value in KINDS:
            self.kind.addItem(KIND_LABELS[value], value)
        self.kind.setCurrentIndex(max(0, self.kind.findData(preset.kind if preset else kind)))
        self.stock = QLineEdit(preset.stock if preset else "FR4")
        self.copper = number(preset.copper_um if preset else 35, 0, 1000, 0, "µm", 1)
        self.kerf = number(preset.kerf_mm if preset else 0, 0, 1, 3, "mm", 0.01)
        self.kerf.setToolTip("Measured line width for this preset. Used as the default kerf compensation "
                             "when this is the default preset for copper fill.")
        self.note = QLineEdit(preset.note if preset else "")
        self.default = QCheckBox("Use for new boards (default for this operation)")
        self.default.setChecked(bool(preset and preset.default))
        form.addRow("Name", self.name)
        form.addRow("Operation", self.kind)
        form.addRow("Material", self.stock)
        form.addRow("Copper thickness", self.copper)
        form.addRow("Measured kerf", self.kerf)
        form.addRow("Note", self.note)
        form.addRow("", self.default)
        self.machine = (profile.name, profile.lens, profile.source) if preset is None else \
            (preset.machine, preset.lens, preset.source)
        form.addRow(hint(f"Recorded for {self.machine[0]} · {self.machine[1] or 'no lens set'}"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_preset(self) -> Preset:
        return Preset(
            self.preset.preset_id if self.preset and not self.preset.builtin else "", self.name.text().strip(),
            self.kind.currentData(), replace(self.layer), self.machine[0], self.machine[1], self.machine[2],
            self.stock.text().strip(), self.copper.value(), self.kerf.value(), self.note.text().strip(),
            False, self.default.isChecked(),
        )


class PresetManagerDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Laser presets")
        self.resize(720, 480)
        self.controller = controller
        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("All operations", None)
        for kind in KINDS:
            self.filter.addItem(KIND_LABELS[kind], kind)
        self.filter.currentIndexChanged.connect(self.fill)
        left.addWidget(self.filter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.show_preset)
        left.addWidget(self.list, 1)
        left.addLayout(row(button("Import…", self.import_file), button("Export all…", self.export)))
        layout.addLayout(left, 1)
        right = QVBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("title")
        self.title.setWordWrap(True)
        self.detail = hint()
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.title)
        right.addWidget(self.detail)
        right.addStretch(1)
        self.edit = button("Edit…", self.edit_preset)
        self.duplicate = button("Duplicate", self.duplicate_preset,
                                tip="Make your own editable copy, for example of a built-in starting point")
        self.default = button("Make default", self.make_default,
                              tip="New boards start with this preset for its operation")
        self.delete = button("Delete", self.delete_preset)
        right.addLayout(row(self.edit, self.duplicate))
        right.addLayout(row(self.default, self.delete))
        right.addWidget(button("Close", self.accept))
        layout.addLayout(right, 1)
        self.fill()

    def fill(self):
        keep = self.current().preset_id if self.current() else ""
        self.list.clear()
        kind = self.filter.currentData()
        for preset in self.controller.presets.all():
            if kind is None or preset.kind == kind:
                item = QListWidgetItem(("★ " if preset.default else "") + preset.label())
                item.setData(Qt.ItemDataRole.UserRole, preset.preset_id)
                self.list.addItem(item)
                if preset.preset_id == keep:
                    self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.show_preset()

    def current(self) -> Preset | None:
        item = self.list.currentItem()
        return self.controller.presets.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def show_preset(self, *_):
        preset = self.current()
        for widget in (self.edit, self.delete, self.default):
            widget.setEnabled(bool(preset) and not preset.builtin)
        self.duplicate.setEnabled(bool(preset))
        if preset is None:
            self.title.setText("No presets")
            self.detail.setText("")
            return
        self.title.setText(preset.name)
        lines = [KIND_LABELS.get(preset.kind, "Any operation"), preset.summary(), ""]
        if preset.stock or preset.copper_um:
            lines.append(f"Material: {preset.stock or '—'}" + (f", {preset.copper_um:g} µm copper" if preset.copper_um else ""))
        if preset.kerf_mm:
            lines.append(f"Measured kerf: {preset.kerf_mm:g} mm")
        if preset.machine or preset.lens:
            lines.append(f"Machine: {preset.machine} · {preset.lens}")
        if preset.note:
            lines += ["", preset.note]
        self.detail.setText("\n".join(lines))

    def _change(self, action):
        try:
            action()
        except Exception as exc:
            QMessageBox.warning(self, "Presets", str(exc))
        self.controller.presets_changed.emit()
        self.fill()

    def edit_preset(self):
        preset = self.current()
        dialog = SavePresetDialog(preset.layer, preset.kind, self.controller, self, preset)
        if dialog.exec():
            self._change(lambda: self.controller.presets.add(dialog.result_preset()))

    def duplicate_preset(self):
        preset = self.current()
        copy = replace(preset, preset_id="", name=preset.name.replace(" — 60 W starting point", "") + " (copy)",
                       builtin=False, default=False, note=preset.note)
        self._change(lambda: self.controller.presets.add(copy))

    def make_default(self):
        self._change(lambda: self.controller.presets.add(replace(self.current(), default=True)))

    def delete_preset(self):
        preset = self.current()
        if QMessageBox.question(self, "Delete preset", f"Delete “{preset.name}”?") == QMessageBox.StandardButton.Yes:
            self._change(lambda: self.controller.presets.remove(preset.preset_id))

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import presets", filter="StudioLaser presets (*.json)")
        if path:
            self._change(lambda: self.controller.say(
                f"Imported {self.controller.presets.import_file(Path(path))} preset(s).", "success"))

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export presets", "studiolaser-presets.json",
                                              "StudioLaser presets (*.json)")
        if path:
            self._change(lambda: self.controller.presets.export(Path(path)))


class MaterialTestDialog(QDialog):
    """Build a grid of patches that varies two settings, to find a working preset."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Material test matrix")
        self.controller = controller
        self.job = None
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "Engraves a grid of small patches on scrap of the real material. Each patch uses different "
            "settings; pick the best one afterwards, select it in the job list and save it as a preset."))
        form = QFormLayout()
        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(KIND_LABELS[kind], kind)
        self.kind.setCurrentIndex(self.kind.findData(COPPER_FILL))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("Test for", self.kind)
        self.base = QComboBox()
        form.addRow("Start from preset", self.base)
        layout.addLayout(form)
        grid = QGridLayout()
        for column, text in enumerate(("", "Setting", "From", "To", "Steps")):
            grid.addWidget(heading(text), 0, column)
        self.axes = []
        for line, (label, param, first, last, count) in enumerate((
            ("Rows", "power_percent", 40, 100, 4), ("Columns", "speed_mm_s", 500, 2000, 4),
        ), start=1):
            combo = QComboBox()
            for key, (name, unit, *_rest) in PARAMETERS.items():
                combo.addItem(f"{name} ({unit})", key)
            combo.setCurrentIndex(combo.findData(param))
            low = number(first, 0.001, 10000, 3)
            high = number(last, 0.001, 10000, 3)
            count_box = whole(count, 1, 12)
            grid.addWidget(QLabel(label), line, 0)
            for column, widget in enumerate((combo, low, high, count_box), start=1):
                grid.addWidget(widget, line, column)
            self.axes.append((combo, low, high, count_box))
        layout.addLayout(grid)
        form = QFormLayout()
        self.cell = number(5, 1, 20, 1, "mm", 1)
        form.addRow("Patch size", self.cell)
        layout.addLayout(form)
        self.note = hint()
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add to job list")
        buttons.accepted.connect(self.build)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._kind_changed()

    def _kind_changed(self):
        self.base.clear()
        for preset in self.controller.presets.for_kind(self.kind.currentData()):
            self.base.addItem(preset.label(), preset.preset_id)
        widths = self.controller.profile.pulse_widths
        self.note.setText("Pulse width values are snapped to the widths this source accepts." if widths else "")

    def build(self):
        preset = self.controller.presets.get(self.base.currentData() or "")
        base = replace(preset.layer) if preset else starting_layer(self.kind.currentData())
        base = replace(base, output=True)
        try:
            values = []
            for combo, low, high, count in self.axes:
                param = combo.currentData()
                integer = PARAMETERS[param][5]
                axis = steps(low.value(), high.value(), count.value(), integer=integer)
                if param == "pulse_width_ns" and self.controller.profile.pulse_widths:
                    widths = self.controller.profile.pulse_widths
                    axis = list(dict.fromkeys(min(widths, key=lambda w: abs(w - v)) for v in axis))
                values.append((param, axis))
            self.job = test_matrix(base, values[0][0], values[0][1], values[1][0], values[1][1],
                                   cell=self.cell.value(), gap=max(2.0, self.cell.value() * 0.4))
        except ValueError as exc:
            QMessageBox.warning(self, "Material test", str(exc))
            return
        self.accept()


def kerf_ladder_job(controller):
    presets = controller.presets
    fill = next((p.layer for p in presets.for_kind(COPPER_FILL) if p.kind == COPPER_FILL), starting_layer(COPPER_FILL))
    contour = next((p.layer for p in presets.for_kind(COPPER_CONTOUR) if p.kind == COPPER_CONTOUR),
                   starting_layer(COPPER_CONTOUR))
    return kerf_ladder(replace(fill, output=True), replace(contour, output=True))


def focus_job(controller, slot: int):
    return focus_test(replace(starting_layer(COPPER_CONTOUR), passes=1, output=True), slot)
