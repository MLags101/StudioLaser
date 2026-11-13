"""Modeless coupon tuning so the normal workspace owns every laser run."""

from dataclasses import replace

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)

from ..job_planner import layer_issues
from ..layers import HATCH_PATTERNS, RASTER_ENGRAVE
from ..presets import Preset, layer_summary, starting_layer
from ..tuning import (
    FIELDS, FREQUENCY_KHZ, KINDS, RANGES, Feedback, coupon_job, suggest, trial_settings,
)
from ..workspace import COPPER_FILL, MASK, SILK
from .panels import button, hint, number, row, whole


class TuningDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tune a PCB preset")
        self.resize(565, 625)
        self.controller = controller
        self.layer = None
        self.job_id = ""
        self.coupon_objects = ()
        self.profile = None
        self.device_choice = ""
        self.tried = []
        self.current_advice = None
        self._baseline = None

        shell = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        shell.addWidget(scroll)
        layout.addWidget(hint("Run one coupon on matching scrap, inspect it, then review one change for the next test. "
                              "Frame, confirm alignment, Arm and Start in the workspace. Frequency stays at 37 kHz."))
        form = QFormLayout()
        self.kind = QComboBox()
        for key, text in KINDS.items():
            self.kind.addItem(text, key)
        form.addRow("Operation", self.kind)
        self.base = QComboBox()
        form.addRow("Start from", self.base)
        self.pattern = QComboBox()
        for key, text in HATCH_PATTERNS.items():
            self.pattern.addItem(text, key)
        form.addRow("Pattern", self.pattern)
        self.stock = QLineEdit()
        self.stock.setPlaceholderText("For example, the laminate or mask used on this board")
        form.addRow("Stock", self.stock)
        self.copper = number(35, 1, 1000, 1, " µm", 1)
        form.addRow("Copper thickness", self.copper)
        self.channel = number(0.25, 0.05, 1, 3, " mm", 0.05)
        form.addRow("Coupon channel width", self.channel)
        layout.addLayout(form)
        self.baseline = hint()
        layout.addWidget(self.baseline)

        self.limits = QGroupBox("Allowed adjustments")
        limits = QFormLayout(self.limits)
        self.bounds = {}
        for key, label in FIELDS.items():
            lo, hi = RANGES[key]
            if key == "passes":
                low, high = whole(lo, lo, hi), whole(hi, lo, hi)
            else:
                decimals = {"speed_mm_s": 0, "power_percent": 1, "raster_pitch_mm": 3}[key]
                low, high = number(lo, lo, hi, decimals), number(hi, lo, hi, decimals)
            values = QHBoxLayout()
            values.addWidget(low)
            values.addWidget(high)
            limits.addRow(f"{label} min / max", values)
            self.bounds[key] = (low, high)
            low.valueChanged.connect(self._invalidate_advice)
            high.valueChanged.connect(self._invalidate_advice)
        layout.addWidget(self.limits)

        self.inspection = QGroupBox("Inspect the completed coupon")
        inspection = QFormLayout(self.inspection)
        self.result = QComboBox()
        inspection.addRow("Result", self.result)
        self.damage = QComboBox()
        for value, label in enumerate(("No visible damage", "Slight discoloration", "Clear marks",
                                       "Heavy damage", "Severe damage"), 1):
            self.damage.addItem(f"{value} · {label}", value)
        inspection.addRow("Surface damage", self.damage)
        self.isolation = QComboBox()
        for value, label in (("unknown", "Not checked"), ("isolated", "Isolated on meter"), ("short", "Still connected")):
            self.isolation.addItem(label, value)
        inspection.addRow("Electrical isolation", self.isolation)
        self.isolation_label = inspection.labelForField(self.isolation)
        self.depth = whole(-1, -1, 100, " %")
        self.depth.setSpecialValueText("Unknown")
        inspection.addRow("Copper depth removed", self.depth)
        self.depth_label = inspection.labelForField(self.depth)
        self.pitting = QComboBox()
        self.pitting.addItem("Not seen", False)
        self.pitting.addItem("Pitting visible", True)
        inspection.addRow("Exposed copper", self.pitting)
        self.pitting_label = inspection.labelForField(self.pitting)
        self.evaluate = button("Review result", self.review_result)
        inspection.addRow(self.evaluate)
        layout.addWidget(self.inspection)

        self.status = hint()
        layout.addWidget(self.status)
        self.add = button("Add coupon job", self.add_coupon, primary=True)
        self.apply = button("Accept change", self.accept_change)
        self.save = button("Save as new preset", self.save_preset)
        shell.addLayout(row(self.add, self.apply, self.save, button("Close", self.close)))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.base.currentIndexChanged.connect(self._baseline_changed)
        self.pattern.currentIndexChanged.connect(self._baseline_changed)
        self.channel.valueChanged.connect(self._baseline_changed)
        for widget in (self.result, self.damage, self.isolation, self.pitting):
            widget.currentIndexChanged.connect(self._invalidate_advice)
        self.depth.valueChanged.connect(self._invalidate_advice)
        controller.state_changed.connect(self.refresh)
        self._kind_changed()

    def _kind_changed(self):
        kind = self.kind.currentData()
        self.base.blockSignals(True)
        self.base.clear()
        library = self.controller.presets
        legacy = {"PCBEn (from EZCAD)": COPPER_FILL, "SMR (from EZCAD)": MASK}
        choices = [p for p in library.all()
                   if (p.kind == kind or legacy.get(p.name) == kind) and p.layer.pattern != "radial"]
        choices.sort(key=lambda p: (0 if p.default and not p.builtin else
                                    1 if "(from EZCAD)" in p.name else 3 if p.builtin else 2))
        for preset in choices:
            self.base.addItem(preset.label(), preset.preset_id)
        self.base.blockSignals(False)
        self.result.clear()
        labels = {
            COPPER_FILL: ("Channel clear", "Copper remains in stripes or bands", "Copper remains across the channel"),
            SILK: ("Legible with good contrast", "Gaps or striping in the marks", "Uniformly faint or hard to read"),
            MASK: ("Opening clear of coating", "Coating remains in stripes or bands", "Coating remains across the opening"),
        }
        self.result.addItem("Uncertain", "unknown")
        for label, key in zip(labels[kind], ("clear", "striped", "uniform")):
            self.result.addItem(label, key)
        for widget in (self.isolation, self.isolation_label, self.depth, self.depth_label):
            widget.setVisible(kind == COPPER_FILL)
        for widget in (self.pitting, self.pitting_label):
            widget.setVisible(kind == MASK)
        self._baseline_changed(reset_pattern=True)

    def _baseline_changed(self, *_args, reset_pattern=False):
        preset = self.controller.presets.get(self.base.currentData() or "")
        layer = preset.layer if preset else starting_layer(self.kind.currentData())
        if reset_pattern or (preset and preset.preset_id != self._baseline):
            self.pattern.blockSignals(True)
            self.pattern.setCurrentIndex(max(0, self.pattern.findData(layer.pattern)))
            self.pattern.blockSignals(False)
        self._baseline = preset.preset_id if preset else None
        self.layer = replace(layer, frequency_khz=FREQUENCY_KHZ, operation=RASTER_ENGRAVE, output=True,
                             hatch_pattern=self.pattern.currentData(), crosshatch=False)
        self.job_id = ""
        self.profile = self.controller.profile
        self.device_choice = self.controller.device_choice
        self.tried.clear()
        self.current_advice = None
        for key, (low, high) in self.bounds.items():
            value = getattr(self.layer, key)
            minimum, maximum = RANGES[key]
            lower, upper = ((max(minimum, value - 1), min(maximum, value + 3)) if key == "passes"
                            else (max(minimum, value * 0.7), min(maximum, value * 1.3)))
            low.setValue(lower)
            high.setValue(upper)
        notes = []
        if preset and "(from EZCAD)" in preset.name:
            notes.append("StudioLaser supplies hatch settings because EZCAD pens store no hatch spacing.")
        if layer.frequency_khz != FREQUENCY_KHZ:
            notes.append(f"This test uses 37 kHz in place of the preset’s {layer.frequency_khz:g} kHz.")
        notes.extend(text for level, text in layer_issues(self.layer, self.controller.profile) if level == "warning")
        self.baseline.setText("\n".join([layer_summary(self.layer), *notes]))
        self.status.setText("Place fresh matching scrap and add the coupon job.")
        self.refresh()

    def _invalidate_advice(self, *_args):
        self.current_advice = None
        self.refresh()

    def _limits(self):
        return {key: (lo.value(), hi.value()) for key, (lo, hi) in self.bounds.items()}

    def _trial(self):
        return next((job for job in self.controller.jobs if job.job_id == self.job_id), None)

    def _completed(self):
        job = self._trial()
        return bool(job and job.status == "complete" and job.objects == self.coupon_objects and self._same_machine())

    def _same_machine(self):
        return self.profile == self.controller.profile and self.device_choice == self.controller.device_choice

    def refresh(self):
        if self.layer is None:
            return
        job = self._trial()
        pending = bool(job and job.status in {"ready", "running"})
        complete = self._completed()
        busy = self.controller.busy
        self.evaluate.setEnabled(complete and not busy)
        self.inspection.setEnabled(complete and not busy)
        self.apply.setEnabled(bool(complete and self.current_advice and self.current_advice.field and not busy))
        self.save.setEnabled(bool(complete and self.current_advice and self.current_advice.accepted and not busy))
        self.add.setEnabled(not busy and not pending and self._same_machine()
                            and (not complete or bool(self.current_advice and self.current_advice.accepted)))
        for widget in (self.kind, self.base, self.pattern, self.channel):
            widget.setEnabled(not pending and not busy)
        if job and job.status == "interrupted":
            self.status.setText("The coupon was interrupted. Use a fresh patch for a new test.")
        elif job and job.status == "complete" and not complete:
            self.status.setText("The coupon settings or machine profile changed. Choose the starting preset again.")
        elif pending:
            self.status.setText("In the workspace, frame the coupon, confirm alignment, then Arm and Start.")
        elif complete and self.current_advice is None:
            self.status.setText("Inspect the completed coupon and review the result.")
        elif not self._same_machine():
            self.status.setText("The machine changed. Choose the starting preset again.")
        self.add.setText("Add repeat coupon" if complete else "Add coupon job")

    def add_coupon(self):
        if not self.add.isEnabled():
            return
        limits = self._limits()
        if any(not lo <= getattr(self.layer, key) <= hi for key, (lo, hi) in limits.items()):
            self.status.setText("Set adjustment bounds that include the current settings.")
            return
        errors = [text for level, text in layer_issues(self.layer, self.controller.profile) if level == "error"]
        if errors:
            self.status.setText(errors[0])
            return
        try:
            job = coupon_job(self.kind.currentData(), self.layer, channel_width=self.channel.value())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.layer = job.objects[0].layer
        self.coupon_objects = job.objects
        self.job_id = job.job_id
        self.current_advice = None
        self.result.setCurrentIndex(0)
        self.damage.setCurrentIndex(0)
        self.isolation.setCurrentIndex(0)
        self.depth.setValue(-1)
        self.pitting.setCurrentIndex(0)
        self.controller.add_jobs([job])
        if self.parent():
            self.parent().activateWindow()
        self.refresh()

    def feedback(self):
        return Feedback(self.result.currentData(), self.damage.currentData(), self.isolation.currentData(),
                        None if self.depth.value() < 0 else self.depth.value(),
                        self.kind.currentData() == MASK and self.pitting.currentData())

    def review_result(self):
        if not self._completed():
            return
        settings = trial_settings(self.layer)
        if settings not in self.tried:
            self.tried.append(settings)
        self.current_advice = None
        advice = suggest(self.kind.currentData(), self.layer, self.feedback(), self._limits(), self.tried)
        if advice.field:
            candidate = replace(self.layer, **{advice.field: advice.value})
            error = next((text for level, text in layer_issues(candidate, self.controller.profile)
                          if level == "error"), "")
            if error:
                self.refresh()
                self.status.setText(error)
                return
        self.current_advice = advice
        change = (f" {FIELDS[advice.field]} {getattr(self.layer, advice.field):g} → {advice.value:g}."
                  if advice.field else "")
        self.status.setText(advice.reason + change)
        self.refresh()

    def accept_change(self):
        if not self.apply.isEnabled():
            return
        advice = self.current_advice
        self.layer = replace(self.layer, **{advice.field: advice.value})
        self.job_id = ""
        self.current_advice = None
        self.baseline.setText(layer_summary(self.layer))
        self.status.setText("Settings updated. Move to fresh scrap before adding the next coupon.")
        self.refresh()

    def save_preset(self):
        if not self.save.isEnabled():
            return
        stock = self.stock.text().strip()
        if not stock:
            self.status.setText("Enter the stock description before saving the preset.")
            return
        from .dialogs import SavePresetDialog

        kind = self.kind.currentData()
        feedback = self.feedback()
        profile = self.controller.profile
        note = (f"Coupon result: {feedback.result}, damage {feedback.damage}/5. "
                f"{'Isolation verified on meter. ' if kind == COPPER_FILL else ''}"
                f"{'Experimental annular coupon only. ' if self.layer.pattern == 'radial' else ''}")
        preset = Preset("", f"{KINDS[kind]} — {stock}", kind, self.layer, profile.name, profile.lens,
                        profile.source, stock, self.copper.value() if kind == COPPER_FILL else 0, note=note)
        dialog = SavePresetDialog(self.layer, kind, self.controller, self, preset)
        dialog.setWindowTitle("Save as new preset")
        if self.layer.pattern == "radial":
            dialog.default.setEnabled(False)
        dialog.kind.setEnabled(False)
        if not dialog.exec():
            return
        try:
            result = dialog.result_preset()
            if not result.stock or (kind == COPPER_FILL and result.copper_um <= 0):
                raise ValueError("Enter the stock and, for isolation, the copper thickness.")
            self.controller.presets.add(result)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self.controller.presets_changed.emit()
        self.status.setText("Saved as a new preset.")
