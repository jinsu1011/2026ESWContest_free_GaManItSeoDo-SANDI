"""C-B6 reduced-feature CO2 runtime: slope contract and interpreter identity.

Synthetic runtime mechanics only. No Raspberry Pi, no SCD40, no live sensors.
The interpreter tests need a TFLite runtime and skip without one.
"""

from __future__ import annotations

from paths import ONDEVICE_AI_ROOT, RUNTIME_ROOT
import importlib.util
import json
from pathlib import Path
import unittest

from ai.co2_canonical_runtime import PROFILE_ID, CO2SlopeWindowBuilder

ROOT = RUNTIME_ROOT
ONDEVICE = ONDEVICE_AI_ROOT
CONTRACT_DIR = ONDEVICE / "models" / "rp_x0_b_complete" / "co2"


def event(event_id: int, clock_ms: float, ppm: float, *, boot="boot-a", valid=True):
    return {
        "device_id": "esp32-01",
        "boot_id": boot,
        "values": {
            "measurement_event_valid": valid,
            "measurement_event_id": event_id,
            "measurement_monotonic_ms": clock_ms,
            "latest_measurement_ppm": ppm,
        },
    }


def feed(builder: CO2SlopeWindowBuilder, samples, *, boot="boot-a", start_id=1):
    for offset, (clock_ms, ppm) in enumerate(samples):
        builder.observe(event(start_id + offset, clock_ms, ppm, boot=boot))
    return builder.latest()


class SlopeProfileContractTests(unittest.TestCase):
    def test_builder_matches_the_locked_slope_profile(self):
        profile = json.loads(
            (CONTRACT_DIR / "co2_slope_feature_profile.json").read_text(encoding="utf-8")
        )
        builder = CO2SlopeWindowBuilder()
        self.assertEqual(PROFILE_ID, profile["profile_id"])
        self.assertEqual(builder.history_seconds, profile["history_duration_seconds"])
        self.assertEqual(builder.minimum_elapsed_seconds, profile["minimum_elapsed_seconds"])
        self.assertEqual(builder.minimum_samples, profile["minimum_source_samples"])
        self.assertEqual(
            builder.max_internal_gap_seconds, profile["max_internal_gap_seconds"]
        )

    def test_endpoint_difference_is_ppm_per_minute(self):
        builder = CO2SlopeWindowBuilder()
        # 60 s cadence, 180 s total span, +90 ppm -> 30.0 ppm/min
        result = feed(builder, [(0, 800.0), (60_000, 830.0), (120_000, 860.0), (180_000, 890.0)])
        self.assertTrue(result.ready)
        self.assertAlmostEqual(result.slope_ppm_per_min, 30.0)
        self.assertEqual(result.ppm, 890.0)
        self.assertEqual(result.metadata["slope_unit"], "ppm/min")
        self.assertEqual(result.metadata["endpoint_span_seconds"], 180.0)
        self.assertEqual(result.metadata["endpoint_ppm"], 800.0)

    def test_negative_slope_is_preserved(self):
        builder = CO2SlopeWindowBuilder()
        result = feed(builder, [(0, 1200.0), (60_000, 1100.0), (120_000, 1000.0), (180_000, 900.0)])
        self.assertTrue(result.ready)
        self.assertAlmostEqual(result.slope_ppm_per_min, -100.0)

    def test_warmup_never_reports_zero_slope(self):
        builder = CO2SlopeWindowBuilder()
        self.assertEqual(builder.latest().status, "CO2_MEASUREMENT_CLOCK_UNAVAILABLE")
        first = feed(builder, [(0, 800.0)])
        self.assertEqual(first.status, "FEATURE_UNAVAILABLE_WARMUP")
        self.assertIsNone(first.slope_ppm_per_min)
        # 120 s of history is under the 150 s requirement.
        short = feed(builder, [(60_000, 830.0), (120_000, 860.0)], start_id=2)
        self.assertEqual(short.status, "FEATURE_UNAVAILABLE_WARMUP")
        self.assertIsNone(short.slope_ppm_per_min)

    def test_forbidden_gap_restarts_history(self):
        builder = CO2SlopeWindowBuilder()
        feed(builder, [(0, 800.0), (60_000, 830.0), (120_000, 860.0), (180_000, 890.0)])
        self.assertTrue(builder.latest().ready)
        # A 200 s gap exceeds max_internal_gap_seconds = 90 s.
        after = feed(builder, [(380_000, 1000.0)], start_id=10)
        self.assertEqual(after.status, "FEATURE_UNAVAILABLE_GAP_RESTART")
        self.assertIsNone(after.slope_ppm_per_min)
        self.assertGreaterEqual(after.metadata["gap_restarts"], 1)
        self.assertTrue(after.metadata["gap_restart_pending"])
        # Recovery: 150 s of clean post-restart history clears the restart flag.
        recovered = feed(
            builder,
            [(440_000, 1010.0), (500_000, 1020.0), (560_000, 1030.0)],
            start_id=11,
        )
        self.assertTrue(recovered.ready, recovered)
        self.assertFalse(recovered.metadata["gap_restart_pending"])

    def test_republished_events_do_not_advance_history(self):
        builder = CO2SlopeWindowBuilder()
        record = event(1, 0.0, 800.0)
        for _ in range(50):
            builder.observe(record)
        result = builder.latest()
        self.assertEqual(result.metadata["accepted_measurement_events"], 1)
        self.assertEqual(result.status, "FEATURE_UNAVAILABLE_WARMUP")

    def test_invalid_measurement_events_are_ignored(self):
        builder = CO2SlopeWindowBuilder()
        builder.observe(event(1, 0.0, 800.0, valid=False))
        self.assertEqual(builder.latest().status, "CO2_MEASUREMENT_CLOCK_UNAVAILABLE")
        builder.observe(event(2, 0.0, float("nan")))
        self.assertEqual(builder.latest().status, "CO2_MEASUREMENT_CLOCK_UNAVAILABLE")

    def test_boot_boundary_restarts_history(self):
        builder = CO2SlopeWindowBuilder()
        feed(builder, [(0, 800.0), (60_000, 830.0), (120_000, 860.0), (180_000, 890.0)])
        self.assertTrue(builder.latest().ready)
        builder.observe(event(99, 0.0, 500.0, boot="boot-b"))
        self.assertFalse(builder.latest().ready)

    def test_missing_event_id_does_not_synthesize_history(self):
        builder = CO2SlopeWindowBuilder()
        builder.observe(
            {
                "device_id": "esp32-01",
                "boot_id": "boot-a",
                "values": {
                    "measurement_event_valid": True,
                    "measurement_event_id": None,
                    "measurement_monotonic_ms": 0.0,
                    "latest_measurement_ppm": 800.0,
                },
            }
        )
        self.assertEqual(builder.latest().status, "CO2_MEASUREMENT_CLOCK_UNAVAILABLE")
        self.assertEqual(builder.latest().metadata["accepted_measurement_events"], 0)

    def test_non_monotonic_source_clock_restarts_history(self):
        builder = CO2SlopeWindowBuilder()
        feed(builder, [(0, 800.0), (60_000, 830.0), (120_000, 860.0), (180_000, 890.0)])
        self.assertTrue(builder.latest().ready)
        after = feed(builder, [(90_000, 900.0)], start_id=20)
        self.assertFalse(after.ready)


