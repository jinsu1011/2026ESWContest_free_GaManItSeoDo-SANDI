"""MH-Z19B live ingest: event identity into frozen H150, no slope-math rewrite.

Synthetic Runtime path only. No ESP32 flash, no model/scaler/threshold change.
"""

from __future__ import annotations

import unittest

from ai.co2_canonical_runtime import h150_model_input_eligible
from ai.pipeline import OnDeviceAIPipeline
from gateway.protocol import PACKET_TELEMETRY_JSON, PacketHeader, TelemetryPayload
from state.manager import SensorStateManager


def packet(
    *,
    sequence: int = 1,
    event_id: int | None = 1,
    clock_ms: int | None = 0,
    event_valid: bool | None = True,
    ppm: float | None = 800.0,
    boot_id: str | None = "boot-a",
    preheat: bool | None = True,
    include_preheat: bool = True,
    sensor_model: str | None = "MH-Z19B",
    identity: str | None = "INFERRED_UART_SAMPLE",
    abc_enabled: bool | None = False,
    configured_range_ppm: int | None = 5000,
) -> TelemetryPayload:
    valid_co2 = ppm is not None
    return TelemetryPayload(
        header=PacketHeader(PACKET_TELEMETRY_JSON, sequence, 100),
        device_id="esp32-01",
        uptime_ms=sequence * 1_000,
        respiration_rate_bpm=16.0,
        heart_rate_bpm=72.0,
        co2_ppm=ppm,
        pir_motion=False,
        valid={"respiration": True, "heart": True, "co2": valid_co2},
        boot_id=boot_id,
        co2_measurement_event_id=event_id,
        co2_measurement_monotonic_ms=clock_ms,
        co2_measurement_event_valid=event_valid,
        co2_sensor_model=sensor_model,
        co2_event_identity_class=identity,
        co2_preheat_complete=preheat if include_preheat else None,
        abc_enabled=abc_enabled,
        configured_range_ppm=configured_range_ppm,
    )


class H150EligibilityTests(unittest.TestCase):
    def test_requires_valid_event_finite_ppm_and_preheat_true(self) -> None:
        self.assertTrue(
            h150_model_input_eligible(
                {
                    "values": {
                        "measurement_event_valid": True,
                        "measurement_event_id": 1,
                        "measurement_monotonic_ms": 0,
                        "latest_measurement_ppm": 800.0,
                        "preheat_complete": True,
                    }
                }
            )
        )

    def test_missing_preheat_is_unknown_not_eligible(self) -> None:
        self.assertFalse(
            h150_model_input_eligible(
                {
                    "values": {
                        "measurement_event_valid": True,
                        "measurement_event_id": 1,
                        "measurement_monotonic_ms": 0,
                        "latest_measurement_ppm": 800.0,
                    }
                }
            )
        )


