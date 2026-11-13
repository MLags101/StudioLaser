from dataclasses import replace
import time

import pytest
from shapely.geometry import LineString, Point

from mopa_luiz.gui.dialogs import PcbImportDialog
from mopa_luiz.gui.tuning import TuningDialog
from mopa_luiz.job_planner import compile_mark
from mopa_luiz.layers import layer_from_dict
from mopa_luiz.pcb.board import polygon_region, prepare_jobs
from mopa_luiz.pcb.inverter import InversionMode
from mopa_luiz.presets import starting_layer
from mopa_luiz.tuning import Feedback, RANGES, coupon_job, suggest
from mopa_luiz.workspace import COPPER_FILL, MASK, SILK, open_project, save_project
from tests.test_gui import app, controller, pump  # noqa: F401
from tests.test_import_fixes import board_files, flashes


def prepare(inputs, **kwargs):
    return prepare_jobs(inputs, {}, mode=kwargs.pop("mode", InversionMode.ISOLATION), allow_inferred=True, **kwargs)


def test_trace_only_preview_and_arm(app, tmp_path, monkeypatch):
    path = flashes(tmp_path / "sample-F_Cu.gbr", "R,2X2", [(8, 5)])
    c = controller(tmp_path, monkeypatch)
    dialog = PcbImportDialog([path], c)
    try:
        def wait_for(predicate):
            limit = time.monotonic() + 4
            while not predicate() and time.monotonic() < limit:
                app.processEvents()
                time.sleep(0.01)
            assert predicate()

        wait_for(dialog.review_button.isEnabled)
        dialog.review()
        wait_for(lambda: bool(dialog.jobs))
        assert dialog.jobs[0].inferred and dialog.jobs[0].retained
        assert not dialog.add_button.isEnabled()
        dialog.coverage.setChecked(True)
        assert dialog.add_button.isEnabled()
        c.commit_gerber_import(dialog.jobs)
        assert "inferred" in c.blockers()["arm"]
        c.set_verified(True)
        assert not c.blockers()["arm"]
        c.arm()
        pump(app, c, lambda: not c.planning)
        assert c.arming.active and c.plan.paths > 0
    finally:
        dialog.close()
        c.shutdown()


def test_outline_requirements_and_fabrication_coordinates(tmp_path):
    files = board_files(tmp_path)
    with pytest.raises(ValueError, match="real board outline"):
        prepare({"top_copper": files["top_copper"]}, mode=InversionMode.FULL_INVERT)
    with pytest.raises(ValueError, match="real board outline"):
        prepare({"top_copper": files["top_copper"], "bottom_mask": files["bottom_mask"]})
    path = files["top_copper"][0]
    text = path.read_text()
    path.write_text("%TF.FilePolarity,Negative*%\n" + text)
    with pytest.raises(ValueError, match="real board outline"):
        prepare({"top_copper": [path]})
    path.write_text(text)
    bottom = prepare(files, first_side="bottom")[0]
    expected = next(obj for obj in bottom.objects if obj.kind == SILK)
    target = replace(bottom, objects=tuple(obj for obj in bottom.objects if obj.kind != SILK))
    merged = prepare({"bottom_silk": files["bottom_silk"]}, target=target)[0]
    assert polygon_region(merged.objects[-1].fills).equals(polygon_region(expected.fills))
    save_project(tmp_path / "job.json", [merged])
    loaded = open_project(tmp_path / "job.json")[0]
    assert loaded.first_side == "bottom" and loaded.work_area == merged.work_area


def test_bottom_first_and_independent_bottom(app, tmp_path, monkeypatch):
    files = board_files(tmp_path)
    jobs = prepare(files, first_side="bottom")
    c = controller(tmp_path, monkeypatch, jobs)
    try:
        assert c.job.side == "bottom"
        c.select(1)
        assert "bottom side" in c.blockers()["arm"]
        c.set_verified(True)
        assert not c.is_verified()
        c.select(0)
        c.set_verified(True)
        c.arm()
        pump(app, c, lambda: c.arming.active)
        c.start()
        pump(app, c, lambda: not c.busy and c.job.status == "complete")
        c.select(1)
        assert "alignment" in c.blockers()["arm"]
        c.set_verified(True)
        assert not c.blockers()["arm"]
        independent = prepare({"bottom_copper": files["top_copper"]})[0]
        c.add_jobs([independent])
        c.set_verified(True)
        assert not c.blockers()["arm"]
    finally:
        c.shutdown()


