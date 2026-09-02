"""Stage 7 Mac-offline deployment preflight and configuration contract tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest

from paths import MODEL_MANIFEST
from backend.runtime_status import runtime_status_document
from backend.views import status_document
from deployment.verify_bundle import verify
from hil.preflight import (
    EXPECTED_ENV_KEYS,
    _mmwave_selector_contract_checks,
    _runtime_import_check,
    offline_preflight_document,
)
from tests.test_runtime_status import ai_result, sensor, state


ROOT = Path(__file__).resolve().parent.parent


class Stage7OfflinePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = offline_preflight_document(ROOT)

    def test_offline_preflight_passes_without_claiming_pi_or_sensors(self) -> None:
        self.assertTrue(self.document["ok"], json.dumps(self.document, indent=2)[:4000])
        self.assertEqual(self.document["mode"], "MAC_OFFLINE_PREFLIGHT")
        self.assertEqual(self.document["pi_checks"], "NOT_RUN")
        self.assertEqual(self.document["sensor_checks"], "NOT_RUN")
        self.assertTrue(self.document["mac_pass_does_not_imply_pi_pass"])

    def test_runtime_entrypoint_and_lcd_web_assets_exist(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["file_backend_run_backend.py"]["passed"])
        self.assertTrue(names["file_deployment_run_pi.sh"]["passed"])
        self.assertTrue(names["file_web_dashboard_index.html"]["passed"])
        self.assertTrue(names["file_sources_display-test2_raspberry_pi_lcd_static_display.html"]["passed"])
        self.assertTrue(names["dashboard_runtime_badge"]["passed"])
        self.assertTrue(names["lcd_consumes_backend_runtime_status"]["passed"])

    def test_listener_contract_remains_tcp_9000_udp_5005(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["tcp_default_port_9000"]["passed"])
        self.assertTrue(names["udp_default_port_5005"]["passed"])
        self.assertTrue(names["http_default_port_8000"]["passed"])

    def test_artifact_selection_activates_public_sdt_and_not_old_b(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["thermal_active_selector_is_public_sdt_fp32"]["passed"])
        self.assertTrue(
            names["thermal_proxy_risk_is_bounded_without_emergency_authority"]["passed"]
        )
        self.assertTrue(names["thermal_legacy_int8_is_nonactive"]["passed"])
        self.assertNotIn("mmwave_primary_deployment_blocked", names)
        self.assertTrue(names["mmwave_primary_selector_is_b23"]["passed"])
        self.assertTrue(names["mmwave_historical_b_not_active"]["passed"])
        self.assertTrue(names["mmwave_device_validation_not_overclaimed"]["passed"])
        self.assertTrue(names["model_thermal_sha256"]["passed"])
        thermal = names["thermal_active_selector_is_public_sdt_fp32"]["observed"]
        self.assertEqual(thermal["selector_key"], "thermal_public_sdt_fp32_active")
        self.assertEqual(thermal["artifact_release"], "FINAL_RUNTIME_MODEL")
        self.assertEqual(thermal["class_2"], "HUMAN_FALL_PROXY")
        self.assertTrue(names["model_mmwave_sha256"]["passed"])
        observed = names["mmwave_primary_selector_is_b23"]["observed"]
        self.assertEqual(observed["model_id"], "M-PV2_FAMILY_B_TRACE_TCN_BREATHING_RR_QUALITY")
        self.assertEqual(observed["runtime_role"], "ACTIVE_B23_PROTOTYPE")
        self.assertTrue(observed["active_runtime_selector"])
        self.assertTrue(observed["deployment_allowed"])
        self.assertTrue(observed["HISTORICAL_B_NOT_ACTIVE"])
        self.assertFalse(observed["DEVICE_VALIDATED"])
        self.assertEqual(observed["hardware_validation"], "NOT_PERFORMED")
        self.assertEqual(observed["PI_SMOKE"], "NOT_PERFORMED")
        self.assertTrue(observed["PRESENCE_GATE_REQUIRED"])
        self.assertTrue(str(observed["path"]).endswith("models/mmwave/m_prot_b23/candidate_seed_23.pt"))

    def test_historical_v010_primary_selector_fails_offline_preflight(self) -> None:
        manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        fixture = dict(manifest["models"]["mmwave_v0_1_0"])
        fixture["active_runtime_selector"] = True
        checks = _mmwave_selector_contract_checks(fixture)
        names = {item["name"]: item for item in checks}
        self.assertFalse(names["mmwave_primary_selector_is_b23"]["passed"])
        self.assertTrue(names["mmwave_primary_selector_is_b23"]["required"])
        with patch("hil.preflight._mmwave_selector_contract_checks", return_value=checks):
            document = offline_preflight_document(ROOT)
        self.assertFalse(document["ok"])
        failed = {item["name"]: item for item in document["checks"] if item["required"] and not item["passed"]}
        self.assertIn("mmwave_primary_selector_is_b23", failed)

    def test_historical_b_active_selector_fails(self) -> None:
        fixture = {
            "model_id": "M-B3_CONV1D_GAP_BASELINE",
            "runtime_role": "HISTORICAL_B_STAGE",
            "active_runtime_selector": True,
            "deployment_allowed": True,
            "HISTORICAL_B_NOT_ACTIVE": False,
            "path": "models/rp_x0_b_complete/mmwave/M-B3_CONV1D_GAP_BASELINE_seed42_M-B5_CAL_CLASS_BALANCED_120_int8.tflite",
            "DEVICE_VALIDATED": False,
            "hardware_validation": "NOT_PERFORMED",
            "PI_SMOKE": "NOT_PERFORMED",
            "PRESENCE_GATE_REQUIRED": True,
        }
        checks = _mmwave_selector_contract_checks(fixture)
        names = {item["name"]: item for item in checks}
        self.assertFalse(names["mmwave_primary_selector_is_b23"]["passed"])
        self.assertFalse(names["mmwave_historical_b_not_active"]["passed"])
        self.assertTrue(names["mmwave_historical_b_not_active"]["required"])
        with patch("hil.preflight._mmwave_selector_contract_checks", return_value=checks):
            document = offline_preflight_document(ROOT)
        self.assertFalse(document["ok"])

    def test_device_validation_overclaim_fails_even_when_b23_identity_is_correct(self) -> None:
        manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        fixture = dict(manifest["models"]["mmwave"])
        fixture["DEVICE_VALIDATED"] = True
        fixture["PI_SMOKE"] = "PASS"
        fixture["hardware_validation"] = "PASS"
        checks = _mmwave_selector_contract_checks(fixture)
        names = {item["name"]: item for item in checks}
        self.assertTrue(names["mmwave_primary_selector_is_b23"]["passed"])
        self.assertFalse(names["mmwave_device_validation_not_overclaimed"]["passed"])
        self.assertTrue(names["mmwave_device_validation_not_overclaimed"]["required"])
        with patch("hil.preflight._mmwave_selector_contract_checks", return_value=checks):
            document = offline_preflight_document(ROOT)
        self.assertFalse(document["ok"])

    def test_no_developer_absolute_runtime_paths(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["no_developer_absolute_runtime_paths"]["passed"])
        self.assertTrue(names["db_path_repository_relative"]["passed"])
        self.assertTrue(names["venv_path_repository_relative"]["passed"])

    def test_env_example_covers_runtime_keys(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["env_example_documents_runtime_keys"]["passed"], names["env_example_documents_runtime_keys"])
        self.assertIn("SAFENEST_VENV_PATH", EXPECTED_ENV_KEYS)
        self.assertIn("SAFENEST_THERMAL_UDP_PORT", EXPECTED_ENV_KEYS)

    def test_status_contract_and_partial_availability_are_preserved(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        self.assertTrue(names["ready_with_limitations_preserved"]["passed"])
        self.assertTrue(names["thermal_active_limited_risk_status_preserved"]["passed"])
        self.assertTrue(names["pir_not_applicable_preserved"]["passed"])
        publication = {
            "timestamp": 1.0,
            "state": state(),
            "ai": {"ai": {"co2": ai_result(available=True)}},
            "risk": {"system_health": "HEALTHY", "components": {}},
            "emergency": {},
        }
        document = status_document(publication)
        self.assertEqual(document["runtime_status"]["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(document["thermal"]["runtime_status"]["ai_status"], "BLOCKED")
        self.assertEqual(document["thermal"]["runtime_status"]["sensor_status"], "AVAILABLE")
        self.assertEqual(document["pir"]["runtime_status"]["ai_status"], "NOT_APPLICABLE")
        self.assertEqual(document["pir"]["runtime_status"]["sensor_value_status"], "NO_MOTION")
        self.assertEqual(document["co2"]["runtime_status"]["ai_status"], "ACTIVE")
        self.assertEqual(document["mmwave"]["runtime_status"]["ai_status"], "BLOCKED")
        self.assertEqual(
            document["mmwave"]["runtime_status"]["blocked_reason"],
            "CANONICAL_FRESHNESS_METADATA_MISSING",
        )
        blocked = runtime_status_document(state(), {})
        self.assertNotEqual(blocked["sensors"]["co2"]["ai_status"], "ACTIVE")
        self.assertEqual(blocked["sensors"]["mmwave"]["ai_status"], "BLOCKED")
        self.assertEqual(
            blocked["sensors"]["mmwave"]["blocked_reason"],
            "CANONICAL_FRESHNESS_METADATA_MISSING",
        )

    def test_bundle_verifier_includes_runtime_status_module(self) -> None:
        result = verify(ROOT)
        self.assertNotIn("backend/runtime_status.py", result["missing"])
        self.assertNotIn("backend/views.py", result["missing"])

    def test_runtime_import_construct_is_optional_only_below_python_3_10(self) -> None:
        names = {item["name"]: item for item in self.document["checks"]}
        check = names["runtime_import_construct"]
        if sys.version_info < (3, 10):
            self.assertFalse(check["required"])
            self.assertTrue(check["passed"])
            self.assertIn("SKIPPED_PYTHON_LT_3_10", str(check["observed"]))
        else:
            self.assertTrue(check["required"])
            self.assertTrue(check["passed"])

    def test_python_lt_3_10_skips_runtime_import_construct(self) -> None:
        with patch("hil.preflight.sys.version_info", (3, 9, 6)):
            check = _runtime_import_check()
        self.assertEqual(check["name"], "runtime_import_construct")
        self.assertFalse(check["required"])
        self.assertTrue(check["passed"])
        self.assertIn("SKIPPED_PYTHON_LT_3_10", str(check["observed"]))

    def test_python_3_10_construction_exception_fails_offline_preflight(self) -> None:
        with patch("hil.preflight.sys.version_info", (3, 10, 14)):
            with patch(
                "hil.preflight._construct_offline_runtime",
                side_effect=RuntimeError("broken runtime"),
            ):
                document = offline_preflight_document(ROOT)
        names = {item["name"]: item for item in document["checks"]}
        check = names["runtime_import_construct"]
        self.assertTrue(check["required"])
        self.assertFalse(check["passed"])
        self.assertIn("RuntimeError: broken runtime", str(check["observed"]))
        self.assertFalse(document["ok"])


if __name__ == "__main__":
    unittest.main()
