"""Deterministic unit tests for RP-X0 O2 Thermal replay math and T-B5 invoke."""

from __future__ import annotations

import unittest

import numpy as np

from hil.thermal_o2_real_snapshot_replay import (
    EXPECTED_INPUT_SCALE,
    EXPECTED_INPUT_ZERO_POINT,
    EXPECTED_SHA256,
    HEIGHT,
    P1_MEAN,
    P1_STD,
    THERMAL_ARTIFACT,
    WIDTH,
    apply_p1,
    celsius_from_raw_uint16,
    invoke_int8,
    load_locked_t_b5,
    quantize_int8,
    representable_celsius_range,
    sha256_file,
)


def raw_from_celsius(celsius: float) -> np.uint16:
    raw = int(round((float(celsius) + 273.15) * 10.0))
    if not 0 <= raw <= 65535:
        raise ValueError(raw)
    return np.uint16(raw)


class ThermalO2ReplayContractTests(unittest.TestCase):
    def test_o1_formula_matches_vendor_example_0x0bc1(self) -> None:
        raw = np.array([[0x0BC1]], dtype=np.uint16)
        celsius = celsius_from_raw_uint16(raw)
        self.assertEqual(celsius.dtype, np.float32)
        self.assertAlmostEqual(float(celsius[0, 0]), 300.9 - 273.15, places=5)

    def test_conversion_does_not_clamp_or_refit(self) -> None:
        raw = np.array([[2086, 3296]], dtype=np.uint16)
        celsius = celsius_from_raw_uint16(raw)
        self.assertAlmostEqual(float(celsius[0, 0]), 208.6 - 273.15, places=4)
        self.assertAlmostEqual(float(celsius[0, 1]), 329.6 - 273.15, places=4)

    def test_p1_at_training_mean_is_zero(self) -> None:
        z = apply_p1(np.array([[P1_MEAN]], dtype=np.float32))
        self.assertEqual(z.dtype, np.float64)
        self.assertAlmostEqual(float(z[0, 0]), 0.0, places=6)

    def test_mean_temperature_quantizes_to_zero_point(self) -> None:
        z = apply_p1(np.full((HEIGHT, WIDTH), P1_MEAN, dtype=np.float32))
        clipped, unclipped = quantize_int8(z, EXPECTED_INPUT_SCALE, EXPECTED_INPUT_ZERO_POINT)
        self.assertTrue(np.all(unclipped == EXPECTED_INPUT_ZERO_POINT))
        self.assertTrue(np.all(clipped == np.int8(EXPECTED_INPUT_ZERO_POINT)))

    def test_quantize_reports_low_saturation_without_changing_p1(self) -> None:
        cold = np.full((HEIGHT, WIDTH), -64.55, dtype=np.float32)
        z = apply_p1(cold)
        clipped, unclipped = quantize_int8(z, EXPECTED_INPUT_SCALE, EXPECTED_INPUT_ZERO_POINT)
        self.assertTrue(np.all(unclipped < -128))
        self.assertTrue(np.all(clipped == np.int8(-128)))
        self.assertLess(float(z.min()), -20.0)

    def test_locked_artifact_sha_and_zero_point_invoke(self) -> None:
        self.assertEqual(sha256_file(THERMAL_ARTIFACT), EXPECTED_SHA256)
        runtime = load_locked_t_b5()
        zp = np.full((1, HEIGHT, WIDTH, 1), runtime["input_zero_point"], dtype=np.int8)
        output = invoke_int8(runtime, zp)
        self.assertEqual(output["output_shape"], [1, 3])
        self.assertEqual(output["raw_output"], [-29, -70, -29])

    def test_uniform_physical_frame_invokes_t_b5(self) -> None:
        raw = np.full((HEIGHT, WIDTH), raw_from_celsius(P1_MEAN), dtype=np.uint16)
        celsius = celsius_from_raw_uint16(raw)
        z = apply_p1(celsius)
        clipped, _unclipped = quantize_int8(z, EXPECTED_INPUT_SCALE, EXPECTED_INPUT_ZERO_POINT)
        runtime = load_locked_t_b5()
        output = invoke_int8(runtime, clipped.reshape(1, HEIGHT, WIDTH, 1))
        self.assertEqual(len(output["raw_output"]), 3)
        self.assertIn(output["class_name"], {"NOT_HUMAN", "HUMAN_NORMAL", "HUMAN_FALL"})
        self.assertTrue(np.all(np.isfinite(np.asarray(output["dequantized_output"]))))

    def test_int8_dynamic_range_is_asymmetric_about_p1_mean(self) -> None:
        limits = representable_celsius_range()
        self.assertAlmostEqual(limits["celsius_at_int8_zero_point"], P1_MEAN, places=6)
        self.assertGreater(limits["celsius_at_int8_minus_128"], 19.0)
        self.assertLess(limits["celsius_at_int8_minus_128"], 21.0)
        self.assertGreater(limits["celsius_at_int8_127"], 200.0)


if __name__ == "__main__":
    unittest.main()
