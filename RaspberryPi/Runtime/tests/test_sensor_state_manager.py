from __future__ import annotations

import json
import struct
import threading
import unittest

from gateway.protocol import (
    PACKET_TELEMETRY_JSON,
    PACKET_THERMAL_U16_BE,
    PacketHeader,
    TelemetryPayload,
    ThermalFrame,
)
from state.manager import SensorStateManager


PEER = ("192.168.1.20", 40_000)


def telemetry(
    sequence: int = 1,
    *,
    respiration: float | None = 16.0,
    heart: float | None = 72.0,
    co2: float | None = 800.0,
    motion: bool = False,
    boot_id: str | None = None,
    co2_event_id: int | None = None,
    co2_event_ms: int | None = None,
    co2_event_valid: bool | None = None,
    pir_event_id: int | None = None,
    pir_transition_ms: int | None = None,
    health: dict[str, int] | None = None,
    breath_phase: float | None = None,
    ts_monotonic_ms: float | None = None,
    phase_age_ms: float | None = None,
    mmwave_sequence: int | None = None,
    human_detected_raw: bool | None = None,
) -> TelemetryPayload:
    valid = {
        "respiration": respiration is not None,
        "heart": heart is not None,
        "co2": co2 is not None,
    }
    return TelemetryPayload(
        header=PacketHeader(PACKET_TELEMETRY_JSON, sequence, 100),
        device_id="esp32-01",
        uptime_ms=sequence * 1_000,
        respiration_rate_bpm=respiration,
        heart_rate_bpm=heart,
        co2_ppm=co2,
        pir_motion=motion,
        valid=valid,
        boot_id=boot_id,
        co2_measurement_event_id=co2_event_id,
        co2_measurement_monotonic_ms=co2_event_ms,
        co2_measurement_event_valid=co2_event_valid,
        pir_event_id=pir_event_id,
        pir_last_transition_monotonic_ms=pir_transition_ms,
        health=health,
        breath_phase=breath_phase,
        ts_monotonic_ms=ts_monotonic_ms,
        phase_age_ms=phase_age_ms,
        human_detected_raw=human_detected_raw,
        mmwave_sequence=mmwave_sequence,
    )


def thermal(sequence: int = 1) -> ThermalFrame:
    pixel_count = 80 * 62
    pixels = struct.pack(f"!{pixel_count}H", *([1_000] * pixel_count))
    return ThermalFrame(
        header=PacketHeader(PACKET_THERMAL_U16_BE, sequence, 9_936),
        width=80,
        height=62,
        frame_sequence=sequence,
        uptime_ms=sequence * 1_000,
        minimum_raw=1_000,
        maximum_raw=1_000,
        pixel_bytes=pixels,
    )


