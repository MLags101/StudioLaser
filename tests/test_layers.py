import unittest
from dataclasses import replace
from unittest import mock

from mopa_luiz.cli import MarkConfig
from mopa_luiz.layers import (
    DISABLED,
    FRAME_ONLY,
    NUMBERS,
    RASTER_ENGRAVE,
    VECTOR_CUT,
    VECTOR_ENGRAVE,
    LayerSpec,
    layer_from_dict,
    validate_layers,
)

PASSES_ERROR = "passes must be between 1 and 999"


def fake_cfg() -> MarkConfig:
    return MarkConfig(path=mock.MagicMock(),
                      values={"MINPWMFREQ": "1000", "MAXPWMFREQ": "4000000"})


def layer(operation=VECTOR_ENGRAVE, **overrides) -> LayerSpec:
    return replace(LayerSpec(
        layer_id="layer", name="Layer", operation=operation, power_percent=50.0,
        frequency_khz=30.0, pulse_width_ns=200.0, speed_mm_s=1000.0,
    ), **overrides)


class ValidateLayersTests(unittest.TestCase):
    def errors(self, spec, cfg=fake_cfg()):
        return validate_layers([spec], cfg).get("layer", [])

    def test_valid_layers_pass(self):
        for operation in (VECTOR_ENGRAVE, VECTOR_CUT, RASTER_ENGRAVE, FRAME_ONLY, DISABLED):
            self.assertEqual(self.errors(layer(operation)), [])
        self.assertEqual(self.errors(layer(passes=999)), [])

    def test_raster_layer_with_invalid_pitch_is_flagged(self):
        self.assertTrue(any("pitch" in e for e in self.errors(layer(RASTER_ENGRAVE, raster_pitch_mm=0))))

    def test_layer_above_100_percent_flagged(self):
        self.assertTrue(self.errors(layer(VECTOR_CUT, power_percent=101)))

    def test_passes_must_be_an_integer_from_1_to_999(self):
        for passes in (0, -1, 1000, 2.5, 2.0, True, "3", None, float("nan")):
            for operation in (VECTOR_ENGRAVE, FRAME_ONLY):
                self.assertEqual(self.errors(layer(operation, passes=passes)), [PASSES_ERROR], passes)

    def test_non_finite_numbers_are_rejected_on_every_layer(self):
        for name in NUMBERS:
            for value in (float("nan"), float("inf"), "12"):
                for operation in (VECTOR_ENGRAVE, RASTER_ENGRAVE, DISABLED):
                    for cfg in (fake_cfg(), None):
                        self.assertEqual(
                            self.errors(layer(operation, **{name: value}), cfg),
                            [f"{name} must be a finite number"],
                        )

    def test_emission_flags(self):
        self.assertTrue(layer().emits_when_marking())
        self.assertTrue(layer(RASTER_ENGRAVE).emits_when_marking())
        self.assertFalse(layer(FRAME_ONLY).emits_when_marking())
        self.assertFalse(layer(DISABLED).emits_when_marking())
        self.assertFalse(layer(output=False).emits_when_marking())


class LayerFromDictTests(unittest.TestCase):
    def test_round_trip(self):
        spec = layer(passes=2, raster_pitch_mm=0.05, crosshatch=True)
        self.assertEqual(layer_from_dict(spec.to_dict()), spec)

    def test_unknown_operation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown layer operation"):
            layer_from_dict({"operation": "plasma"})

    def test_passes_are_validated(self):
        self.assertEqual(layer_from_dict({}).passes, 1)
        self.assertEqual(layer_from_dict({"passes": 999}).passes, 999)
        for passes in (0, -3, 1000, 2.5, float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, PASSES_ERROR):
                layer_from_dict({"passes": passes})

    def test_non_finite_numbers_are_rejected(self):
        for name in NUMBERS:
            for value in (float("nan"), float("-inf"), "nan"):
                with self.assertRaisesRegex(ValueError, f"{name} must be a finite number"):
                    layer_from_dict({name: value})


if __name__ == "__main__":
    unittest.main()
