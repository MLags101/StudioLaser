"""One desktop hardware operation at a time, with cooperative Windows abort."""

from base64 import b64decode
from contextlib import nullcontext, redirect_stdout
from functools import partial
import multiprocessing as mp
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

from .cli import pyusb_probe
from .job_planner import MarkPlan
from .live import JobParams, connect_controller, mock_mode


# The 65 x 65 correction table is 4225 USB round trips; allow for a slow WinUSB link.
CONNECT_SECONDS = 45.0


def check_stop(stop):
    if stop.is_set():
        raise InterruptedError("Operator stopped the hardware operation")


def set_waits(controller, stop, timeout):
    def wait_for(predicate, state):
        deadline = time.monotonic() + timeout
        while True:
            check_stop(stop)
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"The galvo did not report {state}")
            stop.wait(0.01)

    controller.wait_ready = lambda: wait_for(controller.is_ready, "ready")
    controller.wait_idle = lambda: wait_for(lambda: not controller.is_busy(), "idle")
    controller.wait_finished = lambda: wait_for(controller.is_ready_and_not_busy, "completion")


def stream_paths(paths, jump, draw, stop):
    for path in paths:
        points = iter(path)
        check_stop(stop)
        jump(*next(points))
        for point in points:
            check_stop(stop)
            draw(*point)


LONG_JUMP_MM = 2.0   # jumps longer than this get the long settle delay


def execute_plan(controller, plan: MarkPlan, stop, progress):
    long_units = LONG_JUMP_MM * abs(controller.galvos_per_mm)
    last = [None]

    def jump(x, y):
        # Settle longer after a long move, or the first millimetre of the mark is bent.
        far = last[0] is None or abs(complex(x, y) - complex(*last[0])) > long_units
        controller.set_delay_jump(controller.delay_jump_long if far else controller.delay_jump_short)
        controller.list_jump(x, y)
        last[0] = (x, y)

    def draw(x, y):
        controller.mark(x, y)
        last[0] = (x, y)

    for index, batch in enumerate(plan.batches, start=1):
        check_stop(stop)
        set_waits(controller, stop, batch.timeout)
        controller.power = batch.power
        controller.mark_speed = batch.speed
        controller.frequency = batch.frequency
        controller.pulse_width = batch.pulse_width
        controller.marking_configuration()
        # Each planned path starts with a pen-up move, including the first
        # path when the driver's cached start position happens to match it.
        stream_paths(batch.paths, jump, draw, stop)
        check_stop(stop)
        controller.initial_configuration()
        controller.wait_finished()
        progress(index, len(plan.batches))


def protect_connection(controller):
    """A lost command must fail the job instead of reconnecting mid-list."""
    send = controller.send

    def checked_send(data, read=True):
        if not controller.is_connected:
            raise ConnectionError("Lost the galvo USB connection")
        reply = send(data, read)
        if reply == (-1, -1, -1, -1):
            raise ConnectionError("The galvo did not acknowledge a USB operation")
        return reply

    controller.send = checked_send
    return send


def lights_out(controller):
    """Leave the red pointer and the laser gate off, whatever state the driver thinks it is in."""
    controller.port_off(controller.light_pin)
    controller.port_off(controller.laser_pin)
    controller.write_port()


def resolve_device(choice: str) -> tuple[int, str]:
    """galvoplotter opens "device number N" in libusb's order; turn the operator's choice into N.

    Refuses rather than guessing: if the chosen laser is gone, the other one must not be opened.
    """
    from .cli import usb_label

    devices = pyusb_probe()["devices"]
    if choice:
        for index, device in enumerate(devices):
            if device["key"] == choice:
                return index, choice
        raise ConnectionError(f"The selected laser ({usb_label(choice)}) is not connected. Plug it in, or choose "
                              "another laser in the menu at the top right.")
    if len(devices) == 1:
        return 0, devices[0]["key"]
    if not devices:
        raise ConnectionError("No JCZ/LMC laser is connected")
    raise ConnectionError("More than one laser is connected. Choose which one to use in the menu at the top right.")


