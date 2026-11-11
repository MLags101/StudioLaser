"""Machine profile import from the real factory files, lens calibration maths, and the job model."""

from dataclasses import replace
import json
import math
from pathlib import Path

import pytest

from mopa_luiz.correction import EzcadField, FieldMap, fit_field_map, grid_points, points_from_spans, read_cor
from mopa_luiz.device_profile import DeviceProfile, import_ezcad
from mopa_luiz.job_planner import compile_frame, compile_mark, format_duration, layer_issues
from mopa_luiz.layers import RASTER_ENGRAVE, VECTOR_ENGRAVE, LayerSpec
from mopa_luiz.patterns import (
    calibration_grid, focus_test, kerf_ladder, steps, test_matrix as matrix, test_square as square, text_paths,
)
from mopa_luiz.presets import Preset, PresetLibrary, builtin_presets, starting_layer
from mopa_luiz.raster import group_islands, hatch_polylines
from mopa_luiz.sources import JPT_M7_60W, frequency_advice
from mopa_luiz.workspace import (
    COPPER_FILL, KINDS, Artwork, Job, Placement, fit_placement, flipped_placement, open_project, rectangle,
    save_project,
)

FIXTURES = Path(__file__).parent / "fixtures" / "b6_60w"


needs_factory_files = pytest.mark.skipif(
    not (FIXTURES / "markcfg7").is_file(), reason="machine factory files are not in tests/fixtures/b6_60w")


def b6_profile(**changes):
    """The imported B6 profile, or an equivalent built by hand when the factory files are absent."""
    if (FIXTURES / "markcfg7").is_file():
        profile, _ = import_ezcad(FIXTURES / "markcfg7", FIXTURES / "jcz15.cor", lens="150 mm")
    else:
        profile = DeviceProfile(
            name="ComMarker B6 MOPA", lens="150 mm", source="JPT M7 60 W (YDFLP-60-M7)", width=167.5, height=167.5,
            min_hz=1000, max_hz=4_000_000, limits=JPT_M7_60W, base="ezcad",
            ezcad=EzcadField(bulge_x=0.9705, bulge_y=0.9785))
    return replace(profile, enabled=True, **changes)


# ---- factory files

@needs_factory_files
def test_real_factory_files_import_and_can_be_enabled(tmp_path):
    profile, report = import_ezcad(FIXTURES / "markcfg7", FIXTURES / "jcz15.cor", lens="150 mm")
    assert profile.width == profile.height == 167.5
    assert (profile.min_hz, profile.max_hz) == (1000, 4_000_000)
    assert profile.pulse_widths == tuple(row[0] for row in JPT_M7_60W)
    # markcfg7 has ENABLECORFILE=0 and bulge 0.9705/0.9785: EZCAD was using its parametric correction.
    assert profile.base == "ezcad" and "ENABLECORFILE=0" in report
    assert profile.ezcad.bulge_x == pytest.approx(0.9705) and profile.ezcad.bulge_y == pytest.approx(0.9785)
    assert not profile.swap_xy and not profile.invert_x and not profile.invert_y
    assert profile.correction and not profile.enabled
    enabled = replace(profile, enabled=True)
    assert enabled.missing() == []
    enabled.validate(live=True)
    path = tmp_path / "machine.json"
    enabled.save(path)
    assert DeviceProfile.open(path) == enabled


@needs_factory_files
def test_real_cor_file_parses_with_trailer_and_sane_table():
    data = (FIXTURES / "jcz15.cor").read_bytes()
    assert len(data) == 33840 and data[:11] == b"JCZ_COR_2_1"
    scale, table = read_cor(data)
    assert 65536 / scale == pytest.approx(167.5, abs=0.01)
    assert len(table) == 65 * 65 and table[32 * 65 + 32] == (0, 0)
    with pytest.raises(ValueError):
        read_cor(data[:20000])
    with pytest.raises(ValueError):
        read_cor(data + bytes(200))


@needs_factory_files
def test_profile_without_a_cor_is_usable_when_the_base_does_not_need_one():
    profile, report = import_ezcad(FIXTURES / "markcfg7", None, lens="150 mm")
    assert profile.base == "ezcad" and not profile.correction
    replace(profile, enabled=True).validate(live=True)
    needs_file = replace(profile, base="cor", enabled=True)
    assert "the lens correction file" in needs_file.missing()
    with pytest.raises(ValueError, match="lens correction file"):
        needs_file.validate(live=True)


