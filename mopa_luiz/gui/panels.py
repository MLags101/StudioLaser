"""Docked panels: the job tree, the inspector and the activity log."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..job_planner import format_duration, layer_issues
from ..layers import RASTER_ENGRAVE, VECTOR_ENGRAVE
from ..presets import layer_summary
from ..workspace import KIND_LABELS
from . import theme

STATUS_TEXT = {"ready": "", "running": "Running", "interrupted": "Interrupted — review", "complete": "Done"}


def heading(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("heading")
    return label


def hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def number(value=0.0, low=0.0, high=100.0, decimals=2, suffix="", step=1.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    box.setKeyboardTracking(False)   # commit on Enter or focus-out, not on every keystroke
    box.setAccelerated(True)
    if suffix:
        box.setSuffix(f" {suffix}")
    return box


def whole(value=1, low=1, high=999, suffix="") -> QSpinBox:
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    box.setKeyboardTracking(False)
    if suffix:
        box.setSuffix(f" {suffix}")
    return box


def button(text, slot=None, *, primary=False, tip="") -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    if slot:
        widget.clicked.connect(slot)
    if tip:
        widget.setToolTip(tip)
    return widget


def row(*widgets, stretch=False) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(6)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return layout


class JobsPanel(QWidget):
    """Queue of jobs; a PCB side lists its operations with enable ticks."""

    operation_selected = Signal(int)
    import_board = Signal()
    import_artwork = Signal()

    def __init__(self, controller):
        super().__init__()
        self.setObjectName("panel")
        self.controller = controller
        self._building = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(row(
            button("Import PCB…", self.import_board.emit, primary=True,
                   tip="Gerber copper, mask, silkscreen, outline and drill files"),
            button("Import Artwork…", self.import_artwork.emit, tip="SVG, DXF or STL"),
        ))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setIndentation(16)
        self.tree.setUniformRowHeights(False)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemChanged.connect(self._toggled)
        layout.addWidget(self.tree, 1)
        self.review = button("Review and allow re-run", controller.review,
                             tip="Confirm you checked the board, then make this job runnable again")
        layout.addWidget(self.review)
        self.up = button("▲ Up", lambda: controller.move_job(-1), tip="Move this job earlier in the list")
        self.down = button("▼ Down", lambda: controller.move_job(1), tip="Move this job later in the list")
        self.remove = button("Remove", lambda: controller.remove_job())
        layout.addLayout(row(self.up, self.down, self.remove))
        controller.jobs_changed.connect(self.rebuild)
        controller.selection_changed.connect(self.rebuild)
        controller.job_changed.connect(self.rebuild)
        controller.state_changed.connect(self.refresh_state)
        self.rebuild()

    def rebuild(self):
        self._building = True
        selected_op = self._current_operation()
        self.tree.clear()
        current_item = None
        for index, job in enumerate(self.controller.jobs):
            status = STATUS_TEXT.get(job.status, job.status)
            item = QTreeWidgetItem([job.name + (f"   · {status}" if status else "")])
            item.setData(0, Qt.ItemDataRole.UserRole, (index, -1))
            font = item.font(0)
            font.setBold(index == self.controller.current)
            item.setFont(0, font)
            if job.status == "interrupted":
                item.setForeground(0, theme.color("warning"))
            elif job.status == "complete":
                item.setForeground(0, theme.color("success"))
            self.tree.addTopLevelItem(item)
            if index == self.controller.current:
                current_item = item
                for op, artwork in enumerate(job.objects):
                    child = QTreeWidgetItem([f"{artwork.name}\n{layer_summary(artwork.layer)}"])
                    child.setData(0, Qt.ItemDataRole.UserRole, (index, op))
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Checked if artwork.layer.output else Qt.CheckState.Unchecked)
                    child.setForeground(0, theme.color("text" if artwork.layer.output else "faint"))
                    child.setToolTip(0, f"{layer_summary(artwork.layer)}\nTick to run this operation")
                    item.addChild(child)
                    if op == selected_op:
                        current_item = child
                item.setExpanded(True)
        if current_item is not None:
            self.tree.setCurrentItem(current_item)
        self._building = False
        self.refresh_state()

    def _current_operation(self) -> int:
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return data[1] if data and data[0] == self.controller.current else -1

    def _selected(self, item, _previous):
        if self._building or item is None:
            return
        index, op = item.data(0, Qt.ItemDataRole.UserRole)
        if index != self.controller.current:
            self.controller.select(index)
            if index != self.controller.current:   # refused while a job is running
                self.rebuild()
            return
        if op >= 0:
            self.operation_selected.emit(op)

    def _toggled(self, item, _column):
        if self._building:
            return
        index, op = item.data(0, Qt.ItemDataRole.UserRole)
        job = self.controller.job
        if op < 0 or job is None or index != self.controller.current:
            return
        layer = job.objects[op].layer
        wanted = item.checkState(0) == Qt.CheckState.Checked
        if wanted != layer.output:
            self.controller.set_layer(op, replace(layer, output=wanted))
            if self.controller.job.objects[op].layer.output != wanted:
                self.rebuild()

    def refresh_state(self):
        job = self.controller.job
        locked = bool(self.controller.blockers()["edit"])
        self.review.setVisible(bool(job) and job.status in {"interrupted", "complete"})
        for widget in (self.up, self.down, self.remove):
            widget.setEnabled(bool(job) and not locked)
        self.tree.setEnabled(not self.controller.marking)


class Inspector(QScrollArea):
    """Placement and laser settings for the selected job and operation, always visible."""

    register_requested = Signal()
    save_preset_requested = Signal(int)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.operation = 0
        self._loading = False
        self._free_mode = True
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("panel")
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(6)

        self.empty = hint("Import a board or artwork to begin.\n\nFirst time on this machine? Open "
                          "Machine → Machine Setup and load the two factory files.")
        layout.addWidget(self.empty)

        # Placement
        self.placement_box = QWidget()
        self.placement_box.setObjectName("section")
        form = QFormLayout(self.placement_box)
        form.setContentsMargins(0, 0, 0, 0)
        self.x = number(0, -500, 500, 3, "mm", 0.1)
        self.y = number(0, -500, 500, 3, "mm", 0.1)
        self.angle = number(0, -180, 180, 3, "°", 0.1)
        self.scale = number(100, 1, 10000, 3, "%", 1)
        self.mirror = QCheckBox("Mirror left–right")
        for widget in (self.x, self.y, self.angle, self.scale):
            widget.valueChanged.connect(self._placement_edited)
        self.mirror.toggled.connect(self._placement_edited)
        form.addRow(heading("Placement"))
        form.addRow("X", self.x)
        form.addRow("Y", self.y)
        form.addRow("Rotation", self.angle)
        form.addRow("Scale", self.scale)
        form.addRow("", self.mirror)
        # Jog pad: moves the design while the red light traces it, so the trace can be walked onto the board.
        self.jog_step = QComboBox()
        for label, value in (("1 mm · 1°", 1.0), ("0.5 mm · 0.5°", 0.5), ("0.1 mm · 0.1°", 0.1),
                             ("0.05 mm · 0.05°", 0.05), ("0.01 mm · 0.01°", 0.01)):
            self.jog_step.addItem(label, value)
        self.jog_step.setCurrentIndex(2)
        self.jog_step.setToolTip("Step for the jog buttons. On the canvas the arrow keys move 0.1 mm "
                                 "(Shift 0.01, Alt 1) and Ctrl+arrows rotate.")
        pad = QGridLayout()
        pad.setSpacing(4)
        for text, dx, dy, turn, r, col, tip in (
            ("⟲", 0, 0, 1, 0, 0, "Rotate anticlockwise about the design's centre"),
            ("▲", 0, 1, 0, 0, 1, "Move up"),
            ("⟳", 0, 0, -1, 0, 2, "Rotate clockwise about the design's centre"),
            ("◀", -1, 0, 0, 1, 0, "Move left"),
            ("▼", 0, -1, 0, 1, 1, "Move down"),
            ("▶", 1, 0, 0, 1, 2, "Move right"),
        ):
            jog = button(text, lambda _=False, dx=dx, dy=dy, turn=turn: self._jog(dx, dy, turn), tip=tip)
            jog.setFixedSize(40, 30)
            jog.setStyleSheet("font-size: 14pt; padding: 0;")
            jog.setAutoRepeat(True)
            jog.setAutoRepeatDelay(350)
            jog.setAutoRepeatInterval(90)
            pad.addWidget(jog, r, col)
        jog_box = QHBoxLayout()
        jog_box.addLayout(pad)
        steps = QVBoxLayout()
        steps.addWidget(self.jog_step)
        self.frame_hint = hint()
        steps.addWidget(self.frame_hint)
        jog_box.addLayout(steps, 1)
        form.addRow(jog_box)
        self.centre = button("Centre in field", lambda: self._set_xy(0, 0))
        self.register = button("Register on holes…", self.register_requested.emit, primary=True,
                               tip="Fit the board's position from its drilled holes using the red light")
        form.addRow(row(self.centre, self.register))
        self.field_issue = QLabel()
        self.field_issue.setObjectName("issue")
        self.field_issue.setProperty("level", "error")
        self.field_issue.setWordWrap(True)
        form.addRow(self.field_issue)
        layout.addWidget(self.placement_box)

        # Board side check
        self.side_box = QWidget()
        self.side_box.setObjectName("section")
        side = QVBoxLayout(self.side_box)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(heading("Board side"))
        self.verified = QCheckBox("Side and alignment checked")
        self.verified.setToolTip("Required before arming a board side. Cleared when the board is moved.")
        self.verified.toggled.connect(self._verified_toggled)
        side.addWidget(self.verified)
        self.side_hint = hint()
        side.addWidget(self.side_hint)
        layout.addWidget(self.side_box)

        # Operation
        self.operation_box = QWidget()
        self.operation_box.setObjectName("section")
        form = QFormLayout(self.operation_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow(heading("Laser settings"))
        self.operation_pick = QComboBox()
        self.operation_pick.currentIndexChanged.connect(self._operation_picked)
        form.addRow("Operation", self.operation_pick)
        self.preset = QComboBox()
        self.preset.setToolTip("Choose a preset to load its values into this operation")
        self.preset.activated.connect(self._preset_chosen)
        form.addRow("Preset", self.preset)
        self.mode = QComboBox()
        self.mode.addItem("Lines (follow the paths)", VECTOR_ENGRAVE)
        self.mode.addItem("Fill (hatch closed shapes)", RASTER_ENGRAVE)
        self.mode.currentIndexChanged.connect(self._layer_edited)
        self.mode_label = QLabel("Mode")
        form.addRow(self.mode_label, self.mode)
        self.power = number(0, 0, 100, 1, "%", 1)
        self.speed = number(1000, 1, 15000, 0, "mm/s", 50)
        self.frequency = number(30, 1, 4000, 1, "kHz", 1)
        self.pulse = QComboBox()
        self.pulse.setToolTip("MOPA pulse width. Only the values this source accepts are listed.")
        self.passes = whole(1, 1, 999, "×")
        form.addRow("Power", self.power)
        form.addRow("Speed", self.speed)
        form.addRow("Frequency", self.frequency)
        form.addRow("Pulse width", self.pulse)
        form.addRow("Passes", self.passes)
        self.pitch = number(0.03, 0.001, 5, 3, "mm", 0.005)
        self.hatch_angle = number(0, -180, 180, 1, "°", 15)
        self.hatch_step = number(90, -180, 180, 1, "°", 15)
        self.hatch_step.setToolTip("Hatch direction turns by this much on each pass, which evens out the removal")
        self.cross = QCheckBox("Cross-hatch each pass")
        self.hatch_rows = []
        for label, widget in (("Hatch spacing", self.pitch), ("Hatch angle", self.hatch_angle),
                              ("Angle step / pass", self.hatch_step), ("", self.cross)):
            caption = QLabel(label)
            form.addRow(caption, widget)
            self.hatch_rows.append((caption, widget))
        for widget in (self.power, self.speed, self.frequency, self.passes, self.pitch, self.hatch_angle, self.hatch_step):
            widget.valueChanged.connect(self._layer_edited)
        self.pulse.currentIndexChanged.connect(self._layer_edited)
        self.cross.toggled.connect(self._layer_edited)
        self.issues = QLabel()
        self.issues.setObjectName("issue")
        self.issues.setWordWrap(True)
        form.addRow(self.issues)
        self.save_preset = button("Save as preset…", lambda: self.save_preset_requested.emit(self.operation))
        form.addRow(self.save_preset)
        layout.addWidget(self.operation_box)

        self.estimate = hint()
        layout.addWidget(self.estimate)
        layout.addStretch(1)

        controller.selection_changed.connect(self._selection_changed)
        controller.job_changed.connect(self.reload)
        controller.placement_changed.connect(self.reload_placement)
        controller.profile_changed.connect(self.reload)
        controller.presets_changed.connect(self.reload)
        controller.plan_changed.connect(self.reload_estimate)
        controller.state_changed.connect(self.reload_state)
        self.reload()

    # ---- loading

    def _selection_changed(self):
        self.operation = 0
        self.reload()

    def select_operation(self, index: int):
        self.operation = index
        self.reload()

    def reload(self):
        job = self.controller.job
        self._loading = True
        self.empty.setVisible(job is None)
        for box in (self.placement_box, self.operation_box):
            box.setVisible(job is not None)
        self.side_box.setVisible(bool(job and job.side))
        self.operation_box.setVisible(bool(job and job.objects))
        if job is not None and job.objects:
            self.operation = max(0, min(self.operation, len(job.objects) - 1))
            artwork = job.objects[self.operation]
            layer = artwork.layer
            self.operation_pick.clear()
            self.operation_pick.addItems([obj.name for obj in job.objects])
            self.operation_pick.setCurrentIndex(self.operation)
            self.operation_pick.setVisible(len(job.objects) > 1)
            self.preset.clear()
            self.preset.addItem("Choose a preset…", "")
            for preset in self.controller.presets.for_kind(artwork.kind):
                self.preset.addItem(preset.label(), preset.preset_id)
                self.preset.setItemData(self.preset.count() - 1, f"{preset.summary()}\n{preset.note}".strip(),
                                        Qt.ItemDataRole.ToolTipRole)
            # A PCB operation's mode is fixed by what it does; free artwork can be lines or fill.
            show_mode = self._free_mode = not artwork.kind
            self.mode.setVisible(show_mode)
            self.mode_label.setVisible(show_mode)
            self.mode.setCurrentIndex(1 if layer.operation == RASTER_ENGRAVE else 0)
            self.power.setValue(layer.power_percent)
            self.speed.setValue(layer.speed_mm_s)
            self.frequency.setValue(layer.frequency_khz)
            self.passes.setValue(layer.passes)
            widths = list(self.controller.profile.pulse_widths) or [2, 4, 6, 9, 13, 20, 30, 45, 60, 80, 100, 150, 200, 250, 350, 500]
            if int(layer.pulse_width_ns) not in widths:
                widths = sorted({*widths, int(layer.pulse_width_ns)})
            self.pulse.clear()
            for width in widths:
                self.pulse.addItem(f"{width} ns", width)
            self.pulse.setCurrentIndex(widths.index(int(layer.pulse_width_ns)))
            self.pitch.setValue(layer.raster_pitch_mm)
            self.hatch_angle.setValue(layer.raster_angle_deg)
            self.hatch_step.setValue(layer.raster_angle_step_deg)
            self.cross.setChecked(layer.crosshatch)
            raster = layer.operation == RASTER_ENGRAVE
            for caption, widget in self.hatch_rows:
                caption.setVisible(raster)
                widget.setVisible(raster)
            self._show_issues(layer)
            board = bool(job.side)
            self.scale.setEnabled(not board)
            self.mirror.setEnabled(not board)
            self.register.setVisible(board and len(job.references) >= 2)
            self.register.setText("Register on holes…" if job.holes else "Register on corners…")
            self.side_hint.setText(
                "Bottom side: flip the board, then register it on its holes." if job.side == "bottom" else
                "Top side: place the board, register it on its holes, then tick the box.")
        self._loading = False
        self.reload_placement()
        self.reload_estimate()
        self.reload_state()

    def reload_placement(self):
        job = self.controller.job
        if job is None:
            return
        self._loading = True
        p = job.placement
        self.x.setValue(p.x)
        self.y.setValue(p.y)
        self.angle.setValue(p.angle)
        self.scale.setValue(p.scale * 100)
        self.mirror.setChecked(p.mirror)
        self._loading = False
        profile = self.controller.profile
        box = job.bounds()
        outside = box and (box[0] < -profile.width / 2 - 1e-6 or box[2] > profile.width / 2 + 1e-6
                           or box[1] < -profile.height / 2 - 1e-6 or box[3] > profile.height / 2 + 1e-6)
        self.field_issue.setVisible(bool(outside))
        if outside:
            self.field_issue.setText(f"Part of this job is outside the {profile.width:g} × {profile.height:g} mm "
                                     "field. Move it inside before framing or marking.")
        self.reload_state()

    def reload_estimate(self):
        plan = self.controller.plan
        if plan is None:
            self.estimate.setText("Preview the toolpaths to see the time estimate.")
            return
        lines = [f"Estimated time: {format_duration(plan.seconds)}  ({plan.paths:,} paths)"]
        lines += [f"  {name}: {format_duration(seconds)}" for name, seconds in plan.operations]
        self.estimate.setText("\n".join(lines))

    def reload_state(self):
        job = self.controller.job
        c = self.controller
        if c.framing and not c.target:
            self.frame_hint.setText("The red light follows every move. Walk it onto the board, then tick "
                                    "“Side and alignment checked”.")
        elif job is not None and not c.blockers()["frame"]:
            self.frame_hint.setText("Start Frame to see the design on the board while you move it.")
        else:
            self.frame_hint.setText("")
        locked = bool(self.controller.blockers()["edit"])
        for box in (self.placement_box, self.operation_box):
            box.setEnabled(not locked)
        self._loading = True
        self.verified.setChecked(self.controller.is_verified())
        self.verified.setEnabled(bool(job) and not self.controller.marking and job.status == "ready")
        self._loading = False

    def _show_issues(self, layer):
        issues = layer_issues(layer, self.controller.profile) if layer.output else []
        level = "error" if any(severity == "error" for severity, _ in issues) else "warning"
        self.issues.setProperty("level", level)
        self.issues.setText("\n".join(("⚠ " if severity == "warning" else "✖ ") + text for severity, text in issues))
        self.issues.setVisible(bool(issues))
        self.issues.style().unpolish(self.issues)
        self.issues.style().polish(self.issues)

    # ---- edits

    def _set_xy(self, x, y):
        job = self.controller.job
        if job:
            self.controller.set_placement(replace(job.placement, x=x, y=y), merge="")

    def _jog(self, dx, dy, turn):
        step = self.jog_step.currentData()
        self.controller.nudge(dx * step, dy * step, turn * step)

    def _placement_edited(self):
        job = self.controller.job
        if self._loading or job is None:
            return
        self.controller.set_placement(replace(
            job.placement, x=self.x.value(), y=self.y.value(), angle=self.angle.value(),
            scale=self.scale.value() / 100, mirror=self.mirror.isChecked()), merge="type")
        self.reload_placement()   # snap back if the edit was refused

    def _verified_toggled(self, checked):
        if not self._loading:
            self.controller.set_verified(checked)

    def _operation_picked(self, index):
        if not self._loading and index >= 0:
            self.operation = index
            self.reload()

    def _layer_edited(self):
        job = self.controller.job
        if self._loading or job is None or self.pulse.currentData() is None:
            return
        layer = job.objects[self.operation].layer
        self.controller.set_layer(self.operation, replace(
            layer, operation=self.mode.currentData() if self._free_mode else layer.operation,
            power_percent=self.power.value(), speed_mm_s=self.speed.value(), frequency_khz=self.frequency.value(),
            pulse_width_ns=float(self.pulse.currentData()), passes=self.passes.value(),
            raster_pitch_mm=self.pitch.value(), raster_angle_deg=self.hatch_angle.value(),
            raster_angle_step_deg=self.hatch_step.value(), crosshatch=self.cross.isChecked(),
        ), merge=f"layer{self.operation}")

    def _preset_chosen(self, index):
        job = self.controller.job
        preset = self.controller.presets.get(self.preset.itemData(index) or "")
        if job is None or preset is None:
            return
        current = job.objects[self.operation].layer
        # Keep the operation's identity and, for PCB operations, its mode and enable tick.
        layer = replace(preset.layer, layer_id=current.layer_id, name=current.name, output=current.output,
                        operation=current.operation if job.objects[self.operation].kind else preset.layer.operation)
        self.controller.set_layer(self.operation, layer)
        self.controller.say(f"Loaded preset “{preset.name}” into {job.objects[self.operation].name}.")


class LogPanel(QWidget):
    """Progress and a running log, so messages are not lost when the next one arrives."""

    def __init__(self, controller):
        super().__init__()
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(4)
        self.status = QLabel("Ready")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(8)
        self.remaining = QLabel("")
        self.remaining.setObjectName("muted")
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.remaining)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.log, 1)
        controller.message.connect(self.add)
        controller.progress.connect(self.show_progress)

    def add(self, text: str, level: str = "info"):
        self.status.setText(text.splitlines()[0] if text else "")
        token = {"error": "danger", "warning": "warning", "success": "success"}.get(level, "text")
        stamp = datetime.now().strftime("%H:%M:%S")
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        self.log.appendHtml(f'<span style="color:{theme.tokens()["faint"]}">{stamp}</span>&nbsp; '
                            f'<span style="color:{theme.tokens()[token]}">{safe}</span>')

    def show_progress(self, done: float, total: float, label: str):
        if total <= 0:
            self.bar.setValue(0)
            self.remaining.setText("")
            return
        self.bar.setValue(int(1000 * min(1.0, done / total)))
        left = total - done
        self.remaining.setText(f"{label} · about {format_duration(left)} left" if left > 0.5 and label else "")
