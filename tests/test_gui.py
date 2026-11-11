"""Workflow rules in the controller and the window that sits on top of it (offscreen, simulated laser)."""

from dataclasses import replace
import json
import os
from pathlib import Path
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mopa_luiz.gui import theme  # noqa: E402
from mopa_luiz.gui.controller import AppController  # noqa: E402
from mopa_luiz.layers import VECTOR_ENGRAVE, LayerSpec  # noqa: E402
from mopa_luiz.patterns import test_square as square  # noqa: E402
from mopa_luiz.workspace import Artwork, Job, Placement, rectangle, save_project  # noqa: E402

LAYER = LayerSpec("a", "a", VECTOR_ENGRAVE, 20, 45, 200, 1000)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("qsettings")))
    application = QApplication.instance() or QApplication([])
    theme.apply(application, "dark")
    return application


def profile():
    from tests.test_calibration_and_model import b6_profile

    return b6_profile()


def board_side(side, board="b"):
    outline = rectangle(-10, -5, 10, 5)
    art = Artwork("Copper", (outline,), (outline,), LAYER, "copper_contour")
    return Job(f"Board {side}", (art,), boundary=(outline,), references=((-8, -3), (8, -3), (8, 3)),
               board_id=board, side=side, job_id=f"{board}-{side}")


def controller(tmp_path, monkeypatch, jobs=()):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    c = AppController(tmp_path / "state", autostart=False)
    c.profile = profile()
    c.link = "mock"
    c.add_jobs(list(jobs))
    c.undo_stack.clear()
    return c


