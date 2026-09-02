from __future__ import annotations

import json
import unittest

from risk.engine import RiskComponent, SafeNestRiskEngine


def sensor(status="LIVE", values=None, sequence=1, last_update=100.0):
    return {"status": status, "values": values or {}, "sequence": sequence, "last_update": last_update}


def ai_result(sensor_id, *, available=True, score=0.0, state="NORMAL", confidence=0.9,
              timestamp=100.0, error=None, metadata=None):
    return {
        "sensor_id": sensor_id,
        "available": available,
        "score": score if available else None,
        "state": state if available else "INPUT_UNAVAILABLE",
        "confidence": confidence if available else None,
        "timestamp": timestamp,
        "error": error,
        "metadata": metadata or {},
    }


def inputs(*, timestamp=100.0, sensors=None, ai=None):
    base_sensors = {
        "mmwave": sensor(values={
            "respiration_rate_bpm": 15.0, "respiration_valid": True,
            "presence": None, "presence_available": False,
        }),
        "co2": sensor(values={"ppm": 800.0}),
        "pir": sensor(values={"motion": True}),
        "thermal": sensor(values={"frame_available": True}),
    }
    base_ai = {
        "mmwave": ai_result("mmwave", available=False, error="INPUT_UNAVAILABLE"),
        "co2": ai_result("co2", available=False, error="INPUT_UNAVAILABLE"),
        "pir": ai_result("pir", state="MOTION", confidence=1.0),
        "thermal": ai_result("thermal", state="HUMAN_NORMAL"),
    }
    if sensors:
        base_sensors.update(sensors)
    if ai:
        base_ai.update(ai)
    return (
        {"timestamp": timestamp, "revision": 1, "sensors": base_sensors},
        {"timestamp": timestamp, "state_revision": 1, "ai": base_ai},
    )


