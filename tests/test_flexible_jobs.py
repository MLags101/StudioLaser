"""Adding files to jobs as operations, editing operations, the quick size trim and loading a .cor late."""

from dataclasses import replace
from pathlib import Path

import pytest

from mopa_luiz.artwork_files import artwork_for, job_for, read_layer
from mopa_luiz.device_profile import DeviceProfile
from mopa_luiz.layers import RASTER_ENGRAVE, VECTOR_ENGRAVE
from mopa_luiz.pcb.board import prepare_board
from mopa_luiz.pcb.inverter import InversionMode
from mopa_luiz.workspace import SILK, job_from_board, open_project, save_project
from tests.test_calibration_and_model import b6_profile
from tests.test_gui import app, board_side, controller, pump  # noqa: F401
from tests.test_import_fixes import board_files, flashes

REPO = Path(__file__).resolve().parents[1]


def box(paths):
    xs = [x for path in paths for x, _ in path]
    ys = [y for path in paths for _, y in path]
    return min(xs), min(ys), max(xs), max(ys)


@pytest.mark.parametrize("flip", ["vertical", "horizontal"])
def test_a_gerber_added_to_a_board_side_lands_on_that_sides_own_geometry(tmp_path, flip):
    files = board_files(tmp_path)
    silk = files.pop("bottom_silk")[0]
    sides = prepare_board({**files, "bottom_silk": [silk]}, mode=InversionMode.ISOLATION, flip_axis=flip)
    bottom = job_from_board("b", sides[1], "board")
    assert bottom.origin == (10.0, 5.0) and bottom.flip == flip
    expected = next(a for a in bottom.objects if a.kind == SILK)
    added, note = artwork_for(read_layer(silk), bottom)
    assert note == "" and added.kind == SILK and added.layer.operation == RASTER_ENGRAVE
    assert box(added.fills) == pytest.approx(box(expected.fills), abs=1e-6)
    # The origin and flip survive a project round trip, so files can be added after reopening.
    save_project(tmp_path / "p.json", [bottom])
    assert open_project(tmp_path / "p.json")[0].from_fabrication((2, 2)) == bottom.from_fabrication((2, 2))


def test_a_gerber_on_its_own_is_centred_and_a_board_without_origin_says_so(tmp_path):
    path = flashes(tmp_path / "logo.gbr", "R,2.0X2.0", [(40, 30), (44, 30)])
    job = job_for(read_layer(path))
    assert box(job.objects[0].fills) == pytest.approx((-3, -1, 3, 1))
    old_board = board_side("top")        # imported before origins were recorded
    with pytest.raises(ValueError, match="no saved Gerber origin"):
        artwork_for(read_layer(path), old_board)


def svg_file(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 20 10">'
                    f'{body}</svg>', encoding="utf-8")
    return path


