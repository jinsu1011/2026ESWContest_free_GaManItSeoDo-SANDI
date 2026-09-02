#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tests/test_thermal_interpreter.py
SafeNest Thermal Interpreter 단위 및 회귀 자동 테스트 수트 (Standard Unittest & Pytest 지원)
"""

import os
import sys
from pathlib import Path
import hashlib
import json
import unittest
import numpy as np

# 프로젝트 루트 경로 추가
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inference.thermal_interpreter import (
    ThermalInterpreter,
    override_posture_from_bbox,
)


C0_CLASS_MAP = {
    0: "NOT_HUMAN",
    1: "HUMAN_NORMAL",
    2: "HUMAN_FALL_PROXY",
}
LEGACY_INT8_CLASS_MAP = {
    0: "NOT_HUMAN",
    1: "HUMAN_NORMAL",
    2: "HUMAN_FALL",
}
HUMAN_PROBS = np.array([0.05, 0.70, 0.25], dtype=np.float32)
NOT_HUMAN_PROBS = np.array([0.90, 0.05, 0.05], dtype=np.float32)


def _blob(row_slice: slice, col_slice: slice) -> np.ndarray:
    frame = np.zeros((62, 80), dtype=np.float32)
    frame[row_slice, col_slice] = 1.0
    return frame


class TestBboxPostureOverride(unittest.TestCase):
    def test_vertical_blob_is_standing(self):
        spatial = _blob(slice(10, 50), slice(30, 40))
        result = override_posture_from_bbox(
            2, C0_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 1)
        self.assertEqual(result.class_name, "HUMAN_NORMAL")
        self.assertTrue(result.overlay_applied)
        self.assertEqual(result.posture_source, "BBOX")
        self.assertEqual(result.model_class_name, "HUMAN_FALL_PROXY")
        self.assertGreater(result.bbox_height, result.bbox_width)
        self.assertAlmostEqual(result.confidence, 0.70, places=5)

    def test_horizontal_blob_is_lying(self):
        spatial = _blob(slice(20, 30), slice(10, 60))
        result = override_posture_from_bbox(
            1, C0_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 2)
        self.assertEqual(result.class_name, "HUMAN_FALL_PROXY")
        self.assertTrue(result.overlay_applied)
        self.assertEqual(result.posture_source, "BBOX")
        self.assertEqual(result.model_class_name, "HUMAN_NORMAL")
        self.assertGreater(result.bbox_width, result.bbox_height)
        self.assertAlmostEqual(result.confidence, 0.25, places=5)

    def test_square_blob_counts_as_sitting(self):
        spatial = _blob(slice(20, 40), slice(30, 50))
        result = override_posture_from_bbox(
            2, C0_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 1)
        self.assertEqual(result.class_name, "HUMAN_NORMAL")
        self.assertTrue(result.overlay_applied)
        self.assertAlmostEqual(result.confidence, 0.70, places=5)

    def test_empty_mask_is_presence_only_not_model_pose(self):
        spatial = np.zeros((62, 80), dtype=np.float32)
        result = override_posture_from_bbox(
            1, C0_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 1)
        self.assertEqual(result.class_name, "HUMAN_NORMAL")
        self.assertFalse(result.overlay_applied)
        self.assertEqual(result.posture_source, "PRESENCE_ONLY")
        self.assertAlmostEqual(result.confidence, 0.70, places=5)

    def test_too_few_hot_pixels_does_not_keep_model_fall(self):
        spatial = np.zeros((62, 80), dtype=np.float32)
        spatial[0, :10] = 1.0
        result = override_posture_from_bbox(
            2, C0_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 1)
        self.assertEqual(result.class_name, "HUMAN_NORMAL")
        self.assertFalse(result.overlay_applied)
        self.assertEqual(result.posture_source, "PRESENCE_ONLY")
        self.assertEqual(result.model_class_name, "HUMAN_FALL_PROXY")

    def test_not_human_is_never_overridden(self):
        spatial = _blob(slice(10, 50), slice(30, 40))
        result = override_posture_from_bbox(
            0, C0_CLASS_MAP, spatial, NOT_HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 0)
        self.assertEqual(result.class_name, "NOT_HUMAN")
        self.assertEqual(result.posture_source, "NOT_HUMAN")
        self.assertFalse(result.overlay_applied)
        self.assertAlmostEqual(result.confidence, 0.90, places=5)

    def test_legacy_int8_uses_class_map_names(self):
        spatial = _blob(slice(20, 30), slice(10, 60))
        result = override_posture_from_bbox(
            1, LEGACY_INT8_CLASS_MAP, spatial, HUMAN_PROBS
        )
        self.assertEqual(result.class_index, 2)
        self.assertEqual(result.class_name, "HUMAN_FALL")
        self.assertTrue(result.overlay_applied)


class TestThermalInterpreter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = ThermalInterpreter(project_root=PROJECT_ROOT)

    def test_manifest_hash_matches(self):
        manifest_path = PROJECT_ROOT / "models/model_manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selector = manifest["active_runtime_selectors"]["thermal"]
        self.assertEqual(selector, "thermal_public_sdt_fp32_active")
        self.assertEqual(self.runner.model_selector, selector)
        model = manifest["models"][selector]
        model_path = PROJECT_ROOT / model["path"]

        actual_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, model["sha256"])
        self.assertEqual(model_path.stat().st_size, model["size_bytes"])

    def test_tensor_contract(self):
        self.assertEqual(self.runner.input_info["shape"].tolist(), [1, 62, 80, 1])
        self.assertEqual(self.runner.output_info["shape"].tolist(), [1, 3])
        self.assertEqual(self.runner.input_info["dtype"], np.float32)
        self.assertEqual(self.runner.output_info["dtype"], np.float32)

        input_scale, _ = self.runner.input_info["quantization"]
        output_scale, _ = self.runner.output_info["quantization"]
        self.assertEqual(float(input_scale), 0.0)
        self.assertEqual(float(output_scale), 0.0)

    def test_supported_shapes(self):
        for shape in [(62, 80), (62, 80, 1), (1, 62, 80, 1)]:
            frame = np.zeros(shape, dtype=np.float32)
            result = self.runner.predict(frame)

            self.assertIn(result.class_index, (0, 1, 2))
            self.assertEqual(len(result.probabilities), 3)
            self.assertTrue(np.all(np.isfinite(result.probabilities)))
            self.assertAlmostEqual(sum(result.probabilities), 1.0, places=4)

    def test_rejects_wrong_shape(self):
        frame = np.zeros((32, 24), dtype=np.float32)
        with self.assertRaises(ValueError):
            self.runner.predict(frame)

    def test_rejects_invalid_values(self):
        for bad_value in [np.nan, np.inf, -np.inf]:
            frame = np.zeros((62, 80), dtype=np.float32)
            frame[0, 0] = bad_value
            with self.assertRaises(ValueError):
                self.runner.predict(frame)

    def test_current_npz_class_smoke(self):
        dataset_path = PROJECT_ROOT / "thermal/processed_thermal_80x62.npz"
        if not dataset_path.exists():
            self.skipTest("NPZ dataset file not found")

        data = np.load(dataset_path)
        frames = data["X"]
        labels = data["y"]

        seen = set()
        for class_index in (0, 1, 2):
            indices = np.where(labels == class_index)[0]
            self.assertGreater(len(indices), 0, f"class {class_index} is absent")

            result = self.runner.predict(frames[int(indices[0])])
            self.assertIn(result.class_index, (0, 1, 2))
            seen.add(class_index)

        self.assertEqual(seen, {0, 1, 2})

    def test_prediction_does_not_collapse_to_one_class(self):
        dataset_path = PROJECT_ROOT / "thermal/processed_thermal_80x62.npz"
        if not dataset_path.exists():
            self.skipTest("NPZ dataset file not found")

        data = np.load(dataset_path)
        frames = data["X"]
        labels = data["y"]

        selected = []
        for class_index in (0, 1, 2):
            indices = np.where(labels == class_index)[0][:100]
            selected.extend(int(index) for index in indices)

        predictions = {
            self.runner.predict(frames[index]).class_index
            for index in selected
        }

        self.assertGreaterEqual(
            len(predictions), 2, f"model output collapsed to classes: {sorted(predictions)}"
        )


if __name__ == "__main__":
    unittest.main()