@pytest.mark.parametrize("kind", [COPPER_FILL, MASK, SILK])
def test_tuning_one_coupon_then_one_change(app, tmp_path, monkeypatch, kind):
    c = controller(tmp_path, monkeypatch)
    dialog = TuningDialog(c)
    try:
        dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
        original = dialog.layer
        dialog.add_coupon()
        assert c.job.coupon and c.job.objects[0].layer.frequency_khz == 37
        assert not dialog.evaluate.isEnabled() and "fresh area" in c.blockers()["arm"]
        c.set_verified(True)
        c.arm()
        pump(app, c, lambda: c.arming.active)
        c.start()
        pump(app, c, lambda: not c.busy and c.job.status == "complete")
        assert dialog.evaluate.isEnabled()
        dialog.result.setCurrentIndex(dialog.result.findData("uniform"))
        dialog.review_result()
        assert dialog.current_advice.field == "passes"
        dialog.accept_change()
        assert dialog.layer == replace(original, passes=original.passes + 1)
        assert dialog.layer.frequency_khz == 37 and dialog.add.isEnabled()
    finally:
        dialog.close()
        c.shutdown()


def test_hatch_paths_and_feedback():
    from tests.test_calibration_and_model import b6_profile
    from mopa_luiz.workspace import Artwork, Job, rectangle

    profile = b6_profile()
    layer = replace(starting_layer(COPPER_FILL), hatch_pattern="contour", passes=1, raster_pitch_mm=0.1)
    coupon = coupon_job(COPPER_FILL, layer)
    rings = (*coupon.objects[0].fills, rectangle(6, 0.013, 7, 0.03))
    plan = compile_mark(Job("Narrow", (Artwork("Hatch", rings, rings, layer),)), profile)
    region = polygon_region(rings).buffer(1e-9)
    assert plan.notes and all(region.covers(LineString(path)) for path in plan.preview)
    radial = coupon_job(COPPER_FILL, replace(layer, hatch_pattern="radial"))
    plan = compile_mark(radial, profile)
    assert plan.paths > 10 and all(LineString(path).distance(Point(0, 0)) > 2.99 for path in plan.preview)
    with pytest.raises(ValueError, match="coupon"):
        compile_mark(replace(radial, coupon=False), profile)
    data = layer.to_dict()
    del data["hatch_pattern"]
    assert layer_from_dict({**data, "crosshatch": True}).pattern == "crosshatch"
    assert suggest(COPPER_FILL, layer, Feedback("striped", 1), RANGES).field == "raster_pitch_mm"
    assert not suggest(COPPER_FILL, layer, Feedback("uniform", 5), RANGES).field
    assert not suggest(COPPER_FILL, layer, Feedback("clear", 1), RANGES).accepted
    assert suggest(COPPER_FILL, layer, Feedback("clear", 1, "isolated"), RANGES).accepted


def test_gerber_menu_and_drop_use_the_same_review(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QFileDialog
    from mopa_luiz.gui.main_window import MainWindow

    path = flashes(tmp_path / "sample-F_Cu.gbr", "R,2X2", [(8, 5)])
    c = controller(tmp_path, monkeypatch)
    window = MainWindow(controller=c)
    opened = []
    monkeypatch.setattr(window, "import_board", lambda paths, **kwargs: opened.append((paths, kwargs)))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args: ([str(path)], ""))
    try:
        window.pick_board()
        window.pick_artwork()
        window.add_files()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.dropEvent(drop)
        app.processEvents()
        assert [paths for paths, _ in opened] == [[path]] * 4
    finally:
        window.close()


def test_preview_failure_and_machine_state_explain_arm_blockers(app, tmp_path, monkeypatch):
    from mopa_luiz.workspace import Placement

    job = coupon_job(COPPER_FILL, starting_layer(COPPER_FILL))
    c = controller(tmp_path, monkeypatch, [job])
    try:
        c.set_placement(Placement(x=300))
        c.set_verified(True)
        c.preview()
        pump(app, c, lambda: not c.planning)
        assert "outside" in c.blockers()["arm"]
        c.replace_job(replace(c.job, status="interrupted"), "Simulate interruption")
        assert "interrupted" in c.blockers()["arm"]
        c.review()
        c.set_placement(Placement())
        c.profile = replace(c.profile, enabled=False)
        c.preview()
        pump(app, c, lambda: not c.planning)
        assert c.plan and "Machine Setup" in c.blockers()["arm"]
    finally:
        c.shutdown()
