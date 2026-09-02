from __future__ import annotations

from types import SimpleNamespace
import unittest

from ai.pipeline import OnDeviceAIPipeline
from gateway.protocol import PacketHeader, TelemetryPayload, ThermalFrame
from risk.engine import SafeNestRiskEngine
from state.manager import SensorStateManager


class FakeThermalModel:
    def predict(self, _frame):
        return SimpleNamespace(
            class_name="HUMAN_FALL",
            probabilities=[0.01, 0.04, 0.95],
            confidence=0.95,
            latency_ms=2.0,
            model_id="thermal_test",
            model_version="0.1.0",
        )


class GatewayRiskPipelineTests(unittest.TestCase):
    def test_packets_flow_to_emergency_risk_without_other_ai_models(self):
        manager = SensorStateManager()
        telemetry = TelemetryPayload(
            header=PacketHeader(1, 1, 100),
            device_id="node-1",
            uptime_ms=10,
            respiration_rate_bpm=15.0,
            heart_rate_bpm=70.0,
            co2_ppm=800.0,
            pir_motion=False,
            valid={"respiration": True, "heart": True, "co2": True},
        )
        raw = (1000).to_bytes(2, "big") * (80 * 62)
        thermal = ThermalFrame(
            PacketHeader(2, 1, 9936), 80, 62, 1, 10, 1000, 1000, raw
        )
        manager.ingest(telemetry, ("127.0.0.1", 5000), received_at=100.0, monotonic_at=10.0)
        manager.ingest(thermal, ("127.0.0.1", 5000), received_at=100.0, monotonic_at=10.0)
        state = manager.snapshot(now=100.0, monotonic_now=10.0)

        ai = OnDeviceAIPipeline(manager, {"thermal": FakeThermalModel()}).evaluate(state, thermal)
        risk = SafeNestRiskEngine().evaluate(state, ai)

        self.assertEqual(risk.risk_level, "DANGER")
        self.assertEqual(risk.risk_score, 100.0)
        self.assertTrue(risk.is_emergency)
        self.assertEqual(risk.component_status["mmwave"], "RULE_FALLBACK")
        self.assertEqual(risk.component_status["co2"], "RULE_FALLBACK")
        self.assertIn("EMERGENCY_HUMAN_FALL", risk.reasons)


if __name__ == "__main__":
    unittest.main()
