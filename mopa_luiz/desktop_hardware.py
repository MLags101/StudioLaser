"""Run one desktop operation per process, with all controller I/O on its spooler."""

from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import math
import multiprocessing as mp
import os
import queue
import threading
import time

from .cli import describe_usb, pyusb_probe, usb_candidates, usb_label
from .hardware_log import HardwareLog, build_id
from .live import (
    FRAME_LIGHT_SPEED_MM_S, WAIT_TIMEOUT_S, JobParams, LinkLost, OperatorStop, SimulatedDevice,
    claim_device, make_controller, mock_mode,
)
from .usb_lock import lock_galvo


CONNECT_SECONDS = 12.0
FRAME_ONCE_SECONDS = 2.0
FRAME_ONCE_MAX_COMMANDS = 20000
LONG_JUMP_MM = 2.0
STOP_GRACE_SECONDS = 15.0


@dataclass(frozen=True)
class TestOptions:
    seconds: float = 60.0


class MachineStateUnknown(RuntimeError):
    """Communication or cleanup failed before the board could confirm idle."""


def resolve_device(choice):
    devices, backend = usb_candidates()
    if not backend["available"]:
        raise ConnectionError(backend["note"])
    candidates = [(device, describe_usb(device)) for device in devices]
    if choice:
        candidates = [(device, info) for device, info in candidates if info["key"] == choice]
        if not candidates:
            raise ConnectionError(f"The selected laser ({usb_label(choice)}) is not connected.")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ConnectionError("No JCZ/LMC laser is connected")
    if choice:
        raise ConnectionError("More than one device matches the selected USB path.")
    raise ConnectionError("More than one laser is connected. Choose which one to use in the menu at the top right.")


def path_length_units(paths) -> float:
    length, previous = 0.0, None
    for path in paths:
        for point in path:
            if previous is not None:
                length += math.dist(previous, point)
            previous = point
    if previous is not None:
        length += math.dist(previous, paths[0][0])
    return length


