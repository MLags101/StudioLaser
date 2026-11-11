import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz.cli import JPT_M7_PULSE_WIDTHS_NS, MarkConfig, pulse_width_table
from mopa_luiz.safety import validate_layer_settings


def fake_cfg() -> MarkConfig:
    return MarkConfig(
        path=mock.MagicMock(),
        values={"FIELDSIZE": "200", "MINPWMFREQ": "1000", "MAXPWMFREQ": "4000000"},
    )


class LayerValidationTests(unittest.TestCase):
    def test_disabled_layer_skips_checks(self):
        errs = validate_layer_settings(
            operation="disabled",
            power_percent=999, frequency_khz=999, pulse_width_ns=1,
            speed_mm_s=0, cfg=fake_cfg(),
        )
        self.assertEqual(errs, [])

    def test_emitting_layer_up_to_100_percent_passes(self):
        errs = validate_layer_settings(
            operation="vector_cut",
            power_percent=100, frequency_khz=30, pulse_width_ns=200,
            speed_mm_s=300, cfg=fake_cfg(),
        )
        self.assertEqual(errs, [])

    def test_out_of_range_settings_are_flagged(self):
        def check(cfg=fake_cfg(), **overrides):
            settings = dict(operation="vector_cut", power_percent=50, frequency_khz=30,
                            pulse_width_ns=200, speed_mm_s=300)
            return validate_layer_settings(**(settings | overrides), cfg=cfg)

        self.assertEqual(check(), [])
        self.assertTrue(any("out of 0..100" in e for e in check(power_percent=101)))
        self.assertTrue(any("speed" in e for e in check(speed_mm_s=0)))
        self.assertTrue(any("outside machine profile" in e for e in check(frequency_khz=0.1)))
        for name in ("power_percent", "frequency_khz", "pulse_width_ns", "speed_mm_s"):
            for cfg in (fake_cfg(), None):
                self.assertTrue(check(cfg=cfg, **{name: float("nan")}), name)


class ProfilePulseTableTests(unittest.TestCase):
    def custom_cfg(self, pulse_widths="10, 25,500") -> MarkConfig:
        return MarkConfig(
            path=Path("mem"),
            values={
                "MINPWMFREQ": "1000",
                "MAXPWMFREQ": "4000000",
                "PULSEWIDTHS": pulse_widths,
            },
        )

    def test_pulse_width_table_uses_profile_values(self):
        self.assertEqual(pulse_width_table(self.custom_cfg()), (10, 25, 500))

    def test_pulse_width_table_falls_back_to_m7(self):
        self.assertEqual(pulse_width_table(None), JPT_M7_PULSE_WIDTHS_NS)
        self.assertEqual(
            pulse_width_table(MarkConfig(path=Path("mem"), values={})),
            JPT_M7_PULSE_WIDTHS_NS,
        )
        for value in ("a,b", "", "0,-5"):
            self.assertEqual(
                pulse_width_table(self.custom_cfg(value)), JPT_M7_PULSE_WIDTHS_NS
            )

    def test_layer_validation_uses_custom_table(self):
        ok = validate_layer_settings(
            operation="vector_cut",
            power_percent=1.0,
            frequency_khz=30.0,
            pulse_width_ns=25,
            speed_mm_s=300.0,
            cfg=self.custom_cfg(),
        )
        self.assertEqual(ok, [])

        blocked = validate_layer_settings(
            operation="vector_cut",
            power_percent=1.0,
            frequency_khz=30.0,
            pulse_width_ns=200,
            speed_mm_s=300.0,
            cfg=self.custom_cfg(),
        )
        self.assertTrue(
            any("not in machine pulse-width table" in error for error in blocked)
        )

    def test_default_table_snap_and_reject_behavior_is_unchanged(self):
        def check(pulse_width_ns):
            return validate_layer_settings(
                operation="vector_engrave", power_percent=1.0, frequency_khz=30.0,
                pulse_width_ns=pulse_width_ns, speed_mm_s=300.0, cfg=fake_cfg(),
            )

        self.assertEqual(check(199.8), [])
        self.assertTrue(any("not in machine pulse-width table" in error for error in check(170)))


if __name__ == "__main__":
    unittest.main()