class SensorStateManagerTests(unittest.TestCase):
    def test_initial_state_is_explicit_no_data(self) -> None:
        state = SensorStateManager().snapshot(now=100.0, monotonic_now=50.0)
        self.assertEqual(state["system"], "OFFLINE")
        for sensor in state["sensors"].values():
            self.assertEqual(sensor["status"], "NO_DATA")
            self.assertFalse(sensor["connected"])
            self.assertFalse(sensor["current"])
            self.assertIsNone(sensor["last_update"])

    def test_telemetry_updates_three_sensors_not_thermal(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(4, motion=True), PEER, received_at=100.0, monotonic_at=10.0)
        state = manager.snapshot(now=100.1, monotonic_now=10.1)

        self.assertEqual(state["system"], "DEGRADED")
        self.assertEqual(state["sensors"]["mmwave"]["status"], "LIVE")
        self.assertEqual(state["sensors"]["co2"]["values"]["ppm"], 800.0)
        self.assertTrue(state["sensors"]["pir"]["values"]["motion"])
        self.assertEqual(state["sensors"]["thermal"]["status"], "NO_DATA")

    def test_device_health_has_canonical_root_and_legacy_mmwave_alias(self) -> None:
        health = {
            "co2_read_failures": 3,
            "thermal_status_query_failures": 4,
        }
        manager = SensorStateManager()
        manager.ingest(telemetry(health=health), PEER, received_at=100.0, monotonic_at=10.0)

        state = manager.snapshot(now=100.0, monotonic_now=10.0)
        self.assertEqual(state["device_health"], health)
        self.assertEqual(state["sensors"]["mmwave"]["values"]["health"], health)
        self.assertNotIn("health", state["sensors"]["co2"]["values"])
        self.assertNotIn("health", state["sensors"]["pir"]["values"])

    def test_missing_presence_is_not_invented(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(), PEER, received_at=100.0, monotonic_at=10.0)
        values = manager.snapshot(now=100.0, monotonic_now=10.0)["sensors"]["mmwave"]["values"]
        self.assertIsNone(values["presence"])
        self.assertFalse(values["presence_available"])

    def test_invalid_latest_value_does_not_replace_last_valid_timestamp(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(1), PEER, received_at=100.0, monotonic_at=10.0)
        manager.ingest(
            telemetry(2, respiration=None, heart=None, co2=None),
            PEER,
            received_at=102.0,
            monotonic_at=12.0,
        )
        state = manager.snapshot(now=102.1, monotonic_now=12.1)["sensors"]

        self.assertEqual(state["mmwave"]["status"], "INVALID")
        self.assertEqual(state["mmwave"]["last_update"], 102.0)
        self.assertEqual(state["mmwave"]["last_valid_at"], 100.0)
        self.assertEqual(state["co2"]["status"], "INVALID")
        self.assertEqual(state["pir"]["status"], "LIVE")

    def test_connected_but_invalid_is_degraded_not_offline(self) -> None:
        manager = SensorStateManager()
        manager.ingest(
            telemetry(respiration=None, heart=None, co2=None),
            PEER,
            received_at=100.0,
            monotonic_at=10.0,
        )
        state = manager.snapshot(now=100.0, monotonic_now=10.0)
        self.assertEqual(state["system"], "DEGRADED")

    def test_sensor_specific_ttl_marks_mmwave_stale_before_co2_and_pir(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(), PEER, received_at=100.0, monotonic_at=10.0)
        sensors = manager.snapshot(now=104.0, monotonic_now=14.0)["sensors"]
        self.assertEqual(sensors["mmwave"]["status"], "STALE")
        self.assertTrue(sensors["mmwave"]["stale"])
        self.assertEqual(sensors["co2"]["status"], "LIVE")
        self.assertEqual(sensors["pir"]["status"], "LIVE")

    def test_co2_usable_value_updates_once_every_sixty_seconds(self) -> None:
        manager = SensorStateManager(co2_update_interval_seconds=60.0)
        manager.ingest(telemetry(1, co2=700.0), PEER, received_at=100.0, monotonic_at=10.0)
        manager.ingest(telemetry(2, co2=900.0), PEER, received_at=159.9, monotonic_at=69.9)
        before_due = manager.snapshot(now=159.9, monotonic_now=69.9)["sensors"]["co2"]

        self.assertEqual(before_due["values"]["ppm"], 700.0)
        self.assertEqual(before_due["sequence"], 1)
        self.assertEqual(before_due["last_update"], 100.0)
        self.assertEqual(before_due["last_received_at"], 159.9)
        self.assertEqual(before_due["status"], "LIVE")

        manager.ingest(telemetry(3, co2=950.0), PEER, received_at=160.0, monotonic_at=70.0)
        due = manager.snapshot(now=160.0, monotonic_now=70.0)["sensors"]["co2"]
        self.assertEqual(due["values"]["ppm"], 950.0)
        self.assertEqual(due["sequence"], 3)
        self.assertEqual(due["last_update"], 160.0)

    def test_co2_invalid_status_does_not_replace_last_usable_value(self) -> None:
        manager = SensorStateManager(co2_update_interval_seconds=60.0)
        manager.ingest(telemetry(1, co2=700.0), PEER, received_at=100.0, monotonic_at=10.0)
        manager.ingest(telemetry(2, co2=None), PEER, received_at=110.0, monotonic_at=20.0)
        invalid = manager.snapshot(now=110.0, monotonic_now=20.0)["sensors"]["co2"]

        self.assertEqual(invalid["status"], "INVALID")
        self.assertEqual(invalid["values"]["ppm"], 700.0)
        self.assertEqual(invalid["last_update"], 100.0)
        self.assertEqual(invalid["last_received_at"], 110.0)

        manager.ingest(telemetry(3, co2=750.0), PEER, received_at=111.0, monotonic_at=21.0)
        recovered = manager.snapshot(now=111.0, monotonic_now=21.0)["sensors"]["co2"]
        self.assertEqual(recovered["status"], "LIVE")
        self.assertEqual(recovered["values"]["ppm"], 700.0)

    def test_repeated_co2_publications_are_one_physical_event(self) -> None:
        manager = SensorStateManager(co2_update_interval_seconds=60.0)
        for sequence, received in ((1, 10.0), (2, 11.0), (3, 12.0)):
            manager.ingest(
                telemetry(
                    sequence,
                    co2=700.0,
                    boot_id="boot-a",
                    co2_event_id=42,
                    co2_event_ms=5_000,
                    co2_event_valid=True,
                ),
                PEER,
                received_at=100.0 + received,
                monotonic_at=received,
            )
        values = manager.snapshot(now=112.0, monotonic_now=12.0)["sensors"]["co2"]["values"]
        self.assertEqual(values["measurement_event_count"], 1)
        self.assertEqual(values["measurement_event_id"], 42)
        self.assertEqual(values["measurement_monotonic_ms"], 5_000)

        manager.ingest(
            telemetry(4, co2=705.0, boot_id="boot-a", co2_event_id=43, co2_event_ms=10_000, co2_event_valid=True),
            PEER,
            received_at=115.0,
            monotonic_at=15.0,
        )
        values = manager.snapshot(now=115.0, monotonic_now=15.0)["sensors"]["co2"]["values"]
        self.assertEqual(values["measurement_event_count"], 2)
        self.assertEqual(values["latest_measurement_ppm"], 705.0)
        self.assertEqual(values["ppm"], 700.0)

    def test_boot_id_separates_reused_co2_event_ids(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(1, boot_id="boot-a", co2_event_id=42, co2_event_ms=5_000, co2_event_valid=True), PEER, received_at=100.0, monotonic_at=10.0)
        manager.ingest(telemetry(1, boot_id="boot-b", co2_event_id=42, co2_event_ms=5_000, co2_event_valid=True), PEER, received_at=101.0, monotonic_at=11.0)
        co2 = manager.snapshot(now=101.0, monotonic_now=11.0)["sensors"]["co2"]
        self.assertEqual(co2["boot_id"], "boot-b")
        self.assertEqual(co2["values"]["measurement_event_count"], 2)

    def test_pir_state_and_transition_provenance_are_distinct(self) -> None:
        manager = SensorStateManager()
        cases = ((False, 0, 0), (True, 1, 20_000), (True, 1, 20_000), (False, 2, 30_000))
        for sequence, (motion, event_id, event_ms) in enumerate(cases, start=1):
            manager.ingest(telemetry(sequence, motion=motion, boot_id="boot-a", pir_event_id=event_id, pir_transition_ms=event_ms), PEER, received_at=100.0 + sequence, monotonic_at=10.0 + sequence)
        values = manager.snapshot(now=104.0, monotonic_now=14.0)["sensors"]["pir"]["values"]
        self.assertFalse(values["motion"])
        self.assertEqual(values["event_id"], 2)
        self.assertEqual(values["last_transition_monotonic_ms"], 30_000)

    def test_disconnect_and_stale_are_separate_facts(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(), PEER, received_at=100.0, monotonic_at=10.0)
        manager.mark_peer_disconnected(PEER, disconnected_at=101.0)

        immediate = manager.snapshot(now=101.0, monotonic_now=11.0)["sensors"]["mmwave"]
        self.assertEqual(immediate["status"], "DISCONNECTED")
        self.assertFalse(immediate["connected"])
        self.assertFalse(immediate["stale"])

        later = manager.snapshot(now=104.0, monotonic_now=14.0)["sensors"]["mmwave"]
        self.assertEqual(later["status"], "DISCONNECTED")
        self.assertTrue(later["stale"])

    def test_new_packet_recovers_disconnected_state(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(1), PEER, received_at=100.0, monotonic_at=10.0)
        manager.mark_peer_disconnected(PEER, disconnected_at=101.0)
        new_peer = ("192.168.1.20", 40_001)
        manager.ingest(telemetry(0), new_peer, received_at=102.0, monotonic_at=12.0)
        sensor = manager.snapshot(now=102.0, monotonic_now=12.0)["sensors"]["mmwave"]
        self.assertEqual(sensor["status"], "LIVE")
        self.assertTrue(sensor["connected"])
        self.assertEqual(sensor["peer"], "192.168.1.20:40001")

    def test_thermal_bytes_are_kept_out_of_json_snapshot(self) -> None:
        manager = SensorStateManager()
        frame = thermal(3)
        manager.ingest(frame, PEER, received_at=100.0, monotonic_at=10.0)
        snapshot = manager.snapshot(now=100.0, monotonic_now=10.0)
        encoded = json.dumps(snapshot, allow_nan=False)

        self.assertEqual(snapshot["sensors"]["thermal"]["status"], "LIVE")
        self.assertNotIn("pixel_bytes", encoded)
        self.assertIs(manager.latest_thermal_frame(), frame)

    def test_all_four_live_means_online(self) -> None:
        manager = SensorStateManager()
        manager.ingest(telemetry(), PEER, received_at=100.0, monotonic_at=10.0)
        manager.ingest(thermal(), PEER, received_at=100.0, monotonic_at=10.0)
        state = manager.snapshot(now=100.1, monotonic_now=10.1)
        self.assertEqual(state["system"], "ONLINE")

    def test_snapshot_is_safe_during_concurrent_ingest(self) -> None:
        manager = SensorStateManager()
        errors = []

        def writer() -> None:
            try:
                for sequence in range(100):
                    manager.ingest(
                        telemetry(sequence),
                        PEER,
                        received_at=100.0 + sequence,
                        monotonic_at=10.0 + sequence,
                    )
            except Exception as exc:  # pragma: no cover - assertion capture
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(100):
            manager.snapshot(now=300.0, monotonic_now=300.0)
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_phase_source_keeps_mmwave_live_when_vendor_scalars_are_unavailable(self) -> None:
        manager = SensorStateManager()
        manager.ingest(
            telemetry(
                1,
                respiration=None,
                heart=None,
                boot_id="boot-a",
                breath_phase=0.25,
                ts_monotonic_ms=10_000.0,
                phase_age_ms=80.0,
                mmwave_sequence=101,
                human_detected_raw=True,
            ),
            PEER,
            received_at=100.0,
            monotonic_at=10.0,
        )
        mm = manager.snapshot(now=100.0, monotonic_now=10.0)["sensors"]["mmwave"]
        self.assertTrue(mm["valid"])
        self.assertEqual(mm["status"], "LIVE")
        self.assertNotEqual(mm["error"], "MMWAVE_VALUES_INVALID")

    def test_no_phase_and_no_vendor_scalars_marks_mmwave_invalid(self) -> None:
        manager = SensorStateManager()
        manager.ingest(
            telemetry(1, respiration=None, heart=None),
            PEER,
            received_at=100.0,
            monotonic_at=10.0,
        )
        mm = manager.snapshot(now=100.0, monotonic_now=10.0)["sensors"]["mmwave"]
        self.assertFalse(mm["valid"])
        self.assertEqual(mm["status"], "INVALID")
        self.assertEqual(mm["error"], "MMWAVE_VALUES_INVALID")


if __name__ == "__main__":
    unittest.main()
