from base64 import b64encode
from dataclasses import replace
from pathlib import Path
import struct
import threading
import time

import pytest
from shapely import affinity
from shapely.geometry import Point, Polygon

from mopa_luiz.desktop_hardware import HardwareWorker
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


class WireConnection:
    """A PyUSB-shaped connection that records galvoplotter's real packets."""

    def __init__(self):
        self.commands = []
        self.packets = 0
        self.wire = []

    def write(self, endpoint, packet, timeout):
        from galvo.consts import listJumpTo, listMarkTo

        if len(packet) == 0xC00:
            self.packets += 1
            for code, x, y, *_ in struct.iter_unpack("<6H", bytes(packet)):
                if code in {listJumpTo, listMarkTo}:
                    self.wire.append(("jump" if code == listJumpTo else "mark", (x, y)))
            time.sleep(0.001)
        else:
            self.commands.append(struct.unpack("<H", bytes(packet[:2]))[0])
        return len(packet)

    def read(self, endpoint, size, timeout):
        return struct.pack("<4H", 0, 7, 9, 0x20)


def mock_driver(profile):
    from mopa_luiz.desktop_hardware import open_controller

    return open_controller(profile, WireConnection(), threading.Event(), lambda text: None)

def make_job(paths):
    return Job("PCB", (Artwork("Artwork", paths, paths, replace(new_layer(), power_percent=5)),), job_id="test")


def test_dense_job_keeps_every_path_and_long_contour_in_order():
    from mopa_luiz.desktop_hardware import run_mark

    paths = tuple(((i / 1000, 0), (i / 1000, 5)) for i in range(30_000))
    long = tuple((i / 100, -5) for i in range(4501))
    profile = enabled_profile()
    plan = compile_mark(make_job((*paths, long)), profile, live=True)
    assert plan.paths == 30_001 and len(plan.batches[-1].paths) == 1
    assert plan.batches[-1].paths[0] == tuple(map(profile.galvo_point, long))
    driver = mock_driver(profile)
    progress = []
    run_mark(driver, plan, lambda *counts: progress.append(counts))
    expected = [
        ("jump" if i == 0 else "mark", profile.galvo_point(point))
        for path in (*paths, long) for i, point in enumerate(path)
    ]
    assert driver.device.wire == expected
    assert progress[-1] == (len(plan.batches), len(plan.batches))


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
    path = tmp_path / "queue.studiolaser.json"
    save_project(path, [job])
    assert open_project(path) == [replace(job, status="interrupted")]


def test_stop_ends_a_mark_without_reporting_the_batch_complete():
    from mopa_luiz.desktop_hardware import run_mark

    plan = compile_mark(make_job((((0, 0), (1, 0)), ((0, 1), (1, 1)))), enabled_profile(), live=True)
    driver = mock_driver(enabled_profile())
    mark = driver.mark

    def stop_after_mark(*point):
        mark(*point)
        driver.stop.set()

    driver.mark = stop_after_mark
    with pytest.raises(InterruptedError):
        run_mark(driver, plan, lambda *_: pytest.fail("stopped batch reported complete"))


def test_a_board_that_never_gets_ready_stops_the_job_instead_of_being_written_to():
    from mopa_luiz.desktop_hardware import frame_once

    driver = mock_driver(enabled_profile())
    driver.wait_seconds = 0.05
    driver.device.read = lambda *args, **kwargs: struct.pack("<4H", 0, 7, 9, 0)
    with pytest.raises(TimeoutError, match="ready"):
        frame_once(driver, (((1000, 1000), (9000, 1000)),), 1)
    assert driver.device.packets == 0


