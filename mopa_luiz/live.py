from __future__ import annotations

from dataclasses import dataclass
import os
import sys
import threading

from galvo.controller import GalvoController

from .cli import MarkConfig, nearest_pulse_width, pulse_width_table
from .usb_backend import ensure_libusb_backend
from .usb_lock import lock_galvo


# A wedged LMC board connects but never replies until the machine is
# power-cycled, so connects run under a hard deadline with a wedge diagnosis.
CONNECT_TIMEOUT_S = 12.0
FRAME_LIGHT_SPEED_MM_S = 3000.0


def mock_mode() -> bool:
    """OPENMOPA_MOCK=1 exercises the full command chain on a mock connection."""
    return os.environ.get("OPENMOPA_MOCK", "") == "1"


@dataclass(frozen=True)
class JobParams:
    text: str
    power: float
    frequency_khz: float
    pulse_width_ns: float
    size_mm: float
    mark_speed: float


def connect_controller(
    cfg: MarkConfig, params: JobParams, connect_timeout_s: float = CONNECT_TIMEOUT_S,
    *, field_size_mm: float | None = None, options: dict | None = None,
) -> GalvoController:
    """Connect under a hard deadline; field size defaults to the profile's FIELDSIZE.

    The returned controller still has galvoplotter's unbounded wait_* methods.
    The caller must install bounded ones before building a list
    (desktop_hardware.set_waits); the connect phase itself never waits on them.
    """
    # Hardware operations run in fresh spawned processes, so load libusb here too.
    ensure_libusb_backend()
    field_size = field_size_mm if field_size_mm is not None else cfg.get_float("FIELDSIZE", 200.0) or 200.0
    usb_events: list[str] = []
    simulated = mock_mode()
    lease = None if simulated else lock_galvo()
    settings = {
        "power": params.power,
        "frequency": params.frequency_khz,
        "pulse_width": nearest_pulse_width(params.pulse_width_ns, pulse_width_table(cfg)),
        "mark_speed": params.mark_speed,
        "travel_speed": 3000.0,
        "light_speed": FRAME_LIGHT_SPEED_MM_S,
        "galvos_per_mm": 0xFFFF / field_size,
        "mock": simulated,
        "usb_log": usb_events.append,
    }
    try:
        controller = GalvoController(**(settings | (options or {})))
    except Exception:
        if lease:
            lease.close()
        raise
    if lease:
        shutdown = controller.shutdown

        def release_on_shutdown():
            try:
                shutdown()
            finally:
                lease.close()

        controller.shutdown = release_on_shutdown
    connect_errors: list[BaseException] = []

    def _connect() -> None:
        try:
            controller.connect_if_needed()
        except BaseException as exc:  # surfaced below with the usb_log tail
            connect_errors.append(exc)

    # A wedged board can keep galvoplotter's retry loop spinning far past its
    # own attempt limit (its USB resets knock the device off the bus
    # mid-retry), so enforce a hard deadline of our own.
    worker = threading.Thread(target=_connect, daemon=True)
    worker.start()
    worker.join(connect_timeout_s)
    if worker.is_alive() or connect_errors:
        controller.abort_connect()
        controller._sending = False  # make shutdown() safe: no further USB writes
        try:
            controller.shutdown()
        except Exception:
            pass
        cause = connect_errors[0] if connect_errors else None
        detail = "; ".join(usb_events[-4:]) or (str(cause) if cause else "connect timed out")
        log_text = " ".join(usb_events + [str(cause or "")]).lower()
        if any("claim: Success" in event for event in usb_events):
            hint = (
                "The board connects but is not responding (wedged controller). "
                "Power-cycle the laser (full power off, wait 15s, power on), then retry."
            )
        elif sys.platform.startswith("win") and any(
            marker in log_text for marker in ("not supported", "not implemented", "entity not found", "errno 40")
        ):
            hint = (
                "Windows is using the EZCAD (BJJCZ) driver for the laser. Install the WinUSB "
                "driver for USBLMCV4 with Zadig (see the README), then retry."
            )
        elif any(marker in log_text for marker in ("access denied", "errno 13", "permission")):
            hint = (
                "This user is not allowed to open the laser's USB device. On Linux, install "
                "scripts/99-openlaser.rules (see the README), then unplug and replug the laser."
            )
        else:
            hint = "Check power, USB cable, and the Detect panel, then retry."
        raise RuntimeError(
            f"cannot reach the JCZ/LMC laser over USB. {hint} Log: {detail}"
        ) from cause
    return controller
