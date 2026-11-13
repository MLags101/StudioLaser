"""Connect-path tests for the galvo controller.

The mock connect runs against galvoplotter's MockConnection (OPENMOPA_MOCK=1):
no hardware is touched.
"""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz import live
from mopa_luiz.cli import MarkConfig
from mopa_luiz.live import JobParams, SimulatedDevice, make_controller


PARAMS = JobParams(text="", power=1.0, frequency_khz=30.0, pulse_width_ns=200.0, size_mm=10.0, mark_speed=1000.0)


def config(**values: str) -> MarkConfig:
    return MarkConfig(path=Path("mem"), values=values)


class MockModeTest(unittest.TestCase):
    def test_mock_mode_env_flag(self):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}):
            self.assertTrue(live.mock_mode())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(live.mock_mode())

    def test_simulated_device_completes_handshake(self):
        controller = make_controller(SimulatedDevice(), threading.Event(), lambda _: None,
                                     config(FIELDSIZE="200"), PARAMS)
        serial, version = controller.handshake()
        self.assertEqual(serial[1], 7)
        self.assertEqual(version[1], 33827)


class ConnectSettingsTest(unittest.TestCase):
    def connect(self, cfg, **kwargs):
        with mock.patch.object(live, "SessionController") as constructor:
            make_controller(SimulatedDevice(), threading.Event(), lambda _: None, cfg, PARAMS, **kwargs)
            return constructor.call_args.kwargs

    def test_field_size_defaults_to_the_profile(self):
        self.assertEqual(self.connect(config(FIELDSIZE="110"))["galvos_per_mm"], 0xFFFF / 110)
        self.assertEqual(self.connect(config())["galvos_per_mm"], 0xFFFF / 200)

    def test_explicit_field_size_and_options_win(self):
        settings = self.connect(config(FIELDSIZE="110"), field_size_mm=70, options={"light_speed": 500})
        self.assertEqual(settings["galvos_per_mm"], 0xFFFF / 70)
        self.assertEqual(settings["light_speed"], 500)

    def test_pulse_width_snaps_to_the_profile_table(self):
        self.assertEqual(self.connect(config(PULSEWIDTHS="10,150,500"))["pulse_width"], 150)


if __name__ == "__main__":
    unittest.main()