class CB6InterpreterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for module in ("ai_edge_litert", "tflite_runtime", "tensorflow"):
            if importlib.util.find_spec(module) is not None:
                break
        else:
            raise unittest.SkipTest("no TFLite runtime available")
        import sys

        spec = importlib.util.spec_from_file_location(
            "_test_c_b6", ONDEVICE / "inference" / "co2_c_b6_interpreter.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        # dataclasses resolve through sys.modules, so register before executing.
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.interpreter = cls.module.CB6Interpreter(project_root=ONDEVICE)

    def test_manifest_selector_and_identity(self):
        manifest = json.loads(
            (ONDEVICE / "models" / "model_manifest.json").read_text(encoding="utf-8")
        )["models"]["co2_occupancy_c_b6"]
        self.assertEqual(manifest["model_id"], "C_B6_REDUCED_CO2_SLOPE_CANDIDATE_001")
        self.assertEqual(manifest["runtime_role"], "ACTIVE_C_B6")
        self.assertEqual(manifest["input"]["shape"], [1, 2])
        self.assertEqual(manifest["input"]["feature_order"], ["CO2", "CO2_slope"])
        self.assertFalse(manifest["input"]["humidity_included"])
        self.assertEqual(manifest["risk_semantic"], "NONE")
        self.assertEqual(manifest["safety_semantic"], "NONE")
        self.assertEqual(self.interpreter.threshold, 0.43)
        self.assertEqual(self.interpreter.threshold_source, "TRAIN_INTERNAL_ONLY")
        self.assertEqual(self.interpreter.history_seconds, 150.0)
        self.assertEqual(self.interpreter.max_internal_gap_seconds, 90.0)

    def test_predict_takes_two_features_and_no_humidity(self):
        prediction = self.interpreter.predict(1184.0, 1.0)
        self.assertIn(prediction.class_name, {"VACANT", "OCCUPIED"})
        self.assertEqual(len(prediction.probabilities), 2)
        self.assertAlmostEqual(sum(prediction.probabilities), 1.0, places=6)
        self.assertEqual(len(prediction.standardized_features), 2)
        self.assertEqual(prediction.risk_semantic, "NONE")
        self.assertEqual(prediction.safety_semantic, "NONE")
        with self.assertRaises(TypeError):
            self.interpreter.predict(1.0, 45.0, 1184.0)  # historical 3-feature call

    def test_occupancy_is_monotonic_in_ppm(self):
        previous = -1.0
        for ppm in (300.0, 420.0, 600.0, 800.0, 1200.0, 2500.0):
            probability = self.interpreter.predict(ppm, 0.0).occupancy_probability
            self.assertGreaterEqual(probability, previous)
            previous = probability

    def test_empty_room_baseline_is_vacant(self):
        self.assertEqual(self.interpreter.predict(420.0, 0.0).class_name, "VACANT")
        self.assertEqual(self.interpreter.predict(1000.0, 5.0).class_name, "OCCUPIED")

    def test_nonfinite_features_fail_closed(self):
        with self.assertRaises(ValueError):
            self.interpreter.predict(float("nan"), 1.0)
        with self.assertRaises(ValueError):
            self.interpreter.predict(800.0, float("inf"))


if __name__ == "__main__":
    unittest.main()