def test_version_one_profile_migrates(tmp_path):
    old = {"version": 1, "name": "Old", "lens": "150", "source": "JPT", "width": 150, "height": 150,
           "min_hz": 1000, "max_hz": 4000000, "pulse_widths": [20, 200], "swap_xy": False, "invert_x": True,
           "invert_y": False, "red_x": 0, "red_y": 0, "red_scale_x": 1, "red_scale_y": 1, "frame_speed": 3000,
           "correction": "", "delays": {}, "enabled": False}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    profile = DeviceProfile.open(path)
    assert profile.pulse_widths == (20, 200) and profile.base == "none" and profile.invert_x
    assert dict((w, (c, m)) for w, c, m in profile.limits)[200] == (45, 1000)


# ---- calibration

def test_software_correction_is_applied_and_bounded():
    profile = b6_profile()
    assert profile.curved
    assert profile.galvo_point((0, 0)) == (32768, 32768)
    x, y = profile.command_point((60, 60))
    assert x == pytest.approx(60 * (1 + (0.9705 - 1) * (60 / 83.75) ** 2))
    assert y == pytest.approx(60 * (1 + (0.9785 - 1) * (60 / 83.75) ** 2))
    with pytest.raises(ValueError, match="outside"):
        profile.galvo_point((90, 0))
    flat = replace(profile, base="none")
    assert not flat.curved and flat.command_point((60, 60)) == (60, 60)


def test_fit_recovers_a_synthetic_lens_distortion_and_refines_on_repeat():
    half = 75.0
    lens = EzcadField(scale_x=1.02, scale_y=0.99, bulge_x=0.98, skew_y=1.004, trapezoid_y=1.01).field_map(
        half, distortion=True)   # what the machine does to a commanded position
    targets = grid_points(120, 5)
    measured = [lens.apply(point) for point in targets]
    fit = fit_field_map(targets, measured, targets, half)
    assert fit.rms_before > 0.3
    check = grid_points(110, 7)
    worst = max(math.dist(lens.apply(fit.field_map.apply(point)), point) for point in check)
    assert worst < 0.01
    # Second round: mark with the first calibration active, measure the small remaining error, fit again.
    commanded = [fit.field_map.apply(point) for point in targets]
    second = fit_field_map(commanded, [lens.apply(point) for point in commanded], targets, half)
    assert second.rms_before < 0.01
    assert max(math.dist(lens.apply(second.field_map.apply(point)), point) for point in check) < 0.01


def test_quick_calibration_from_caliper_spans():
    span = 100.0
    exact = points_from_spans(span, (100, 100, 100), (100, 100, 100), (span * 2 ** 0.5,) * 2)
    assert [v for point in exact for v in point] == pytest.approx(
        [v for point in grid_points(span, 3) for v in point])
    # 1 % too wide in X, otherwise true: the fit must shrink X commands by about 1 %.
    wide = points_from_spans(span, (101, 101, 101), (100, 100, 100), (math.hypot(101, 100),) * 2)
    targets = grid_points(span, 3)
    fit = fit_field_map(targets, wide, targets, 75)
    x, y = fit.field_map.apply((50, 50))
    assert x == pytest.approx(50 / 1.01, abs=0.01) and y == pytest.approx(50, abs=0.01)
    with pytest.raises(ValueError, match="25 %"):
        points_from_spans(span, (10, 100, 100), (100, 100, 100), (141, 141))
    with pytest.raises(ValueError, match="implausibly"):
        fit_field_map(targets, [(x * 0.6, y * 0.6) for x, y in targets], targets, 75)


def test_calibrated_profile_bends_long_lines_in_the_plan():
    cal = EzcadField(bulge_x=1.02).field_map(83.75, distortion=True)
    profile = replace(b6_profile(base="none"), calibration=cal)
    line = (((-60, 60), (60, 60)),)
    job = Job("Line", (Artwork("Line", line, (), LayerSpec("a", "a", VECTOR_ENGRAVE, 50, 45, 200, 1000)),), job_id="x")
    plan = compile_mark(job, profile, live=True)
    sent = plan.batches[0].paths[0]
    assert len(sent) > 20   # subdivided so the corrected path is a curve, not a chord
    assert len(compile_frame(line, profile)[0]) > 20
    assert len(compile_mark(job, replace(profile, calibration=None), live=True).batches[0].paths[0]) == 2
    assert FieldMap().identity and not cal.identity


# ---- source limits and operation checks

def test_frequency_window_advice_for_the_60w_source():
    assert frequency_advice(JPT_M7_60W, 200, 45) == ("", "")
    assert frequency_advice(JPT_M7_60W, 200, 30)[0] == "warning"
    assert frequency_advice(JPT_M7_60W, 200, 1200)[0] == "error"
    assert frequency_advice(JPT_M7_60W, 55, 100)[0] == "error"
    profile = b6_profile()
    layer = starting_layer(COPPER_FILL)
    assert layer_issues(layer, profile) == []
    assert [s for s, _ in layer_issues(replace(layer, frequency_khz=20), profile)] == ["warning"]
    assert any("not a pulse width" in text for _, text in layer_issues(replace(layer, pulse_width_ns=55), profile))
    assert any("0 %" in text for _, text in layer_issues(replace(layer, power_percent=0), profile))
    assert any("Passes" in text for _, text in layer_issues(replace(layer, passes=0), profile))
    # An offline preview profile has no source table and must not reject valid widths.
    assert layer_issues(replace(layer, pulse_width_ns=55), DeviceProfile()) == []


