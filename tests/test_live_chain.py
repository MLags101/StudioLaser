"""Connect-path tests for the galvo controller.

The mock connect runs against galvoplotter's MockConnection (OPENMOPA_MOCK=1):
no hardware is touched.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz import live
from mopa_luiz.cli import MarkConfig
from mopa_luiz.live import JobParams, connect_controller


PARAMS = JobParams(text="", power=1.0, frequency_khz=30.0, pulse_width_ns=200.0, size_mm=10.0, mark_speed=1000.0)


def config(**values: str) -> MarkConfig:
    return MarkConfig(path=Path("mem"), values=values)


class StubController:
    """Records its settings and connects instantly."""

    def __init__(self, **kwargs):
        self.settings = kwargs
        self._sending = True
        self.abort_requested = False

    def connect_if_needed(self):
        pass

    def abort_connect(self):
        self.abort_requested = True

    def shutdown(self):
        pass


class StubHangingController(StubController):
    """Mimics a wedged board: claims the interface, then never answers."""

    def connect_if_needed(self):
        self.settings["usb_log"]("Attempting to claim interface.")
        self.settings["usb_log"]("Interface claim: Success")
        time.sleep(2.0)


class StubRefusedController(StubController):
    def connect_if_needed(self):
        raise ConnectionRefusedError("Could not connect to the LMC controller.")


class MockModeTest(unittest.TestCase):
    def test_mock_mode_env_flag(self):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}):
            self.assertTrue(live.mock_mode())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(live.mock_mode())

    def test_connect_controller_mock_connects(self):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}):
            controller = connect_controller(config(FIELDSIZE="200"), PARAMS)
        try:
            self.assertTrue(controller.mock)
        finally:
            controller._sending = False
            controller.shutdown()


class ConnectSettingsTest(unittest.TestCase):
    def connect(self, cfg, **kwargs):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}), \
             mock.patch.object(live, "GalvoController", StubController):
            return connect_controller(cfg, PARAMS, **kwargs).settings

    def test_field_size_defaults_to_the_profile(self):
        self.assertEqual(self.connect(config(FIELDSIZE="110"))["galvos_per_mm"], 0xFFFF / 110)
        self.assertEqual(self.connect(config())["galvos_per_mm"], 0xFFFF / 200)

    def test_explicit_field_size_and_options_win(self):
        settings = self.connect(config(FIELDSIZE="110"), field_size_mm=70, options={"light_speed": 500})
        self.assertEqual(settings["galvos_per_mm"], 0xFFFF / 70)
        self.assertEqual(settings["light_speed"], 500)

    def test_pulse_width_snaps_to_the_profile_table(self):
        self.assertEqual(self.connect(config(PULSEWIDTHS="10,150,500"))["pulse_width"], 150)


class WedgeProtectionTest(unittest.TestCase):
    def connect_error(self, controller_class, timeout=0.2):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}), \
             mock.patch.object(live, "GalvoController", controller_class):
            with self.assertRaises(RuntimeError) as ctx:
                connect_controller(config(FIELDSIZE="200"), PARAMS, connect_timeout_s=timeout)
        return str(ctx.exception)

    def test_connect_timeout_reports_wedged_board(self):
        started = time.monotonic()
        message = self.connect_error(StubHangingController)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("Power-cycle", message)
        self.assertIn("not responding", message)

    def test_connect_error_is_reported_with_its_cause(self):
        message = self.connect_error(StubRefusedController, timeout=5)
        self.assertIn("cannot reach the JCZ/LMC laser", message)
        self.assertIn("Could not connect to the LMC controller.", message)


if __name__ == "__main__":
    unittest.main()
