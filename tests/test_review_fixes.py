"""Regression tests for the safety review findings."""

from dataclasses import replace
import multiprocessing as mp
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from mopa_luiz import desktop_hardware as dh  # noqa: E402
from mopa_luiz.workspace import Job, Placement, read_jobs  # noqa: E402
from tests.test_calibration_and_model import b6_profile  # noqa: E402
from tests.test_gui import app, board_side, controller, pump  # noqa: E402,F401


class FlakyGalvo:
    """A controller whose abort fails once, as a single lost USB reply would."""

    light_pin, laser_pin = 8, 0
    is_connected = True

    def __init__(self):
        self.log = []
        self._sending = True
        self.connection = type("Connection", (), {})()

    def send(self, data, read=True):
        return (0, 0, 0, 0)

    def status(self):
        return 0x20

    def lighting_configuration(self):
        pass

    def dark(self, x, y):
        pass

    def light(self, x, y):
        pass

    def light_off(self):
        pass

    def initial_configuration(self):
        pass

    def is_ready_and_not_busy(self):
        return True

    def is_ready(self):
        return True

    def is_busy(self):
        return False

    def abort(self, dummy_packet=True):
        self.log.append("abort")
        raise ConnectionError("The galvo did not acknowledge a USB operation")

    def port_off(self, bit):
        self.log.append(f"port_off({bit})")

    def write_port(self):
        self.log.append("write_port")

    def shutdown(self):
        self.log.append("shutdown")


def test_failed_abort_still_turns_the_lights_off_and_reports_unknown(monkeypatch):
    monkeypatch.setenv("OPENMOPA_MOCK", "1")
    fake = FlakyGalvo()
    monkeypatch.setattr(dh, "connect_controller", lambda *args, **kwargs: fake)
    commands_in, commands_out = mp.Pipe(duplex=False)
    results_in, results_out = mp.Pipe(duplex=False)
    worker = threading.Thread(target=dh.hardware_operation,
                              args=(commands_in, "frame-loop", b6_profile(), (((1, 1), (2, 2)),), results_out))
    worker.start()
    time.sleep(0.4)
    commands_out.send(("abort", None))
    worker.join(5)
    events = []
    try:
        while results_in.poll():
            events.append(results_in.recv())
    except (EOFError, OSError):
        pass                                    # the worker closes its end when it is done
    assert "port_off(8)" in fake.log and "write_port" in fake.log and fake.log[-1] == "shutdown"
    assert events[-1][0] == "unknown"


def test_bottom_side_cannot_be_ticked_early_and_auto_placement_clears_a_tick(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [board_side("top"), board_side("bottom")])
    try:
        c.select(1)
        c.set_verified(True)
        assert not c.is_verified()
        c.verified.add(c.jobs[1].job_id)               # however it got there, seeding must clear it
        c.running_id = c.jobs[0].job_id
        c.jobs[0] = replace(c.jobs[0], placement=Placement(3, 2, 15))
        c._finish("complete")
        assert c.jobs[1].placement == Placement(3, 2, -15) and not c.is_verified(c.jobs[1])
    finally:
        c.shutdown()


def test_profile_change_clears_alignment_ticks(app, tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        c.set_verified(True)
        assert c.set_profile(replace(c.profile, red_x=2.0))
        assert not c.is_verified()
    finally:
        c.shutdown()


def test_escape_stops_even_when_a_dialog_has_the_keyboard(app, tmp_path, monkeypatch):
    from mopa_luiz.gui.main_window import EscapeStops

    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        c.frame(True)
        pump(app, c, lambda: c.worker.action == "frame-loop")
        stopped = []
        c.stop = lambda: stopped.append(True)
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.show()
        app.processEvents()
        EscapeStops(c).eventFilter(box.windowHandle(),
                                   QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        assert stopped
        box.close()
        del c.stop
        c.stop()
        pump(app, c, lambda: not c.busy)
    finally:
        c.shutdown()


def test_job_without_operations_is_rejected_and_inspector_survives(app, tmp_path, monkeypatch):
    from mopa_luiz.gui.panels import Inspector

    with pytest.raises(ValueError, match="no operations"):
        read_jobs({"version": 2, "jobs": [{"name": "Empty", "objects": [], "placement": {"x": 0, "y": 0, "angle": 0},
                                           "job_id": "e"}]})
    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        inspector = Inspector(c)
        c.jobs[0] = replace(c.jobs[0], objects=())
        inspector.reload()
        assert not inspector._loading and not inspector.operation_box.isVisibleTo(inspector)
    finally:
        c.shutdown()


def test_red_light_save_never_interrupts_an_engraving(app, tmp_path, monkeypatch):
    from mopa_luiz.gui.machine import CalibrationDialog

    c = controller(tmp_path, monkeypatch, [board_side("top")])
    try:
        dialog = CalibrationDialog(c)
        dialog.red_points["centre"] = [0.5, 0.0]
        c.running_id = c.jobs[0].job_id                 # pretend a mark is running
        monkeypatch.setattr("mopa_luiz.gui.machine.QMessageBox.information", lambda *args: None)
        stopped = []
        c.stop = lambda: stopped.append(True)
        dialog.save_red()
        assert not stopped and c.profile.red_x == 0
        c.running_id = None
        dialog.close()
    finally:
        c.shutdown()
