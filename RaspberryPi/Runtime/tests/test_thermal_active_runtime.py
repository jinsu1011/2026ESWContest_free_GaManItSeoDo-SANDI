"""End-to-end selector and invocation checks for the active thermal runtime."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

import numpy as np


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
ONDEVICE_ROOT = RUNTIME_ROOT.parent / "Ondevice_AI"
RUNTIME_MODULE_PATH = RUNTIME_ROOT / "ai" / "runtime.py"


def load_runtime_module():
    if str(RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT))
    module_name = "_safenest_test_active_thermal_runtime"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(module_name, RUNTIME_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {RUNTIME_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ActiveThermalRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_runtime_module()
        cls.manifest = json.loads(
            (ONDEVICE_ROOT / "models" / "model_manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_runtime_and_manifest_select_the_final_fp32_model(self) -> None:
        selector = "thermal_public_sdt_fp32_active"
        self.assertEqual(
            self.runtime.LazyModel._ADAPTERS["thermal"][2],
            selector,
        )
        self.assertEqual(
            self.manifest["active_runtime_selectors"]["thermal"],
            selector,
        )
        model = self.manifest["models"][selector]
        self.assertEqual(model["artifact_release"], "FINAL_RUNTIME_MODEL")
        self.assertEqual(model["model_lifecycle"], "ACTIVE_FINAL")
        self.assertTrue(model["deployment_allowed"])
        self.assertEqual(model["input"]["dtype"], "float32")
        self.assertFalse(model["safety_authority"])
        self.assertEqual(model["risk_authority"], "LIMITED_POSTURE_PROXY")

    def test_lazy_runtime_loads_and_invokes_active_artifact(self) -> None:
        model = self.runtime.LazyModel("thermal")
        interpreter = model._load()
        prediction = model.predict(
            np.linspace(18.0, 36.0, 62 * 80, dtype=np.float32).reshape(62, 80)
        )

        self.assertEqual(
            interpreter.model_selector,
            "thermal_public_sdt_fp32_active",
        )
        self.assertEqual(model.model_selector, interpreter.model_selector)
        self.assertEqual(
            interpreter.model_meta["artifact_release"],
            "FINAL_RUNTIME_MODEL",
        )
        self.assertEqual(model.model_meta["risk_authority"], "LIMITED_POSTURE_PROXY")
        self.assertFalse(model.model_meta["safety_authority"])
        self.assertIn(
            prediction.class_name,
            {"NOT_HUMAN", "HUMAN_NORMAL", "HUMAN_FALL_PROXY"},
        )
        self.assertTrue(np.isfinite(prediction.probabilities).all())
        self.assertAlmostEqual(float(sum(prediction.probabilities)), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