@pytest.mark.parametrize("slots", [1, 2, 3])
def test_streaming_starts_when_preload_fills_without_writing_past_ready(slots):
    from galvo.consts import ExecuteList, GetVersion

    from mopa_luiz.desktop_hardware import frame_loop

    driver = mock_driver(enabled_profile())
    wire = driver.device
    write = wire.write
    executing = False
    ready = True
    started_at = []
    reports = []
    driver.log = reports.append
    driver.wait_seconds = 0.1

    def send(endpoint, packet, timeout):
        nonlocal executing, ready
        if len(packet) == 0xC00:
            assert ready, "driver wrote another list packet without READY"
        elif int.from_bytes(packet[:2], "little") == ExecuteList:
            assert not executing
            started_at.append(wire.packets)
            executing = True
        return write(endpoint, packet, timeout)

    def reply(endpoint, size, timeout):
        nonlocal ready
        if wire.commands[-1] == GetVersion:
            ready = wire.packets < slots or executing
        return struct.pack("<4H", 0, 33827, 9, 0x0220 if ready else 0x0200)

    wire.write, wire.read = send, reply
    laps, _ = frame_loop(driver, lambda: (((1000, 1000), (9000, 1000)),), seconds=0.02)
    assert started_at == [slots] and wire.packets > slots and laps > 0
    assert any("before start" in line for line in reports) == (slots < 3)


def test_running_list_that_loses_ready_is_never_restarted_or_written_to():
    from galvo.consts import ExecuteList, GetVersion

    from mopa_luiz.desktop_hardware import frame_loop

    driver = mock_driver(enabled_profile())
    wire = driver.device
    driver.wait_seconds = 0.03

    def reply(endpoint, size, timeout):
        blocked = wire.commands[-1] == GetVersion and wire.packets > 0
        return struct.pack("<4H", 0, 33827, 9, 0x0204 if blocked else 0x0220)

    wire.read = reply
    with pytest.raises(TimeoutError, match=r"status 0x0204, 1 list packets sent, list executing"):
        frame_loop(driver, lambda: (((1000, 1000), (9000, 1000)),))
    assert wire.packets == 1
    assert wire.commands.count(ExecuteList) == 1


def test_first_failed_transfer_stops_the_frame_and_logs_what_led_to_it():
    from mopa_luiz.desktop_hardware import frame_loop
    from mopa_luiz.live import LinkLost

    driver = mock_driver(enabled_profile())
    wire = driver.device
    write = wire.write
    attempts = []
    log = []
    driver.log = log.append

    def failing_write(endpoint, packet, timeout):
        if wire.packets >= 5 and len(packet) == 0xC00:
            attempts.append((endpoint, timeout))
            return len(packet) - 1
        return write(endpoint, packet, timeout)

    wire.write = failing_write
    with pytest.raises(LinkLost, match="short write"):
        frame_loop(driver, lambda: (((1000, 1000), (9000, 1000), (1000, 1000)),))
    assert len(attempts) == 1 and attempts[0][1] == 2000
    assert any("USB write failed for list packet" in line for line in log)
    assert any("GetVersion" in line for line in log)
    sent = len(wire.commands)
    with pytest.raises(LinkLost):
        driver.get_version()
    assert len(wire.commands) == sent

