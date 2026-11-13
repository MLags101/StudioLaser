"""Choosing which USB laser to use when more than one is connected."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mopa_luiz import desktop_hardware  # noqa: E402
from mopa_luiz.cli import describe_usb, usb_label  # noqa: E402
from mopa_luiz.gui import controller as controller_module  # noqa: E402
from tests.test_gui import app, board_side, controller  # noqa: E402,F401

TWO = {"available": True, "devices": [
    {"key": "1-4", "bus": 1, "address": 9, "port": "4"},
    {"key": "1-2.1", "bus": 1, "address": 7, "port": "2.1"},
]}


class FakeDevice:
    idVendor, idProduct, bus, address = 0x9588, 0x9899, 2, 11
    port_numbers = (3, 1)


def test_device_key_follows_the_port_and_reads_well():
    device = describe_usb(FakeDevice())
    assert device["key"] == "2-3.1" and usb_label(device["key"]) == "USB bus 2, port 3.1"
    FakeDevice.port_numbers = None
    assert describe_usb(FakeDevice())["key"] == "2:11"
    assert usb_label("2:11") == "USB bus 2, device 11"


def test_worker_binds_the_exact_device_regardless_of_enumeration_order(monkeypatch):
    from types import SimpleNamespace

    def candidate(port, address):
        return SimpleNamespace(idVendor=0x9588, idProduct=0x9899, bus=1, port_numbers=port, address=address)

    first, second = candidate((4,), 9), candidate((2, 1), 7)
    devices = [first, second]
    monkeypatch.setattr(desktop_hardware, "usb_candidates", lambda: (devices, {"available": True}))
    assert desktop_hardware.resolve_device("1-2.1")[0] is second
    devices.reverse()
    assert desktop_hardware.resolve_device("1-2.1")[0] is second
    with pytest.raises(ConnectionError, match="not connected"):
        desktop_hardware.resolve_device("1-9")                           # unplugged: do not open the other one
    with pytest.raises(ConnectionError, match="Choose which one"):
        desktop_hardware.resolve_device("")
    devices.append(candidate((2, 1), 8))
    with pytest.raises(ConnectionError, match="matches the selected"):
        desktop_hardware.resolve_device("1-2.1")
    devices[:] = [first]
    assert desktop_hardware.resolve_device("")[0] is first


def test_link_state_follows_the_choice(monkeypatch):
    monkeypatch.delenv("OPENMOPA_MOCK", raising=False)
    monkeypatch.setattr(controller_module, "pyusb_probe", lambda: TWO)
    assert controller_module.probe_link("")[0] == "multiple"
    link, note, devices = controller_module.probe_link("1-2.1")
    assert link == "connected" and "port 2.1" in note and len(devices) == 2
    assert controller_module.probe_link("1-9")[0] == "missing"


def test_enumeration_and_claim_do_not_count_as_a_successful_handshake(monkeypatch):
    from contextlib import contextmanager
    import multiprocessing as mp
    import struct
    from tests.test_desktop_workflow import enabled_profile

    monkeypatch.delenv("OPENMOPA_MOCK", raising=False)
    monkeypatch.setattr(desktop_hardware, "HardwareLog", lambda enabled: type(
        "Log", (), {"__call__": lambda self, line: None, "close": lambda self: None})())
    events = []

    class Device:
        def write(self, endpoint, data, timeout):
            events.append(("write", int.from_bytes(data[:2], "little")))
            return len(data)

        def read(self, endpoint, size, timeout):
            return struct.pack("<4H", 0, 0, 0, 0)

    device = Device()

    @contextmanager
    def claim(bound, log):
        assert bound is device
        events.append("claim")
        try:
            yield
        finally:
            events.append("release")

    monkeypatch.setattr(desktop_hardware, "resolve_device", lambda _: (device, TWO["devices"][0]))
    monkeypatch.setattr(desktop_hardware, "claim_device", claim)
    monkeypatch.setattr(desktop_hardware, "lock_galvo", lambda _: type(
        "Lease", (), {"close": lambda self: events.append("unlock")})())
    input_pipe, parent = mp.Pipe(duplex=False)
    results = []
    sender = type("Sender", (), {"send": lambda self, result: results.append(result), "close": lambda self: None})()
    try:
        desktop_hardware.hardware_operation(input_pipe, "frame", enabled_profile(), (((1, 1), (2, 2)),), sender)
    finally:
        parent.close()
    assert results[-1][0] == "unknown"
    assert not any(kind in {"running", "responding"} for kind, _ in results)
    assert events == ["claim", ("write", 9), "release", "unlock"]


def test_switching_lasers_clears_alignment_and_fills_the_menu(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings
    from mopa_luiz.gui import main_window

    monkeypatch.setattr(main_window, "QSettings", lambda org, app: QSettings(
        str(tmp_path / f"{org}-{app}.ini"), QSettings.Format.IniFormat))

    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        c.set_verified(True)
        assert c.set_device("1-2.1") and c.device_choice == "1-2.1" and not c.is_verified()
        assert not c.online                      # re-checked before anything can run
    finally:
        c.shutdown()
    window = main_window.MainWindow(tmp_path / "window")
    try:
        window.controller._timer.stop()
        window.controller.usb_devices = TWO["devices"]
        window.fill_device_menu()
        texts = [action.text() for action in window.device_menu.actions()]
        assert texts[0].startswith("Automatic") and any("port 2.1" in text for text in texts)
        chosen = next(action for action in window.device_menu.actions() if "port 2.1" in action.text())
        chosen.trigger()
        assert window.controller.device_choice == "1-2.1" and window.settings.value("usb_device") == "1-2.1"
        window.set_device("")
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