def component(name, score):
    return RiskComponent(name, True, score, "ai", "NORMAL", 100.0)


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = SafeNestRiskEngine()

    def test_official_weights_and_warning_name_boundaries(self):
        self.assertEqual(self.engine.weights, {
            "mmwave": 0.35, "co2": 0.35, "pir": 0.15, "thermal": 0.15,
        })
        parts = {name: component(name, 0.0) for name in self.engine.weights}
        parts["mmwave"] = component("mmwave", 30.0 / 35.0)
        self.assertEqual(self.engine.fuse(parts, timestamp=100.0).risk_level, "WARNING")
        parts["mmwave"] = component("mmwave", 1.0)
        parts["co2"] = component("co2", 25.0 / 35.0)
        self.assertEqual(self.engine.fuse(parts, timestamp=100.0).risk_level, "DANGER")

    def test_all_unavailable_never_becomes_normal(self):
        names = ("mmwave", "co2", "pir", "thermal")
        state, ai = inputs(
            sensors={name: sensor("NO_DATA") for name in names},
            ai={name: ai_result(name, available=False) for name in names},
        )
        result = self.engine.evaluate(state, ai)
        self.assertIsNone(result.risk_score)
        self.assertIsNone(result.risk_level)
        self.assertEqual(result.system_health, "FAILED")
        self.assertIn("ALL_RISK_COMPONENTS_UNAVAILABLE", result.reasons)

    def test_thermal_fall_confidence_override(self):
        state, ai = inputs(ai={
            "thermal": ai_result("thermal", score=1.0, state="HUMAN_FALL", confidence=0.91)
        })
        result = self.engine.evaluate(state, ai)
        self.assertEqual(result.risk_score, 100.0)
        self.assertEqual(result.risk_level, "DANGER")
        self.assertTrue(result.is_emergency)
        self.assertIn("EMERGENCY_HUMAN_FALL", result.reasons)

    def test_low_confidence_fall_has_no_emergency_override(self):
        state, ai = inputs(ai={
            "thermal": ai_result("thermal", score=1.0, state="HUMAN_FALL", confidence=0.7)
        })
        result = self.engine.evaluate(state, ai)
        self.assertFalse(result.is_emergency)
        self.assertNotEqual(result.risk_score, 100.0)

    def test_limited_thermal_proxy_enters_score_without_emergency(self):
        state, ai = inputs(ai={
            "thermal": ai_result(
                "thermal",
                score=0.4,
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
                metadata={
                    "safety_authority": False,
                    "risk_authority": "LIMITED_POSTURE_PROXY",
                },
            )
        })
        result = self.engine.evaluate(state, ai)
        self.assertEqual(result.components["thermal"]["score"], 0.4)
        self.assertFalse(result.is_emergency)
        self.assertNotEqual(result.risk_score, 100.0)

    def test_thermal_proxy_without_authority_is_rejected(self):
        state, ai = inputs(ai={
            "thermal": ai_result(
                "thermal",
                score=0.4,
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
            )
        })
        result = self.engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")
        self.assertIn("THERMAL_PROXY_RISK_AUTHORITY_MISSING", result.reasons)

    def test_unverified_apnea_does_not_emergency_override(self):
        state, ai = inputs(ai={
            "mmwave": ai_result(
                "mmwave", score=1.0, state="APNEA", metadata={"apnea_verified": False}
            )
        })
        result = self.engine.evaluate(state, ai)
        self.assertFalse(result.is_emergency)
        self.assertIn("APNEA_UNVERIFIED_NO_OVERRIDE", result.reasons)

    def test_ai_failure_uses_finite_respiration_rule(self):
        state, ai = inputs(sensors={
            "mmwave": sensor(values={
                "respiration_rate_bpm": 24.0,
                "respiration_valid": True,
                "presence_available": False,
            })
        })
        result = self.engine.evaluate(state, ai)
        mmwave = result.components["mmwave"]
        self.assertTrue(mmwave["available"])
        self.assertEqual(mmwave["source"], "rule_fallback")
        self.assertEqual(mmwave["score"], 0.75)
        self.assertIn("ABNORMAL_RESPIRATION_RPM", result.reasons)
        self.assertEqual(result.system_health, "DEGRADED")

    def test_co2_rule_uses_locked_thresholds_and_slope(self):
        state1, ai1 = inputs(sensors={
            "co2": sensor(values={"ppm": 900.0}, sequence=1, last_update=100.0)
        })
        self.engine.evaluate(state1, ai1)
        state2, ai2 = inputs(timestamp=160.0, sensors={
            "co2": sensor(values={"ppm": 2600.0}, sequence=2, last_update=160.0)
        })
        result = self.engine.evaluate(state2, ai2)
        self.assertEqual(result.components["co2"]["score"], 1.0)
        self.assertIn("HIGH_CO2_DANGER", result.reasons)
        self.assertIn("FAST_CO2_RISE", result.reasons)

    def test_long_no_motion_requires_confirmed_presence(self):
        state1, ai1 = inputs(
            sensors={"pir": sensor(values={"motion": False}, sequence=1, last_update=100.0)},
            ai={"thermal": ai_result("thermal", state="HUMAN_NORMAL", timestamp=100.0)},
        )
        first = self.engine.evaluate(state1, ai1)
        self.assertEqual(first.components["pir"]["score"], 0.5)
        state2, ai2 = inputs(
            timestamp=116.0,
            sensors={"pir": sensor(values={"motion": False}, sequence=2, last_update=116.0)},
            ai={"thermal": ai_result("thermal", state="HUMAN_NORMAL", timestamp=116.0)},
        )
        second = self.engine.evaluate(state2, ai2)
        self.assertEqual(second.components["pir"]["score"], 1.0)
        self.assertIn("LONG_NO_MOTION", second.reasons)

        other = SafeNestRiskEngine()
        state3, ai3 = inputs(
            timestamp=200.0,
            sensors={"pir": sensor(values={"motion": False}, last_update=200.0)},
            ai={"thermal": ai_result("thermal", state="NOT_HUMAN", timestamp=200.0)},
        )
        third = other.evaluate(state3, ai3)
        self.assertEqual(third.components["pir"]["score"], 0.0)
        self.assertIn("PRESENCE_NOT_CONFIRMED", third.reasons)

    def test_stale_sensor_is_excluded_not_zeroed(self):
        state, ai = inputs(sensors={"thermal": sensor("STALE")})
        result = self.engine.evaluate(state, ai)
        self.assertIsNone(result.component_scores["thermal"])
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")
        self.assertEqual(result.system_health, "DEGRADED")

    def test_stale_ai_result_is_not_reused(self):
        state, ai = inputs(timestamp=100.0, ai={
            "thermal": ai_result("thermal", state="HUMAN_FALL", score=1.0, timestamp=90.0)
        })
        result = self.engine.evaluate(state, ai)
        self.assertIsNone(result.component_scores["thermal"])
        self.assertFalse(result.is_emergency)

    def test_presence_cross_validation_mismatch_reason(self):
        state, ai = inputs(sensors={
            "mmwave": sensor(values={
                "respiration_rate_bpm": 15.0,
                "respiration_valid": True,
                "presence_available": True,
                "presence": True,
            })
        }, ai={"thermal": ai_result("thermal", state="NOT_HUMAN")})
        result = self.engine.evaluate(state, ai)
        self.assertIn("MMWAVE_THERMAL_MISMATCH", result.reasons)
        self.assertTrue(result.presence_detected)
        self.assertEqual(result.presence_source, "MMWAVE")

    def test_thermal_presence_is_exposed_when_protocol_has_no_mmwave_presence(self):
        state, ai = inputs()
        result = self.engine.evaluate(state, ai)
        self.assertTrue(result.presence_detected)
        self.assertEqual(result.presence_source, "THERMAL")

    def test_nan_ai_is_unavailable_and_json_is_strict(self):
        state, ai = inputs(ai={"thermal": ai_result("thermal", score=float("nan"))})
        result = self.engine.evaluate(state, ai)
        self.assertIsNone(result.component_scores["thermal"])
        json.dumps(result.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
