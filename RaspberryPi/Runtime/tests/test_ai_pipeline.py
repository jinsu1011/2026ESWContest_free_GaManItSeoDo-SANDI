from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from ai.pipeline import OnDeviceAIPipeline
from ai.result import AIResult
from ai.runtime import LazyModel, ModelRuntimeUnavailable
from gateway.protocol import PacketHeader, ThermalFrame
from state.manager import SensorStateManager


def sensor(status="LIVE", values=None, sequence=1, last_update=100.0):
    return {
        "status": status,
        "values": values or {},
        "sequence": sequence,
        "last_update": last_update,
    }


def snapshot(**overrides):
    sensors = {
        "thermal": sensor(values={"frame_available": True}),
        "mmwave": sensor(values={}),
        "co2": sensor(values={"ppm": 800.0}),
        "pir": sensor(values={"motion": True}),
    }
    sensors.update(overrides)
    return {"timestamp": 100.0, "revision": 7, "sensors": sensors}


class FakeModel:
    def __init__(self, prediction=None, error=None, *, model_meta=None, model_selector="thermal"):
        self.prediction = prediction
        self.error = error
        self.calls = []
        self.model_meta = dict(model_meta or {})
        self.model_selector = model_selector

    def predict(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.prediction


def prediction(class_name, probabilities, confidence=0.9, **extra):
    fields = dict(
        class_name=class_name,
        probabilities=probabilities,
        confidence=confidence,
        latency_ms=2.5,
        model_id="test_model",
        model_version="0.1.0",
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


class AIPipelineTests(unittest.TestCase):
    def setUp(self):
        self.manager = SensorStateManager()

    def test_thermal_frame_runs_model_with_correct_shape(self):
        thermal = FakeModel(prediction("HUMAN_FALL", [0.01, 0.04, 0.95], 0.95))
        pipeline = OnDeviceAIPipeline(self.manager, {"thermal": thermal})
        pixels = [1000 + (index % 20) for index in range(80 * 62)]
        raw = b"".join(value.to_bytes(2, "big") for value in pixels)
        frame = ThermalFrame(PacketHeader(2, 1, 9936), 80, 62, 1, 10, 1000, 1019, raw)

        result = pipeline.evaluate(snapshot(), frame)["ai"]["thermal"]

        self.assertTrue(result["available"])
        self.assertEqual(result["state"], "HUMAN_FALL")
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(thermal.calls[0][0].shape, (62, 80))
        self.assertFalse(result["metadata"]["temperature_calibrated"])
        preview = result["metadata"]["heatmap_preview"]
        self.assertEqual((preview["width"], preview["height"]), (20, 16))
        self.assertEqual(len(preview["values"]), 320)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in preview["values"]))

    def test_model_failure_isolated_and_pir_rule_continues(self):
        thermal = FakeModel(error=RuntimeError("invoke failed"))
        pipeline = OnDeviceAIPipeline(self.manager, {"thermal": thermal})
        raw = (1000).to_bytes(2, "big") * (80 * 62)
        frame = ThermalFrame(PacketHeader(2, 1, 9936), 80, 62, 1, 10, 1000, 1000, raw)

        output = pipeline.evaluate(snapshot(), frame)["ai"]

        self.assertFalse(output["thermal"]["available"])
        self.assertEqual(output["thermal"]["error"], "MODEL_RUNTIME_UNAVAILABLE")
        self.assertEqual(len(output["thermal"]["metadata"]["heatmap_preview"]["values"]), 320)
        self.assertTrue(output["pir"]["available"])
        self.assertEqual(output["pir"]["source"], "rule")

    def test_active_proxy_model_contributes_bounded_risk_without_fall_state(self):
        thermal = FakeModel(
            prediction(
                "HUMAN_FALL_PROXY",
                [0.05, 0.70, 0.25],
                0.25,
                overlay_applied=True,
                posture_source="BBOX",
                model_class_name="HUMAN_NORMAL",
                bbox_height=10,
                bbox_width=50,
            ),
            model_selector="thermal_public_sdt_fp32_active",
            model_meta={
                "safety_authority": False,
                "risk_authority": "LIMITED_POSTURE_PROXY",
                "risk_contribution": "LIMITED_NON_EMERGENCY",
                "proxy_risk_score": 0.4,
            },
        )
        pipeline = OnDeviceAIPipeline(self.manager, {"thermal": thermal})
        raw = (1000).to_bytes(2, "big") * (80 * 62)
        frame = ThermalFrame(PacketHeader(2, 1, 9936), 80, 62, 1, 10, 1000, 1000, raw)

        result = pipeline.evaluate(snapshot(), frame)["ai"]["thermal"]

        self.assertEqual(result["state"], "HUMAN_FALL_PROXY")
        self.assertEqual(result["score"], 0.4)
        self.assertEqual(result["metadata"]["risk_authority"], "LIMITED_POSTURE_PROXY")
        self.assertFalse(result["metadata"]["safety_authority"])
        self.assertTrue(result["metadata"]["thermal_overlay_applied"])
        self.assertEqual(result["metadata"]["thermal_posture_source"], "BBOX")
        self.assertEqual(result["metadata"]["thermal_model_class_name"], "HUMAN_NORMAL")
        self.assertEqual(result["metadata"]["thermal_bbox_width"], 50)

    def test_stale_sensor_does_not_call_model(self):
        thermal = FakeModel(prediction("HUMAN_NORMAL", [0.0, 1.0, 0.0]))
        pipeline = OnDeviceAIPipeline(self.manager, {"thermal": thermal})
        output = pipeline.evaluate(snapshot(thermal=sensor("STALE")))["ai"]["thermal"]
        self.assertEqual(output["error"], "SENSOR_STALE")
        self.assertEqual(thermal.calls, [])

    def test_mmwave_empty_snapshot_fail_closes_without_mn9(self):
        mmwave = FakeModel(prediction("NORMAL", [0.9, 0.08, 0.02]))
        pipeline = OnDeviceAIPipeline(self.manager, {"mmwave": mmwave})
        missing = pipeline.evaluate(snapshot())["ai"]["mmwave"]
        self.assertFalse(missing["available"])
        self.assertNotEqual(missing["source"], "tflite")
        self.assertNotIn(missing["state"], {"NORMAL", "APNEA", "APNEA-proxy"})
        self.assertEqual(mmwave.calls, [])

    def test_mmwave_heuristic_fallback_is_not_reported_as_ai(self):
        pred = prediction(
            "APNEA", [0.02, 0.03, 0.95], fallback_used=True, fallback_reason="NO_TFLITE"
        )
        pipeline = OnDeviceAIPipeline(self.manager, {"mmwave": FakeModel(pred)})
        ready = snapshot(mmwave=sensor(values={}))
        result = pipeline.evaluate(ready)["ai"]["mmwave"]
        self.assertFalse(result["available"])
        self.assertNotEqual(result["state"], "APNEA")
        self.assertNotEqual(result["source"], "tflite")

    def test_co2_uses_the_c_b6_reduced_contract_without_humidity(self):
        """C-B6 takes ppm and ppm/min only; humidity is a forbidden input."""

        co2 = FakeModel(prediction("OCCUPIED", [0.1, 0.9]))
        pipeline = OnDeviceAIPipeline(self.manager, {"co2": co2})
        result = pipeline.evaluate(snapshot())["ai"]["co2"]
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "CO2_MEASUREMENT_CLOCK_UNAVAILABLE")
        self.assertEqual(co2.calls, [])

        def measurement(event_id: int, clock_ms: float, ppm: float):
            return sensor(
                values={
                    "ppm": ppm,
                    "latest_measurement_ppm": ppm,
                    "measurement_event_valid": True,
                    "measurement_event_id": event_id,
                    "measurement_monotonic_ms": clock_ms,
                },
                sequence=event_id,
            )

        # One event: warming up, never a fabricated 0.0 ppm/min slope.
        warming = pipeline.evaluate(snapshot(co2=measurement(1, 0.0, 800.0)))["ai"]["co2"]
        self.assertEqual(warming["state"], "FEATURE_UNAVAILABLE_WARMUP")
        self.assertIsNone(warming["metadata"].get("slope_ppm_per_min"))
        self.assertEqual(co2.calls, [])

        # Nominal SCD40 cadence is 60 s, inside max_internal_gap_seconds = 90 s.
        # Under the 150 s history requirement it is still warming up.
        for event_id, clock_ms, ppm in ((2, 60_000.0, 830.0), (3, 120_000.0, 860.0)):
            early = pipeline.evaluate(snapshot(co2=measurement(event_id, clock_ms, ppm)))["ai"]["co2"]
            self.assertEqual(early["state"], "FEATURE_UNAVAILABLE_WARMUP", early)
            self.assertEqual(co2.calls, [])

        # 180 s endpoint span: (890 - 800) / 3 min = 30.0 ppm/min
        ready = pipeline.evaluate(snapshot(co2=measurement(4, 180_000.0, 890.0)))["ai"]["co2"]
        self.assertTrue(ready["available"], ready)
        self.assertEqual(ready["metadata"]["endpoint_span_seconds"], 180.0)
        self.assertEqual(len(co2.calls[0]), 2)  # ppm, slope - no humidity argument
        self.assertAlmostEqual(co2.calls[0][0], 890.0)
        self.assertAlmostEqual(co2.calls[0][1], 30.0)
        self.assertAlmostEqual(ready["metadata"]["co2_slope_ppm_per_min"], 30.0)
        self.assertEqual(ready["metadata"]["slope_unit"], "ppm/min")
        self.assertEqual(ready["metadata"]["slope_method"], "ENDPOINT_DIFFERENCE")
        self.assertFalse(ready["metadata"]["humidity_required"])
        # Occupancy is not a hazard weight.
        self.assertEqual(ready["score"], 0.0)
        self.assertTrue(ready["metadata"]["risk_contribution_deferred"])
        self.assertEqual(ready["metadata"]["co2_baseline_status"], "CO2_BASELINE_LOCKED")

    def test_co2_room_baseline_locks_after_three_minutes(self):
        pipeline = OnDeviceAIPipeline(self.manager, {"co2": FakeModel(prediction("VACANT", [0.9, 0.1]))})

        def measurement(event_id: int, clock_ms: float, ppm: float):
            return sensor(
                values={
                    "ppm": ppm,
                    "latest_measurement_ppm": ppm,
                    "measurement_event_valid": True,
                    "measurement_event_id": event_id,
                    "measurement_monotonic_ms": clock_ms,
                },
                sequence=event_id,
            )

        warming = pipeline.evaluate(snapshot(co2=measurement(1, 0.0, 400.0)))["ai"]["co2"]
        self.assertEqual(warming["metadata"]["co2_baseline_status"], "CO2_BASELINE_UNLOCKED_WARMUP")
        self.assertFalse(warming["metadata"]["co2_relative_warning"])
        pipeline.evaluate(snapshot(co2=measurement(2, 60_000.0, 400.0)))
        pipeline.evaluate(snapshot(co2=measurement(3, 120_000.0, 400.0)))
        locked = pipeline.evaluate(snapshot(co2=measurement(4, 180_000.0, 400.0)))["ai"]["co2"]
        self.assertEqual(locked["metadata"]["co2_baseline_status"], "CO2_BASELINE_LOCKED")
        self.assertEqual(locked["metadata"]["co2_baseline_ppm"], 400.0)
        self.assertEqual(locked["metadata"]["co2_delta_plus_ppm"], 0.0)

    def test_co2_restarts_history_after_a_forbidden_gap(self):
        co2 = FakeModel(prediction("OCCUPIED", [0.1, 0.9]))
        pipeline = OnDeviceAIPipeline(self.manager, {"co2": co2})

        def measurement(event_id: int, clock_ms: float, ppm: float):
            return sensor(
                values={
                    "ppm": ppm,
                    "latest_measurement_ppm": ppm,
                    "measurement_event_valid": True,
                    "measurement_event_id": event_id,
                    "measurement_monotonic_ms": clock_ms,
                },
                sequence=event_id,
            )

        pipeline.evaluate(snapshot(co2=measurement(1, 0.0, 800.0)))
        # 120 s gap exceeds max_internal_gap_seconds = 90 s -> history restarts.
        after_gap = pipeline.evaluate(snapshot(co2=measurement(2, 300_000.0, 900.0)))["ai"]["co2"]
        self.assertFalse(after_gap["available"])
        self.assertGreaterEqual(after_gap["metadata"]["gap_restarts"], 1)
        self.assertEqual(co2.calls, [])

    def test_result_rejects_nan_and_output_is_strict_json(self):
        with self.assertRaises(ValueError):
            AIResult("thermal", 1.0, True, "rule", "X", score=float("nan"))
        encoded = json.dumps(OnDeviceAIPipeline(self.manager).evaluate(snapshot()), allow_nan=False)
        self.assertIn('"degraded": true', encoded)

    def test_lazy_model_caches_load_failure(self):
        calls = []

        def broken_factory():
            calls.append(1)
            raise ModuleNotFoundError("tflite runtime missing")

        model = LazyModel("thermal", factory=broken_factory)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "tflite runtime missing"):
                model.predict([])
        self.assertEqual(calls, [1])

    def test_historical_v0_1_0_mmwave_remains_release_blocked(self):
        root = (__import__("paths", fromlist=["ONDEVICE_AI_ROOT"]).ONDEVICE_AI_ROOT)
        manifest = json.loads((root / "models" / "model_manifest.json").read_text(encoding="utf-8"))
        historical = manifest["models"]["mmwave_v0_1_0"]
        self.assertFalse(historical["deployment_allowed"])
        self.assertEqual(historical["block_reason"], "CLASS_COLLAPSE_ON_REPOSITORY_NPZ")
        self.assertEqual(historical["runtime_role"], "HISTORICAL_V0_1_0")
        active = manifest["models"]["mmwave"]
        self.assertEqual(active["model_id"], "M-PV2_FAMILY_B_TRACE_TCN_BREATHING_RR_QUALITY")
        self.assertEqual(active["runtime_role"], "ACTIVE_B23_PROTOTYPE")
        self.assertTrue(active["active_runtime_selector"])
        self.assertEqual(active["deployment_scope"], "PROTOTYPE_INTEGRATION_ONLY")
        self.assertEqual(active["hardware_validation"], "NOT_PERFORMED")
        self.assertFalse(active["DEVICE_VALIDATED"])
        legacy = manifest["models"]["mmwave_m_n9"]
        self.assertEqual(legacy["runtime_role"], "LEGACY_M_N9_NONACTIVE")
        self.assertFalse(legacy["deployment_allowed"])
        self.assertEqual(
            LazyModel._ADAPTERS["mmwave"],
            ("mmwave_m_n9_interpreter.py", "MN9Interpreter", "mmwave_m_n9"),
        )

    def test_thermal_runtime_selector_is_public_sdt_fp32(self):
        root = (__import__("paths", fromlist=["ONDEVICE_AI_ROOT"]).ONDEVICE_AI_ROOT)
        manifest = json.loads((root / "models" / "model_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["active_runtime_selectors"]["thermal"],
            "thermal_public_sdt_fp32_active",
        )
        active = manifest["models"]["thermal_public_sdt_fp32_active"]
        self.assertEqual(active["artifact_release"], "FINAL_RUNTIME_MODEL")
        self.assertTrue(active["deployment_allowed"])
        self.assertEqual(
            LazyModel._ADAPTERS["thermal"],
            (
                "thermal_interpreter.py",
                "ThermalInterpreter",
                "thermal_public_sdt_fp32_active",
            ),
        )

    def test_frozen_model_hashes_match_manifest(self):
        root = (__import__("paths", fromlist=["ONDEVICE_AI_ROOT"]).ONDEVICE_AI_ROOT)
        manifest = json.loads((root / "models" / "model_manifest.json").read_text(encoding="utf-8"))
        for name, metadata in manifest["models"].items():
            with self.subTest(name=name):
                model = root / metadata["path"]
                self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(), metadata["sha256"])
                if "size_bytes" in metadata:
                    self.assertEqual(model.stat().st_size, metadata["size_bytes"])


if __name__ == "__main__":
    unittest.main()
