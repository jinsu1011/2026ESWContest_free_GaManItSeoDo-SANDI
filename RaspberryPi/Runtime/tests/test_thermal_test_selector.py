"""Fail-closed Thermal controlled-test selector checks."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import patch

from backend.runtime_status import runtime_status_document
from paths import MODEL_MANIFEST, RUNTIME_ROOT
from tests.test_runtime_status import ai_result, sensor, state
from thermal_test_selector import (
    SELECTOR_ENV,
    TEST_MODE_ENV,
    ThermalSelectorError,
    resolve_thermal_runtime_selector,
)


BASELINE = "thermal_public_sdt_fp32_active"
CANDIDATE_A = "thermal_tv2_candidate_a_a0_fp32_v1"
CANDIDATE_B = "thermal_tv2_candidate_b_seed42_fp32_test_v1"


def load_runtime_module():
    if str(RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT))
    module_name = "_safenest_test_thermal_selector_runtime"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        module_name, RUNTIME_ROOT / "ai" / "runtime.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load ai/runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ThermalTestSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        cls.runtime = load_runtime_module()
        cls.LazyModel = cls.runtime.LazyModel
        cls.ModelRuntimeUnavailable = cls.runtime.ModelRuntimeUnavailable

    def test_no_test_env_selects_baseline(self) -> None:
        with patch.dict(os.environ, {TEST_MODE_ENV: "", SELECTOR_ENV: ""}):
            selector = resolve_thermal_runtime_selector(self.manifest)
            self.assertEqual(selector, BASELINE)
            model = self.LazyModel("thermal")
            self.assertEqual(model.model_selector, BASELINE)

    def test_test_selector_without_test_mode_is_rejected(self) -> None:
        with patch.dict(os.environ, {TEST_MODE_ENV: "", SELECTOR_ENV: CANDIDATE_A}):
            with self.assertRaises(ThermalSelectorError):
                resolve_thermal_runtime_selector(self.manifest)
            with self.assertRaises(self.ModelRuntimeUnavailable):
                self.LazyModel("thermal")

    def test_explicit_test_a_and_b(self) -> None:
        for choice, expected in (("a", CANDIDATE_A), ("b", CANDIDATE_B)):
            with self.subTest(choice=choice):
                with patch.dict(
                    os.environ,
                    {TEST_MODE_ENV: "1", SELECTOR_ENV: expected},
                    clear=False,
                ):
                    selector = resolve_thermal_runtime_selector(self.manifest)
                    self.assertEqual(selector, expected)
                    model = self.LazyModel("thermal")
                    self.assertEqual(model.model_selector, expected)
                    interpreter = model._load()
                    self.assertEqual(interpreter.model_selector, expected)
                    self.assertEqual(model.model_selector, interpreter.model_selector)

    def test_unknown_selector_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {TEST_MODE_ENV: "1", SELECTOR_ENV: "not_a_real_thermal_model"},
            clear=False,
        ):
            with self.assertRaises(ThermalSelectorError):
                resolve_thermal_runtime_selector(self.manifest)
            with self.assertRaises(self.ModelRuntimeUnavailable):
                self.LazyModel("thermal")

    def test_runtime_status_reports_selected_selector(self) -> None:
        result = ai_result(available=True)
        result["metadata"] = {
            "model_selector": CANDIDATE_A,
            "risk_authority": "LIMITED_POSTURE_PROXY",
            "preprocessing_id": "FRAME_ROBUST_P2_P98_V1",
        }
        thermal = runtime_status_document(
            state(thermal=sensor()), {"thermal": result}
        )["sensors"]["thermal"]
        self.assertEqual(thermal["model_selector"], CANDIDATE_A)
        self.assertEqual(thermal["preprocessing_id"], "FRAME_ROBUST_P2_P98_V1")


if __name__ == "__main__":
    unittest.main()