def test_spawned_mock_hardware_worker_runs_then_aborts_frame(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    monkeypatch.setenv("STUDIOLASER_LOG_DIR", str(tmp_path))
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
        worker.start("frame", profile, frame)
        assert ("finished", "frame") in wait_for("idle")
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


def test_connection_test_runs_every_step_against_the_simulated_laser(monkeypatch, tmp_path):
    from mopa_luiz.desktop_hardware import TestOptions

    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    monkeypatch.setenv("STUDIOLASER_LOG_DIR", str(tmp_path))
    worker = HardwareWorker()
    worker.start("test", enabled_profile(), TestOptions(seconds=1))
    events = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(kind == "idle" for kind, _ in events):
        events.extend(worker.poll())
        time.sleep(0.025)
    finished = [data for kind, data in events if kind == "test-step" and data[1] is not None]
    assert len(finished) == 6 and all(ok for _, ok, _ in finished), events
    assert ("finished", "test") in events


def test_long_jumps_get_the_long_settle_delay():
    from mopa_luiz.desktop_hardware import run_mark

    plan = compile_mark(make_job((((0, 0), (1, 0)), ((1.5, 0), (2, 0)), ((50, 0), (51, 0)))),
                        enabled_profile(), live=True)
    driver = mock_driver(enabled_profile())
    delays = []
    set_delay = driver.set_delay_jump
    driver.set_delay_jump = lambda delay: (delays.append(delay), set_delay(delay))
    run_mark(driver, plan, lambda *_: None)
    assert delays == [200, 8, 200]   # first move, short hop, long hop (galvoplotter's own defaults)


def test_frame_keeps_one_open_list_and_follows_moves_without_restarting():
    from galvo.consts import ExecuteList, GetMarkTime, ResetList, StopExecute, WriteCorLine

    from mopa_luiz.desktop_hardware import frame_loop

    driver = mock_driver(enabled_profile())
    wire = driver.device
    frame = [(((1000, 1000), (9000, 1000), (9000, 9000), (1000, 1000)),)]

    def run():
        with pytest.raises(InterruptedError):
            frame_loop(driver, lambda: frame[0])

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while wire.packets < 20 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wire.packets >= 20
        frame[0] = (((2000, 2000), (8000, 2000), (2000, 2000)),)
        moved = len(wire.wire)
        while ("jump", (8000, 2000)) not in wire.wire[moved:] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ("jump", (8000, 2000)) in wire.wire[moved:]
    finally:
        driver.stop.set()
        worker.join(5)
    assert wire.commands.count(ResetList) == 1 and wire.commands.count(ExecuteList) == 1
    assert StopExecute not in wire.commands and GetMarkTime not in wire.commands
    assert WriteCorLine not in wire.commands


def test_connection_test_uses_original_motion_and_software_correction():
    from mopa_luiz.desktop_hardware import open_controller

    profile = replace(enabled_profile(), correction=b64encode(correction_bytes()).decode(), base="cor",
                      driver="ezcad", jump_speed=4000)
    args = profile, WireConnection(), threading.Event(), lambda text: None
    regular = open_controller(*args)
    diagnostic = open_controller(*args, diagnostic=True)
    assert regular.travel_speed == 4000
    assert diagnostic.travel_speed == diagnostic.light_speed == 3000
    assert regular.cor_file is None and diagnostic.cor_file is None


def test_diagnostic_handoff_waits_for_drain_in_one_spooler_task():
    from contextlib import nullcontext

    from galvo.consts import ExecuteList
    from mopa_luiz import desktop_hardware as dh

    driver = mock_driver(enabled_profile())
    wire = driver.device
    drain = threading.Event()
    draining = threading.Event()
    results = []
    owners = set()
    read = wire.read

    def status(endpoint, size, timeout):
        owners.add(threading.get_ident())
        if wire.commands.count(ExecuteList) == 1 and not drain.is_set():
            draining.set()
            return struct.pack("<4H", 0, 7, 9, 0x24)
        return read(endpoint, size, timeout)

    wire.read = status
    finished = threading.Event()

    def step(name, action):
        results.append((name, "started"))
        action()
        results.append((name, "passed"))

    def task(c):
        try:
            results.append(dh.run_session(c, "test", enabled_profile(), dh.TestOptions(0.05), None,
                                          results.append, nullcontext, step))
        finally:
            finished.set()
        return True

    driver.submit(task)
    try:
        assert draining.wait(3)
        assert not any(name.startswith("Frame a 20") for name, _ in results)
        assert len(driver._queue) == 1
    finally:
        drain.set()
    assert finished.wait(5)
    assert results[-1] == ("finished", "test")
    assert len(owners) == 1 and wire.commands.count(ExecuteList) == 2


@pytest.mark.parametrize("stop_fails", [False, True])
def test_connection_test_verifies_stop_when_continuous_frame_stays_busy(stop_fails):
    from contextlib import nullcontext

    from galvo.consts import ExecuteList, GetSerialNo, StopExecute
    from mopa_luiz import desktop_hardware as dh

    profile = enabled_profile()
    driver = mock_driver(profile)
    wire = driver.device
    driver.wait_seconds = 0.05
    steps = []
    log = []
    driver.log = log.append

    def status(endpoint, size, timeout):
        if stop_fails and wire.commands[-1] == StopExecute:
            raise OSError("Lost Stop acknowledgement")
        # The dot drains, but the continuous square remains BUSY with READY set
        # until an explicit StopExecute, as in the B6 report.
        busy = wire.commands.count(ExecuteList) == 2 and StopExecute not in wire.commands
        return struct.pack("<4H", 0, 7, 9, 0x0224 if busy else 0x0220)

    wire.read = status

    def step(name, action):
        steps.append(name)
        action()

    def run():
        return dh.run_session(driver, "test", profile, dh.TestOptions(0.02), None,
                              lambda event: None, nullcontext, step)

    if stop_fails:
        with pytest.raises(dh.MachineStateUnknown, match="Stop could not be verified"):
            run()
        assert steps[-1] == "Verify stop" and wire.commands[-1] == StopExecute
    else:
        assert run() == ("finished", "test")
        assert steps[-2:] == ["Verify stop", "Reconnect and handshake"]
        assert GetSerialNo in wire.commands[wire.commands.index(StopExecute) + 1:]
        assert any("Stop verified" in entry for entry in log)
    assert wire.commands.count(StopExecute) == 1


def test_mark_drain_failure_does_not_advance_to_the_next_batch():
    from contextlib import nullcontext
    from galvo.consts import GetVersion, ExecuteList
    from mopa_luiz.desktop_hardware import MachineStateUnknown, run_session

    profile = enabled_profile()
    plan = compile_mark(make_job((((0, 0), (1, 1)),)), profile, live=True)
    plan = replace(plan, batches=plan.batches * 2)
    driver = mock_driver(profile)
    wire = driver.device
    read = wire.read
    events = []

    def fail_drain(endpoint, size, timeout):
        if ExecuteList in wire.commands and wire.commands[-1] == GetVersion:
            return b"\x00" * 4
        return read(endpoint, size, timeout)

    wire.read = fail_drain
    with pytest.raises(MachineStateUnknown, match="invalid 4-byte reply"):
        run_session(driver, "mark", profile, plan, None, events.append, nullcontext, None)
    assert not any(kind == "progress" for kind, _ in events)
    assert wire.commands[-1] == GetVersion and wire.commands.count(ExecuteList) == 1


def test_stop_during_pending_transfer_uses_same_io_owner_and_sends_no_list():
    from contextlib import nullcontext
    from galvo.consts import GetVersion, ResetList, StopExecute, WritePort
    from mopa_luiz.desktop_hardware import run_session

    profile = enabled_profile()
    driver = mock_driver(profile)
    wire = driver.device
    entered = threading.Event()
    release = threading.Event()
    owners = set()
    read = wire.read

    def block_first_ready(endpoint, size, timeout):
        owners.add(threading.get_ident())
        if ResetList in wire.commands and wire.commands[-1] == GetVersion and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return read(endpoint, size, timeout)

    wire.read = block_first_ready
    outcomes = []
    paths = (((1000, 1000), (9000, 1000)),)

    def run():
        try:
            run_session(driver, "frame", profile, paths, None, outcomes.append, nullcontext, None)
        except InterruptedError:
            outcomes.append(("stopped", None))

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        driver.stop.set()
    finally:
        release.set()
        worker.join(5)
    assert outcomes[-1][0] == "stopped"
    assert not any(kind == "running" for kind, _ in outcomes)
    assert wire.packets == 0 and len(owners) == 1
    assert StopExecute in wire.commands and WritePort in wire.commands[wire.commands.index(StopExecute):]
