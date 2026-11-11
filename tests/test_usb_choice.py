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


def test_worker_opens_the_chosen_laser_by_its_position_and_never_guesses(monkeypatch):
    monkeypatch.setattr(desktop_hardware, "pyusb_probe", lambda: TWO)
    assert desktop_hardware.resolve_device("1-2.1") == (1, "1-2.1")   # second in libusb's order
    assert desktop_hardware.resolve_device("1-4") == (0, "1-4")
    with pytest.raises(ConnectionError, match="not connected"):
        desktop_hardware.resolve_device("1-9")                           # unplugged: do not open the other one
    with pytest.raises(ConnectionError, match="Choose which one"):
        desktop_hardware.resolve_device("")
    monkeypatch.setattr(desktop_hardware, "pyusb_probe", lambda: {"available": True, "devices": TWO["devices"][:1]})
    assert desktop_hardware.resolve_device("") == (0, "1-4")


def test_link_state_follows_the_choice(monkeypatch):
    monkeypatch.delenv("OPENMOPA_MOCK", raising=False)
    monkeypatch.setattr(controller_module, "pyusb_probe", lambda: TWO)
    assert controller_module.probe_link("")[0] == "multiple"
    link, note, devices = controller_module.probe_link("1-2.1")
    assert link == "connected" and "port 2.1" in note and len(devices) == 2
    assert controller_module.probe_link("1-9")[0] == "missing"


def test_switching_lasers_clears_alignment_and_fills_the_menu(app, tmp_path, monkeypatch):
    from mopa_luiz.gui.main_window import MainWindow

    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        c.set_verified(True)
        assert c.set_device("1-2.1") and c.device_choice == "1-2.1" and not c.is_verified()
        assert not c.online                      # re-checked before anything can run
    finally:
        c.shutdown()
    window = MainWindow(tmp_path / "window")
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
        window.controller.shutdown()
        window.close()
