from base64 import b64encode
from dataclasses import replace
import struct
import threading
import time

import pytest
from shapely import affinity
from shapely.geometry import Point, Polygon

from mopa_luiz.desktop_hardware import HardwareWorker, execute_plan, protect_connection, set_waits
from mopa_luiz.device_profile import DeviceProfile, import_ezcad, validate_cor
from mopa_luiz.job_planner import Arming, compile_frame, compile_mark
from mopa_luiz.pcb.board import InversionMode, detect_role, prepare_board, read_drills
from mopa_luiz.pcb.svg_exporter import export_to_svg
from mopa_luiz.svg_artwork import SvgSizeRequired, import_svg
from mopa_luiz.patterns import test_square as square
from mopa_luiz.workspace import Artwork, Job, Placement, new_layer, open_project, register, save_project


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def correction_bytes():
    return bytes(36 + 65 * 65 * 8)


def enabled_profile():
    return DeviceProfile(
        name="Test B6", source="Fixture JPT source", lens="Fixture lens", width=200, height=200,
        min_hz=1_000, max_hz=400_000, limits=((200, 20, 400),), enabled=True, base="cor",
        correction=b64encode(correction_bytes()).decode("ascii"),
    )


def test_svg_units_curves_transform_and_winding(tmp_path):
    svg = write(tmp_path / "vector.svg", """
        <svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 40 20">
          <g transform="translate(2 2)">
            <path fill-rule="evenodd" d="M 0,0 H 20 V 16 H 0 Z M 4,4 H 8 V 8 H 4 Z"/>
            <path fill="none" d="M 25,4 C 28,18 35,-10 37,4 A 1,1 0 0 1 38,5"/>
          </g>
        </svg>
    """)
    result = import_svg(svg)
    outer, hole, curve = result.vectors
    assert outer[0] == pytest.approx((-9, 4))
    assert outer[1][0] - outer[0][0] == pytest.approx(10)
    assert len(hole) == 5
    assert len(curve) > 10
    assert len(result.fills) == 2
    svg.write_text(svg.read_text().replace("evenodd", "nonzero"))
    assert len(import_svg(svg).fills) == 1
    ambiguous = write(tmp_path / "ambiguous.svg", '<svg viewBox="0 0 100 50"><rect width="50" height="20"/></svg>')
    with pytest.raises(SvgSizeRequired):
        import_svg(ambiguous)
    ring = import_svg(ambiguous, 40).fills[0]
    assert max(x for x, _ in ring) - min(x for x, _ in ring) == pytest.approx(40)


def pcb_fixture(tmp_path):
    header = "%FSLAX36Y36*%\n%MOMM*%\n%LPD*%\n"
    outline = write(tmp_path / "outline.gbr", header + """%TF.FileFunction,Profile,NP*%
%ADD10C,0.100*%
D10*
X10000000Y20000000D02*
X30000000Y20000000D01*
X30000000Y30000000D01*
X24000000Y30000000D01*
X24000000Y40000000D01*
X10000000Y40000000D01*
X10000000Y20000000D01*
X12000000Y32000000D02*
X15000000Y32000000D01*
X15000000Y35000000D01*
X12000000Y35000000D01*
X12000000Y32000000D01*
M02*
""")
    # Intentionally misleading filenames. X2 role metadata wins.
    top = write(tmp_path / "part-B_Cu.gbr", header + """%TF.FileFunction,Copper,L1,Top*%
%ADD10R,4.0X4.0*%
%ADD11C,0.50*%
D10*
X20000000Y25000000D03*
%LPC*%
D11*
X20000000Y25000000D03*
M02*
""")
    bottom = write(tmp_path / "part-F_Cu.gbr", header + """%TF.FileFunction,Copper,L2,Bot*%
%TF.FilePolarity,Negative*%
%ADD10R,3.0X5.0*%
D10*
X25000000Y25000000D03*
M02*
""")
    drill = write(tmp_path / "holes.drl", """M48
METRIC
G90
T01C1.0
%
T01
X14.0Y24.0
X17.0Y25.0G85X19.0Y25.0
M30
""")
    return {"outline": [outline], "top_copper": [top], "bottom_copper": [bottom], "drill": [drill]}