class Mhz19bH150IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = OnDeviceAIPipeline(SensorStateManager())

    def _accepted(self) -> int:
        return int(self.pipeline._co2_window.latest().metadata["accepted_measurement_events"])

    def test_same_event_id_does_not_grow_history(self) -> None:
        first = packet(sequence=1, event_id=1, clock_ms=0, ppm=800.0)
        self.pipeline.observe_telemetry(first)
        self.pipeline.observe_telemetry(
            packet(sequence=2, event_id=1, clock_ms=0, ppm=800.0)
        )
        self.pipeline.observe_telemetry(
            packet(sequence=3, event_id=1, clock_ms=0, ppm=800.0)
        )
        result = self.pipeline._co2_window.latest()
        self.assertEqual(result.metadata["accepted_measurement_events"], 1)
        self.assertEqual(result.metadata["retained_samples"], 1)
        self.assertEqual(result.status, "FEATURE_UNAVAILABLE_WARMUP")

    def test_two_events_150s_apart_compute_endpoint_h150_slope(self) -> None:
        # Nominal ~60 s cadence. A raw 150 s silent gap would restart history
        # (frozen max_internal_gap_seconds = 90). Intermediate event 2 keeps
        # the window continuous so event 1..3 can select a 150 s endpoint.
        self.pipeline.observe_telemetry(
            packet(sequence=1, event_id=1, clock_ms=0, ppm=800.0)
        )
        self.pipeline.observe_telemetry(
            packet(sequence=2, event_id=1, clock_ms=0, ppm=800.0)
        )
        self.pipeline.observe_telemetry(
            packet(sequence=3, event_id=2, clock_ms=60_000, ppm=860.0)
        )
        self.pipeline.observe_telemetry(
            packet(sequence=4, event_id=3, clock_ms=150_000, ppm=950.0)
        )
        result = self.pipeline._co2_window.latest()
        self.assertTrue(result.ready, result)
        self.assertAlmostEqual(result.slope_ppm_per_min, 60.0)
        self.assertEqual(result.ppm, 950.0)
        self.assertEqual(result.metadata["slope_method"], "ENDPOINT_DIFFERENCE")
        self.assertEqual(result.metadata["endpoint_span_seconds"], 150.0)
        self.assertEqual(result.metadata["accepted_measurement_events"], 3)

    def test_event_valid_false_is_not_ingested(self) -> None:
        self.pipeline.observe_telemetry(
            packet(
                sequence=1,
                event_id=0,
                clock_ms=0,
                event_valid=False,
                ppm=800.0,
            )
        )
        self.assertEqual(self._accepted(), 0)
        self.assertEqual(
            self.pipeline._co2_window.latest().status,
            "CO2_MEASUREMENT_CLOCK_UNAVAILABLE",
        )

    def test_missing_event_triple_does_not_synthesize_ids_from_seq(self) -> None:
        self.pipeline.observe_telemetry(
            packet(
                sequence=99,
                event_id=None,
                clock_ms=None,
                event_valid=None,
                ppm=800.0,
            )
        )
        self.assertEqual(self._accepted(), 0)
        self.assertIsNone(self.pipeline._co2_window._last_event_key)

    def test_preheat_false_is_not_c_b6_input(self) -> None:
        self.pipeline.observe_telemetry(
            packet(sequence=1, event_id=1, clock_ms=0, ppm=800.0, preheat=False)
        )
        self.assertEqual(self._accepted(), 0)

    def test_missing_preheat_is_not_c_b6_input(self) -> None:
        self.pipeline.observe_telemetry(
            packet(
                sequence=1,
                event_id=1,
                clock_ms=0,
                ppm=800.0,
                include_preheat=False,
            )
        )
        self.assertEqual(self._accepted(), 0)

    def test_device_domain_change_does_not_pool_scd40_and_mhz19b_history(self) -> None:
        self.pipeline.observe_telemetry(
            packet(
                sequence=1,
                event_id=1,
                clock_ms=0,
                ppm=800.0,
                sensor_model="SCD40",
            )
        )
        self.pipeline.observe_telemetry(
            packet(
                sequence=2,
                event_id=2,
                clock_ms=60_000,
                ppm=830.0,
                sensor_model="SCD40",
            )
        )
        self.assertEqual(self._accepted(), 2)
        self.pipeline.observe_telemetry(
            packet(
                sequence=3,
                event_id=3,
                clock_ms=120_000,
                ppm=500.0,
                sensor_model="MH-Z19B",
            )
        )
        result = self.pipeline._co2_window.latest()
        self.assertEqual(result.metadata["retained_samples"], 1)
        self.assertEqual(result.ppm, 500.0)
        self.assertFalse(result.ready)

    def test_evaluate_exposes_mhz19b_domain_metadata(self) -> None:
        manager = SensorStateManager()
        pipeline = OnDeviceAIPipeline(manager)
        live = packet(sequence=1, event_id=1, clock_ms=0, ppm=800.0)
        manager.ingest(live, ("192.168.1.20", 40000), received_at=100.0, monotonic_at=10.0)
        pipeline.observe_telemetry(live)
        snapshot = manager.snapshot(now=100.0, monotonic_now=10.0)
        result = pipeline.evaluate(snapshot)["ai"]["co2"]
        self.assertEqual(result["metadata"]["co2_sensor_model"], "MH-Z19B")
        self.assertEqual(
            result["metadata"]["co2_event_identity_class"], "INFERRED_UART_SAMPLE"
        )
        self.assertEqual(
            result["metadata"]["co2_identity_limitation"],
            "INFERRED_UART_SAMPLE_NOT_SCD40_DATA_READY",
        )
        self.assertTrue(result["metadata"]["co2_preheat_complete"])
        self.assertEqual(result["metadata"]["co2_device_domain"], "MH-Z19B")


class SensorStateMhz19bPassThroughTests(unittest.TestCase):
    def test_snapshot_keeps_mhz19b_fields_without_inventing_event_ids(self) -> None:
        manager = SensorStateManager()
        manager.ingest(
            packet(
                sequence=1,
                event_id=None,
                clock_ms=None,
                event_valid=None,
                ppm=820.0,
                sensor_model="MH-Z19B",
                identity="INFERRED_UART_SAMPLE",
                preheat=False,
            ),
            ("192.168.1.20", 40000),
            received_at=100.0,
            monotonic_at=10.0,
        )
        values = manager.snapshot(now=100.0, monotonic_now=10.0)["sensors"]["co2"]["values"]
        self.assertEqual(values["sensor_model"], "MH-Z19B")
        self.assertEqual(values["event_identity_class"], "INFERRED_UART_SAMPLE")
        self.assertFalse(values["preheat_complete"])
        self.assertIsNone(values["measurement_event_id"])
        self.assertEqual(values["measurement_event_count"], 0)
        self.assertEqual(values["ppm"], 820.0)


if __name__ == "__main__":
    unittest.main()