def test_builtin_presets_are_valid_for_the_b6_and_flagged_untested():
    profile = b6_profile()
    presets = builtin_presets()
    assert {preset.kind for preset in presets} == set(KINDS)
    for preset in presets:
        assert preset.builtin and "Untested" in preset.note
        assert not [text for severity, text in layer_issues(preset.layer, profile) if severity == "error"], preset.name


def test_time_estimate_and_island_hatch_order():
    profile = replace(b6_profile(base="none"), jump_speed=2000)
    layer = LayerSpec("f", "f", RASTER_ENGRAVE, 50, 45, 200, 1000, raster_pitch_mm=1.0)
    job = square(10, layer, filled=True)
    plan = compile_mark(job, profile)
    assert plan.paths == 10
    assert plan.seconds == pytest.approx(10 * 10 / 1000 + 9 * 1 / 2000 + 10 * 0.0006, rel=0.02)
    assert plan.operations[0][0] == job.objects[0].name
    assert format_duration(59.6) == "1 min 00 s" and format_duration(3700) == "1 h 01 min"
    # Two separate squares: each is filled completely before the galvo moves to the next.
    left, right = rectangle(-30, 0, -20, 10), rectangle(20, 0, 30, 10)
    hole = rectangle(-27, 3, -23, 7)
    assert sorted(len(island) for island in group_islands([list(left), list(right), list(hole)])) == [1, 2]
    together = hatch_polylines([left, right], pitch_mm=1.0, closure_tol_mm=1e-6).segments
    by_island = hatch_polylines([left, right], pitch_mm=1.0, closure_tol_mm=1e-6, by_island=True).segments
    assert sorted(map(tuple, map(sorted, together))) == sorted(map(tuple, map(sorted, by_island)))
    sides = [segment[0][0] < 0 for segment in by_island]
    assert sum(a != b for a, b in zip(sides, sides[1:])) == 1


# ---- placement, registration, projects

def test_placement_scale_mirror_and_n_point_registration():
    placement = Placement(10, 5, 90, 2, True)
    assert placement.apply((1, 0)) == pytest.approx((10, 3))
    design = [(0, 0), (40, 0), (40, 30), (0, 30)]
    true = Placement(3.2, -1.5, 12.5)
    result = fit_placement(design, [true.apply(point) for point in design])
    assert result.placement.x == pytest.approx(3.2) and result.placement.angle == pytest.approx(12.5)
    assert result.worst < 1e-9 and result.spacing_error < 1e-9
    # One hole measured 0.3 mm off shows up as residual and spacing error, not as a silent shift.
    measured = [true.apply(point) for point in design]
    measured[2] = (measured[2][0] + 0.3, measured[2][1])
    noisy = fit_placement(design, measured)
    assert 0.1 < noisy.worst < 0.3 and noisy.spacing_error > 0.15
    assert noisy.placement.scale == 1
    scaled = fit_placement(design, [(x * 1.01, y * 1.01) for x, y in design], allow_scale=True)
    assert scaled.placement.scale == pytest.approx(1.01)
    with pytest.raises(ValueError, match="distinct"):
        fit_placement([(0, 0), (0, 0)], [(1, 1), (2, 2)])
    assert flipped_placement(Placement(4, 5, 10)) == Placement(4, 5, -10)


def test_project_round_trip_and_version_one_files(tmp_path):
    layer = LayerSpec("a", "a", VECTOR_ENGRAVE, 50, 45, 200, 1000)
    job = Job("J", (Artwork("A", (((0, 0), (1, 0)),), (), layer, COPPER_FILL),), Placement(1, 2, 3, 1.5, True),
              job_id="one", holes=False)
    path = tmp_path / "p.openlaser.json"
    save_project(path, [job])
    assert open_project(path) == [job]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    for key in ("scale", "mirror"):
        del data["jobs"][0]["placement"][key]
    del data["jobs"][0]["objects"][0]["kind"]
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = open_project(path)[0]
    assert loaded.placement == Placement(1, 2, 3) and loaded.objects[0].kind == ""


# ---- presets and patterns

