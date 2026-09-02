from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inference.thermal_interpreter import tflite


MANIFEST_PATH = ROOT / "models" / "model_manifest.json"


class ThermalPublicSdtActiveModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.legacy = cls.manifest["models"]["thermal"]
        cls.active = cls.manifest["models"]["thermal_public_sdt_fp32_active"]
        cls.model_path = ROOT / cls.active["path"]

    def test_legacy_thermal_model_is_preserved_but_nonactive(self) -> None:
        self.assertEqual(self.legacy["model_id"], "thermal_fall_int8")
        self.assertEqual(self.legacy["path"], "models/thermal/thermal_fall_int8_v0.1.0.tflite")
        self.assertEqual(
            self.legacy["sha256"],
            "5b56da8d127ccef85f30b6459cc0cfe2d86490e41f3caa5bd2a7b70bbc46ae84",
        )
        self.assertFalse(self.legacy["deployment_allowed"])
        self.assertFalse(self.legacy["active_runtime_selector"])

    def test_active_identity_and_claim_boundary(self) -> None:
        self.assertTrue(self.model_path.is_file())
        self.assertEqual(self.model_path.stat().st_size, 70592)
        self.assertEqual(
            hashlib.sha256(self.model_path.read_bytes()).hexdigest(),
            "f88d65d76dbb21862e1f3cdff17cefb038a432047ded6ac8d5563bc8bc8c52ff",
        )
        self.assertEqual(
            self.manifest["active_runtime_selectors"]["thermal"],
            "thermal_public_sdt_fp32_active",
        )
        self.assertTrue(self.active["deployment_allowed"])
        self.assertTrue(self.active["active_runtime_selector"])
        self.assertTrue(self.active["default_activation"])
        self.assertEqual(self.active["artifact_release"], "FINAL_RUNTIME_MODEL")
        self.assertFalse(self.active["safety_authority"])
        self.assertEqual(self.active["risk_authority"], "LIMITED_POSTURE_PROXY")
        self.assertEqual(self.active["proxy_risk_score"], 0.4)
        self.assertEqual(self.active["runtime_role"], "ACTIVE_THERMAL_PUBLIC_SDT_SOFTWARE_ONLY")
        self.assertEqual(self.active["class_map"]["2"], "HUMAN_FALL_PROXY")
        self.assertEqual(self.active["hardware_validation"], "BLOCKED_HARDWARE")

    def test_fp32_tensor_contract_and_inference(self) -> None:
        interpreter = tflite.Interpreter(model_path=str(self.model_path))
        interpreter.allocate_tensors()
        input_info = interpreter.get_input_details()[0]
        output_info = interpreter.get_output_details()[0]

        self.assertEqual(input_info["shape"].tolist(), [1, 62, 80, 1])
        self.assertEqual(output_info["shape"].tolist(), [1, 3])
        self.assertEqual(input_info["dtype"], np.float32)
        self.assertEqual(output_info["dtype"], np.float32)
        self.assertEqual(float(input_info["quantization"][0]), 0.0)
        self.assertEqual(float(output_info["quantization"][0]), 0.0)

        sample = np.linspace(0.0, 1.0, 62 * 80, dtype=np.float32).reshape(1, 62, 80, 1)
        interpreter.set_tensor(input_info["index"], sample)
        interpreter.invoke()
        probabilities = interpreter.get_tensor(output_info["index"])[0]

        self.assertTrue(np.all(np.isfinite(probabilities)))
        self.assertTrue(np.all(probabilities >= 0.0))
        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=5)

    def test_metadata_matches_manifest(self) -> None:
        metadata_path = ROOT / self.active["metadata_path"]
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["artifact_sha256"], self.active["sha256"])
        self.assertEqual(metadata["artifact_size_bytes"], self.active["size_bytes"])
        self.assertEqual(
            metadata["deployment_boundary"]["runtime_role"],
            "ACTIVE_THERMAL_PUBLIC_SDT_SOFTWARE_ONLY",
        )
        self.assertEqual(
            metadata["deployment_boundary"]["risk_authority"],
            "LIMITED_POSTURE_PROXY",
        )
        self.assertEqual(metadata["dataset"]["locked_public_test_access_count"], 0)


if __name__ == "__main__":
    unittest.main()
