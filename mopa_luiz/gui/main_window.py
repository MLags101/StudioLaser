"""Main window: toolbar, docks, menus and shortcuts around the controller."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut, QWindow
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

from ..patterns import test_square
from ..presets import starting_layer
from ..workspace import COPPER_FILL
from . import theme
from .canvas import Canvas
from .controller import ARTWORK_SUFFIXES, FRAME_MODES, LINK_TEXT, AppController
from .dialogs import (
    MaterialTestDialog, PcbImportDialog, PresetManagerDialog, RegistrationDialog, SavePresetDialog, focus_job,
    kerf_ladder_job,
)
from .machine import CalibrationDialog, MachineSetupDialog
from .panels import Inspector, JobsPanel, LogPanel, button

GUIDE = """<h3>Two-sided PCB, start to finish</h3>
<ol>
<li><b>Machine Setup</b> (once): load <i>markcfg7</i> and the lens <i>.cor</i> file, check the source and lens, enable the profile.</li>
<li><b>Calibrate Lens</b> (once per lens): mark a grid on card, measure it, apply. Then align the red light.</li>
<li><b>Material test</b> (once per laminate): run a test matrix and the kerf ladder on scrap, save the best patch as a preset and make it the default.</li>
<li><b>Import PCB</b>: copper, mask, silkscreen, outline and drill files, exported 1:1 and unmirrored.</li>
<li><b>Top side</b>: place the board, <b>Frame</b> and move it until the red outline and hole rings sit on the board, or <b>Register</b> on
holes or on the board edge, frame to check, tick <i>Side and alignment checked</i>, Preview, Arm, Start.</li>
<li><b>Flip</b> the board, select the bottom side, register it on the same holes, and run it the same way.</li>
</ol>
<p><b>Stop</b> (or Esc) is always available. Wear eyewear rated for 1064 nm and run the fume extraction.</p>"""


class EscapeStops(QObject):
    """Esc stops the laser from anywhere, including inside modal dialogs; the key still reaches them too."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
                and not event.isAutoRepeat() and isinstance(obj, QWindow)
                and (self.controller.busy or self.controller.arming.active)):
            self.controller.stop()
        return False