def test_double_sided_outline_drills_negative_polarity_and_svg_parity(tmp_path):
    files = pcb_fixture(tmp_path)
    assert detect_role(files["top_copper"][0]) == "top_copper"
    assert detect_role(files["bottom_copper"][0]) == "bottom_copper"
    front, back = prepare_board(files, mode=InversionMode.FULL_INVERT)
    assert front.origin == back.origin == (20, 30)
    assert front.references == ((14.0, 24.0),)
    assert back.references == ((26.0, 24.0),)
    assert front.outline.area == pytest.approx(331)
    assert front.boundary.area < front.outline.area
    assert read_drills(files["drill"])[0].covers(Point(18, 25))
    assert back.boundary.equals_exact(affinity.scale(front.boundary, -1, 1, origin=front.origin), 1e-8)
    for point in [(14, 24), (18, 25), (13, 33), (28, 33)]:
        assert not front.removal.covers(Point(point))
    assert not front.retained.covers(Point(20, 25))  # clear flash in a dark pad
    assert front.removal.covers(Point(20, 25))
    assert back.removal.covers(Point(15, 25))
    isolated = prepare_board(files, mode=InversionMode.ISOLATION)[0]
    assert isolated.removal.area < front.removal.area
    assert isolated.removal.difference(front.removal).area < 1e-8
    svg = tmp_path / "output.svg"
    export_to_svg(back.removal, str(svg), view_bounds=back.outline.bounds)
    fills = import_svg(svg).fills
    assert len(fills) == 1
    assert Polygon(fills[0]).symmetric_difference(affinity.translate(back.removal, -20, -30)).area < 0.002


def test_profile_import_rejects_bad_correction_and_retains_axes(tmp_path):
    config = write(tmp_path / "markcfg7", """[LMC_CFG]
FieldSize=150
MINPWMFREQ=1000
MAXPWMFREQ=300000
PULSEWIDTHS=10, 200
INVERTX=1
LaserOnDelay=15
DoorInPort=2
""")
    correction = tmp_path / "lens.cor"
    correction.write_bytes(correction_bytes())
    profile, report = import_ezcad(config, correction)
    assert "lens.cor" in report
    assert profile.pulse_widths == (10, 200) and profile.base == "cor"
    assert profile.width == 150 and profile.delays == {"delay_laser_on": 15}
    assert profile.galvo_point((-75, 0))[0] == 65535
    assert profile.galvo_point((75, 0))[0] == 0
    profile = replace(profile, lens="150", source="Fixture JPT", enabled=True)
    path = tmp_path / "lens.json"
    profile.save(path)
    assert DeviceProfile.open(path) == profile
    with pytest.raises(ValueError):
        validate_cor(correction_bytes()[:-1])
    validate_cor(correction_bytes() + bytes(4))   # EZCAD's JCZ_COR_2_1 files carry a short trailer


class RecordingGalvo:
    is_connected = True
    delay_jump_short = 8
    delay_jump_long = 200
    galvos_per_mm = 327.675

    def __init__(self):
        self.calls = []
        self.jump_delays = []

    def marking_configuration(self):
        pass

    def initial_configuration(self):
        pass

    def is_ready(self):
        return True

    def is_busy(self):
        return False

    def is_ready_and_not_busy(self):
        return True

    def set_delay_jump(self, delay):
        self.jump_delays.append(delay)

    def list_jump(self, x, y):
        self.calls.append(("jump", (x, y)))

    def mark(self, x, y):
        self.calls.append(("mark", (x, y)))

    def send(self, data, read=True):
        return (-1, -1, -1, -1)


def make_job(paths):
    return Job("PCB", (Artwork("Artwork", paths, paths, replace(new_layer(), power_percent=5)),), job_id="test")


def test_dense_job_keeps_every_path_and_long_contour_in_order():
    from galvo.consts import listJumpTo, listMarkTo
    from galvo.controller import GalvoController

    paths = tuple(((i / 1000, 0), (i / 1000, 5)) for i in range(30_000))
    long = tuple((i / 100, -5) for i in range(4501))
    profile = enabled_profile()
    plan = compile_mark(make_job((*paths, long)), profile, live=True)
    assert plan.paths == 30_001 and len(plan.batches[-1].paths) == 1
    assert plan.batches[-1].paths[0] == tuple(map(profile.galvo_point, long))
    recorder = RecordingGalvo()
    progress = []
    execute_plan(recorder, plan, threading.Event(), lambda *counts: progress.append(counts))
    expected = [
        ("jump" if i == 0 else "mark", profile.galvo_point(point))
        for path in (*paths, long) for i, point in enumerate(path)
    ]
    assert recorder.calls == expected
    assert progress[-1] == (len(plan.batches), len(plan.batches))

    wire = []

    class PacketConnection:
        def is_open(self, index):
            return True

        def write(self, index, packet):
            if len(packet) == 0xC00:
                for code, x, y, *_ in struct.iter_unpack("<6H", packet):
                    if code in {listJumpTo, listMarkTo}:
                        wire.append(("jump" if code == listJumpTo else "mark", (x, y)))

        def read(self, index):
            return struct.pack("<4H", 0, 0, 0, 0x20)

    driver = GalvoController(mock=True, galvos_per_mm=65535 / profile.width)
    driver.connection = PacketConnection()
    execute_plan(driver, plan, threading.Event(), lambda *_: None)
    assert wire == expected


