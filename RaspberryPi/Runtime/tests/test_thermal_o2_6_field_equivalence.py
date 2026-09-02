"""Deterministic sampling and metric tests for RP-X0 O2.6."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from hil.thermal_o2_6_field_equivalence import (
    CANONICAL_POLICY_STATUS,
    assign_time_bin,
    even_indices,
    select_deterministic_sample,
    summarize,
)
from hil.thermal_o2_5_float_int8_compat import EXPECTED_FP32_SHA256
from hil.thermal_o2_real_snapshot_replay import EXPECTED_SHA256, celsius_from_raw_uint16, apply_p1, P1_MEAN


class ThermalO26SamplingTests(unittest.TestCase):
    def test_even_indices_are_deterministic_and_bounded(self) -> None:
        self.assertEqual(even_indices(7, 10), list(range(7)))
        self.assertEqual(even_indices(10, 10), list(range(10)))
        first = even_indices(100, 10)
        second = even_indices(100, 10)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 0)
        self.assertEqual(first[-1], 99)
        self.assertEqual(len(first), len(set(first)))
        self.assertLessEqual(len(first), 10)

    def test_time_bins_cover_closed_interval(self) -> None:
        self.assertEqual(assign_time_bin(0.0, 0.0, 24.0, 24), 0)
        self.assertEqual(assign_time_bin(23.9, 0.0, 24.0, 24), 23)
        self.assertEqual(assign_time_bin(24.0, 0.0, 24.0, 24), 23)

    def test_sample_selection_independent_of_outputs(self) -> None:
        frames = []
        for i in range(240):
            frames.append(
                {
                    "filename": f"f{i:04d}.npz",
                    "path": Path(f"f{i:04d}.npz"),
                    "frame_index": 0,
                    "timestamp": float(i),
                }
            )
        selected_a, plan_a = select_deterministic_sample(frames)
        selected_b, plan_b = select_deterministic_sample(frames)
        ids_a = [(x["filename"], x["frame_index"], x["bin"]) for x in selected_a]
        ids_b = [(x["filename"], x["frame_index"], x["bin"]) for x in selected_b]
        self.assertEqual(ids_a, ids_b)
        self.assertEqual(plan_a["actual_frames"], 240)
        self.assertTrue(plan_a["identities_frozen_before_inference"])
        self.assertEqual(plan_b["actual_frames"], 240)

    def test_conversion_and_p1_unchanged(self) -> None:
        raw = np.array([[0x0BC1]], dtype=np.uint16)
        celsius = celsius_from_raw_uint16(raw)
        z = apply_p1(np.array([[P1_MEAN]], dtype=np.float32))
        self.assertAlmostEqual(float(celsius[0, 0]), 300.9 - 273.15, places=4)
        self.assertAlmostEqual(float(z[0, 0]), 0.0, places=6)

    def test_lineage_and_canonical_policy_constants(self) -> None:
        self.assertEqual(
            EXPECTED_FP32_SHA256,
            "fbe891520f07e0534b1a7074dc819d8ed44bca58b27e35c78916c3ddae73a779",
        )
        self.assertEqual(
            EXPECTED_SHA256,
            "fa9730c29535477a3994c11e664474a0ca0116afaaa172889f47446ab2ac46be",
        )
        self.assertEqual(
            CANONICAL_POLICY_STATUS,
            "EQUIVALENCE_MEASURED_NO_PREEXISTING_ABSOLUTE_ACCEPTANCE_THRESHOLD",
        )

    def test_summarize_agreement_and_transitions(self) -> None:
        def row(agree: bool, sat: float, fclass: str, iclass: str, mae: float) -> dict:
            return {
                "filename": "x.npz",
                "frame_index": 0,
                "timestamp": 1.0,
                "float_class": fclass,
                "int8_class": iclass,
                "float_output": [0.0, 1.0, 0.0],
                "int8_dequantized": [0.0, 1.0, 0.0],
                "top1_agree": agree,
                "ranking_agree": agree,
                "probability_mae": mae,
                "margin_difference": 0.0,
                "int8_input": {"q_minus_128_fraction": sat, "q_plus_127_fraction": 0.0},
                "celsius": {"min": 10.0, "median": 20.0, "max": 30.0},
            }

        rows = [row(True, 0.05, "HUMAN_FALL", "HUMAN_FALL", 0.001) for _ in range(20)]
        rows += [row(False, 0.80, "HUMAN_FALL", "HUMAN_NORMAL", 0.5) for _ in range(5)]
        summary = summarize(rows)
        self.assertEqual(summary["n"], 25)
        self.assertEqual(summary["top1_agree"], 20)
        self.assertEqual(summary["top1_disagree"], 5)
        self.assertEqual(summary["transition_matrix"]["HUMAN_FALL"]["HUMAN_NORMAL"], 5)

    def test_float_and_int8_invoke_on_synthetic_p1_mean_frame(self) -> None:
        from hil.thermal_o2_5_float_int8_compat import invoke_float, load_float_t_b5
        from hil.thermal_o2_real_snapshot_replay import (
            HEIGHT,
            THERMAL_ARTIFACT,
            WIDTH,
            invoke_int8,
            load_locked_t_b5,
            quantize_int8,
        )

        float_path = Path("/tmp/safenest-o2-5-artifacts/SMALL_CNN_BASELINE_V1_P1_float32.tflite")
        if not float_path.is_file():
            self.skipTest("FLOAT counterpart not materialized locally")
        z = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
        float_rt = load_float_t_b5(float_path)
        int8_rt = load_locked_t_b5(THERMAL_ARTIFACT)
        float_out = invoke_float(float_rt, z)
        clipped, _unclipped = quantize_int8(z, int8_rt["input_scale"], int8_rt["input_zero_point"])
        int8_out = invoke_int8(int8_rt, clipped.reshape(1, HEIGHT, WIDTH, 1))
        self.assertTrue(float_out["finite"])
        self.assertEqual(len(float_out["probabilities"]), 3)
        self.assertEqual(len(int8_out["dequantized_output"]), 3)
        self.assertIn(float_out["class_name"], {"NOT_HUMAN", "HUMAN_NORMAL", "HUMAN_FALL"})
        self.assertIn(int8_out["class_name"], {"NOT_HUMAN", "HUMAN_NORMAL", "HUMAN_FALL"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