def frame_once_repeats(paths, units_per_mm, light_speed=FRAME_LIGHT_SPEED_MM_S):
    seconds = max(path_length_units(paths) / units_per_mm / light_speed, 1e-3)
    commands = max(1, sum(map(len, paths)))
    return max(1, min(math.ceil(FRAME_ONCE_SECONDS / seconds), FRAME_ONCE_MAX_COMMANDS // commands, 500))


def trace(controller, paths):
    for path in paths:
        controller.checkpoint()
        controller.light_off()
        controller.dark(*path[0])
        for point in path[1:]:
            controller.checkpoint()
            controller.light(*point)


def frame_once(controller, paths, repeats):
    controller.lighting_configuration()
    for _ in range(repeats):
        trace(controller, paths)
    finish_list(controller)


def frame_loop(controller, read_frame, seconds=None):
    """Use one open list. Start the timer when ExecuteList receives a valid reply."""
    started = None
    laps = 0
    previous_callback = controller.on_execute

    def executed():
        nonlocal started
        if started is None:
            started = time.monotonic()
        if previous_callback is not None:
            previous_callback()

    controller.on_execute = executed
    controller.lighting_configuration()
    startup_deadline = time.monotonic() + WAIT_TIMEOUT_S
    next_stats = time.monotonic() + 10
    try:
        while True:
            controller.checkpoint()
            if started is None and time.monotonic() >= startup_deadline:
                raise TimeoutError("The frame did not start")
            if seconds is not None and started is not None and time.monotonic() - started >= seconds:
                return laps, time.monotonic() - started
            paths = read_frame()
            trace(controller, paths)
            laps += 1
            if time.monotonic() >= next_stats:
                controller.log(f"Framing: {laps} laps, {usb_stats(controller)}")
                next_stats = time.monotonic() + 10
    finally:
        controller.on_execute = previous_callback


def finish_list(controller):
    controller.initial_configuration()
    controller.outputs_off()
    controller.wait_finished()
    controller.log("List complete: outputs off, controller ready and idle")


def run_mark(controller, plan, progress):
    long_units = LONG_JUMP_MM * abs(controller.galvos_per_mm)
    for index, batch in enumerate(plan.batches, start=1):
        controller.wait_seconds = batch.timeout
        controller.deadline = time.monotonic() + batch.timeout + 10
        controller.power = batch.power
        controller.mark_speed = batch.speed
        controller.frequency = batch.frequency
        controller.pulse_width = batch.pulse_width
        last = None
        controller.marking_configuration()
        for path in batch.paths:
            controller.checkpoint()
            x, y = path[0]
            far = last is None or math.dist((x, y), last) > long_units
            controller.set_delay_jump(controller.delay_jump_long if far else controller.delay_jump_short)
            controller.list_jump(x, y)
            for point in path[1:]:
                controller.checkpoint()
                controller.mark(*point)
            last = path[-1]
        finish_list(controller)
        controller.log(f"Batch {index}/{len(plan.batches)} complete ({batch.name})")
        progress(index, len(plan.batches))
    controller.wait_seconds = WAIT_TIMEOUT_S
    controller.deadline = None


def open_controller(profile, device, stop, log, *, diagnostic=False):
    params = JobParams("", 0, profile.min_hz / 1000, profile.pulse_widths[0], 10, 1000)
    options = {}
    if profile.driver == "ezcad" and not diagnostic:
        options = {"light_speed": profile.frame_speed, "travel_speed": profile.jump_speed, **profile.delays}
    log(f"Field {profile.width:g} mm; software correction={profile.base}; "
        f"motion={'original' if diagnostic else profile.driver}")
    return make_controller(device, stop, log, profile.config(), params, field_size_mm=profile.width, options=options)


def usb_stats(controller):
    return (f"{controller.transfers} USB commands, longest wait {controller.longest_wait:.2f} s, "
            f"slowest command {controller.slowest_transfer * 1000:.0f} ms")


def test_step(send, log, name, action):
    started = time.monotonic()
    send(("test-step", (name, None, "")))
    try:
        detail = action() or ""
    except Exception as exc:
        took = time.monotonic() - started
        log(f"Test {name}: {exc} ({took:.2f} s)")
        send(("test-step", (name, False, f"{exc} ({took:.1f} s)")))
        raise
    took = time.monotonic() - started
    log(f"Test {name}: passed ({took:.2f} s) {detail}")
    send(("test-step", (name, True, f"{detail} ({took:.1f} s)".strip())))


def connection_test(controller, profile, options, step):
    def dot():
        # A finite list holds the visible guide at centre without filling the board's buffer.
        controller.lighting_configuration()
        controller.light_off()
        controller.dark(0x8000, 0x8000)
        controller.light_on()
        for _ in range(6):
            controller.list_delay_time(50_000)  # six 500 ms holds
        controller.light_off()
        before_execute = controller.on_execute
        started = None

        def executed():
            nonlocal started
            started = time.monotonic()

        controller.on_execute = executed
        try:
            finish_list(controller)
        finally:
            controller.on_execute = before_execute
        elapsed = time.monotonic() - started
        return f"3.00 s programmed; execute to verified idle {elapsed:.2f} s"

    def square():
        half = round(10 * profile.units_per_mm)
        low, high = 0x8000 - half, 0x8000 + half
        paths = (((low, low), (high, low), (high, high), (low, high), (low, low)),)
        laps, active = frame_loop(controller, lambda: paths, options.seconds)
        # The next step uses the same verified Stop as an operator ending a
        # continuous frame. The B6 can remain BUSY when a streamed list runs dry.
        return f"active {active:.2f} s; {laps} laps"

    step("Red dot at the centre for 3 s", dot)
    step(f"Frame a 20 mm square for {options.seconds:g} s", square)


def run_session(controller, action, profile, payload, read_frame, send, claim, step):
    """The sole spooler task owns claim, handshake, lists, stop and release."""
    diagnostic = action == "test"
    may_have_output = False
    identity = None
    with ExitStack() as session:
        try:
            controller.deadline = time.monotonic() + CONNECT_SECONDS

            def connect():
                nonlocal may_have_output, identity
                controller.checkpoint()
                session.enter_context(claim())
                identity = serial, firmware = controller.handshake()
                may_have_output = True
                controller.init_laser()
                controller.outputs_off()
                controller.wait_finished()
                send(("responding", f"Serial {serial}, firmware {firmware}"))
                return f"serial {serial}, firmware {firmware}"

            if diagnostic:
                step("Claim and handshake", connect)
            else:
                connect()
            controller.deadline = None

            if action in {"frame", "frame-loop"}:
                def frame_started():
                    controller.on_execute = None
                    send(("running", action))

                controller.on_execute = frame_started
            else:
                send(("running", action))

            if diagnostic:
                connection_test(controller, profile, payload, step)
                may_have_output = False
                try:
                    step("Verify stop", controller.verified_stop)
                except Exception as exc:
                    controller.log(f"Stop unverified: {exc}")
                    raise MachineStateUnknown(f"Stop could not be verified: {exc}") from exc
            elif action == "mark":
                run_mark(controller, payload, lambda n, total: send(("progress", (n, total))))
            elif action == "frame":
                controller.deadline = time.monotonic() + 30
                repeats = frame_once_repeats(payload, profile.units_per_mm, controller.light_speed)
                frame_once(controller, payload, repeats)
                controller.deadline = None
            else:
                frame_loop(controller, read_frame)
            controller.checkpoint()
        except LinkLost as exc:
            controller.log("Stop unverified: USB link failed")
            raise MachineStateUnknown(str(exc)) from exc
        except Exception as exc:
            if may_have_output:
                try:
                    controller.verified_stop()
                except Exception as stop_error:
                    controller.log(f"Stop unverified: {stop_error}")
                    raise MachineStateUnknown(f"{exc}. Stop could not be verified: {stop_error}") from stop_error
            raise
        finally:
            controller.discard_list()

    if diagnostic:
        def reconnect():
            with claim():
                controller.deadline = time.monotonic() + CONNECT_SECONDS
                serial, firmware = controller.handshake()
                initial_serial, initial_firmware = identity
                if (serial, firmware) != (initial_serial, initial_firmware):
                    raise ConnectionError("The controller identity changed after reconnect")
                controller.wait_finished()
                return f"serial {serial}, firmware {firmware}"

        step("Reconnect and handshake", reconnect)
    controller.log(f"Session complete: {usb_stats(controller)}")
    return "finished", action


def hardware_operation(pipe, action, profile, payload, ready, device=""):
    stop = threading.Event()
    done = threading.Event()
    frames = queue.SimpleQueue()
    messages = queue.SimpleQueue()
    simulated = mock_mode()
    log = HardwareLog(action != "detect" and not simulated)
    started = time.monotonic()
    frame = payload

    def listen():
        while not done.is_set():
            try:
                if not pipe.poll(0.05):
                    continue
                command, data = pipe.recv()
            except (OSError, EOFError):
                stop.set()
                return
            if command == "abort":
                log("Stop requested")
                stop.set()
                return
            if command == "frame":
                frames.put(data)

    def read_frame():
        nonlocal frame
        try:
            frame = frames.get_nowait()
        except queue.Empty:
            pass
        else:
            messages.put(("frame-ready", None))
        return frame

    def step(name, action):
        return test_step(messages.put, log, name, action)

    lease = None
    listener = None
    outcome = None
    try:
        if action == "detect":
            report = pyusb_probe()
            ready.send(("detected", (simulated or bool(report["devices"]), report.get("note", ""))))
            return
        if action not in {"test", "frame", "frame-loop", "mark"}:
            raise ValueError(f"Unknown hardware operation {action}")
        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        profile.validate(live=True)

        def find():
            if simulated:
                return SimulatedDevice(), {"key": "simulated", "address": None}
            return resolve_device(device)

        if action == "test":
            ready.send(("test-step", ("Find the laser", None, "")))
        try:
            bound, info = find()
        except Exception as exc:
            if action == "test":
                ready.send(("test-step", ("Find the laser", False, str(exc))))
            raise
        key = info["key"]
        log(f"=== {action}; build={build_id() if not simulated else 'mock'}; "
            f"{dependency_versions()}; USB path={key}, address={info['address']}")
        if action == "test":
            ready.send(("test-step", ("Find the laser", True, f"USB path {key}")))
        if not simulated:
            lease = lock_galvo(f"galvo-{key}")
        controller = open_controller(profile, bound, stop, log, diagnostic=action == "test")
        claim = nullcontext if simulated else lambda: claim_device(bound, log)
        ready.send(("status", f"Laser detected on {usb_label(key)}. Opening the controller…"
                    if not simulated else "Opening the simulated controller…"))

        def task(c):
            try:
                result = run_session(c, action, profile, payload, read_frame, messages.put, claim, step)
            except MachineStateUnknown as exc:
                result = ("unknown", f"Machine state unknown. {exc}. Use the machine's stop and verify it is idle.")
            except OperatorStop as exc:
                result = ("stopped", str(exc))
            except Exception as exc:
                result = ("error", str(exc) or type(exc).__name__)
            finally:
                c.discard_list()
            messages.put(result)
            return True

        controller.submit(task)
        spooler = controller._spooler_thread
        next_beat = 0.0
        last_transfers = 0
        while outcome is None:
            try:
                name, data = messages.get(timeout=0.05)
            except queue.Empty:
                if (spooler is None or not spooler.is_alive()) and messages.empty():
                    outcome = ("unknown", "The controller task ended without verifying the machine state.")
            else:
                if name in {"finished", "error", "stopped", "unknown"}:
                    outcome = (name, data)
                else:
                    ready.send((name, data))
            now = time.monotonic()
            if now >= next_beat and outcome is None and controller.transfers != last_transfers:
                last_transfers = controller.transfers
                ready.send(("heartbeat", None))
                next_beat = now + 1
        if spooler is not None:
            spooler.join()
    except Exception as exc:
        outcome = outcome or ("error", str(exc) or type(exc).__name__)
    finally:
        done.set()
        if listener is not None:
            listener.join()
        if lease is not None:
            lease.close()
        if outcome is not None:
            log(f"--- {outcome[0]} after {time.monotonic() - started:.1f} s: {outcome[1]}")
        log.close()
        try:
            if outcome is not None:
                ready.send(outcome)
        finally:
            pipe.close()
            ready.close()


def dependency_versions():
    values = []
    for package in ("galvoplotter", "pyusb", "libusb-package"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "unavailable"
        values.append(f"{package}={installed}")
    return ", ".join(values)


class HardwareWorker:
    """Parent-side process and pipe ownership, polled by the Qt event loop."""

    def __init__(self):
        self.process = None
        self.action = ""
        self._frame_pending = False
        self._deadline = 0
        self._abort_deadline = 0
        self._batch_timeouts = ()
        self._saw_result = False

    def start(self, action, profile=None, payload=None, device=""):
        if self.process is not None:
            raise RuntimeError("Wait for the current galvo operation to finish")
        context = mp.get_context("spawn")
        child_commands, self.commands = context.Pipe(duplex=False)
        self.results, child_results = context.Pipe(duplex=False)
        process = context.Process(
            target=hardware_operation, args=(child_commands, action, profile, payload, child_results, device),
        )
        try:
            process.start()
        except BaseException:
            self.commands.close()
            self.results.close()
            raise
        finally:
            child_commands.close()
            child_results.close()
        self.process = process
        self.action = action
        self._frame_pending = False
        self._deadline = time.monotonic() + CONNECT_SECONDS + 15
        self._abort_deadline = 0
        self._batch_timeouts = tuple(batch.timeout for batch in payload.batches) if action == "mark" else ()
        self._saw_result = False

    def update_frame(self, paths):
        if self.action == "frame-loop" and self.process and not self._frame_pending and not self._abort_deadline:
            try:
                self.commands.send(("frame", paths))
            except (BrokenPipeError, OSError):
                return False
            self._frame_pending = True
            return True
        return False

    def stop(self):
        if self.process and not self._abort_deadline:
            # The process stops the board itself; killing it mid-USB-transfer is what wedges an LMC board.
            self._abort_deadline = time.monotonic() + STOP_GRACE_SECONDS
            try:
                self.commands.send(("abort", None))
            except (BrokenPipeError, OSError):
                pass

    def poll(self):
        if self.process is None:
            return []
        alive = self.process.is_alive()
        events = []
        try:
            while self.results.poll():
                name, data = self.results.recv()
                if name == "frame-ready":
                    self._frame_pending = False
                if name == "heartbeat":
                    self._deadline = max(self._deadline, time.monotonic() + 25)
                if name == "test-step":   # the next step may be a connect, which can take a while
                    self._deadline = max(self._deadline, time.monotonic() + CONNECT_SECONDS + 15)
                if name == "running":
                    self._deadline = time.monotonic() + (
                        self._batch_timeouts[0] + 20 if self._batch_timeouts else 25
                    )
                if name == "progress":
                    completed, total = data
                    self._deadline = time.monotonic() + (
                        self._batch_timeouts[completed] + 20 if completed < total else 20
                    )
                if name in {"finished", "error", "stopped", "unknown"}:
                    self._saw_result = True
                events.append((name, data))
        except (EOFError, OSError):
            pass
        now = time.monotonic()
        if alive and not self._saw_result:
            if self._abort_deadline and now >= self._abort_deadline:
                self.process.terminate()
                self._saw_result = True
                self._abort_deadline = float("inf")
                events.append(("unknown", "The laser process did not stop by itself and was ended. The controller "
                                          "may need a power cycle; use the machine's own stop and check it."))
            elif not self._abort_deadline and now > self._deadline:
                self.stop()
                events.append(("timeout", "The laser stopped answering. Stopping the operation."))
        elif not alive:
            self.process.join()
            self.process.close()
            self.results.close()
            self.commands.close()
            self.process = None
            self.action = ""
            if not self._saw_result:
                events.append(("unknown", "Hardware worker exited before confirming the result. Verify the machine state."))
            events.append(("idle", None))
        return events