def test_preset_library_defaults_migration_and_damaged_file(tmp_path):
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps({"version": 1, "recipes": [{
        "name": "Old copper", "stock": "FR4", "copper_um": 35, "machine": "B6", "lens": "150", "source": "JPT",
        "layer": starting_layer(COPPER_FILL).to_dict()}]}), encoding="utf-8")
    library = PresetLibrary(path)
    assert [preset.name for preset in library.user] == ["Old copper"]
    mine = library.add(Preset("", "Mine", COPPER_FILL, replace(starting_layer(COPPER_FILL), power_percent=77),
                              kerf_mm=0.06, default=True))
    other = library.add(Preset("", "Other", COPPER_FILL, starting_layer(COPPER_FILL), default=True))
    reloaded = PresetLibrary(path)
    assert [p.default for p in reloaded.user if p.preset_id in (mine.preset_id, other.preset_id)] == [False, True]
    assert reloaded.defaults()[COPPER_FILL].power_percent == 100 and reloaded.kerf() == 0
    reloaded.add(replace(mine, default=True))
    assert reloaded.defaults()[COPPER_FILL].power_percent == 77 and reloaded.kerf() == pytest.approx(0.06)
    assert len(reloaded.all()) == 3 + len(builtin_presets())
    with pytest.raises(ValueError):
        reloaded.add(Preset("", " ", COPPER_FILL, starting_layer(COPPER_FILL)))
    path.write_text("{not json", encoding="utf-8")
    damaged = PresetLibrary(path)
    assert damaged.error and damaged.user == []
    with pytest.raises(OSError):
        damaged.add(Preset("", "X", COPPER_FILL, starting_layer(COPPER_FILL)))
    assert path.read_text(encoding="utf-8") == "{not json"   # never overwritten


def test_patterns_build_and_stay_plannable():
    profile = b6_profile(base="none")
    base = starting_layer(COPPER_FILL)
    job = matrix(base, "power_percent", steps(40, 100, 4), "speed_mm_s", steps(500, 2000, 4))
    cells = [artwork for artwork in job.objects if artwork.name != "Labels"]
    assert len(cells) == 16
    assert {(a.layer.power_percent, a.layer.speed_mm_s) for a in cells} == {
        (p, s) for p in (40, 60, 80, 100) for s in (500, 1000, 1500, 2000)}
    assert job.objects[-1].layer.operation == VECTOR_ENGRAVE and job.objects[-1].paths
    compile_mark(job, profile, live=True)
    with pytest.raises(ValueError):
        matrix(base, "power_percent", [50], "power_percent", [60])
    with pytest.raises(ValueError):
        matrix(base, "power_percent", [150], "speed_mm_s", [500])
    assert steps(1, 5, 3, integer=True) == [1, 3, 5]
    ladder = kerf_ladder(base, starting_layer("copper_contour"))
    assert len(ladder.objects[0].fills) == 14 and len(ladder.objects[1].paths) == 3
    compile_mark(ladder, profile, live=True)
    grid = calibration_grid(100, 3)
    assert len(grid.objects[0].paths) == 9 * 2 + 1
    compile_mark(grid, profile, live=True)
    assert focus_test(base, 3).objects[0].layer.operation == VECTOR_ENGRAVE
    with pytest.raises(ValueError):
        focus_test(base, 12)
    assert text_paths("12.5%", 2, 0, 0) and text_paths("é", 2, 0, 0) == ()


@needs_factory_files
def test_ezcad_pen_library_gives_timing_and_the_operators_sets():
    from mopa_luiz.markparam import guess_kind, read_markparam

    library = read_markparam(FIXTURES / "MarkParam.lib")
    assert library.delays == {"delay_laser_on": -300, "delay_laser_off": 100, "delay_polygon": 100,
                              "delay_jump_short": 500, "delay_jump_long": 500}
    assert library.jump_speed == 4000
    sets = {pen.name: pen for pen in library.sets}
    assert {"PCBCut", "SMR", "PCBClean", "PCBEn"} <= set(sets)
    smr = sets["SMR"]
    assert (smr.passes, smr.speed_mm_s, smr.power_percent, smr.frequency_khz, smr.pulse_width_ns) == (1, 1000, 50, 40, 200)
    assert (sets["PCBEn"].power_percent, sets["PCBEn"].frequency_khz) == (97, 37)
    assert [guess_kind(name) for name in ("SMR", "PCBClean", "PCBEn")] == ["mask", "cleanup", ""]
    # A negative laser-on delay is valid and survives a save.
    profile = b6_profile(delays=library.delays, jump_speed=library.jump_speed)
    profile.validate(live=True)
    assert DeviceProfile.from_dict(profile.to_dict()).delays["delay_laser_on"] == -300
    with pytest.raises(ValueError, match="delay"):
        replace(profile, delays={"delay_polygon": -5}).validate()
