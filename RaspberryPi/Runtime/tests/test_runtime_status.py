from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.runtime_status import runtime_status_document
from backend.views import sensors_document, status_document


def sensor(status: str = "LIVE", *, motion: bool = False) -> dict[str, object]:
    return {
        "status": status,
        "connected": status != "DISCONNECTED",
        "stale": status == "STALE",
        "values": {"motion": motion},
    }


def ai_result(*, available: bool, error: str | None = None) -> dict[str, object]:
    return {"available": available, "error": error, "state": "OCCUPIED" if available else "INPUT_UNAVAILABLE"}


def state(**overrides: dict[str, object]) -> dict[str, object]:
    sensors = {name: sensor() for name in ("co2", "thermal", "mmwave", "pir")}
    sensors.update(overrides)
    return {"system": "ONLINE", "sensors": sensors}


class RuntimeStatusTests(unittest.TestCase):
    def test_co2_active_contract_does_not_require_temperature_or_humidity(self) -> None:
        document = runtime_status_document(
            state(co2=sensor()),
            {"co2": ai_result(available=True)},
        )
        co2 = document["sensors"]["co2"]
        self.assertEqual(co2["sensor_status"], "AVAILABLE")
        self.assertEqual(co2["data_freshness"], "CURRENT")
        self.assertEqual(co2["artifact_status"], "PRESENT")
        self.assertEqual(co2["input_contract_status"], "SATISFIED")
        self.assertEqual(co2["ai_status"], "ACTIVE")
        self.assertNotIn("humidity", co2)
        self.assertNotIn("temperature", co2)

    def test_stale_co2_is_not_collapsed_into_generic_input_unavailable(self) -> None:
        document = runtime_status_document(
            state(co2=sensor("STALE")),
            {"co2": ai_result(available=False, error="INPUT_UNAVAILABLE")},
        )
        co2 = document["sensors"]["co2"]
        self.assertEqual(co2["sensor_status"], "STALE")
        self.assertEqual(co2["data_freshness"], "STALE")
        self.assertEqual(co2["blocked_reason"], "SENSOR_STALE")

    def test_thermal_sensor_can_be_available_while_ai_is_intentionally_blocked(self) -> None:
        document = runtime_status_document(state(thermal=sensor()), {})
        thermal = document["sensors"]["thermal"]
        self.assertEqual(thermal["sensor_status"], "AVAILABLE")
        self.assertEqual(thermal["artifact_status"], "PRESENT")
        self.assertEqual(thermal["ai_status"], "BLOCKED")
        self.assertEqual(thermal["blocked_reason"], "THERMAL_MODEL_RUNTIME_UNAVAILABLE")
        self.assertEqual(thermal["safety_status"], "LIMITED_PROXY_NO_EMERGENCY")
        self.assertEqual(thermal["model_selector"], "thermal_public_sdt_fp32_active")
        self.assertEqual(
            thermal["preprocessing_id"],
            "PUBLIC_SDT_BILINEAR_62X80_FRAME_MINMAX_V1",
        )

    def test_thermal_final_model_result_is_active_with_limited_risk(self) -> None:
        result = ai_result(available=True)
        result["state"] = "HUMAN_FALL_PROXY"
        result["metadata"] = {
            "model_selector": "thermal_public_sdt_fp32_active",
            "risk_authority": "LIMITED_POSTURE_PROXY",
        }
        thermal = runtime_status_document(
            state(thermal=sensor()), {"thermal": result}
        )["sensors"]["thermal"]
        self.assertEqual(thermal["ai_status"], "ACTIVE")
        self.assertEqual(thermal["output_status"], "AVAILABLE")
        self.assertEqual(thermal["model_selector"], "thermal_public_sdt_fp32_active")
        self.assertEqual(thermal["risk_authority"], "LIMITED_POSTURE_PROXY")
        self.assertEqual(
            thermal["preprocessing_id"],
            "PUBLIC_SDT_BILINEAR_62X80_FRAME_MINMAX_V1",
        )
        self.assertEqual(
            thermal["model_sha256"],
            "f88d65d76dbb21862e1f3cdff17cefb038a432047ded6ac8d5563bc8bc8c52ff",
        )
        self.assertIsNone(thermal["blocked_reason"])

    def test_thermal_identity_survives_stale_sensor_and_warmup(self) -> None:
        candidate_a = "thermal_tv2_candidate_a_a0_fp32_v1"
        env = {
            "SAFENEST_THERMAL_TEST_MODE": "1",
            "SAFENEST_THERMAL_MODEL_SELECTOR": candidate_a,
        }
        with patch.dict(os.environ, env, clear=False):
            stale = runtime_status_document(
                state(thermal=sensor("STALE")), {}
            )["sensors"]["thermal"]
            warmup = runtime_status_document(
                state(thermal=sensor()),
                {"thermal": ai_result(available=False, error="THERMAL_FRAME_MISSING")},
            )["sensors"]["thermal"]
        for thermal in (stale, warmup):
            self.assertEqual(thermal["model_selector"], candidate_a)
            self.assertEqual(thermal["preprocessing_id"], "FRAME_ROBUST_P2_P98_V1")
            self.assertEqual(thermal["model_id"], candidate_a)
            self.assertEqual(
                thermal["model_sha256"],
                "a158a70c4735e28eec70b5a996f82c91f452b94bcc24c040838143f4a55b1985",
            )
            self.assertEqual(thermal["ai_status"], "BLOCKED")
        self.assertEqual(stale["blocked_reason"], "SENSOR_STALE")

    def test_thermal_controlled_b_identity_without_prediction(self) -> None:
        candidate_b = "thermal_tv2_candidate_b_seed42_fp32_test_v1"
        env = {
            "SAFENEST_THERMAL_TEST_MODE": "1",
            "SAFENEST_THERMAL_MODEL_SELECTOR": candidate_b,
        }
        with patch.dict(os.environ, env, clear=False):
            thermal = runtime_status_document(state(thermal=sensor()), {})["sensors"]["thermal"]
        self.assertEqual(thermal["model_selector"], candidate_b)
        self.assertEqual(thermal["preprocessing_id"], "FRAME_ROBUST_P2_P98_V1")
        self.assertEqual(
            thermal["model_sha256"],
            "f5b9ecef8def2668bb65131671134e443c600e38c2575d4350e242f1abc0dfb4",
        )
        self.assertEqual(thermal["ai_status"], "BLOCKED")

    def test_pir_motion_and_no_motion_are_both_valid_without_ai(self) -> None:
        for motion, expected in ((True, "MOTION"), (False, "NO_MOTION")):
            with self.subTest(motion=motion):
                pir = runtime_status_document(state(pir=sensor(motion=motion)), {})["sensors"]["pir"]
                self.assertEqual(pir["sensor_status"], "AVAILABLE")
                self.assertEqual(pir["sensor_value_status"], expected)
                self.assertEqual(pir["ai_status"], "NOT_APPLICABLE")

    def test_partial_availability_is_ready_with_limitations(self) -> None:
        document = runtime_status_document(
            state(),
            {"co2": ai_result(available=True)},
        )
        self.assertEqual(document["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(document["sensors"]["mmwave"]["ai_status"], "BLOCKED")
        self.assertEqual(
            document["sensors"]["mmwave"]["blocked_reason"],
            "CANONICAL_FRESHNESS_METADATA_MISSING",
        )

    def test_global_status_distinguishes_degraded_from_not_ready(self) -> None:
        degraded = runtime_status_document(
            state(thermal=sensor("STALE")), {"co2": ai_result(available=True)}
        )
        not_ready = runtime_status_document(
            state(**{name: sensor("NO_DATA") for name in ("co2", "thermal", "mmwave", "pir")}),
            {},
        )
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertEqual(not_ready["status"], "NOT_READY")

    def test_backend_status_exposes_the_derived_contract_without_new_storage(self) -> None:
        publication = {
            "timestamp": 100.0,
            "publication_revision": 1,
            "state": state(),
            "ai": {"ai": {"co2": ai_result(available=True)}},
            "risk": {"system_health": "HEALTHY", "components": {}},
            "emergency": {},
        }
        document = status_document(publication)
        self.assertEqual(document["runtime_status"]["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(document["co2"]["runtime_status"]["ai_status"], "ACTIVE")
        self.assertEqual(document["thermal"]["runtime_status"]["sensor_status"], "AVAILABLE")
        self.assertEqual(
            sensors_document(publication)["runtime_status"]["sensors"]["co2"]["ai_status"],
            "ACTIVE",
        )

    def test_mmwave_projects_live_tflite_result_as_active(self) -> None:
        document = runtime_status_document(
            state(),
            {"mmwave": ai_result(available=True)},
        )
        mmwave = document["sensors"]["mmwave"]
        self.assertEqual(mmwave["artifact_status"], "PRESENT")
        self.assertEqual(mmwave["ai_status"], "ACTIVE")
        self.assertIsNone(mmwave["blocked_reason"])

    def test_mmwave_presence_gap_is_a_specific_block_not_model_pending(self) -> None:
        document = runtime_status_document(
            state(),
            {
                "mmwave": {
                    "available": False,
                    "error": "PRESENCE_STATE_UNAVAILABLE",
                    "state": "RESPIRATORY_INFERENCE_SUPPRESSED",
                    "metadata": {
                        "canonical_window_status": "CANONICAL_WINDOW_READY",
                        "suppression_reason": "PRESENCE_STATE_UNAVAILABLE",
                        "missing": ["human_detected_raw"],
                    },
                }
            },
        )
        mmwave = document["sensors"]["mmwave"]
        self.assertEqual(mmwave["ai_status"], "BLOCKED")
        self.assertEqual(mmwave["blocked_reason"], "PRESENCE_STATE_UNAVAILABLE")
        self.assertEqual(mmwave["input_contract_status"], "UNSATISFIED")

    def test_offline_documents_keep_a_not_ready_runtime_status(self) -> None:
        self.assertEqual(status_document(None)["runtime_status"]["status"], "NOT_READY")
        self.assertEqual(sensors_document(None)["runtime_status"]["status"], "NOT_READY")


if __name__ == "__main__":
    unittest.main()
