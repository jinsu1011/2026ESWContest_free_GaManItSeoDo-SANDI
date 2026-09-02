"""Unit tests for RP-X0 O2.5 FLOAT/INT8 comparison math and lineage constants."""

from __future__ import annotations

import unittest

import numpy as np

from hil.thermal_o2_5_float_int8_compat import (
    EQUIVALENCE_CONTRACT,
    EXPECTED_FP32_SHA256,
    O2_SELECTED_FRAMES,
    compare_outputs,
    int8_low_clip_threshold_celsius,
    int8_q_minus_128_represented_celsius,
)
from hil.thermal_o2_real_snapshot_replay import EXPECTED_SHA256, P1_MEAN


class ThermalO25CompatTests(unittest.TestCase):
    def test_o2_frame_set_is_frozen_and_complete(self) -> None:
        roles = [item["role"] for item in O2_SELECTED_FRAMES]
        self.assertEqual(
            roles,
            [
                "early_field_capture",
                "middle_field_capture",
                "late_field_capture",
                "low_temperature_looking_frame",
                "high_temperature_looking_frame",
            ],
        )
        self.assertEqual(len({item["filename"] for item in O2_SELECTED_FRAMES}), 5)

    def test_lineage_shas_match_t_b4_t_b5_lock(self) -> None:
        self.assertEqual(
            EXPECTED_FP32_SHA256,
            "fbe891520f07e0534b1a7074dc819d8ed44bca58b27e35c78916c3ddae73a779",
        )
        self.assertEqual(
            EXPECTED_SHA256,
            "fa9730c29535477a3994c11e664474a0ca0116afaaa172889f47446ab2ac46be",
        )
        self.assertEqual(
            EQUIVALENCE_CONTRACT,
            "EQUIVALENCE_MEASURED_NO_PREEXISTING_ABSOLUTE_ACCEPTANCE_THRESHOLD",
        )

    def test_q_minus_128_is_not_the_clip_boundary(self) -> None:
        represented = int8_q_minus_128_represented_celsius()
        clip_edge = int8_low_clip_threshold_celsius()
        self.assertAlmostEqual(represented, 20.033537069976386, places=6)
        self.assertLess(clip_edge, represented)
        self.assertGreater(clip_edge, 19.4)
        self.assertLess(clip_edge, 19.8)
        self.assertAlmostEqual(P1_MEAN, 22.769290618485442)

    def test_compare_outputs_detects_agreement_and_distance(self) -> None:
        agree = compare_outputs(
            np.array([0.1, 0.7, 0.2]),
            np.array([0.12, 0.68, 0.20]),
        )
        self.assertTrue(agree["top1_agree"])
        self.assertTrue(agree["ranking_agree"])
        self.assertEqual(agree["float_class"], "HUMAN_NORMAL")
        disagree = compare_outputs(
            np.array([0.05, 0.10, 0.85]),
            np.array([0.02, 0.90, 0.08]),
        )
        self.assertFalse(disagree["top1_agree"])
        self.assertFalse(disagree["ranking_agree"])
        self.assertGreater(disagree["probability_mae"], 0.3)

    def test_nonmonotonic_saturation_is_insufficient_evidence(self) -> None:
        from hil.thermal_o2_5_float_int8_compat import classify_saturation_relationship

        def row(agree: bool, sat: float, mae: float) -> dict:
            return {
                "int8": {"low_saturation_fraction": sat},
                "comparison": {"top1_agree": agree, "probability_mae": mae, "float_margin": 1.0, "int8_margin": 0.99},
            }

        rows = [
            row(True, 0.13, 0.001),
            row(False, 0.70, 0.66),
            row(True, 0.78, 0.001),
            row(True, 0.24, 0.001),
            row(True, 0.29, 0.001),
        ]
        self.assertEqual(classify_saturation_relationship(rows), "INSUFFICIENT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
