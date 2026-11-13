"""An explicit, single-owner session for galvoplotter 0.2.0."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import os
import struct
import threading
import time

from galvo.consts import ExecuteList, GetSerialNo, GetVersion, single_command_lookup
from galvo.controller import READY, GalvoController
from galvo.usb_connection import READ_ENDPOINT, WRITE_ENDPOINT
from usb import util

from .cli import MarkConfig, nearest_pulse_width, pulse_width_table


FRAME_LIGHT_SPEED_MM_S = 3000.0
TRAVEL_SPEED_MM_S = 3000.0
WAIT_TIMEOUT_S = 15.0
USB_TIMEOUT_MS = 2000
STOP_TIMEOUT_S = 8.0


class LinkLost(ConnectionError):
    """A command or its reply failed, leaving the board state unverified."""


class OperatorStop(InterruptedError):
    """The operator cancelled the current operation."""


def mock_mode() -> bool:
    return os.environ.get("OPENMOPA_MOCK", "") == "1"


@dataclass(frozen=True)
class JobParams:
    text: str
    power: float
    frequency_khz: float
    pulse_width_ns: float
    size_mm: float
    mark_speed: float


@contextmanager
def claim_device(device, log):
    """Claim the enumerated object and release it without resetting the USB device."""
    from usb.core import USBError

    detached = claimed = failed = False
    interface = 0
    try:
        # An already configured WinUSB device does not need SetConfiguration.
        try:
            config = device.get_active_configuration()
        except USBError as exc:
            if str(exc) != "Configuration not set":
                raise
            device.set_configuration()
            config = device.get_active_configuration()
        interface = config[(0, 0)].bInterfaceNumber
        try:
            if device.is_kernel_driver_active(interface):
                device.detach_kernel_driver(interface)
                detached = True
        except NotImplementedError:
            pass
        util.claim_interface(device, interface)
        claimed = True
        yield device
    except BaseException:
        failed = True
        raise
    finally:
        try:
            try:
                if claimed:
                    util.release_interface(device, interface)
            finally:
                try:
                    if detached:
                        device.attach_kernel_driver(interface)
                finally:
                    util.dispose_resources(device)
        except Exception as exc:
            log(f"USB release failed: {exc}")
            if not failed:
                raise


class SimulatedDevice:
    """PyUSB-shaped device with deterministic replies, used by the spawned mock worker."""

    def write(self, endpoint, data, timeout):
        if len(data) == 0xC00:
            time.sleep(0.02)
        self.command = int.from_bytes(data[:2], "little")
        return len(data)

    def read(self, endpoint, size, timeout):
        return struct.pack("<4H", 0, 7 if self.command == GetSerialNo else 33827, 9, 0x20)


class SessionController(GalvoController):
    """Use galvoplotter's list encoding and spooler with one bound, strict transport."""

    def __init__(self, device, stop, log, **settings):
        super().__init__(usb_log=log, **settings)
        self.device = device
        self.stop = stop
        self.log = log
        self.failure = None
        self.recent = deque(maxlen=40)
        self.transfers = 0
        self.slowest_transfer = 0.0
        self.longest_wait = 0.0
        self.last_status = None
        self.last_command = None
        self.wait_seconds = WAIT_TIMEOUT_S
        self.deadline = None
        self.cleaning = False
        self.execute_count = 0
        self.on_execute = None

    def checkpoint(self):
        if self.failure is not None:
            raise self.failure
        if not self.cleaning and self.stop.is_set():
            raise OperatorStop("Operator stopped the hardware operation")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("The controller did not finish this stage in time")

    def send(self, data, read=True):
        self.checkpoint()
        code = int.from_bytes(data[:2], "little")
        name = "list packet" if len(data) == 0xC00 else single_command_lookup.get(code, f"0x{code:04x}")
        phase = "write"
        started = time.monotonic()
        try:
            written = self.device.write(WRITE_ENDPOINT, data, timeout=USB_TIMEOUT_MS)
            if written != len(data):
                raise ValueError(f"short write ({written}/{len(data)} bytes)")
            reply = None
            if read:
                phase = "read"
                raw = bytes(self.device.read(READ_ENDPOINT, 8, timeout=USB_TIMEOUT_MS))
                if len(raw) != 8 or raw == b"\xff" * 8:
                    raise ValueError(f"invalid {len(raw)}-byte reply ({raw.hex()})")
                reply = struct.unpack("<4H", raw)
                if code in (GetVersion, GetSerialNo) and not any(reply[:3]):
                    raise ValueError(f"invalid identity reply {reply}")
                if code == GetVersion:
                    self.last_status = reply[3]
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.failure = LinkLost(f"USB {phase} failed for {name}: {exc or type(exc).__name__}")
            self.log(f"{self.failure} after {elapsed * 1000:.1f} ms; "
                     f"last command={self.last_command}, last status={self.last_status!r}; "
                     f"backend error={getattr(exc, 'backend_error_code', None)}")
            for entry in self.recent:
                self.log(f"  {entry}")
            raise self.failure from exc
        elapsed = time.monotonic() - started
        self.transfers += 1
        self.slowest_transfer = max(self.slowest_transfer, elapsed)
        self.last_command = name
        self.recent.append(f"{name}: {elapsed * 1000:.1f} ms"
                           + (f" -> {reply}" if reply is not None else ""))
        self.checkpoint()
        if len(data) == 12 and code == ExecuteList:
            self.execute_count += 1
            if self.on_execute is not None:
                self.on_execute()
        return reply

    def wait_for_status(self, predicate, label):
        started = time.monotonic()
        deadline = started + self.wait_seconds
        try:
            while True:
                self.checkpoint()
                if time.monotonic() >= deadline:
                    raise self.status_timeout(label)
                ready = predicate()
                self.checkpoint()
                if time.monotonic() >= deadline:
                    raise self.status_timeout(label)
                if ready:
                    return
                if self.cleaning:
                    time.sleep(0.01)
                else:
                    self.stop.wait(0.01)
        finally:
            self.longest_wait = max(self.longest_wait, time.monotonic() - started)

    def status_timeout(self, label):
        status = "none" if self.last_status is None else f"0x{self.last_status:04x}"
        self.log(f"Status wait expired for {label}. Recent commands:")
        for entry in self.recent:
            self.log(f"  {entry}")
        return TimeoutError(
            f"The controller did not report {label} in {self.wait_seconds:g} s "
            f"(status {status}, {self._number_of_list_packets} list packets sent, "
            f"list {'executing' if self._list_executing else 'not started'})"
        )

    def wait_ready(self):
        self.wait_for_status(self.is_ready, "ready")

    def _list_end(self):
        """Start a primed list when the board stops accepting packets before execution."""
        with self._list_build_lock:
            if not self._active_index:
                return
            self.wait_for_status(self._ready_to_send_list_packet, "ready for a list packet")
            self.send(self._active_list, read=False)
            self.set_end_of_list(0)
            self._number_of_list_packets += 1
            self._active_list = None
            self._active_index = 0
            if self._number_of_list_packets >= 3 and not self._list_executing:
                self._start_streaming_list()

    def _ready_to_send_list_packet(self):
        status = self.status()
        if status & READY:
            return True
        if self._number_of_list_packets and not self._list_executing:
            # Waiting here would deadlock if the queued list must start before
            # firmware can free another slot. Keep waiting for READY after start.
            self.log(f"List buffer stopped accepting packets before start (status 0x{status:04x})")
            self._start_streaming_list()
        return False

    def _start_streaming_list(self):
        self.execute_list()
        self._list_executing = True
        self.log(f"Streaming list started with {self._number_of_list_packets} packets queued")

    def wait_idle(self):
        self.wait_for_status(lambda: not self.is_busy(), "idle")

    def wait_finished(self):
        self.wait_for_status(self.is_ready_and_not_busy, "ready and idle")

    def handshake(self):
        serial = self.get_serial_number()
        version = self.get_version()
        firmware = version[:3]
        self.log(f"Controller responding: serial={serial}, firmware={firmware}, status=0x{version[3]:04x}")
        return serial, firmware

    def verified_stop(self):
        """Stop the executing list, discard buffered work, and verify the board is idle."""
        self.cleaning = True
        self.deadline = time.monotonic() + STOP_TIMEOUT_S
        try:
            self.stop_execute()
            if self.source == "fiber":
                self.set_fiber_mo(0)
            self.reset_list()
            self.outputs_off()
            self.wait_finished()
            self.log("Stop verified: outputs off, controller ready and idle")
        finally:
            # The library invokes initial_configuration after the spooler task returns.
            # Discard the host buffer even if Stop could not reach the board.
            self.discard_list()
            self.cleaning = False
            self.deadline = None

    def outputs_off(self):
        self.port_off(self.light_pin)
        self.port_off(self.laser_pin)
        self.write_port()

    def discard_list(self):
        self._active_list = None
        self._active_index = 0
        self._list_executing = False
        self._number_of_list_packets = 0
        self.laser_configuration = "initial"


def make_controller(device, stop, log, cfg: MarkConfig, params: JobParams, *, field_size_mm=None, options=None):
    field_size = field_size_mm or cfg.get_float("FIELDSIZE", 200.0) or 200.0
    settings = {
        "power": params.power,
        "frequency": params.frequency_khz,
        "pulse_width": nearest_pulse_width(params.pulse_width_ns, pulse_width_table(cfg)),
        "mark_speed": params.mark_speed,
        "travel_speed": TRAVEL_SPEED_MM_S,
        "light_speed": FRAME_LIGHT_SPEED_MM_S,
        "galvos_per_mm": 0xFFFF / field_size,
    }
    return SessionController(device, stop, log, **(settings | (options or {})))