def test_preflight_crosshatch_and_late_error():
    profile = enabled_profile()
    layer = replace(new_layer(), power_percent=5, crosshatch=True, raster_pitch_mm=1)
    job = square(10, layer, filled=True)
    plan = compile_mark(job, profile, live=True)
    assert plan.paths == 20
    assert any(abs(a[1] - b[1]) < 1e-8 for a, b in plan.preview)
    assert any(abs(a[0] - b[0]) < 1e-8 for a, b in plan.preview)
    outside = Artwork("Late operation", (((0, 0), (101, 0)),), (), layer)
    with pytest.raises(ValueError, match="outside"):
        compile_mark(replace(job, objects=(*job.objects, outside)), profile, live=True)
    with pytest.raises(ValueError, match="beyond the field"):
        compile_frame((((0, 0), (100, 0)),), replace(profile, red_x=1))


def test_arm_expiration_one_shot_registration_and_project_resume(tmp_path):
    clock = [0]
    arming = Arming(clock=lambda: clock[0])
    arming.set(1, "plan")
    assert arming.take(1) == "plan"
    with pytest.raises(PermissionError):
        arming.take(1)
    arming.set(1, "plan")
    clock[0] = 31
    with pytest.raises(PermissionError):
        arming.take(1)
    arming.set(1, "plan")
    with pytest.raises(PermissionError):
        arming.take(2)
    pose, error = register(((0, 0), (5, 0)), ((2, 3), (2, 8)))
    assert error == 0 and pose.angle == 90
    assert pose.apply((5, 0)) == pytest.approx((2, 8))
    job = replace(make_job((((0, 0), (2, 0)),)), placement=pose, status="running")
    path = tmp_path / "queue.openlaser.json"
    save_project(path, [job])
    assert open_project(path) == [replace(job, status="interrupted")]


def test_stop_disconnect_and_timeout_do_not_continue_a_batch():
    plan = compile_mark(make_job((((0, 0), (1, 0)), ((0, 1), (1, 1)))), enabled_profile(), live=True)
    controller = RecordingGalvo()
    stop = threading.Event()
    record = controller.mark

    def stop_after_mark(*point):
        record(*point)
        stop.set()

    controller.mark = stop_after_mark
    with pytest.raises(InterruptedError):
        execute_plan(controller, plan, stop, lambda *_: pytest.fail("stopped batch reported complete"))
    assert len(controller.calls) == 2
    protect_connection(controller)
    with pytest.raises(ConnectionError, match="acknowledge"):
        controller.send(b"test")
    controller.is_connected = False
    with pytest.raises(ConnectionError, match="Lost"):
        controller.send(b"test")
    controller.is_ready = lambda: False
    set_waits(controller, threading.Event(), 0)
    with pytest.raises(TimeoutError):
        controller.wait_ready()


def test_spawned_mock_hardware_worker_runs_then_aborts_frame(monkeypatch):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    profile = enabled_profile()
    worker = HardwareWorker()

    def wait_for(event, after=None):
        deadline = time.monotonic() + 12
        events = []
        while time.monotonic() < deadline:
            events.extend(worker.poll())
            if any(kind == event for kind, _ in events):
                if after:
                    after()
                return events
            time.sleep(0.025)
        pytest.fail(str(events))

    try:
        plan = compile_mark(make_job((((0, 0), (1, 1)),)), profile, live=True)
        worker.start("mark", profile, plan)
        assert ("finished", "mark") in wait_for("idle")
        frame = compile_frame((((0, 0), (1, 1)),), profile)
        worker.start("frame-loop", profile, frame)
        wait_for("running")
        assert worker.update_frame(frame)
        wait_for("frame-ready")
        worker.stop()
        assert any(kind == "stopped" for kind, _ in wait_for("idle"))
    finally:
        if worker.process:
            worker.stop()
            wait_for("idle")


def test_long_jumps_get_the_long_settle_delay():
    plan = compile_mark(make_job((((0, 0), (1, 0)), ((1.5, 0), (2, 0)), ((50, 0), (51, 0)))),
                        enabled_profile(), live=True)
    controller = RecordingGalvo()
    execute_plan(controller, plan, threading.Event(), lambda *_: None)
    assert controller.jump_delays == [200, 8, 200]   # first move, short hop, long hop


def test_lights_out_clears_the_red_pointer_and_laser_gate():
    from galvo.controller import GalvoController

    from mopa_luiz.desktop_hardware import lights_out

    driver = GalvoController(mock=True)
    driver.port_on(driver.light_pin)
    driver.port_on(driver.laser_pin)
    written = []
    driver.write_port = lambda: written.append(driver._port_bits)
    lights_out(driver)
    assert not driver.is_port(driver.light_pin) and not driver.is_port(driver.laser_pin)
    assert written and written[-1] & (1 << driver.light_pin) == 0
