"""Behaviour contract for the v1 risk formula. Synthetic inputs only, no hardware."""

from __future__ import annotations

import unittest

import copy
import json
import tempfile
from pathlib import Path

from risk.formula_v1 import CONFIG_PATH, SafeNestRiskFormulaV1


def _trusted_engine() -> SafeNestRiskFormulaV1:
    """Engine with mmwave.neural_trust flipped to TRUSTED, for neural-path tests."""

    config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    config["mmwave"]["neural_trust"] = "TRUSTED"
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(config, handle)
    handle.close()
    return SafeNestRiskFormulaV1(handle.name)


def sensor(status="LIVE", values=None, *, last_update=1000.0, sequence=1, ttl=3.0):
    return {
        "status": status,
        "values": dict(values or {}),
        "last_update": last_update,
        "sequence": sequence,
        "ttl_seconds": ttl,
    }


def ai_entry(
    *,
    available=True,
    state="NORMAL",
    confidence=0.9,
    probabilities=(0.9, 0.06, 0.04),
    timestamp=1000.0,
    extra=None,
):
    metadata = {"probabilities": list(probabilities)}
    metadata.update(extra or {})
    return {
        "available": available,
        "source": "tflite" if available else "unavailable",
        "state": state,
        "score": 0.0,
        "confidence": confidence,
        "timestamp": timestamp,
        "error": None if available else "INPUT_UNAVAILABLE",
        "metadata": metadata,
    }


def scene(
    *,
    thermal=None,
    mmwave=None,
    co2=None,
    pir=None,
    thermal_ai=None,
    mmwave_ai=None,
    co2_ai=None,
    timestamp=1000.0,
):
    state = {
        "timestamp": timestamp,
        "revision": 1,
        "sensors": {
            "thermal": thermal if thermal is not None else sensor("NO_DATA"),
            "mmwave": mmwave if mmwave is not None else sensor("NO_DATA"),
            "co2": co2 if co2 is not None else sensor("NO_DATA"),
            "pir": pir if pir is not None else sensor("NO_DATA"),
        },
    }
    unavailable = {"available": False, "source": "unavailable", "state": "INPUT_UNAVAILABLE",
                   "score": None, "confidence": None, "timestamp": timestamp,
                   "error": "INPUT_UNAVAILABLE", "metadata": {}}
    ai = {
        "timestamp": timestamp,
        "ai": {
            "thermal": thermal_ai or unavailable,
            "mmwave": mmwave_ai or unavailable,
            "co2": co2_ai or unavailable,
            "pir": unavailable,
        },
    }
    return state, ai