def hardware_operation(pipe, action, profile, payload, ready, device=""):
    stop = threading.Event()
    frame = [payload]
    pending_frame = threading.Event()
    done = threading.Event()

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
                stop.set()
                return
            if command == "frame":
                frame[0] = data
                pending_frame.set()

    controller = None
    error = None
    cleanup_failures = []
    try:
        if action == "detect":
            report = pyusb_probe()
            ready.send(("detected", (mock_mode() or bool(report["devices"]), report.get("note", ""))))
            return
        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        profile.validate(live=True)
        machine_index, device_key = (0, "simulated") if mock_mode() else resolve_device(device)
        params = JobParams("", 0, profile.min_hz / 1000, profile.pulse_widths[0], 10, 1000)
        original_send = None
        with TemporaryDirectory(prefix="studiolaser-") as directory:
            # Only a factory .cor goes to the board. Other bases upload a blank
            # table and correct in software (DeviceProfile.galvo_point).
            correction = None
            if profile.base == "cor":
                correction = Path(directory) / "active.cor"
                correction.write_bytes(b64decode(profile.correction))
                ready.send(("status", "Uploading the lens correction to the controller…"))
            try:
                check_stop(stop)
                simulated = mock_mode()
                with open(os.devnull, "w") if simulated else nullcontext() as sink:
                    with redirect_stdout(sink) if simulated else nullcontext():
                        controller = connect_controller(
                            profile.config(), params, CONNECT_SECONDS, field_size_mm=profile.width,
                            lock_name=f"galvo-{device_key}",
                            options={"machine_index": machine_index,
                                     "cor_file": str(correction) if correction else None,
                                     "light_speed": profile.frame_speed, "travel_speed": profile.jump_speed,
                                     "galvos_per_mm": profile.units_per_mm, **profile.delays},
                        )
                if simulated:
                    controller.connection.send = None
                    controller.connection.recv = None
                    controller.status = lambda: 0x20
                else:
                    # galvoplotter 0.2 retries a transfer through a USB reset
                    # by default. An active mark must fail on the first error.
                    connection = controller.connection
                    connection.write = partial(connection.write, attempt=4)
                    connection.read = partial(connection.read, attempt=4)
                original_send = protect_connection(controller)
                check_stop(stop)
                ready.send(("running", action))
                if action == "mark":
                    execute_plan(controller, payload, stop, lambda n, total: ready.send(("progress", (n, total))))
                else:
                    set_waits(controller, stop, 15)
                    deadline = time.monotonic() + 2
                    last_heartbeat = 0
                    while not stop.is_set() and (action == "frame-loop" or time.monotonic() < deadline):
                        if pending_frame.is_set():
                            pending_frame.clear()
                            ready.send(("frame-ready", None))
                        paths = frame[0]
                        controller.lighting_configuration()
                        stream_paths(paths, controller.dark, controller.light, stop)
                        controller.light_off()
                        check_stop(stop)
                        controller.initial_configuration()
                        controller.wait_finished()
                        now = time.monotonic()
                        if now - last_heartbeat >= 1:
                            ready.send(("heartbeat", None))
                            last_heartbeat = now
                        stop.wait(0.04)
                    check_stop(stop)
            finally:
                if controller is not None:
                    # Cleanup must finish even after Stop: its waits get their own short deadline and
                    # ignore the stop request (which is why we are here).
                    set_waits(controller, threading.Event(), 5)
                    # Avoid the driver's dummy list on cancel. It can flush a pending mark packet.
                    for step in (lambda: controller.abort(dummy_packet=False), lambda: lights_out(controller)):
                        try:
                            step()
                        except Exception as exc:
                            cleanup_failures.append(exc)
                    controller._sending = False
                    if original_send is not None:
                        controller.send = original_send
                    try:
                        controller.shutdown()
                    except Exception as exc:
                        cleanup_failures.append(exc)
    except InterruptedError as exc:
        error = ("stopped", str(exc))
    except Exception as exc:
        error = ("error", str(exc))
    finally:
        done.set()
        if cleanup_failures and (error is None or error[0] == "stopped"):
            error = ("unknown", "The laser could not confirm it stopped and turned the laser and red light off "
                                f"({cleanup_failures[0]}). Use the machine's own stop and check it.")
        try:
            ready.send(error or ("finished", action))
        finally:
            pipe.close()
            ready.close()


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
            self._abort_deadline = time.monotonic() + 8
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
                    self._deadline = time.monotonic() + 25
                if name == "running":
                    self._deadline = time.monotonic() + (
                        self._batch_timeouts[0] + 10 if self._batch_timeouts else 25
                    )
                if name == "progress":
                    completed, total = data
                    self._deadline = time.monotonic() + (
                        self._batch_timeouts[completed] + 10 if completed < total else 12
                    )
                if name in {"finished", "error", "stopped", "unknown"}:
                    self._saw_result = True
                events.append((name, data))
        except (EOFError, OSError):
            pass
        now = time.monotonic()
        if alive:
            if self._abort_deadline and now >= self._abort_deadline:
                self.process.terminate()
                self._saw_result = True
                self._abort_deadline = float("inf")
                events.append(("unknown", "Galvo stop could not be confirmed. Use the machine stop and verify its state."))
            elif not self._abort_deadline and now > self._deadline:
                self.stop()
                events.append(("timeout", "The hardware operation timed out. Stopping the galvo."))
        else:
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