def pump(app, c, done, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        c.tick()
        app.processEvents()
        if done():
            return
        time.sleep(0.01)
    pytest.fail("timed out")


def test_bad_profile_does_not_lose_presets_or_the_recovered_queue(app, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-machine.json").write_text('{"version": 99}', encoding="utf-8")
    save_project(state / "recovery.json", [square(10, LAYER, filled=False)])
    c = AppController(state, autostart=False)
    messages = []
    c.message.connect(lambda text, level: messages.append((level, text)))
    c.load_state()
    assert len(c.jobs) == 1 and not c.profile.enabled
    assert any(level == "error" and "machine profile" in text for level, text in messages)
    c.flush_recovery()
    assert len(json.loads((state / "recovery.json").read_text(encoding="utf-8"))["jobs"]) == 1
    # An unreadable recovery file is set aside, never overwritten by an empty queue.
    (state / "recovery.json").write_text("{broken", encoding="utf-8")
    c2 = AppController(state, autostart=False)
    c2.load_state()
    c2.add_jobs([square(5, LAYER, filled=False)])
    c2.flush_recovery()
    kept = list(state.glob("recovery-unreadable-*.json"))
    assert kept and kept[0].read_text(encoding="utf-8") == "{broken"


def test_arming_rules_verification_and_back_side_gate(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [board_side("top"), board_side("bottom")])
    c.select(0)
    assert "alignment" in c.blockers()["arm"]
    c.set_verified(True)
    assert c.blockers()["arm"] == ""
    # Verification belongs to one job and survives Stop, but not a move.
    c.stop()
    assert c.is_verified()
    c.select(1)
    assert not c.is_verified() and "top side" in c.blockers()["arm"]
    c.set_verified(True)                     # refused until the top side is done
    assert not c.is_verified()
    c.jobs[0] = replace(c.jobs[0], status="complete")
    c.set_verified(True)
    assert c.blockers()["arm"] == ""
    c.nudge(0.1, 0)
    assert not c.is_verified() and c.job.placement.x == pytest.approx(0.1)
    c.select(0)
    assert "complete" in c.blockers()["arm"]
    c.review()
    assert c.job.status == "ready"
    # Machine not ready: every hardware action explains why.
    c.link, c.link_note = "driver", "Windows is using the EZCAD driver"
    assert "EZCAD" in c.blockers()["frame"] and "EZCAD" in c.blockers()["arm"]
    c.link = "mock"
    c.state_unknown = True
    assert "unknown state" in c.blockers()["start"]
    c.acknowledge()
    assert not c.state_unknown
    c.profile = replace(c.profile, enabled=False)
    assert "Machine Setup" in c.blockers()["frame"]
    c.shutdown()


def test_edits_cancel_arming_and_undo_restores_placement(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [square(10, LAYER, filled=False)])
    c.arm()
    pump(app, c, lambda: c.arming.active)
    assert c.plan is not None and c.plan.seconds > 0
    c.set_layer(0, replace(c.job.objects[0].layer, power_percent=30))
    assert not c.arming.active and c.plan is None
    c.set_placement(Placement(5, 5, 0), merge="")
    c.nudge(0.1, 0)
    c.nudge(0.1, 0)       # merged with the previous nudge into one undo step
    assert c.job.placement.x == pytest.approx(5.2)
    c.undo()
    assert c.job.placement == Placement(5, 5, 0)
    c.undo()
    assert c.job.placement == Placement()
    c.redo()
    assert c.job.placement == Placement(5, 5, 0)
    c.undo()
    c.undo()
    assert c.job.objects[0].layer.power_percent == 20
    # A plan computed for an older revision is reported, not silently dropped.
    messages = []
    c.message.connect(lambda text, level: messages.append(text))
    c.preview()
    c.nudge(1, 0)
    pump(app, c, lambda: not c.planning)
    assert c.plan is None and any("changed during planning" in text for text in messages)
    c.shutdown()


def test_full_simulated_run_marks_complete_and_seeds_the_other_side(app, tmp_path, monkeypatch):
    top = replace(board_side("top"), placement=Placement(3, 2, 15))
    c = controller(tmp_path, monkeypatch, [top, board_side("bottom")])
    progress = []
    c.progress.connect(lambda done, total, label: progress.append((done, total)))
    c.select(0)
    c.set_verified(True)
    c.start()                                    # not armed: refused
    assert c.running_id is None
    c.arm()
    pump(app, c, lambda: c.arming.active)
    c.start()
    assert c.jobs[0].status == "running" and c.blockers()["edit"]
    c.nudge(1, 0)                                # edits are refused while marking
    assert c.jobs[0].placement == Placement(3, 2, 15)
    pump(app, c, lambda: c.jobs[0].status == "complete" and not c.busy, 40)
    assert progress and progress[-1][0] == progress[-1][1]
    assert c.jobs[1].placement == Placement(3, 2, -15)
    assert not c.undo_stack                      # history cannot resurrect a pre-run status
    recovered = json.loads((tmp_path / "state" / "recovery.json").read_text(encoding="utf-8"))
    assert [job["status"] for job in recovered["jobs"]] == ["complete", "ready"]
    # Framing follows live edits and Stop ends it without clearing the alignment check.
    c.select(1)
    c.set_verified(True)
    c.frame(True)
    pump(app, c, lambda: c.worker.action == "frame-loop" and not c._frame_dirty)
    c.show_point((1, 1))
    pump(app, c, lambda: not c._frame_dirty)
    c.clear_target()
    c.stop()
    pump(app, c, lambda: not c.busy)
    assert c.is_verified() and not c.state_unknown
    assert c.shutdown()


def test_window_builds_in_both_themes_and_canvas_click_does_not_move_the_job(app, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    from mopa_luiz.gui.dialogs import RegistrationDialog
    from mopa_luiz.gui.main_window import MainWindow

    for mode in ("light", "dark"):
        theme.apply(app, mode)
        window = MainWindow(tmp_path / f"state-{mode}")
        c = window.controller
        c._timer.stop()
        try:
            assert c.set_profile(profile())
            c.link = "mock"
            c.add_jobs([board_side("top"), board_side("bottom")])
            c.select(0)
            window.resize(1200, 800)
            window.show()
            window.fit()
            app.processEvents()
            assert window.stop_button.isVisible() and window.act["stop"].isEnabled()
            assert not window.act["arm"].isEnabled() and "alignment" in window.act["arm"].toolTip()
            assert window.inspector.register.text().startswith("Register on holes")
            canvas = window.canvas
            centre = canvas.mapFromScene(0.0, 0.0)
            # A click with a pixel of jitter is not a drag.
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=centre)
            QTest.mouseMove(canvas.viewport(), centre + QPoint(2, 1))
            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=centre + QPoint(2, 1))
            assert c.job.placement == Placement()
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=centre)
            QTest.mouseMove(canvas.viewport(), centre + QPoint(40, 0))
            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=centre + QPoint(40, 0))
            assert c.job.placement.x > 0 and c.job.placement.y == pytest.approx(0, abs=0.01)
            c.undo()
            QTest.keyClick(canvas, Qt.Key.Key_Right)
            QTest.keyClick(canvas, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
            QTest.keyClick(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
            assert (c.job.placement.x, c.job.placement.y, c.job.placement.angle) == pytest.approx((0.1, 0.01, 0.1))
            # Inspector edits reach the job; the tick box follows the controller.
            window.inspector.power.setValue(33)
            assert c.job.objects[0].layer.power_percent == 33
            window.inspector.verified.setChecked(True)
            assert c.is_verified()
            # Registration: three holes measured at a known pose are fitted back to it.
            window.open_registration()
            dialog = window.registration
            assert isinstance(dialog, RegistrationDialog) and canvas.picking
            true = Placement(4, -2, 7)
            assert dialog.mode.currentData() == "holes" and dialog.mode.count() == 2
            for index, hole in enumerate(c.job.references):
                dialog.add_hole(index)
                dialog.points[index][2][:] = true.apply(hole)
            dialog._fill()
            assert dialog.result.worst < 1e-6
            dialog.accept()
            assert c.job.placement.x == pytest.approx(4) and c.job.placement.angle == pytest.approx(7)
            assert not canvas.picking and c.target is None and not c.is_verified()
            # Edge registration: points anywhere along the real board's edges fix the placement.
            window.open_registration()
            dialog = window.registration
            dialog.mode.setCurrentIndex(dialog.mode.findData("edge"))
            assert canvas.pick_mode == "edge"
            true = Placement(5.5, -1.0, 9)
            for design in ((-10, -4), (0, 5), (10, 1)):          # on the left, top and right edges
                dialog.add_edge(*design)
                dialog.points[-1][2][:] = true.apply(design)
            dialog._fill()
            assert dialog.result is not None and dialog.result.worst < 0.002
            dialog.accept()
            assert c.job.placement.x == pytest.approx(5.5, abs=0.002)
            assert c.job.placement.angle == pytest.approx(9, abs=0.01)
            # Clicking an operation's tick box used to crash the app: the tree rebuilt itself mid-click.
            tree = window.jobs.tree
            child = tree.topLevelItem(c.current).child(0)
            rect = tree.visualItemRect(child)
            before = c.job.objects[0].layer.output
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.topLeft() + QPoint(10, rect.height() // 2))
            app.processEvents()
            assert c.job.objects[0].layer.output != before
            tree.topLevelItem(c.current).child(0).setCheckState(0, Qt.CheckState.Checked if not c.job.objects[0].layer.output
                                                                else Qt.CheckState.Unchecked)
            app.processEvents()
            assert c.job.objects[0].layer.output == before
            # Choosing a preset from the drop-down replaces the drop-down's own items: also deferred.
            window.inspector.preset.setCurrentIndex(1)
            window.inspector.preset.activated.emit(1)
            app.processEvents()
        finally:
            c.shutdown()
            window.close()
            window.deleteLater()
            app.processEvents()


def test_setup_calibration_import_and_preset_dialogs(app, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    from mopa_luiz.gui.dialogs import MaterialTestDialog, PcbImportDialog, PresetManagerDialog, SavePresetDialog
    from mopa_luiz.gui.machine import CalibrationDialog, MachineSetupDialog
    from tests.test_desktop_workflow import pcb_fixture

    c = AppController(tmp_path / "state", autostart=False)
    c.presets = __import__("mopa_luiz.presets", fromlist=["PresetLibrary"]).PresetLibrary(tmp_path / "state" / "recipes.json")
    try:
        # Machine Setup: a hand-written markcfg and a blank .cor stand in for the factory files.
        config = tmp_path / "markcfg7"
        config.write_text("[LMC_CFG]\nFIELDSIZE=167.5\nMINPWMFREQ=1000\nMAXPWMFREQ=4000000\nGALVOX=1\n"
                          "GALVODISTOR0=0.9705\nGALVODISTOR1=0.9785\nENABLECORFILE=0\n", encoding="utf-8")
        cor = tmp_path / "lens.cor"
        cor.write_bytes(b"JCZ_COR_2_1\x00" + bytes(24) + bytes(65 * 65 * 8) + bytes(4))
        setup = MachineSetupDialog(c)
        assert "Still needed" in setup.status.text() or "Complete" in setup.status.text()
        (tmp_path / "MarkParam.lib").write_text(
            "[LASERMODE]\nYAG=1\n\n[Default]\nSTARTTC=-300\nLASEROFFTC=100\nPOLYTC=100\nJUMPSPEED=4000\n"
            "m_nMinJumpDelayTCUs=500\nm_nMaxJumpDelayTCUs=500\n\n[SMR]\nLOOP=1\nMARKSPEED=1000\nPOWERRATIO=50\n"
            "FREQ=40000\nQPULSEWIDTH=200\n", encoding="utf-8")
        setup.cfg_path.setText(str(config))
        setup.cor_path.setText(str(cor))
        setup.import_factory()                       # finds MarkParam.lib beside markcfg7 by itself
        assert "ENABLECORFILE=0" in setup.report.toPlainText() and "MarkParam.lib" in setup.report.toPlainText()
        assert setup.delays["delay_laser_on"].value() == -300 and setup.jump_speed.value() == 4000
        assert setup.width.value() == 167.5 and setup.limits.rowCount() == 16
        setup.limits.item(0, 1).setText("")          # an incomplete row is refused, not saved
        with pytest.raises(ValueError, match="incomplete"):
            setup.collect()
        setup._reset_limits()
        setup.enabled.setChecked(True)
        setup.save()
        assert c.profile.enabled and c.profile.base == "ezcad" and c.profile.correction
        assert c.profile.delays["delay_jump_short"] == 500
        imported = [p for p in c.presets.user if p.name == "SMR (from EZCAD)"]
        assert len(imported) == 1 and imported[0].kind == "mask" and imported[0].layer.power_percent == 50
        assert (tmp_path / "state" / "active-machine.json").is_file()
        c.link = "mock"

        # Calibrate Lens: a grid that measured 1 % wide in X gives a correction that shrinks X.
        cal = CalibrationDialog(c)
        cal.add_grid()
        assert c.job.name.startswith("Calibration grid")
        for key in ("wt", "wm", "wb"):
            cal.spans[key].setValue(101.0)
        for key in ("d1", "d2"):
            cal.spans[key].setValue((101 ** 2 + 100 ** 2) ** 0.5)
        cal.calculate()
        assert cal.fit is not None and cal.apply.isEnabled()
        cal.apply_calibration()
        assert c.profile.calibration is not None and "grid" in c.profile.calibration_note
        assert c.profile.calibration.apply((50, 0))[0] == pytest.approx(50 / 1.01, abs=0.02)
        # Changing the correction base discards a calibration measured on top of the old one.
        cal.base.setCurrentIndex(cal.base.findData("cor"))
        assert c.profile.base == "cor" and c.profile.calibration is None
        # Red light: the dot had to be aimed 0.4 mm right of centre to land on the centre cross.
        cal.aim("centre")
        for _ in range(4):
            cal.red_jog(1, 0)
        cal.save_red()
        pump(app, c, lambda: c.profile.red_x == pytest.approx(0.4), 10)
        assert c.target is None
        cal.close()

        # PCB import: roles are detected off the UI thread, then the board is prepared.
        files = pcb_fixture(tmp_path)
        paths = [path for group in files.values() for path in group]
        dialog = PcbImportDialog(paths, c)
        pump(app, c, lambda: dialog._future.done() and all(combo.isEnabled() for combo in dialog.roles), 20)
        assert dialog.inputs().keys() == {"outline", "top_copper", "bottom_copper", "drill"}
        assert dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
        dialog.roles[0].setCurrentIndex(dialog.roles[0].findData("other"))
        assert "outline" in dialog.problem.text()
        dialog.roles[0].setCurrentIndex(dialog.roles[0].findData("outline"))
        before = len(c.jobs)
        c.import_board(dialog.inputs(), **dialog.settings())
        pump(app, c, lambda: len(c.jobs) == before + 2, 30)
        top = c.jobs[before]
        assert [obj.kind for obj in top.objects] == ["copper_contour", "copper_fill", "cleanup"]
        assert top.objects[0].layer.output and not top.objects[2].layer.output and top.references

        # Presets: save one from an operation, make it default, and new boards start from it.
        c.select(before)
        layer = replace(top.objects[1].layer, power_percent=66)
        save = SavePresetDialog(layer, "copper_fill", c)
        save.name.setText("My copper")
        save.kerf.setValue(0.07)
        save.default.setChecked(True)
        preset = c.presets.add(save.result_preset())
        assert c.presets.defaults()["copper_fill"].power_percent == 66 and c.presets.kerf() == pytest.approx(0.07)
        manager = PresetManagerDialog(c)
        assert manager.list.count() == 2 + 5
        manager.list.setCurrentRow(manager.list.count() - 1)      # a built-in: read-only, but can be copied
        assert not manager.delete.isEnabled() and manager.duplicate.isEnabled()
        manager.duplicate_preset()
        assert len(c.presets.user) == 3 and c.presets.get(preset.preset_id).default

        # Material test matrix.
        matrix = MaterialTestDialog(c)
        matrix.build()
        assert matrix.job is not None and len(matrix.job.objects) == 17
    finally:
        c.shutdown()


def test_frame_content_follows_moves_and_rotation_keeps_the_centre(app, tmp_path, monkeypatch):
    import math

    off_centre = replace(board_side("top"), placement=Placement(20, 10, 0))
    c = controller(tmp_path, monkeypatch, [off_centre])
    try:
        outline = c.frame_paths()
        assert len(outline) == 1 + 3            # outline plus a ring at each of the three holes
        ring = outline[1]
        hole = c.job.placement.apply(c.job.references[0])
        assert all(math.dist(point, hole) == pytest.approx(0.4) for point in ring)
        c.set_frame_mode("outline")
        assert len(c.frame_paths()) == 1
        c.set_frame_mode("box")
        assert c.frame_paths()[0][0] == pytest.approx((10, 5))
        c.set_frame_mode("design")
        assert len(c.frame_paths()) == 2        # outline plus the one copper contour
        c.set_frame_mode("holes")
        # Rotating turns about the design's centre: the centre stays put, the outline turns.
        centre_before = c.job.placement.apply((0, 0))
        c.nudge(angle=90)
        assert c.job.placement.apply((0, 0)) == pytest.approx(centre_before)
        assert c.job.placement.angle == pytest.approx(90)
        c.undo()
        # A live frame picks up each move, and the canvas overlay shows what is traced.
        c.frame(True)
        pump(app, c, lambda: c.worker.action == "frame-loop" and not c._frame_dirty)
        sent = []
        original = c.worker.update_frame
        c.worker.update_frame = lambda paths: sent.append(paths) or original(paths)
        c.nudge(1.0, 0)
        pump(app, c, lambda: sent and not c._frame_dirty)
        first = sent[-1][0][0]
        c.nudge(1.0, 0)
        pump(app, c, lambda: len(sent) >= 2 and not c._frame_dirty)
        assert sent[-1][0][0] != first          # the outline sent to the laser moved with the design
        c.stop()
        pump(app, c, lambda: not c.busy)
    finally:
        c.shutdown()