def test_files_go_into_the_selected_job_in_order_and_operations_can_be_edited(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        a = svg_file(tmp_path, "a.svg", '<rect x="1" y="1" width="4" height="4"/>')
        b = svg_file(tmp_path, "b.svg", '<path d="M 1 1 L 9 9" stroke="black"/>')
        c.add_files_to_job([a, b])
        pump(app, c, lambda: len(c.job.objects) == 3)
        assert [o.name for o in c.job.objects] == ["Copper", "a", "b"]
        assert c.job.objects[1].layer.operation == VECTOR_ENGRAVE and c.job.objects[1].layer.power_percent == 0

        c.rename_layer(1, "Logo")
        c.set_layer_kind(1, SILK)
        c.duplicate_layer(1)
        assert [o.name for o in c.job.objects] == ["Copper", "Logo", "Logo (copy)", "b"]
        assert c.job.objects[2].kind == SILK and c.job.objects[2].layer.layer_id != c.job.objects[1].layer.layer_id
        c.move_layer(3, -3)
        assert [o.name for o in c.job.objects] == ["b", "Copper", "Logo", "Logo (copy)"]
        for _ in range(3):
            c.remove_layer(0)
        c.remove_layer(0)                      # the last operation stays
        assert [o.name for o in c.job.objects] == ["Logo (copy)"]
        c.undo()
        assert len(c.job.objects) == 2
        c.rename_job("Panel")
        assert c.job.name == "Panel"
    finally:
        c.shutdown()


def test_several_files_can_become_one_job(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    try:
        paths = [svg_file(tmp_path, f"{n}.svg", '<rect x="1" y="1" width="4" height="4"/>') for n in "xyz"]
        c.import_artwork(paths, together=True)
        pump(app, c, lambda: c.jobs and len(c.jobs[0].objects) == 3)
        assert len(c.jobs) == 1 and [o.name for o in c.jobs[0].objects] == ["x", "y", "z"]
    finally:
        c.shutdown()


def test_size_trim_scales_about_the_centre_and_converges():
    profile = b6_profile()
    base = profile.command_point((10, 10))
    trimmed = profile.with_trim(20, 20.2, 19.9)
    assert (trimmed.trim_x, trimmed.trim_y) == pytest.approx((20 / 20.2, 20 / 19.9), abs=1e-6)
    # A second round refines the first rather than replacing it.
    again = trimmed.with_trim(20, 20.02, 20.0)
    assert again.trim_x == pytest.approx(20 / 20.2 * 20 / 20.02, abs=1e-6)
    plain = replace(profile, base="none")
    assert replace(plain, trim_x=0.99, trim_y=1.01).command_point((10, 10)) == pytest.approx((9.9, 10.1))
    assert profile.command_point((0, 0)) == trimmed.command_point((0, 0))
    assert base != trimmed.command_point((10, 10))
    with pytest.raises(ValueError):
        profile.with_trim(20, 30, 20)
    # A grid fit is relative to calibration and trim together, so applying one resets the trim.
    assert trimmed.with_calibration(None).trim_x == 1.0
    assert trimmed.calibrated((10, 0))[0] == pytest.approx(10 * trimmed.trim_x)


def test_profiles_keep_trim_and_cor_name_and_older_profiles_still_open(tmp_path):
    profile = replace(b6_profile(), trim_x=1.002, trim_y=0.998, correction_name="jcz15.cor")
    profile.save(tmp_path / "m.json")
    again = DeviceProfile.open(tmp_path / "m.json")
    assert (again.trim_x, again.trim_y, again.correction_name) == (1.002, 0.998, "jcz15.cor")
    data = profile.to_dict()
    for key in ("trim_x", "trim_y", "correction_name"):
        data.pop(key)
    assert DeviceProfile.from_dict(data).trim_x == 1.0


def test_trim_can_change_while_only_the_red_light_runs(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        c.frame(True)
        pump(app, c, lambda: c.worker.action == "frame-loop")
        assert c.set_profile(c.profile.with_trim(20, 20.1, 20.1), while_framing=True)
        assert not c.set_profile(replace(c.profile, red_x=1.0))
        c.stop()
        pump(app, c, lambda: not c.busy)
    finally:
        c.shutdown()


@pytest.mark.skipif(not (REPO / "jcz15.cor").is_file(), reason="factory .cor not in the repository")
def test_calibration_dialog_loads_the_cor_when_the_profile_has_none(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from mopa_luiz.gui.machine import CalibrationDialog

    c = controller(tmp_path, monkeypatch)
    c.profile = replace(c.profile, correction="", correction_name="", base="ezcad")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(REPO / "jcz15.cor"), ""))
    dialog = CalibrationDialog(c)
    try:
        dialog.base.setCurrentIndex(dialog.base.findData("cor"))
        assert c.profile.base == "cor" and c.profile.correction and c.profile.correction_name == "jcz15.cor"
        assert "jcz15.cor" in dialog.base_hint.text()
        dialog.square.setValue(20)
        dialog.measured_x.setValue(20.1)
        dialog.measured_y.setValue(19.95)
        dialog.adjust_size()
        assert (c.profile.trim_x, c.profile.trim_y) == pytest.approx((20 / 20.1, 20 / 19.95), abs=1e-6)
    finally:
        dialog.close()
        c.shutdown()