class ConfigContractTests(unittest.TestCase):
    def test_weights_sum_to_one_and_thresholds_are_ordered(self):
        engine = SafeNestRiskFormulaV1()
        self.assertAlmostEqual(sum(engine.weights.values()), 1.0)
        self.assertLess(engine.warning_min, engine.danger_min)
        self.assertEqual(engine.formula_id, "SAFENEST_RISK_V1")
        self.assertEqual(engine.formula_version, "1.3.3")
        self.assertEqual(engine.caution_formula_id, "SAFENEST_CAUTION_CO2_V1")
        self.assertEqual(engine.caution_delta_enter, 500.0)
        self.assertTrue(engine.caution_requires_baseline_lock)
        self.assertEqual(engine.caution_slope_ppm_per_min, 50.0)
        self.assertNotIn("warning_ppm", engine._co2)
        self.assertNotIn("danger_ppm", engine._co2)
        self.assertEqual(engine._co2["immediate_danger_ppm"], 5000.0)
        warning_floors = {
            name for name, level in engine._floors.items() if level == "WARNING"
        }
        self.assertEqual(warning_floors, {"co2_relative_warning", "co2_fast_rise"})
        self.assertNotIn("co2_danger", engine._floors)
        self.assertEqual(engine._floors["co2_immediate_danger"], "DANGER")

    def test_document_is_a_superset_of_the_legacy_contract(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(co2=sensor(values={"ppm": 700.0}))
        document = engine.evaluate(state, ai).to_dict()
        for key in (
            "timestamp", "risk_score", "risk_level", "system_health", "degraded_mode",
            "is_emergency", "presence_detected", "presence_source", "reasons",
            "component_scores", "component_status", "components", "weights",
            "thresholds", "config_status",
        ):
            self.assertIn(key, document)
        for key in ("formula_id", "score_level", "level_source", "effective_weight",
                    "evidence_sufficient", "escalation_floors", "caution_formula_id",
                    "caution_active", "caution_reasons"):
            self.assertIn(key, document)


class DecisivenessGateTests(unittest.TestCase):
    def test_near_uniform_head_is_refused_instead_of_scored(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_FALL", confidence=0.42,
                                probabilities=(0.0, 0.5, 0.5)),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")
        self.assertIn("THERMAL_AI_OUTPUT_INDECISIVE", result.reasons)
        self.assertFalse(result.is_emergency)

    def test_low_confidence_head_is_refused(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_FALL", confidence=0.30,
                                probabilities=(0.30, 0.29, 0.41)),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")

    def test_stale_ai_beyond_sensor_ttl_is_refused(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(ttl=3.0),
            thermal_ai=ai_entry(state="HUMAN_NORMAL", timestamp=990.0),
            timestamp=1000.0,
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")


class EscalationFloorTests(unittest.TestCase):
    def test_final_thermal_proxy_affects_risk_but_never_emergency(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
                probabilities=(0.01, 0.04, 0.95),
                extra={
                    "safety_authority": False,
                    "risk_authority": "LIMITED_POSTURE_PROXY",
                    "model_selector": "thermal_public_sdt_fp32_active",
                },
            ),
            co2=sensor(values={"ppm": 500.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.components["thermal"]["score"], 0.4)
        self.assertGreater(result.risk_score, 0.0)
        self.assertFalse(result.is_emergency)
        self.assertNotEqual(result.risk_level, "DANGER")
        self.assertIn("THERMAL_FALL_PROXY_LIMITED_RISK_NO_EMERGENCY", result.reasons)

    def test_bbox_lying_overlay_is_the_thermal_risk_input(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
                probabilities=(0.05, 0.70, 0.25),
                extra={
                    "safety_authority": False,
                    "risk_authority": "LIMITED_POSTURE_PROXY",
                    "model_selector": "thermal_public_sdt_fp32_active",
                    "thermal_overlay_applied": True,
                    "thermal_posture_source": "BBOX",
                    "thermal_model_class_name": "HUMAN_NORMAL",
                    "thermal_bbox_height": 10,
                    "thermal_bbox_width": 50,
                },
            ),
            co2=sensor(values={"ppm": 500.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        thermal = result.components["thermal"]
        self.assertEqual(thermal["state"], "HUMAN_FALL_PROXY")
        self.assertEqual(thermal["score"], 0.4)
        self.assertEqual(thermal["metadata"]["thermal_posture_source"], "BBOX")
        self.assertTrue(thermal["metadata"]["thermal_overlay_applied"])
        self.assertFalse(result.is_emergency)

    def test_presence_only_thermal_does_not_use_model_fall(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.70,
                probabilities=(0.05, 0.70, 0.25),
                extra={
                    "safety_authority": False,
                    "risk_authority": "LIMITED_POSTURE_PROXY",
                    "model_selector": "thermal_public_sdt_fp32_active",
                    "thermal_overlay_applied": False,
                    "thermal_posture_source": "PRESENCE_ONLY",
                    "thermal_model_class_name": "HUMAN_FALL_PROXY",
                },
            ),
        )
        result = engine.evaluate(state, ai)
        thermal = result.components["thermal"]
        self.assertEqual(thermal["state"], "HUMAN_NORMAL")
        self.assertEqual(thermal["score"], 0.0)
        self.assertNotIn("THERMAL_FALL_PROXY_LIMITED_RISK_NO_EMERGENCY", result.reasons)
        self.assertFalse(result.is_emergency)

    def test_unscoped_non_authoritative_thermal_model_remains_unavailable(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
                probabilities=(0.01, 0.04, 0.95),
                extra={"safety_authority": False},
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")
        self.assertIn("THERMAL_MODEL_SAFETY_AUTHORITY_UNVERIFIED", result.reasons)

    def test_proxy_without_explicit_limited_authority_is_unavailable(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL_PROXY",
                confidence=0.95,
                probabilities=(0.01, 0.04, 0.95),
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["thermal"], "UNAVAILABLE")
        self.assertIn("THERMAL_PROXY_RISK_AUTHORITY_MISSING", result.reasons)

    def test_confident_thermal_fall_is_an_emergency_regardless_of_calm_peers(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_FALL", confidence=0.95,
                                probabilities=(0.01, 0.04, 0.95)),
            co2=sensor(values={"ppm": 500.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.is_emergency)
        self.assertEqual(result.risk_level, "DANGER")
        self.assertEqual(result.risk_score, 100.0)
        self.assertIn("EMERGENCY_HUMAN_FALL", result.reasons)

    def test_retired_absolute_co2_ceiling_does_not_raise_caution(self):
        engine = SafeNestRiskFormulaV1()
        mm = sensor(values={
            "presence": True,
            "presence_available": True,
            "respiration_rate_bpm": 16.0,
            "respiration_valid": True,
        })
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            mmwave=mm,
            co2=sensor(values={"ppm": 1500.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        self.assertAlmostEqual(result.component_scores["co2"], 0.325)
        self.assertLess(result.risk_score, engine.danger_min)
        self.assertEqual(result.score_level, "NORMAL")
        self.assertEqual(result.risk_level, "NORMAL")
        self.assertFalse(result.caution_active)
        self.assertNotIn("co2_warning", result.escalation_floors)
        self.assertNotIn("HIGH_CO2_WARNING", result.reasons)
        self.assertFalse(result.is_emergency)

    def test_retired_2500_ppm_does_not_raise_danger(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_NORMAL", confidence=0.98,
                                probabilities=(0.01, 0.98, 0.01)),
            co2=sensor(values={"ppm": 3000.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        self.assertLess(result.risk_score, engine.danger_min)
        self.assertEqual(result.score_level, "NORMAL")
        self.assertEqual(result.risk_level, "NORMAL")
        self.assertNotIn("co2_danger", result.escalation_floors)
        self.assertNotIn("HIGH_CO2_DANGER", result.reasons)
        self.assertFalse(result.caution_active)
        self.assertFalse(result.is_emergency)

    def test_immediate_danger_co2_is_an_emergency(self):
        engine = SafeNestRiskFormulaV1()
        below = engine.evaluate(*scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_NORMAL", confidence=0.98,
                                probabilities=(0.01, 0.98, 0.01)),
            co2=sensor(values={"ppm": 4999.0}),
            pir=sensor(values={"motion": True}),
        ))
        self.assertFalse(below.is_emergency)
        self.assertNotEqual(below.risk_level, "DANGER")
        self.assertNotIn("co2_immediate_danger", below.escalation_floors)

        result = engine.evaluate(*scene(
            mmwave=sensor(values={
                "presence": True,
                "presence_available": True,
                "respiration_rate_bpm": 16.0,
                "respiration_valid": True,
            }),
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_NORMAL", confidence=0.98,
                                probabilities=(0.01, 0.98, 0.01)),
            co2=sensor(values={"ppm": 5000.0}),
            pir=sensor(values={"motion": True}),
        ))
        self.assertTrue(result.is_emergency)
        self.assertEqual(result.risk_level, "DANGER")
        self.assertIn("co2_immediate_danger", result.escalation_floors)

    def test_unverified_apnea_proxy_cannot_raise_caution(self):
        # neural_trust must be TRUSTED for the neural class to score at all.
        engine = _trusted_engine()
        mm = sensor(values={"presence": True, "presence_available": True,
                            "respiration_rate_bpm": 16.0, "respiration_valid": True})
        apnea = ai_entry(state="APNEA-proxy", confidence=0.71,
                         probabilities=(0.10, 0.19, 0.71))
        state, ai = scene(mmwave=mm, mmwave_ai=apnea,
                          co2=sensor(values={"ppm": 500.0}))
        first = engine.evaluate(state, ai)
        self.assertIn("APNEA_PROXY_AWAITING_PERSISTENCE", first.reasons)
        self.assertFalse(first.is_emergency)
        second = engine.evaluate(state, ai)
        self.assertIn("mmwave_apnea_proxy_sustained", second.escalation_floors)
        self.assertNotEqual(second.risk_level, "WARNING")
        self.assertFalse(second.caution_active)
        self.assertFalse(second.is_emergency)

    def test_observe_only_keeps_the_neural_class_out_of_the_score(self):
        engine = SafeNestRiskFormulaV1()
        self.assertEqual(engine.mmwave_neural_trust, "OBSERVE_ONLY")
        mm = sensor(values={"presence": True, "presence_available": True,
                            "respiration_rate_bpm": 16.0, "respiration_valid": True})
        apnea = ai_entry(
            state="APNEA-proxy", confidence=0.99, probabilities=(0.005, 0.005, 0.99),
            extra={"spectral_status": "SPECTRAL_ESTIMATE_READY",
                   "spectral_rate_rpm": 18.4, "spectral_band_power_fraction": 0.86},
        )
        state, ai = scene(mmwave=mm, mmwave_ai=apnea, co2=sensor(values={"ppm": 500.0}))
        for _ in range(4):
            result = engine.evaluate(state, ai)
        component = result.components["mmwave"]
        self.assertEqual(result.component_status["mmwave"], "RULE_FALLBACK")
        self.assertEqual(component["metadata"]["observed_neural_state"], "APNEA-proxy")
        self.assertEqual(component["metadata"]["neural_trust"], "OBSERVE_ONLY")
        # Spectral rate wins over the MR60 scalar and 18.4 rpm is in band.
        self.assertEqual(component["metadata"]["respiration_rate_source"],
                         "SPECTRAL_CANONICAL_WINDOW")
        self.assertEqual(component["metadata"]["respiration_rate_bpm"], 18.4)
        self.assertEqual(component["score"], 0.0)
        self.assertFalse(result.is_emergency)
        self.assertEqual(result.escalation_floors, ())

    def test_observe_only_still_honours_hardware_verified_apnea(self):
        engine = SafeNestRiskFormulaV1()
        mm = sensor(values={"presence": True, "presence_available": True})
        apnea = ai_entry(state="APNEA", confidence=0.9,
                         probabilities=(0.05, 0.05, 0.90),
                         extra={"apnea_verified": True})
        state, ai = scene(mmwave=mm, mmwave_ai=apnea)
        result = engine.evaluate(state, ai)
        self.assertTrue(result.is_emergency)
        self.assertEqual(result.risk_level, "DANGER")

    def test_spectral_rate_is_preferred_over_the_mr60_scalar(self):
        engine = SafeNestRiskFormulaV1()
        # MR60 reports 0.0 rpm on a window the spectrum reads as 20.6 rpm; this
        # is the exact pattern observed in the committed 20260817 capture.
        mm = sensor(values={"presence": True, "presence_available": True,
                            "respiration_rate_bpm": 0.0, "respiration_valid": True})
        entry = ai_entry(
            available=False, state="RESPIRATORY_INFERENCE_REFUSED",
            extra={"spectral_status": "SPECTRAL_ESTIMATE_READY",
                   "spectral_rate_rpm": 20.6, "spectral_band_power_fraction": 0.87},
        )
        entry["metadata"]["probabilities"] = []
        state, ai = scene(mmwave=mm, mmwave_ai=entry, co2=sensor(values={"ppm": 500.0}))
        component = engine.evaluate(state, ai).components["mmwave"]
        self.assertEqual(component["metadata"]["respiration_rate_source"],
                         "SPECTRAL_CANONICAL_WINDOW")
        self.assertEqual(component["metadata"]["respiration_rate_bpm"], 20.6)
        self.assertEqual(component["metadata"]["mr60_breath_rate_raw"], 0.0)
        self.assertEqual(component["score"], 0.0)  # 20.6 rpm is inside 10-24

    def test_mr60_scalar_is_used_only_when_no_spectral_estimate_exists(self):
        engine = SafeNestRiskFormulaV1()
        mm = sensor(values={"presence": True, "presence_available": True,
                            "respiration_rate_bpm": 30.0, "respiration_valid": True})
        entry = ai_entry(available=False, state="INPUT_UNAVAILABLE")
        entry["metadata"]["probabilities"] = []
        state, ai = scene(mmwave=mm, mmwave_ai=entry, co2=sensor(values={"ppm": 500.0}))
        component = engine.evaluate(state, ai).components["mmwave"]
        self.assertEqual(component["metadata"]["respiration_rate_source"],
                         "MR60_BREATH_RATE_RAW")
        self.assertEqual(component["metadata"]["respiration_rate_bpm"], 30.0)
        self.assertGreater(component["score"], 0.0)  # 30 rpm is outside 10-24

    def test_hardware_verified_apnea_is_an_emergency(self):
        engine = _trusted_engine()
        mm = sensor(values={"presence": True, "presence_available": True})
        apnea = ai_entry(state="APNEA", confidence=0.9,
                         probabilities=(0.05, 0.05, 0.90),
                         extra={"apnea_verified": True})
        state, ai = scene(mmwave=mm, mmwave_ai=apnea)
        result = engine.evaluate(state, ai)
        self.assertTrue(result.is_emergency)
        self.assertEqual(result.risk_level, "DANGER")


class EvidenceSufficiencyTests(unittest.TestCase):
    def test_normal_is_not_published_from_a_weight_minority(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(pir=sensor(values={"motion": True}))
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["pir"], "RULE")
        self.assertAlmostEqual(result.effective_weight, engine.weights["pir"])
        self.assertFalse(result.evidence_sufficient)
        self.assertEqual(result.risk_level, "INDETERMINATE")
        self.assertIn("INSUFFICIENT_EVIDENCE_FOR_NORMAL", result.reasons)

    def test_all_unavailable_fails_closed_without_a_level(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene()
        result = engine.evaluate(state, ai)
        self.assertIsNone(result.risk_score)
        self.assertIsNone(result.risk_level)
        self.assertEqual(result.system_health, "FAILED")
        self.assertIn("ALL_RISK_COMPONENTS_UNAVAILABLE", result.reasons)

    def test_majority_weight_permits_normal(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(state="HUMAN_NORMAL", confidence=0.98,
                                probabilities=(0.01, 0.98, 0.01)),
            co2=sensor(values={"ppm": 550.0}),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.evidence_sufficient)
        self.assertEqual(result.risk_level, "NORMAL")


class PirSemanticsTests(unittest.TestCase):
    def test_no_motion_without_presence_is_unavailable_not_zero(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(pir=sensor(values={"motion": False}),
                          co2=sensor(values={"ppm": 500.0}))
        result = engine.evaluate(state, ai)
        self.assertEqual(result.component_status["pir"], "UNAVAILABLE")
        self.assertIn("PIR_PRESENCE_UNCONFIRMED", result.components["pir"]["reasons"])

    def test_no_motion_ramps_only_after_the_grace_period(self):
        engine = SafeNestRiskFormulaV1()
        thermal_ai = ai_entry(state="HUMAN_NORMAL", confidence=0.98,
                              probabilities=(0.01, 0.98, 0.01))
        early_state, early_ai = scene(
            thermal=sensor(), thermal_ai=thermal_ai,
            pir=sensor(values={"motion": False}, last_update=1000.0),
        )
        self.assertEqual(engine.evaluate(early_state, early_ai).component_scores["pir"], 0.0)

        late_state, late_ai = scene(
            thermal=sensor(last_update=1400.0), thermal_ai=ai_entry(
                state="HUMAN_NORMAL", confidence=0.98,
                probabilities=(0.01, 0.98, 0.01), timestamp=1400.0),
            pir=sensor(values={"motion": False}, last_update=1400.0),
            timestamp=1400.0,
        )
        late = engine.evaluate(late_state, late_ai)
        self.assertEqual(late.component_scores["pir"], 1.0)
        self.assertIn("pir_long_no_motion", late.escalation_floors)
        self.assertNotEqual(late.risk_level, "WARNING")
        self.assertFalse(late.caution_active)


class Co2CurveTests(unittest.TestCase):
    def test_curve_is_monotonic_and_anchored(self):
        engine = SafeNestRiskFormulaV1()
        previous = -1.0
        for ppm in (0, 400, 600, 800, 1000, 1500, 2000, 3500, 5000, 8000, 12000):
            state, ai = scene(co2=sensor(values={"ppm": float(ppm)}, sequence=ppm))
            score = engine.evaluate(state, ai).component_scores["co2"]
            self.assertGreaterEqual(score, previous)
            previous = score
        self.assertEqual(previous, 1.0)

    def test_field_capture_level_co2_is_a_low_score(self):
        # 20260817 field captures sit around 1.2k ppm; that is elevated, not alarming.
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(co2=sensor(values={"ppm": 1184.0}))
        result = engine.evaluate(state, ai)
        score = result.component_scores["co2"]
        self.assertGreater(score, 0.15)
        self.assertLess(score, 0.25)
        self.assertNotIn("co2_warning", result.escalation_floors)

    def test_absolute_co2_ceilings_are_not_caution_floors(self):
        engine = SafeNestRiskFormulaV1()
        at_default = engine.evaluate(*scene(co2=sensor(values={"ppm": 1000.0})))
        at_retired = engine.evaluate(*scene(co2=sensor(values={"ppm": 1500.0})))
        self.assertFalse(at_default.caution_active)
        self.assertFalse(at_retired.caution_active)
        self.assertNotIn("co2_warning", at_default.escalation_floors)
        self.assertNotIn("co2_warning", at_retired.escalation_floors)
        self.assertEqual(at_retired.risk_level, "INDETERMINATE")


class Co2CautionFormulaTests(unittest.TestCase):
    def test_relative_rise_of_500_after_lock_is_caution(self):
        engine = SafeNestRiskFormulaV1()
        mm = sensor(values={
            "presence": True,
            "presence_available": True,
            "respiration_rate_bpm": 16.0,
            "respiration_valid": True,
        })
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            mmwave=mm,
            pir=sensor(values={"motion": True}),
            co2=sensor(values={"ppm": 900.0}),
            co2_ai=ai_entry(
                available=False,
                state="FEATURE_UNAVAILABLE_WARMUP",
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 400.0,
                    "co2_delta_plus_ppm": 500.0,
                    "co2_relative_warning": True,
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.caution_active)
        self.assertEqual(result.caution_reasons, ("co2_relative_warning",))
        self.assertIn("co2_relative_warning", result.escalation_floors)
        self.assertNotIn("co2_warning", result.escalation_floors)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertEqual(result.level_source, "CAUTION")
        self.assertEqual(result.score_level, "NORMAL")
        self.assertEqual(result.components["co2"]["state"], "CO2_RELATIVE_WARNING")
        self.assertEqual(result.components["co2"]["metadata"]["occupancy_risk_semantic"], "NONE")
        self.assertFalse(result.is_emergency)

    def test_locked_room_that_idles_at_1500_is_not_caution(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            pir=sensor(values={"motion": True}),
            co2=sensor(values={"ppm": 1500.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 1500.0,
                    "co2_delta_plus_ppm": 0.0,
                    "co2_relative_warning": False,
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.risk_level, "NORMAL")
        self.assertFalse(result.caution_active)
        self.assertEqual(result.components["co2"]["state"], "CO2_NORMAL")

    def test_overall_score_in_the_old_warning_band_stays_normal(self):
        engine = SafeNestRiskFormulaV1()
        self.assertEqual(engine.classify(40.0), "NORMAL")
        self.assertEqual(engine.classify(64.9), "NORMAL")
        self.assertEqual(engine.classify(65.0), "DANGER")

    def test_fast_slope_raises_caution_without_other_sensors(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            co2=sensor(values={"ppm": 800.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 700.0,
                    "co2_delta_plus_ppm": 100.0,
                    "co2_relative_warning": False,
                    "co2_slope_ppm_per_min": 50.0,
                    "slope_profile_id": "CO2_SLOPE_FEATURE_PROFILE_001",
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.caution_active)
        self.assertIn("co2_fast_rise", result.caution_reasons)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertEqual(result.level_source, "CAUTION")

    def test_gentle_slope_alone_does_not_raise_caution(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            co2=sensor(values={"ppm": 800.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 700.0,
                    "co2_delta_plus_ppm": 100.0,
                    "co2_relative_warning": False,
                    "co2_slope_ppm_per_min": 15.0,
                    "slope_profile_id": "CO2_SLOPE_FEATURE_PROFILE_001",
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertFalse(result.caution_active)
        self.assertNotIn("co2_fast_rise", result.escalation_floors)
        self.assertIn("FAST_CO2_RISE", result.reasons)
        self.assertEqual(result.risk_level, "NORMAL")


class Co2LocalizationTests(unittest.TestCase):
    def test_relative_rise_warns_below_the_absolute_ceiling(self):
        engine = SafeNestRiskFormulaV1()
        mm = sensor(values={
            "presence": True,
            "presence_available": True,
            "respiration_rate_bpm": 16.0,
            "respiration_valid": True,
        })
        state, ai = scene(
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            mmwave=mm,
            pir=sensor(values={"motion": True}),
            co2=sensor(values={"ppm": 900.0}),
            co2_ai=ai_entry(
                available=False,
                state="FEATURE_UNAVAILABLE_WARMUP",
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 400.0,
                    "co2_delta_plus_ppm": 500.0,
                    "co2_relative_warning": True,
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertIn("co2_relative_warning", result.escalation_floors)
        self.assertNotIn("co2_warning", result.escalation_floors)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertEqual(result.level_source, "CAUTION")
        self.assertEqual(result.components["co2"]["state"], "CO2_RELATIVE_WARNING")
        self.assertEqual(result.components["co2"]["metadata"]["occupancy_risk_semantic"], "NONE")
        self.assertFalse(result.is_emergency)

    def test_drop_below_baseline_is_not_a_hazard(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            co2=sensor(values={"ppm": 200.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 400.0,
                    "co2_delta_plus_ppm": 0.0,
                    "co2_relative_warning": False,
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertNotIn("co2_relative_warning", result.escalation_floors)
        self.assertEqual(result.components["co2"]["state"], "CO2_NORMAL")

    def test_unlocked_baseline_is_localizing_not_normal(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            co2=sensor(values={"ppm": 800.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_UNLOCKED_WARMUP",
                    "co2_relative_warning": False,
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.components["co2"]["state"], "CO2_LOCALIZING")
        self.assertIn("CO2_BASELINE_UNLOCKED", result.components["co2"]["reasons"])
        self.assertNotIn("co2_relative_warning", result.escalation_floors)

    def test_unlocked_fast_slope_does_not_raise_caution(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            co2=sensor(values={"ppm": 1640.0}),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_UNLOCKED_WARMUP",
                    "co2_relative_warning": False,
                    "co2_slope_ppm_per_min": 50.0,
                    "slope_profile_id": "CO2_SLOPE_FEATURE_PROFILE_001",
                },
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertEqual(result.components["co2"]["state"], "CO2_LOCALIZING")
        self.assertIn("VERY_FAST_CO2_RISE", result.reasons)
        self.assertFalse(result.caution_active)
        self.assertNotIn("co2_fast_rise", result.escalation_floors)
        self.assertNotIn("co2_relative_warning", result.escalation_floors)

    def test_local_endpoint_slope_does_not_raise_caution_after_lock(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            co2=sensor(
                values={"ppm": 800.0},
                last_update=15.0,
                sequence=2,
            ),
            co2_ai=ai_entry(
                available=False,
                extra={
                    "co2_baseline_status": "CO2_BASELINE_LOCKED",
                    "co2_baseline_ppm": 700.0,
                    "co2_delta_plus_ppm": 100.0,
                    "co2_relative_warning": False,
                },
            ),
        )
        engine._co2_history.clear()
        engine._co2_history.append((0.0, 675.0))
        engine._last_co2_sequence = 1
        result = engine.evaluate(state, ai)
        self.assertEqual(
            result.components["co2"]["metadata"]["slope_source"],
            "RISK_LOCAL_ENDPOINT",
        )
        self.assertGreaterEqual(
            result.components["co2"]["metadata"]["slope_ppm_per_min"], 50.0
        )
        self.assertFalse(result.caution_active)
        self.assertNotIn("co2_fast_rise", result.escalation_floors)


class OccupancyDangerGateTests(unittest.TestCase):
    def _vacant_mmwave(self):
        return sensor(values={
            "presence": False,
            "presence_available": True,
            "respiration_rate_bpm": 16.0,
            "respiration_valid": True,
        })

    def _occupied_mmwave(self):
        return sensor(values={
            "presence": True,
            "presence_available": True,
            "respiration_rate_bpm": 16.0,
            "respiration_valid": True,
        })

    def _locked_co2_ai(self, **extra):
        payload = {
            "co2_baseline_status": "CO2_BASELINE_LOCKED",
            "co2_baseline_ppm": 400.0,
            "co2_delta_plus_ppm": 100.0,
            "co2_relative_warning": False,
        }
        payload.update(extra)
        return ai_entry(available=False, extra=payload)

    def test_vacant_thermal_fall_is_not_danger(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._vacant_mmwave(),
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL",
                confidence=0.95,
                probabilities=(0.01, 0.04, 0.95),
            ),
            co2=sensor(values={"ppm": 500.0}),
            pir=sensor(values={"motion": False}),
        )
        result = engine.evaluate(state, ai)
        self.assertFalse(result.presence_detected)
        self.assertFalse(result.is_emergency)
        self.assertNotEqual(result.risk_level, "DANGER")
        self.assertIn(result.risk_level, {"NORMAL", "WARNING"})
        self.assertIn("VACANT_DANGER_SUPPRESSED", result.reasons)

    def test_vacant_score_in_danger_band_is_not_danger(self):
        from risk.engine import RiskComponent

        engine = SafeNestRiskFormulaV1()
        parts = {
            name: RiskComponent(name, True, 1.0, "rule", "HIGH", 1000.0)
            for name in ("mmwave", "co2", "pir", "thermal")
        }
        result = engine.fuse(
            parts,
            timestamp=1000.0,
            presence_detected=False,
            presence_source="MMWAVE",
        )
        self.assertGreaterEqual(result.risk_score, engine.danger_min)
        self.assertEqual(result.score_level, "NORMAL")
        self.assertNotEqual(result.risk_level, "DANGER")
        self.assertFalse(result.is_emergency)
        self.assertIn("VACANT_SCORE_DANGER_SUPPRESSED", result.reasons)

    def test_vacant_locked_delta_500_is_warning(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._vacant_mmwave(),
            co2=sensor(values={"ppm": 900.0}),
            co2_ai=self._locked_co2_ai(
                co2_delta_plus_ppm=500.0,
                co2_relative_warning=True,
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertFalse(result.presence_detected)
        self.assertTrue(result.caution_active)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertFalse(result.is_emergency)

    def test_vacant_locked_canonical_slope_50_is_warning(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._vacant_mmwave(),
            co2=sensor(values={"ppm": 800.0}),
            co2_ai=self._locked_co2_ai(
                co2_slope_ppm_per_min=50.0,
                slope_profile_id="CO2_SLOPE_FEATURE_PROFILE_001",
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertFalse(result.presence_detected)
        self.assertTrue(result.caution_active)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertFalse(result.is_emergency)

    def test_vacant_5000_ppm_is_warning_not_emergency(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._vacant_mmwave(),
            co2=sensor(values={"ppm": 5000.0}),
        )
        result = engine.evaluate(state, ai)
        self.assertFalse(result.presence_detected)
        self.assertFalse(result.is_emergency)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertTrue(result.caution_active)
        self.assertIn("co2_immediate_danger", result.caution_reasons)
        self.assertIn("VACANT_CO2_EMERGENCY_DEMOTED", result.reasons)

    def test_occupied_thermal_fall_stays_emergency(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._occupied_mmwave(),
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_FALL",
                confidence=0.95,
                probabilities=(0.01, 0.04, 0.95),
            ),
            co2=sensor(values={"ppm": 500.0}),
            pir=sensor(values={"motion": True}),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.presence_detected)
        self.assertTrue(result.is_emergency)
        self.assertEqual(result.risk_level, "DANGER")

    def test_occupied_locked_delta_500_is_warning(self):
        engine = SafeNestRiskFormulaV1()
        state, ai = scene(
            mmwave=self._occupied_mmwave(),
            thermal=sensor(),
            thermal_ai=ai_entry(
                state="HUMAN_NORMAL",
                confidence=0.98,
                probabilities=(0.01, 0.98, 0.01),
            ),
            pir=sensor(values={"motion": True}),
            co2=sensor(values={"ppm": 900.0}),
            co2_ai=self._locked_co2_ai(
                co2_delta_plus_ppm=500.0,
                co2_relative_warning=True,
            ),
        )
        result = engine.evaluate(state, ai)
        self.assertTrue(result.presence_detected)
        self.assertEqual(result.risk_level, "WARNING")
        self.assertFalse(result.is_emergency)


if __name__ == "__main__":
    unittest.main()
