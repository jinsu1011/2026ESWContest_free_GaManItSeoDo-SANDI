#!/usr/bin/env python3
"""Manifest, preprocessing, and TFLite checks for Team Thermal V2 test staging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inference.thermal_interpreter import (
    ThermalInterpreter,
    prepare_frame_robust_p2_p98_v1,
    tflite,
)

MANIFEST_PATH = PROJECT_ROOT / "models" / "model_manifest.json"
BASELINE = "thermal_public_sdt_fp32_active"
CANDIDATE_A = "thermal_tv2_candidate_a_a0_fp32_v1"
CANDIDATE_B = "thermal_tv2_candidate_b_seed42_fp32_test_v1"
A_SHA = "a158a70c4735e28eec70b5a996f82c91f452b94bcc24c040838143f4a55b1985"
B_SHA = "f5b9ecef8def2668bb65131671134e443c600e38c2575d4350e242f1abc0dfb4"


class ThermalTv2ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.models = cls.manifest["models"]

    def test_baseline_active_selector_unchanged(self) -> None:
        self.assertEqual(self.manifest["active_runtime_selectors"]["thermal"], BASELINE)
        baseline = self.models[BASELINE]
        self.assertTrue(baseline["active_runtime_selector"])
        self.assertTrue(baseline["default_activation"])
        self.assertEqual(baseline["sha256"], "f88d65d76dbb21862e1f3cdff17cefb038a432047ded6ac8d5563bc8bc8c52ff")

    def test_candidates_exist_as_nondefault_allowlisted_entries(self) -> None:
        allowlist = self.manifest["controlled_test_runtime_selectors"]["thermal"]
        self.assertEqual(allowlist, [BASELINE, CANDIDATE_A, CANDIDATE_B])
        for key in (CANDIDATE_A, CANDIDATE_B):
            entry = self.models[key]
            self.assertFalse(entry["active_runtime_selector"])
            self.assertFalse(entry["default_activation"])
            self.assertTrue(entry["controlled_test_allowed"])
            self.assertFalse(entry["safety_authority"])
            self.assertEqual(entry["risk_authority"], "LIMITED_POSTURE_PROXY")
            self.assertEqual(entry["proxy_risk_score"], 0.4)
            self.assertFalse(entry["DEVICE_VALIDATED"])
            self.assertEqual(entry["PI_SMOKE"], "NOT_PERFORMED")
            self.assertEqual(entry["preprocessing_id"], "FRAME_ROBUST_P2_P98_V1")
            path = PROJECT_ROOT / entry["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            self.assertEqual(path.stat().st_size, entry["size_bytes"])

    def test_candidate_status_labels(self) -> None:
        a_notes = " ".join(self.models[CANDIDATE_A]["notes"])
        b_notes = " ".join(self.models[CANDIDATE_B]["notes"])
        self.assertIn("A_PREFERRED_OFFLINE", a_notes)
        self.assertIn("CONTROLLED_TEAM_TEST", a_notes)
        self.assertNotEqual(self.models[CANDIDATE_A]["status"], "production")
        self.assertFalse(self.models[CANDIDATE_A]["default_activation"])
        self.assertEqual(self.models[CANDIDATE_B]["offline_family_status"], "B_NOT_COMPETITIVE")
        self.assertEqual(self.models[CANDIDATE_B]["runtime_role"], "CONTROLLED_COMPARISON_ONLY")
        self.assertFalse(self.models[CANDIDATE_B]["preferred"])
        self.assertIn("B_NOT_COMPETITIVE", b_notes)
        self.assertIn("CONTROLLED_COMPARISON_ONLY", b_notes)

    def test_hashes_match_committed_binaries(self) -> None:
        self.assertEqual(self.models[CANDIDATE_A]["sha256"], A_SHA)
        self.assertEqual(self.models[CANDIDATE_B]["sha256"], B_SHA)
        self.assertEqual(
            self.models[CANDIDATE_B]["source_keras_sha256"],
            "42563c3316e9e8511ab897aaa4dfd9a154887f3a0270d5dfb77a7a344cd3ff35",
        )


class ThermalTv2PreprocessingTests(unittest.TestCase):
    def test_baseline_minmax_path_still_used_by_default_interpreter(self) -> None:
        runner = ThermalInterpreter(project_root=PROJECT_ROOT)
        self.assertEqual(runner.model_selector, BASELINE)
        frame = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
        # Existing baseline helper remains min-max; full-frame contract uses 62x80.
        full = np.linspace(10.0, 40.0, 62 * 80, dtype=np.float32).reshape(62, 80)
        prepared = runner._prepare_float_frame(full)
        self.assertEqual(prepared.shape, (1, 62, 80, 1))
        self.assertEqual(prepared.dtype, np.float32)
        self.assertAlmostEqual(float(prepared.min()), 0.0, places=5)
        self.assertAlmostEqual(float(prepared.max()), 1.0, places=5)
        self.assertNotEqual(runner.preprocessing_id, "FRAME_ROBUST_P2_P98_V1")
        del frame

    def test_robust_p2_p98_is_deterministic_and_shared(self) -> None:
        frame = np.arange(62 * 80, dtype=np.float32).reshape(62, 80)
        first = prepare_frame_robust_p2_p98_v1(frame)
        second = prepare_frame_robust_p2_p98_v1(frame.copy())
        self.assertEqual(first.shape, (1, 62, 80, 1))
        self.assertEqual(first.dtype, np.float32)
        self.assertTrue(np.isfinite(first).all())
        self.assertGreaterEqual(float(first.min()), 0.0)
        self.assertLessEqual(float(first.max()), 1.0)
        np.testing.assert_array_equal(first, second)
        a = ThermalInterpreter(project_root=PROJECT_ROOT, model_key=CANDIDATE_A)
        b = ThermalInterpreter(project_root=PROJECT_ROOT, model_key=CANDIDATE_B)
        self.assertEqual(a.preprocessing_id, "FRAME_ROBUST_P2_P98_V1")
        self.assertEqual(b.preprocessing_id, "FRAME_ROBUST_P2_P98_V1")
        np.testing.assert_array_equal(
            a._prepare_model_frame(frame),
            b._prepare_model_frame(frame),
        )

    def test_robust_rejects_nan_inf_and_wrong_shape(self) -> None:
        with self.assertRaises(ValueError):
            prepare_frame_robust_p2_p98_v1(np.zeros((32, 24), dtype=np.float32))
        for bad in (np.nan, np.inf, -np.inf):
            frame = np.zeros((62, 80), dtype=np.float32)
            frame[0, 0] = bad
            with self.assertRaises(ValueError):
                prepare_frame_robust_p2_p98_v1(frame)


class ThermalTv2TfliteTests(unittest.TestCase):
    def _load_and_invoke(self, selector: str, expected_sha: str) -> None:
        runner = ThermalInterpreter(project_root=PROJECT_ROOT, model_key=selector)
        self.assertEqual(runner.sha256_hash, expected_sha)
        self.assertEqual(runner.input_info["shape"].tolist(), [1, 62, 80, 1])
        self.assertEqual(runner.output_info["shape"].tolist(), [1, 3])
        self.assertEqual(runner.input_info["dtype"], np.float32)
        self.assertEqual(runner.output_info["dtype"], np.float32)
        frame = np.linspace(18.0, 36.0, 62 * 80, dtype=np.float32).reshape(62, 80)
        result = runner.predict(frame)
        self.assertTrue(np.isfinite(result.probabilities).all())
        self.assertEqual(len(result.probabilities), 3)
        self.assertEqual(result.model_selector, selector)
        self.assertEqual(result.preprocessing_id, "FRAME_ROBUST_P2_P98_V1")
        self.assertEqual(result.model_sha256, expected_sha)

        raw = tflite.Interpreter(model_path=str(PROJECT_ROOT / runner.model_meta["path"]))
        raw.allocate_tensors()
        inp = raw.get_input_details()[0]
        out = raw.get_output_details()[0]
        prepared = prepare_frame_robust_p2_p98_v1(frame)
        raw.set_tensor(inp["index"], prepared)
        raw.invoke()
        output = raw.get_tensor(out["index"])
        self.assertTrue(np.isfinite(output).all())
        self.assertEqual(list(output.shape), [1, 3])

    def test_candidate_a_load_allocate_invoke(self) -> None:
        self._load_and_invoke(CANDIDATE_A, A_SHA)

    def test_candidate_b_load_allocate_invoke(self) -> None:
        self._load_and_invoke(CANDIDATE_B, B_SHA)


if __name__ == "__main__":
    unittest.main()