class MainWindow(QMainWindow):
    def __init__(self, state_dir: Path | None = None, controller: AppController | None = None):
        super().__init__()
        self.setWindowTitle("StudioLaser")
        self.resize(1320, 840)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)
        self.setDockNestingEnabled(True)
        self.controller = controller or AppController(state_dir)
        self.settings = QSettings("StudioLaser", "Desktop")
        if not self.settings.allKeys():
            # First start after the rename: keep window layout, theme, folders and laser choice.
            former = QSettings("OpenLaser", "Desktop")
            for key in former.allKeys():
                self.settings.setValue(key, former.value(key))
        self.registration = None
        self.calibration = None
        self._closing = False
        c = self.controller

        self.canvas = Canvas(c)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(12, 8, 12, 8)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        banner.addWidget(self.banner_text, 1)
        banner.addWidget(button("I checked — the machine is idle", c.acknowledge))
        centre = QWidget()
        box = QVBoxLayout(centre)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self.banner)
        box.addWidget(self.canvas, 1)
        self.setCentralWidget(centre)

        self.jobs = JobsPanel(c)
        self.inspector = Inspector(c)
        self.log = LogPanel(c)
        self.docks = {}
        for name, title, widget, area, width in (
            ("jobs", "Jobs", self.jobs, Qt.DockWidgetArea.LeftDockWidgetArea, 330),
            ("inspector", "Settings", self.inspector, Qt.DockWidgetArea.RightDockWidgetArea, 330),
            ("log", "Activity", self.log, Qt.DockWidgetArea.BottomDockWidgetArea, 0),
        ):
            dock = QDockWidget(title, self)
            dock.setObjectName(name)
            dock.setWidget(widget)
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
            if width:
                widget.setMinimumWidth(width - 40)
            self.addDockWidget(area, dock)
            self.docks[name] = dock
        self.resizeDocks([self.docks["jobs"], self.docks["inspector"]], [330, 340], Qt.Orientation.Horizontal)
        self.resizeDocks([self.docks["log"]], [150], Qt.Orientation.Vertical)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.cursor = QLabel("")
        self.machine = QLabel("")
        self.statusBar().addWidget(self.machine, 1)
        self.statusBar().addPermanentWidget(self.cursor)

        self.jobs.import_board.connect(self.pick_board)
        self.jobs.import_artwork.connect(self.pick_artwork)
        self.jobs.operation_selected.connect(self.inspector.select_operation)
        self.inspector.register_requested.connect(self.open_registration)
        self.inspector.save_preset_requested.connect(self.save_preset)
        self.canvas.cursor_moved.connect(lambda x, y: self.cursor.setText(f"X {x:8.2f}   Y {y:8.2f} mm"))
        c.state_changed.connect(self.refresh)
        c.profile_changed.connect(self.refresh)
        c.size_required.connect(self.ask_size)
        # Stop must work from anywhere in the application, including tool windows.
        stop = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        stop.setContext(Qt.ShortcutContext.ApplicationShortcut)
        stop.activated.connect(c.stop)
        # A shortcut does not fire while a modal dialog is open, so Esc is also caught before any widget sees it.
        self.esc_stop = EscapeStops(c)
        QApplication.instance().installEventFilter(self.esc_stop)

        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
            self.restoreState(self.settings.value("state", b""))
        c.device_choice = str(self.settings.value("usb_device", "") or "")
        c.load_state()
        self.refresh()
        QTimer.singleShot(0, self.canvas.fit)
        if not c.profile.enabled:
            c.say("Welcome. Start with Machine → Machine Setup to load your laser's factory files.")

    # ---- construction

    def _action(self, text, slot, shortcut=None, icon=None, tip=""):
        action = QAction(text, self)
        if icon:
            action.setIcon(theme.icon(icon))
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
        action.setProperty("tip", tip or text)
        action.triggered.connect(lambda _=False: slot())
        return action

    def _build_actions(self):
        c = self.controller
        a = self._action
        self.act = {
            "board": a("Import PCB…", self.pick_board, "Ctrl+Shift+I", "board", "Import Gerber and drill files for a board"),
            "artwork": a("Import Artwork…", self.pick_artwork, "Ctrl+I", "import", "Import SVG, DXF or STL artwork"),
            "open": a("Open Project…", self.open_project, "Ctrl+O", "open"),
            "save": a("Save Project", self.save_project, "Ctrl+S", "save"),
            "save_as": a("Save Project As…", lambda: self.save_project(True), "Ctrl+Shift+S"),
            "quit": a("Quit", self.close, "Ctrl+Q"),
            "undo": a("Undo", c.undo, "Ctrl+Z", "undo"),
            "redo": a("Redo", c.redo, "Ctrl+Y", "redo"),
            "remove": a("Remove Job", c.remove_job, "Del"),
            "fit": a("Fit View", self.fit, "F", "fit", "Zoom to the job and field (F)"),
            "zoom_in": a("Zoom In", lambda: self.canvas.zoom(1.25), "Ctrl+="),
            "zoom_out": a("Zoom Out", lambda: self.canvas.zoom(0.8), "Ctrl+-"),
            "preview": a("Preview", c.preview, "Ctrl+P", "preview", "Plan the toolpaths and estimate the time"),
            "frame_once": a("Frame Once", lambda: c.frame(False), "Ctrl+F", None, "Trace the design once with the red light"),
            "frame": a("Frame", self.toggle_frame, "Ctrl+Shift+F", "frame",
                       "Trace the design with the red light until stopped. Move the design while it runs: "
                       "the red light follows."),
            "arm": a("Arm", c.arm, "Ctrl+R", "arm", "Validate the job and allow Start for 30 seconds"),
            "start": a("Start", c.start, "Ctrl+Return", "play", "Fire the laser and run the armed job"),
            "stop": a("STOP", c.stop, None, "stop", "Stop the laser now (Esc)"),
            "setup": a("Machine Setup…", self.open_setup, None, "gear"),
            "calibrate": a("Calibrate Lens…", self.open_calibration, None, "target"),
            "acknowledge": a("Confirm Machine Is Idle", c.acknowledge),
            "presets": a("Laser Presets…", lambda: PresetManagerDialog(c, self).exec()),
            "matrix": a("Material Test Matrix…", self.add_matrix),
            "kerf": a("Kerf and Isolation Ladder", lambda: self.add_pattern(kerf_ladder_job(c))),
            "focus": a("Focus Test…", self.add_focus),
            "square_outline": a("Outline Test Square…", lambda: self.add_square(False)),
            "square_filled": a("Filled Test Square…", lambda: self.add_square(True)),
            "guide": a("Workflow Guide", lambda: QMessageBox.information(self, "Workflow guide", GUIDE), "F1"),
        }
        self.act["frame"].setCheckable(True)
        self.act["arm"].setCheckable(True)

    def _build_menus(self):
        bar = self.menuBar()
        act = self.act
        # What the red light traces; shared by the Job menu and the Frame button's drop-down.
        self.frame_menu = QMenu("Frame Shows", self)
        group = QActionGroup(self)
        saved = self.settings.value("frame_mode", "holes") or "holes"
        self.controller.set_frame_mode(saved if saved in FRAME_MODES else "holes")
        self.frame_actions = {}
        for key, label in FRAME_MODES.items():
            action = QAction(label, self, checkable=True)
            action.setChecked(key == self.controller.frame_mode)
            action.triggered.connect(lambda _=False, key=key: self.set_frame_mode(key))
            group.addAction(action)
            self.frame_menu.addAction(action)
            self.frame_actions[key] = action
        for title, names in (
            ("&File", ("board", "artwork", None, "open", "save", "save_as", None, "quit")),
            ("&Edit", ("undo", "redo", None, "remove")),
            ("&Job", ("preview", None, "frame_once", "frame", None, "arm", "start", "stop")),
            ("&Machine", ("setup", "calibrate", None, "acknowledge")),
            ("&Tools", ("presets", None, "matrix", "kerf", "focus", None, "square_outline", "square_filled")),
        ):
            menu = bar.addMenu(title)
            for name in names:
                menu.addSeparator() if name is None else menu.addAction(act[name])
            if title == "&Job":
                menu.insertMenu(act["arm"], self.frame_menu)
                menu.insertSeparator(act["arm"])
        view = bar.addMenu("&View")
        for name in ("fit", "zoom_in", "zoom_out"):
            view.addAction(act[name])
        view.addSeparator()
        group = QActionGroup(self)
        for key, label in (("", "Theme: follow system"), ("dark", "Theme: dark"), ("light", "Theme: light")):
            action = QAction(label, self, checkable=True)
            action.setChecked((self.settings.value("theme", "") or "") == key)
            action.triggered.connect(lambda _=False, key=key: self.set_theme(key))
            group.addAction(action)
            view.addAction(action)
        bar.addMenu("&Help").addAction(act["guide"])

    def _build_toolbar(self):
        bar = self.addToolBar("Main")
        bar.setObjectName("main")
        bar.setMovable(False)
        bar.setIconSize(QSize(20, 20))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.buttons = {}
        for name in ("undo", "redo", "fit", None, "preview", "frame", None, "arm", "start"):
            if name is None:
                bar.addSeparator()
                continue
            bar.addAction(self.act[name])
            widget = bar.widgetForAction(self.act[name])
            widget.setObjectName(name)
            self.buttons[name] = widget
            if name in ("undo", "redo", "fit"):
                widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            if name == "frame":
                widget.setMenu(self.frame_menu)
                widget.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # Status and Stop live in the menu bar's corner, which never collapses into an overflow menu.
        corner = self.corner = QWidget(self)
        corner.setObjectName("corner")
        layout = QHBoxLayout(corner)
        layout.setContentsMargins(0, 3, 8, 3)
        layout.setSpacing(10)
        # The connection chip is also the laser picker: click it to choose which USB laser to use.
        self.chip = QToolButton()
        self.chip.setObjectName("chip")
        self.chip.setText("Laser offline")
        self.chip.setMinimumWidth(170)
        self.chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.device_menu = QMenu(self.chip)
        self.device_menu.aboutToShow.connect(self.fill_device_menu)
        self.chip.setMenu(self.device_menu)
        layout.addWidget(self.chip)
        self.stop_button = QToolButton()
        self.stop_button.setObjectName("stop")
        self.stop_button.setDefaultAction(self.act["stop"])
        self.stop_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.act["stop"].setIcon(theme.icon("stop", "#ffffff"))
        layout.addWidget(self.stop_button)
        self.menuBar().setCornerWidget(corner, Qt.Corner.TopRightCorner)

    # ---- state → widgets

    def refresh(self):
        c = self.controller
        why = c.blockers()
        act = self.act

        def gate(name, reason, allowed_text=None):
            act[name].setEnabled(not reason)
            base = act[name].property("tip")
            act[name].setToolTip(f"{base}\n\nUnavailable: {reason}" if reason else base)

        gate("preview", why["preview"])
        gate("frame_once", why["frame"] or ("Already framing" if c.framing else ""))
        gate("frame", "" if c.worker.action == "frame-loop" else why["frame"])
        gate("arm", "" if c.arming.active else why["arm"])
        gate("start", why["start"])
        busy = "Stop the laser first: Stop must stay reachable" if c.busy else ""
        for name in ("presets", "save", "save_as"):
            gate(name, why["edit"] or busy)
        for name in ("board", "artwork", "open", "remove", "matrix", "kerf", "focus", "square_outline",
                     "square_filled", "setup"):
            gate(name, why["edit"])
        act["undo"].setEnabled(bool(c.undo_stack) and not why["edit"])
        act["redo"].setEnabled(bool(c.redo_stack) and not why["edit"])
        act["acknowledge"].setEnabled(c.state_unknown and not c.busy)
        act["frame"].setChecked(c.worker.action == "frame-loop")
        act["frame"].setText("Stop Frame" if c.worker.action == "frame-loop" else "Frame")
        act["arm"].setChecked(c.arming.active)
        act["arm"].setText(f"Armed {c.arming.remaining:.0f} s" if c.arming.active else "Arm")
        # Stop is never disabled: pressing it when idle is harmless and it must never be out of reach.
        self.chip.setText(LINK_TEXT[c.link] if c.profile.enabled or c.link != "connected" else "Laser found — set up machine")
        state = "ok" if c.online and c.profile.enabled else "warn" if c.link in {"driver", "multiple"} or c.online else ""
        if c.marking:
            self.chip.setText("Engraving")
            state = "bad"
        self.chip.setProperty("state", state)
        self.chip.setText(self.chip.text() + "  ▾")
        self.chip.setToolTip(f"{c.link_note}\nClick to choose which laser to use.")
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)
        self.banner.setVisible(c.state_unknown)
        self.banner_text.setText("The last operation ended without confirming the laser stopped. Use the machine's "
                                 "own stop if needed, check that it is idle, then confirm here to continue.")
        profile = c.profile
        self.machine.setText(
            f"{profile.name} · {profile.lens or 'no lens set'} · field {profile.width:g} × {profile.height:g} mm"
            + (f" · calibrated {profile.calibration_note.split(',')[0]}" if profile.calibration else " · lens not calibrated")
            if profile.enabled else "Machine not set up — framing and marking are disabled")
        self.setWindowTitle(("StudioLaser — " + c.project_path.name) if c.project_path else "StudioLaser")
        if self._closing and not c.busy:
            self.close()

    def fill_device_menu(self):
        from ..cli import usb_label

        c = self.controller
        menu = self.device_menu
        menu.clear()
        group = QActionGroup(menu)

        def choice(text, key, *, enabled=True):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(c.device_choice == key)
            action.setEnabled(enabled and not c.busy)
            action.triggered.connect(lambda _=False, key=key: self.set_device(key))
            group.addAction(action)

        choice("Automatic — the only laser connected", "")
        menu.addSeparator()
        keys = [device["key"] for device in c.usb_devices]
        for number, key in enumerate(keys, start=1):
            choice(f"Laser {number} — {usb_label(key)}", key)
        if c.device_choice and c.device_choice not in keys:
            choice(f"{usb_label(c.device_choice)} (not connected)", c.device_choice)
        if not keys:
            menu.addAction("No lasers found on USB").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Check USB again", c.refresh_devices)
        if c.busy:
            menu.addAction("Stop the laser to switch lasers").setEnabled(False)

    def set_device(self, key: str):
        if self.controller.set_device(key):
            self.settings.setValue("usb_device", key)

    def set_frame_mode(self, key: str):
        self.settings.setValue("frame_mode", key)
        self.controller.set_frame_mode(key)
        self.frame_actions[key].setChecked(True)

    def toggle_frame(self):
        self.controller.toggle_frame()
        # Arrow keys go to the canvas, so the design can be walked onto the board straight away.
        if self.controller.framing:
            self.canvas.setFocus()

    def set_theme(self, key: str):
        from PySide6.QtWidgets import QApplication

        self.settings.setValue("theme", key)
        theme.apply(QApplication.instance(), key or None)
        for name, action in self.act.items():
            if not action.icon().isNull():
                kind = {"board": "board", "artwork": "import", "open": "open", "save": "save", "undo": "undo",
                        "redo": "redo", "fit": "fit", "preview": "preview", "frame": "frame", "arm": "arm",
                        "start": "play", "setup": "gear", "calibrate": "target"}.get(name)
                if kind:
                    action.setIcon(theme.icon(kind))
        self.canvas.rebuild()

    def fit(self):
        self.canvas._zoomed = False
        self.canvas.fit()

    # ---- files

    def _folder(self, key="folder") -> str:
        return self.settings.value(key, "") or ""

    def _remember(self, path, key="folder"):
        self.settings.setValue(key, str(Path(path).parent))

    def pick_artwork(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import artwork", self._folder(), "Artwork (*.svg *.dxf *.stl)")
        if paths:
            self._remember(paths[0])
            self.controller.import_artwork([Path(path) for path in paths])

    def pick_board(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import PCB — select all the board's Gerber and drill files", self._folder("board_folder"),
            "Fabrication files (*.gbr *.g?? *.drl *.xln *.exc *.txt *.cmp *.sol *.top *.bot);;All files (*)")
        if paths:
            self._remember(paths[0], "board_folder")
            self.import_board([Path(path) for path in paths])

    def import_board(self, paths):
        if not self.controller.can_edit():
            return
        dialog = PcbImportDialog(paths, self.controller, self)
        if dialog.exec():
            self.controller.import_board(dialog.inputs(), **dialog.settings())

    def ask_size(self, path):
        width, ok = QInputDialog.getDouble(
            self, path.name, "This SVG has no physical size.\nFinished artwork width (mm):", 25, 0.001, 10000, 3)
        if ok:
            self.controller.import_artwork([path], width)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self._folder("project_folder"),
                                              "StudioLaser project (*.studiolaser.json *.openlaser.json);;JSON (*.json)")
        if path:
            self._remember(path, "project_folder")
            self.controller.open_project(Path(path))

    def save_project(self, as_new=False):
        path = self.controller.project_path
        if path is None or as_new:
            name, _ = QFileDialog.getSaveFileName(self, "Save project", self._folder("project_folder"),
                                                  "StudioLaser project (*.studiolaser.json)")
            if not name:
                return
            path = Path(name if name.lower().endswith(".json") else name + ".studiolaser.json")
            self._remember(path, "project_folder")
        self.controller.save_project(path)
        self.refresh()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        artwork = [path for path in paths if path.suffix.lower() in ARTWORK_SUFFIXES]
        board = [path for path in paths if path.suffix.lower() not in ARTWORK_SUFFIXES and path.is_file()]
        # Return from the drop first, so the source window is not held while a dialog is open.
        if artwork:
            QTimer.singleShot(0, lambda: self.controller.import_artwork(artwork))
        if board:
            QTimer.singleShot(0, lambda: self.import_board(board))

    # ---- tools

    def add_pattern(self, job):
        if self.controller.can_edit():
            self.controller.add_jobs([job])
            self.controller.say(f"Added “{job.name}”. Review its settings, then arm and run it on scrap.", "success")

    def add_matrix(self):
        dialog = MaterialTestDialog(self.controller, self)
        if dialog.exec() and dialog.job:
            self.add_pattern(dialog.job)

    def add_focus(self):
        slot, ok = QInputDialog.getInt(
            self, "Focus test", "Run this once at each lens height, using a new slot each time.\n"
                                "The sharpest, thinnest lines mark the best focus.\n\nSlot (1–9):", 1, 1, 9)
        if ok:
            self.add_pattern(focus_job(self.controller, slot))

    def add_square(self, filled):
        profile = self.controller.profile
        size, ok = QInputDialog.getDouble(self, "Test square", "Side length (mm):", 20, 0.1,
                                          min(profile.width, profile.height), 2)
        if ok:
            job = self.controller.job
            layer = job.objects[0].layer if job else starting_layer(COPPER_FILL)
            self.add_pattern(test_square(size, layer, filled=filled))

    def save_preset(self, operation: int):
        job = self.controller.job
        if job is None or self.controller.marking:
            return
        artwork = job.objects[operation]
        dialog = SavePresetDialog(artwork.layer, artwork.kind if artwork.kind in
                                  ("copper_contour", "copper_fill", "cleanup", "mask", "silk") else "",
                                  self.controller, self)
        if dialog.exec():
            try:
                preset = self.controller.presets.add(dialog.result_preset())
            except Exception as exc:
                self.controller.say(f"The preset was not saved: {exc}", "error")
                return
            self.controller.presets_changed.emit()
            self.controller.say(f"Saved preset “{preset.name}”.", "success")

    def open_setup(self):
        if self.controller.can_edit():
            MachineSetupDialog(self.controller, self).exec()

    def open_calibration(self):
        if not self.controller.profile.limits:
            self.controller.say("Run Machine Setup before calibrating the lens.", "warning")
            self.open_setup()
            return
        if self.calibration is None:
            self.calibration = CalibrationDialog(self.controller, self)
        self.calibration.show()
        self.calibration.raise_()

    def open_registration(self):
        job = self.controller.job
        if job is None or not (job.references or job.boundary) or not self.controller.can_edit():
            return
        if self.registration is not None:
            self.registration.close()
        self.registration = RegistrationDialog(self.controller, self.canvas, self)
        self.registration.finished.connect(lambda _: setattr(self, "registration", None))
        self.registration.show()

    # ---- closing

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("state", self.saveState())
        if self.controller.shutdown():
            QApplication.instance().removeEventFilter(self.esc_stop)
            event.accept()
        else:
            self._closing = True
            self.controller.say("Stopping the laser before closing…", "warning")
            event.ignore()
