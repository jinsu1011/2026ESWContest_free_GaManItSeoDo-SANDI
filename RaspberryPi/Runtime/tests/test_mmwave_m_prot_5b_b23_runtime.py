"""M-PROT-5B team-runtime B23 port tests (offline/replay only).

Goes through OnDeviceAIPipeline. No Raspberry Pi, no MR60, no live hardware.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai.mmwave_b23_bridge import bundle_from_packet, observation_timestamp_s
from ai.mmwave_b23_runtime import B23TeamRuntime, MODEL_ID
from ai.mmwave_prototype.mmwave_m_prot_3_integration_runtime import (
    DEFAULT_MAX_GAP_S,
    MProt3IntegrationRuntime,
)
from ai.mmwave_prototype.mmwave_sw01_interface_checker import Sample, StreamBundle
from ai.mmwave_prototype.mmwave_m_prot_2_b23_runtime import (
    CANONICAL_PARAMETER_SHA256,
    SCALER_CONTENT_SHA256,
    SOURCE_ARTIFACT_SHA256,
    TRACE_SAMPLES,
    verify_artifact,
    verify_scaler,
)
from ai.pipeline import OnDeviceAIPipeline
from ai.runtime import LazyModel, ModelRuntimeUnavailable
from gateway.protocol import PACKET_TELEMETRY_JSON, PacketHeader, TelemetryPayload, decode_telemetry
from paths import ONDEVICE_AI_ROOT
from state.manager import SensorStateManager

PHYSIOLOGY_OK = {"PHYSIOLOGY_ELIGIBLE", "ABSENT", "QUALITY_SUPPRESSED", "RR_UNAVAILABLE"}
ART_REL = Path("models/mmwave/m_prot_b23/candidate_seed_23.pt")
SCALER_REL = Path("models/mmwave/m_prot_b23/scaler_statistics.json")


class FakeThermal:
    def predict(self, pixels):
        return SimpleNamespace(
            class_name="NO_HUMAN",
            probabilities=[0.99, 0.005, 0.005],
            confidence=0.99,
            latency_ms=1.0,
            model_id="thermal-fake",
            model_version="test",
        )


class CountingMN9:
    def __init__(self) -> None:
        self.calls = []

    def predict(self, tensor):
        self.calls.append(tensor)
        raise AssertionError("old M-N9 path must not be called")


def telemetry(
    index: int,
    *,
    rate: float = 10.0,
    presence: bool | None = True,
    session: str = "sess-a",
    phase=None,
    ts_monotonic_ms=None,
    phase_age_ms: float = 3.0,
    seq=None,
    publication_seq=None,
    device_id: str = "mprot5b-fixture",
    valid_respiration: bool = True,
    valid_heart: bool = True,
    boot_id: str = "boot-a",
    breath_rate_raw: float | None = None,
) -> TelemetryPayload:
    dt_ms = 1000.0 / rate
    event_ms = float(index) * dt_ms
    ts = event_ms if ts_monotonic_ms is None else float(ts_monotonic_ms)
    t_s = ts / 1000.0
    if phase is None:
        phase = math.sin(2 * math.pi * 0.25 * t_s)
    nested = index + 1 if seq is None else seq
    outer = index + 1 if publication_seq is None else publication_seq
    return TelemetryPayload(
        header=PacketHeader(1, outer, 8),
        device_id=device_id,
        uptime_ms=int(ts) + 10,
        respiration_rate_bpm=16.0 if valid_respiration else None,
        heart_rate_bpm=62.0 if valid_heart else None,
        co2_ppm=800.0,
        pir_motion=False,
        valid={"respiration": valid_respiration, "heart": valid_heart, "co2": True},
        boot_id=boot_id,
        breath_phase=phase,
        ts_monotonic_ms=ts,
        phase_age_ms=phase_age_ms,
        human_detected_raw=presence,
        session_id=session,
        mmwave_sequence=nested,
        breath_rate_raw=breath_rate_raw,
    )


def feed(
    pipeline: OnDeviceAIPipeline,
    manager: SensorStateManager,
    packet: TelemetryPayload,
    index: int,
    *,
    rate: float = 10.0,
) -> float:
    wall = 10_000.0 + index / rate
    pipeline.observe_telemetry(packet)
    manager.ingest(
        packet,
        ("127.0.0.1", 5000),
        received_at=wall,
        monotonic_at=wall,
    )
    return wall


def ready_count(rate: float = 10.0) -> int:
    return int(round(29.9 * rate)) + 1


class B23PipelinePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = SensorStateManager()
        self.mn9 = CountingMN9()
        self.pipeline = OnDeviceAIPipeline(
            self.manager,
            {"mmwave": self.mn9, "thermal": FakeThermal()},
        )
        self.now = 10_000.0

    def _evaluate(self):
        return self.pipeline.evaluate(
            self.manager.snapshot(now=self.now, monotonic_now=self.now)
        )["ai"]["mmwave"]

    def _feed(self, packet: TelemetryPayload, index: int, *, rate: float = 10.0) -> None:
        self.now = feed(self.pipeline, self.manager, packet, index, rate=rate)

    def _assert_not_mn9(self, result: dict) -> None:
        self.assertEqual(self.mn9.calls, [])
        self.assertNotEqual(result.get("source"), "tflite")
        self.assertNotIn(result.get("state"), {"NORMAL", "RAPID_OR_ABNORMAL", "APNEA", "APNEA-proxy"})
        self.assertFalse(result.get("metadata", {}).get("m_n9_fallback"))
        self.assertFalse(result.get("metadata", {}).get("spectral_fallback"))
        self.assertFalse(result.get("metadata", {}).get("vendor_rr_model_input"))

    def test_a_valid_10hz_through_team_pipeline(self) -> None:
        n = ready_count(10.0)
        result = {"available": False}
        for i in range(n):
            self._feed(telemetry(i, rate=10.0), i, rate=10.0)
            result = self._evaluate()
        self._assert_not_mn9(result)
        self.assertGreaterEqual(self.pipeline._mmwave_b23.buffered_count, 300)
        if result["available"]:
            self.assertEqual(result["source"], "pytorch")
            self.assertEqual(result["model_id"], MODEL_ID)
            self.assertEqual(result["metadata"]["r1_sample_count"], TRACE_SAMPLES)
            self.assertEqual(result["metadata"]["assembled_dim"], 621)
            self.assertEqual(result["metadata"]["artifact_sha256"], SOURCE_ARTIFACT_SHA256)
            self.assertTrue(result["metadata"]["risk_contribution_deferred"])
            self.assertEqual(result["score"], 0.0)
        else:
            self.assertIn(
                result["state"],
                PHYSIOLOGY_OK | {"WINDOW_NOT_READY", "QUALITY_SUPPRESSED", "RR_UNAVAILABLE"},
            )
        ready = self._evaluate()
        self.assertTrue(ready["metadata"].get("window_ready") or ready["state"] == "WINDOW_NOT_READY")
        if ready["metadata"].get("window_ready") and ready["metadata"].get("r1_sample_count"):
            self.assertEqual(ready["metadata"]["r1_sample_count"], 300)
            self.assertEqual(ready["metadata"]["assembled_dim"], 621)

    def test_b_valid_20hz_r1_downsamples(self) -> None:
        n = 600
        self.assertGreater(n, 300)
        for i in range(n):
            self._feed(telemetry(i, rate=20.0), i, rate=20.0)
        result = self._evaluate()
        self._assert_not_mn9(result)
        if result["metadata"].get("window_ready") and result["metadata"].get("r1_sample_count"):
            self.assertEqual(result["metadata"]["r1_sample_count"], 300)

    def test_c_multi_bundle_continued_observations(self) -> None:
        for i in range(ready_count(10.0)):
            self._feed(telemetry(i, rate=10.0), i, rate=10.0)
        first = self._evaluate()
        extra = ready_count(10.0)
        for i in range(extra, extra + 20):
            self._feed(telemetry(i, rate=10.0), i, rate=10.0)
        second = self._evaluate()
        self._assert_not_mn9(second)
        self.assertTrue(first["metadata"].get("window_ready") or first["state"] == "WINDOW_NOT_READY")
        self.assertTrue(second["metadata"].get("window_ready") or second["state"] == "WINDOW_NOT_READY")

    def test_d_warmup_before_30s(self) -> None:
        self._feed(telemetry(0), 0)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "WINDOW_NOT_READY")
        self._assert_not_mn9(result)

    def test_e_repeated_inference_after_ready(self) -> None:
        n = ready_count(10.0)
        for i in range(n):
            self._feed(telemetry(i), i)
        a = self._evaluate()
        self._feed(telemetry(n), n)
        b = self._evaluate()
        self._assert_not_mn9(b)
        if a["metadata"].get("r1_sample_count"):
            self.assertEqual(a["metadata"]["r1_sample_count"], 300)
        if b["metadata"].get("r1_sample_count"):
            self.assertEqual(b["metadata"]["r1_sample_count"], 300)

    def test_missing_phase_fail_closed(self) -> None:
        pkt = telemetry(0)
        object.__setattr__(pkt, "breath_phase", None)
        self._feed(pkt, 0)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])
        self._assert_not_mn9(result)

    def test_timestamp_regression_fail_closed(self) -> None:
        self._feed(telemetry(5, rate=10.0), 5, rate=10.0)
        self._feed(telemetry(1, rate=10.0, seq=7, publication_seq=7), 6, rate=10.0)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self._assert_not_mn9(result)

    def test_nested_seq_jump_is_diagnostic_not_runtime_failure(self) -> None:
        self._feed(telemetry(0, seq=101, publication_seq=500), 0)
        self._feed(telemetry(1, seq=103, publication_seq=501), 1)
        result = self._evaluate()
        self._assert_not_mn9(result)
        self.assertEqual(self.pipeline._mmwave_b23.buffered_count, 2)
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "WINDOW_NOT_READY")
        monitor = result["metadata"]["live_phase_seq_monitor"]
        self.assertEqual(monitor["previous_nested_phase_seq"], 101)
        self.assertEqual(monitor["current_nested_phase_seq"], 103)
        self.assertEqual(monitor["delta"], 2)
        self.assertEqual(monitor["missing_phase_event_count"], 1)
        self.assertEqual(result["metadata"]["live_phase_seq_jump_monitor"], "PREPARED_FOR_M_PROT_5C")

    def test_large_timestamp_gap_does_not_bridge(self) -> None:
        self._feed(telemetry(0, rate=10.0), 0, rate=10.0)
        late = telemetry(1, rate=10.0, seq=2)
        object.__setattr__(late, "ts_monotonic_ms", 3.0 + 2000.0)
        self._feed(late, 1, rate=10.0)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self._assert_not_mn9(result)

    def test_boot_id_change_does_not_bridge(self) -> None:
        for i in range(50):
            self._feed(telemetry(i, boot_id="boot-a"), i)
        for i in range(50, 80):
            self._feed(telemetry(i, boot_id="boot-b"), i)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self.assertLess(self.pipeline._mmwave_b23.buffered_count, 50)
        self._assert_not_mn9(result)

    def test_presence_unavailable(self) -> None:
        for i in range(ready_count(10.0)):
            self._feed(telemetry(i, presence=None), i)
        result = self._evaluate()
        self.assertFalse(result["available"])
        if result["metadata"].get("window_ready"):
            self.assertEqual(result["state"], "PRESENCE_UNAVAILABLE")
        self.assertNotEqual(result["state"], "NORMAL")
        self.assertNotEqual(result["state"], "APNEA")
        self._assert_not_mn9(result)

    def test_presence_false(self) -> None:
        for i in range(ready_count(10.0)):
            self._feed(telemetry(i, presence=False), i)
        result = self._evaluate()
        self.assertFalse(result["available"])
        if result["metadata"].get("window_ready"):
            self.assertEqual(result["state"], "PRESENCE_FALSE")
        self.assertNotEqual(result["state"], "APNEA")
        self.assertNotEqual(result["state"], "APNEA-proxy")
        self._assert_not_mn9(result)

    def test_presence_true_pulse_latches_across_nulls(self) -> None:
        """Live ESP occupancy is a 1-sample True pulse; null must not drop the latch."""
        self._feed(telemetry(0, presence=True), 0)
        n = ready_count(10.0)
        for i in range(1, n):
            self._feed(telemetry(i, presence=None), i)
        result = self._evaluate()
        self._assert_not_mn9(result)
        self.assertNotEqual(result["state"], "PRESENCE_UNAVAILABLE")
        self.assertIs(result["metadata"].get("occupancy_latch"), True)
        self.assertFalse(result["metadata"].get("occupancy_null_coerced_to_false"))
        if result["available"]:
            self.assertEqual(result["source"], "pytorch")
            self.assertEqual(result["metadata"]["r1_sample_count"], TRACE_SAMPLES)
            self.assertEqual(result["metadata"]["assembled_dim"], 621)

    def test_irregular_physical_dt_still_reaches_r1_grid(self) -> None:
        """Seeed millis() ~9.8 Hz must not fail frozen R1 after index-grid mapping."""
        n = 300
        for i in range(n):
            pkt = telemetry(
                i,
                ts_monotonic_ms=float(i) * 102.0,
                presence=True if i == 0 else None,
            )
            self._feed(pkt, i)
        result = self._evaluate()
        self._assert_not_mn9(result)
        self.assertNotEqual(result.get("error"), "R1_TIMESTAMP_GRID_INCONSISTENT")
        self.assertNotEqual(result["state"], "PRESENCE_UNAVAILABLE")
        self.assertEqual(result["metadata"].get("r1_timebase"), "SAMPLE_INDEX_10HZ_AFTER_PHYSICAL_FRESHNESS")
        if result["metadata"].get("window_ready") and result.get("available"):
            self.assertEqual(result["metadata"]["r1_sample_count"], 300)
            self.assertEqual(result["metadata"]["assembled_dim"], 621)
            self.assertEqual(result["source"], "pytorch")

    def test_below_10hz_r1_rejects_without_mn9(self) -> None:
        n = ready_count(8.0)
        for i in range(n):
            self._feed(telemetry(i, rate=8.0), i, rate=8.0)
        result = self._evaluate()
        self._assert_not_mn9(result)
        self.assertFalse(result["available"])
        if result["metadata"].get("window_ready"):
            self.assertIn("SOURCE_RATE_BELOW_TARGET", str(result.get("error") or result.get("state") or ""))

    def test_vendor_rr_is_not_model_input(self) -> None:
        pkt = telemetry(0)
        object.__setattr__(pkt, "breath_phase", None)
        object.__setattr__(pkt, "respiration_rate_bpm", 18.0)
        self._feed(pkt, 0)
        result = self._evaluate()
        self.assertFalse(result["available"])
        self._assert_not_mn9(result)

    def test_old_mn9_predict_never_called_on_default_path(self) -> None:
        n = ready_count(10.0)
        for i in range(n):
            self._feed(telemetry(i), i)
        self._evaluate()
        self.assertEqual(self.mn9.calls, [])
        src = Path(__file__).resolve().parents[1] / "ai" / "pipeline.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn("MR60CanonicalWindowBuilder", text)
        self.assertNotIn("estimate_respiration", text)
        self.assertNotIn('self.models["mmwave"].predict', text)

    def test_legacy_lazy_model_cannot_load_mn9_by_default(self) -> None:
        with self.assertRaises(ModelRuntimeUnavailable) as ctx:
            LazyModel("mmwave").predict([[0.0]])
        self.assertIn("MODEL_RELEASE_BLOCKED", str(ctx.exception))


class B23IdentityTests(unittest.TestCase):
    def test_frozen_identities(self) -> None:
        verify_artifact(ONDEVICE_AI_ROOT)
        verify_scaler(ONDEVICE_AI_ROOT)
        art = ONDEVICE_AI_ROOT / ART_REL
        self.assertEqual(hashlib.sha256(art.read_bytes()).hexdigest(), SOURCE_ARTIFACT_SHA256)
        self.assertEqual(CANONICAL_PARAMETER_SHA256, "6db949c242e25888dd20c3fc8e2305af03448aa229e3ca73e4159216a266d78e")
        self.assertEqual(SCALER_CONTENT_SHA256, "5a2583b5b5064be5480b0cf56f2a2c12d40a4a2d005eb087dc8e12106881159c")

    def test_wrong_artifact_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / ART_REL
            dest.parent.mkdir(parents=True)
            data = bytearray((ONDEVICE_AI_ROOT / ART_REL).read_bytes())
            data[0] ^= 0xFF
            dest.write_bytes(bytes(data))
            shutil.copy2(ONDEVICE_AI_ROOT / SCALER_REL, root / SCALER_REL)
            runtime = B23TeamRuntime(root=root)
            manager = SensorStateManager()
            pipeline = OnDeviceAIPipeline(manager, {"mmwave": CountingMN9()})
            pipeline._mmwave_b23 = runtime
            now = 10_000.0
            for i in range(ready_count(10.0)):
                now = feed(pipeline, manager, telemetry(i), i)
            result = pipeline.evaluate(manager.snapshot(now=now, monotonic_now=now))["ai"]["mmwave"]
            self.assertFalse(result["available"])
            self.assertNotEqual(result["state"], "PHYSIOLOGY_ELIGIBLE")
            self.assertNotEqual(result.get("source"), "tflite")

    def test_wrong_scaler_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ART_REL).parent.mkdir(parents=True)
            shutil.copy2(ONDEVICE_AI_ROOT / ART_REL, root / ART_REL)
            scaler = root / SCALER_REL
            text = (ONDEVICE_AI_ROOT / SCALER_REL).read_text()
            scaler.write_text(text.replace(SCALER_CONTENT_SHA256, "0" * 64))
            runtime = B23TeamRuntime(root=root)
            manager = SensorStateManager()
            pipeline = OnDeviceAIPipeline(manager)
            pipeline._mmwave_b23 = runtime
            now = 10_000.0
            for i in range(ready_count(10.0)):
                now = feed(pipeline, manager, telemetry(i), i)
            result = pipeline.evaluate(manager.snapshot(now=now, monotonic_now=now))["ai"]["mmwave"]
            self.assertFalse(result["available"])
            self.assertNotEqual(result["state"], "PHYSIOLOGY_ELIGIBLE")


class ESPProducerContractTests(unittest.TestCase):
    """Independent ESP mmWave audit contract for the M-PROT-5B B23 path."""

    def test_a_physical_timestamp_used_directly(self) -> None:
        packet = telemetry(0, ts_monotonic_ms=10_000.0, phase_age_ms=80.0, seq=101, publication_seq=500)
        sample = bundle_from_packet(packet).samples[0]
        self.assertEqual(sample.t, 10.0)
        self.assertEqual(observation_timestamp_s(10_000, 80), 10.0)

    def test_b_phase_age_is_not_double_subtracted(self) -> None:
        packet = telemetry(0, ts_monotonic_ms=10_000.0, phase_age_ms=80.0, seq=101, publication_seq=500)
        sample = bundle_from_packet(packet).samples[0]
        self.assertEqual(sample.t, 10.000)
        self.assertNotEqual(sample.t, 9.920)
        reconstructed = (10_000.0 - 80.0) / 1000.0
        self.assertEqual(reconstructed, 9.92)
        self.assertNotEqual(sample.t, reconstructed)

    def test_c_nested_mmwave_seq_is_parsed(self) -> None:
        body = json.dumps(
            {
                "schema": "safenest.telemetry.v1",
                "device_id": "esp32-01",
                "seq": 11,
                "uptime_ms": 3730,
                "resp_rate_bpm": 19.0,
                "heart_rate_bpm": 62.0,
                "co2_ppm": 800.0,
                "pir_motion": False,
                "valid": {"respiration": True, "heart": True, "co2": True},
                "mmwave": {
                    "breath_phase": -0.136825,
                    "phase_age_ms": 12,
                    "ts_monotonic_ms": 3718,
                    "seq": 42,
                    "breath_rate_raw": 7.0,
                },
            }
        ).encode("utf-8")
        packet = decode_telemetry(PacketHeader(PACKET_TELEMETRY_JSON, 11, len(body)), body)
        self.assertEqual(packet.mmwave_sequence, 42)
        self.assertEqual(packet.breath_rate_raw, 7.0)

    def test_d_outer_seq_and_nested_seq_remain_distinct(self) -> None:
        body = json.dumps(
            {
                "schema": "safenest.telemetry.v1",
                "device_id": "esp32-01",
                "seq": 11,
                "uptime_ms": 3730,
                "resp_rate_bpm": 19.0,
                "heart_rate_bpm": 62.0,
                "co2_ppm": 800.0,
                "pir_motion": False,
                "valid": {"respiration": True, "heart": True, "co2": True},
                "mmwave": {
                    "breath_phase": 0.1,
                    "phase_age_ms": 12,
                    "ts_monotonic_ms": 3718,
                    "seq": 42,
                },
            }
        ).encode("utf-8")
        packet = decode_telemetry(PacketHeader(PACKET_TELEMETRY_JSON, 11, len(body)), body)
        self.assertEqual(packet.header.sequence, 11)
        self.assertEqual(packet.mmwave_sequence, 42)
        self.assertNotEqual(packet.header.sequence, packet.mmwave_sequence)
        sample = bundle_from_packet(packet).samples[0]
        self.assertEqual(sample.seq, 42)
        self.assertNotEqual(sample.seq, packet.header.sequence)

        legacy = json.dumps(
            {
                "schema": "safenest.telemetry.v1",
                "device_id": "esp32-01",
                "seq": 9,
                "uptime_ms": 100,
                "resp_rate_bpm": 16.0,
                "heart_rate_bpm": 62.0,
                "co2_ppm": 800.0,
                "pir_motion": False,
                "valid": {"respiration": True, "heart": True, "co2": True},
            }
        ).encode("utf-8")
        no_nested = decode_telemetry(PacketHeader(PACKET_TELEMETRY_JSON, 9, len(legacy)), legacy)
        self.assertEqual(no_nested.header.sequence, 9)
        self.assertIsNone(no_nested.mmwave_sequence)

    def test_e_same_nested_seq_across_publications_is_deduplicated(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(
            telemetry(0, seq=101, publication_seq=500, ts_monotonic_ms=10_000.0, phase=0.1)
        )
        runtime.observe_packet(
            telemetry(0, seq=101, publication_seq=501, ts_monotonic_ms=10_000.0, phase=0.1)
        )
        self.assertEqual(runtime.buffered_count, 1)
        self.assertEqual(runtime.phase_seq_monitor["republish_skip_count"], 1)

    def test_f_nested_seq_advance_creates_one_new_source_sample(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(
            telemetry(0, seq=101, publication_seq=500, ts_monotonic_ms=10_000.0, phase=0.1)
        )
        runtime.observe_packet(
            telemetry(0, seq=101, publication_seq=501, ts_monotonic_ms=10_000.0, phase=0.1)
        )
        runtime.observe_packet(
            telemetry(1, seq=102, publication_seq=502, ts_monotonic_ms=10_100.0, phase=0.2)
        )
        self.assertEqual(runtime.buffered_count, 2)
        self.assertEqual(runtime.phase_seq_monitor["current_nested_phase_seq"], 102)
        self.assertEqual(runtime.phase_seq_monitor["previous_nested_phase_seq"], 101)
        self.assertEqual(runtime.phase_seq_monitor["delta"], 1)
        self.assertEqual(runtime.phase_seq_monitor["missing_phase_event_count"], 0)

    def test_g_boot_id_change_flushes_causal_state(self) -> None:
        runtime = B23TeamRuntime()
        for i in range(40):
            runtime.observe_packet(telemetry(i, boot_id="boot-a"))
        self.assertEqual(runtime.buffered_count, 40)
        runtime.observe_packet(telemetry(40, boot_id="boot-b"))
        self.assertEqual(runtime.buffered_count, 1)
        sessions = {sample.session_id for sample in runtime._runtime.composer._buf}
        self.assertEqual(sessions, {"boot:boot-b"})

    def test_h_presence_null_remains_unavailable(self) -> None:
        manager = SensorStateManager()
        pipeline = OnDeviceAIPipeline(manager, {"mmwave": CountingMN9()})
        n = ready_count(10.0)
        now = 10_000.0
        for i in range(n):
            now = feed(pipeline, manager, telemetry(i, presence=None), i)
        result = pipeline.evaluate(manager.snapshot(now=now, monotonic_now=now))["ai"]["mmwave"]
        self.assertFalse(result["available"])
        if result["metadata"].get("window_ready"):
            self.assertEqual(result["state"], "PRESENCE_UNAVAILABLE")
        self.assertNotEqual(result["state"], "APNEA")
        self.assertNotEqual(result["state"], "NORMAL")

    def test_i_breath_rate_raw_is_not_model_input(self) -> None:
        left = B23TeamRuntime()
        right = B23TeamRuntime()
        for i in range(8):
            left.observe_packet(telemetry(i, breath_rate_raw=7.0))
            right.observe_packet(telemetry(i, breath_rate_raw=99.0))
        left_phases = [sample.phase for sample in left._runtime.composer._buf]
        right_phases = [sample.phase for sample in right._runtime.composer._buf]
        self.assertEqual(left_phases, right_phases)
        packet = telemetry(0, breath_rate_raw=18.5, seq=1)
        sample = bundle_from_packet(packet).samples[0]
        self.assertIsNone(sample.scalar_rr)
        self.assertEqual(packet.breath_rate_raw, 18.5)

    def test_j_old_mn4_ts_age_is_not_used_by_active_b23_path(self) -> None:
        bridge = Path(__file__).resolve().parents[1] / "ai" / "mmwave_b23_bridge.py"
        runtime = Path(__file__).resolve().parents[1] / "ai" / "mmwave_b23_runtime.py"
        canonical = Path(__file__).resolve().parents[1] / "ai" / "mmwave_canonical_runtime.py"
        bridge_text = bridge.read_text(encoding="utf-8")
        runtime_text = runtime.read_text(encoding="utf-8")
        canonical_text = canonical.read_text(encoding="utf-8")
        self.assertNotIn("ts_monotonic_ms) - float(phase_age_ms)", bridge_text)
        self.assertNotIn("float(ts_monotonic_ms) - float(phase_age_ms)", bridge_text)
        self.assertNotIn("ts_ms) - float(age_ms)", runtime_text)
        self.assertIn("update_ms = float(ts_ms) - float(age_ms)", canonical_text)
        self.assertEqual(observation_timestamp_s(10_000, 80), 10.0)


def _mprot3_phase_bundle(
    *,
    seq: int,
    t: float,
    session_id: str = "sess-a",
    reset_flag: bool = False,
) -> StreamBundle:
    return StreamBundle(
        device_identity="safenest-mmwave",
        interface_identity="safenest.telemetry.v1",
        configuration_identity="mr60_tcp_v1_phase_waveform",
        observation_kind="near_raw_phase",
        samples=[
            Sample(
                t=t,
                phase=0.1,
                seq=seq,
                health_ok=True,
                session_id=session_id,
                reset_flag=reset_flag,
            )
        ],
    )


class MProt6SeqHoleContinuityTests(unittest.TestCase):
    """Short nested-seq holes must not flush the 30s causal buffer."""

    def test_seq_plus_one_keeps_continuity(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(telemetry(0, seq=100, publication_seq=1))
        runtime.observe_packet(telemetry(1, seq=101, publication_seq=2))
        self.assertEqual(runtime.buffered_count, 2)
        self.assertEqual(runtime.phase_seq_monitor["missing_phase_event_count"], 0)

    def test_short_seq_hole_preserves_buffer_and_counts_missing(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(telemetry(0, seq=100, publication_seq=1))
        runtime.observe_packet(telemetry(1, seq=102, publication_seq=2))
        self.assertEqual(runtime.buffered_count, 2)
        self.assertEqual(runtime.phase_seq_monitor["missing_phase_event_count"], 1)
        self.assertEqual(runtime.phase_seq_monitor["delta"], 2)

    def test_seq_hole_with_large_timestamp_gap_flushes(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(telemetry(0, seq=100, publication_seq=1, ts_monotonic_ms=0.0))
        late = telemetry(1, seq=102, publication_seq=2)
        object.__setattr__(late, "ts_monotonic_ms", 2000.0)
        runtime.observe_packet(late)
        self.assertEqual(runtime.buffered_count, 1)

    def test_boot_id_change_flushes(self) -> None:
        runtime = B23TeamRuntime()
        for i in range(20):
            runtime.observe_packet(telemetry(i, boot_id="boot-a"))
        self.assertEqual(runtime.buffered_count, 20)
        runtime.observe_packet(telemetry(20, boot_id="boot-b"))
        self.assertEqual(runtime.buffered_count, 1)

    def test_timestamp_regression_flushes(self) -> None:
        runtime = B23TeamRuntime()
        runtime.observe_packet(telemetry(5, seq=100, publication_seq=1, rate=10.0))
        self.assertEqual(runtime.buffered_count, 1)
        runtime.observe_packet(telemetry(1, seq=101, publication_seq=2, rate=10.0))
        self.assertEqual(runtime.buffered_count, 1)

    def test_short_seq_holes_still_reach_30s_window(self) -> None:
        runtime = B23TeamRuntime()
        n = ready_count(10.0)
        seq = 100
        last = None
        for i in range(n):
            last = telemetry(i, seq=seq, publication_seq=i + 1)
            runtime.observe_packet(last)
            seq += 2 if i in {10, 50, 120} else 1
        self.assertEqual(runtime.buffered_count, n)
        self.assertGreaterEqual(runtime.phase_seq_monitor["missing_phase_event_count"], 3)
        result = runtime.evaluate(sensor_view(last), 20_000.0)
        self.assertTrue(result.metadata.get("window_ready"))

    def test_mprot3_seq_hole_with_small_index_gap_does_not_flush(self) -> None:
        runtime = MProt3IntegrationRuntime()
        runtime.ingest_bundle(_mprot3_phase_bundle(seq=100, t=0.0))
        self.assertEqual(runtime.composer.buffered_count, 1)
        runtime.ingest_bundle(_mprot3_phase_bundle(seq=102, t=0.1))
        self.assertEqual(runtime.composer.buffered_count, 2)
        self.assertLessEqual(0.1, DEFAULT_MAX_GAP_S)

    def test_mprot3_seq_hole_with_large_index_gap_flushes(self) -> None:
        runtime = MProt3IntegrationRuntime()
        runtime.ingest_bundle(_mprot3_phase_bundle(seq=100, t=0.0))
        runtime.ingest_bundle(_mprot3_phase_bundle(seq=102, t=DEFAULT_MAX_GAP_S + 0.1))
        self.assertEqual(runtime.composer.buffered_count, 1)


def sensor_view(packet: TelemetryPayload) -> dict:
    return {
        "status": "LIVE",
        "device_id": packet.device_id,
        "boot_id": packet.boot_id,
        "sequence": packet.header.sequence,
        "values": {
            "breath_phase": packet.breath_phase,
            "ts_monotonic_ms": packet.ts_monotonic_ms,
            "phase_age_ms": packet.phase_age_ms,
            "mmwave_sequence": packet.mmwave_sequence,
            "presence": packet.human_detected_raw,
            "presence_available": isinstance(packet.human_detected_raw, bool),
            "human_detected_raw": packet.human_detected_raw,
            "respiration_valid": packet.valid.get("respiration"),
            "session_id": packet.session_id,
        },
    }


class FailClosedWindowTests(unittest.TestCase):
    """Invalid/stale source must drop a previously ready B23 window."""

    def _ready(self, *, boot_id: str = "boot-a") -> tuple[B23TeamRuntime, int]:
        runtime = B23TeamRuntime()
        n = ready_count(10.0)
        for i in range(n):
            runtime.observe_packet(telemetry(i, boot_id=boot_id))
        self.assertGreaterEqual(runtime.buffered_count, 300)
        return runtime, n

    def _assert_no_old_inference(self, runtime: B23TeamRuntime, packet: TelemetryPayload) -> None:
        result = runtime.evaluate(sensor_view(packet), 20_000.0)
        self.assertEqual(runtime.buffered_count, 0)
        self.assertFalse(result.available)
        self.assertEqual(result.state, "WINDOW_NOT_READY")
        self.assertNotEqual(result.state, "PHYSIOLOGY_ELIGIBLE")
        self.assertFalse(result.metadata.get("window_ready"))

    def test_a_ready_window_stale_phase_drops_old_inference(self) -> None:
        runtime, n = self._ready()
        stale = telemetry(n, phase_age_ms=5_000.0)
        runtime.observe_packet(stale)
        self._assert_no_old_inference(runtime, stale)
        self.assertEqual(runtime.evaluate(sensor_view(stale), 20_000.0).error, "PHASE_STALE")

    def test_b_ready_window_null_phase_is_skipped_not_wiped(self) -> None:
        """null breath_phase 는 건너뛴 tick 이지 세션 단절이 아닙니다.

        이 테스트는 원래 "null 이 ready 창을 없앤다"를 요구했습니다. 그 계약은
        현장에서 mmWave 를 영구히 죽였습니다. MR60 의 phase 리포트는 본래
        간헐적이라 10 Hz 발행에서는 상당수 패킷의 phase 가 null 인데, null 하나가
        299개까지 모은 창을 0 으로 되돌리면 창은 절대 300 에 도달하지 못합니다.
        실제로 ESP 가 phase 를 2,785개 만들어 보낸 동안 Pi 창은 0 이었습니다.
        """

        runtime, n = self._ready()
        before = runtime.buffered_count
        missing = telemetry(n)
        object.__setattr__(missing, "breath_phase", None)
        runtime.observe_packet(missing)

        # 버퍼가 그대로여야 하고
        self.assertEqual(runtime.buffered_count, before)
        # PHASE_MISSING 으로 오염되어 추론이 막히면 안 됩니다.
        result = runtime.evaluate(sensor_view(missing), 20_000.0)
        self.assertNotEqual(result.error, "PHASE_MISSING")

    def test_b2_null_phase_then_valid_continues_same_window(self) -> None:
        """null 이후의 유효 샘플은 새 창이 아니라 같은 창에 이어 쌓입니다."""

        runtime = B23TeamRuntime()
        for i in range(50):
            runtime.observe_packet(telemetry(i, boot_id="boot-x"))
        self.assertEqual(runtime.buffered_count, 50)

        missing = telemetry(50, boot_id="boot-x")
        object.__setattr__(missing, "breath_phase", None)
        runtime.observe_packet(missing)
        self.assertEqual(runtime.buffered_count, 50)  # 1 로 리셋되지 않음

        for i in range(51, 61):
            runtime.observe_packet(telemetry(i, boot_id="boot-x"))
        self.assertEqual(runtime.buffered_count, 60)  # 51 이 아니라 60

    def test_b3_mixed_valid_null_stream_reaches_ready(self) -> None:
        """유효/null 이 섞인 스트림도 유효 샘플만으로 300 에 도달합니다."""

        runtime = B23TeamRuntime()
        target = ready_count(10.0)
        valid = 0
        index = 0
        while valid < target:
            packet = telemetry(index, boot_id="boot-y")
            if index % 3 == 1:  # 3틱마다 하나는 null
                object.__setattr__(packet, "breath_phase", None)
            else:
                valid += 1
            runtime.observe_packet(packet)
            index += 1
        self.assertGreaterEqual(runtime.buffered_count, 300)

    def test_b4_null_phase_does_not_use_vendor_bpm_as_sample(self) -> None:
        """벤더 호흡수/심박수는 표시용입니다. 결측 phase 를 대신 채우지 않습니다."""

        runtime = B23TeamRuntime()
        for i in range(20):
            runtime.observe_packet(telemetry(i, boot_id="boot-z"))
        before = runtime.buffered_count

        missing = telemetry(20, boot_id="boot-z")
        object.__setattr__(missing, "breath_phase", None)
        object.__setattr__(missing, "breath_rate_raw", 16.0)
        object.__setattr__(missing, "respiration_rate_bpm", 16.0)
        runtime.observe_packet(missing)

        # BPM 이 있어도 샘플은 늘지 않습니다.
        self.assertEqual(runtime.buffered_count, before)

    def test_c_ready_window_missing_nested_seq_drops_old_inference(self) -> None:
        runtime, n = self._ready()
        missing = telemetry(n)
        object.__setattr__(missing, "mmwave_sequence", None)
        runtime.observe_packet(missing)
        self._assert_no_old_inference(runtime, missing)
        self.assertEqual(
            runtime.evaluate(sensor_view(missing), 20_000.0).error,
            "PHASE_SEQUENCE_MISSING",
        )

    def test_d_new_boot_invalid_phase_resets_immediately(self) -> None:
        runtime, n = self._ready(boot_id="boot-a")
        invalid = telemetry(n, boot_id="boot-b")
        object.__setattr__(invalid, "breath_phase", None)
        runtime.observe_packet(invalid)
        self._assert_no_old_inference(runtime, invalid)
        self.assertEqual(runtime.evaluate(sensor_view(invalid), 20_000.0).error, "BOOT_BOUNDARY")
        still_invalid = telemetry(n + 1, boot_id="boot-b")
        object.__setattr__(still_invalid, "breath_phase", None)
        runtime.observe_packet(still_invalid)
        self.assertEqual(runtime.buffered_count, 0)

    def test_e_valid_samples_after_failure_start_fresh_window(self) -> None:
        runtime, n = self._ready()
        before = runtime.buffered_count
        stale = telemetry(n, phase_age_ms=5_000.0)
        runtime.observe_packet(stale)
        self.assertEqual(runtime.buffered_count, 0)
        for i in range(n, n + 12):
            runtime.observe_packet(telemetry(i))
        self.assertEqual(runtime.buffered_count, 12)
        self.assertLess(runtime.buffered_count, before)
        result = runtime.evaluate(sensor_view(telemetry(n + 11)), 20_000.0)
        self.assertFalse(result.available)
        self.assertEqual(result.state, "WINDOW_NOT_READY")

    def test_vendor_rr_validity_does_not_gate_b23_phase_admission(self) -> None:
        runtime = B23TeamRuntime()
        packet = telemetry(0, valid_respiration=False)
        runtime.observe_packet(packet)
        self.assertEqual(runtime.buffered_count, 1)
        sample = bundle_from_packet(packet).samples[0]
        self.assertTrue(sample.health_ok)
        self.assertIsNone(sample.scalar_rr)
        self.assertEqual(sample.seq, packet.mmwave_sequence)


class EndToEndPhaseValidityTests(unittest.TestCase):
    """State-manager validity must not require vendor RR/heart for B23."""

    def test_vendor_scalars_unavailable_does_not_yield_sensor_invalid(self) -> None:
        manager = SensorStateManager()
        pipeline = OnDeviceAIPipeline(manager, {"mmwave": CountingMN9(), "thermal": FakeThermal()})
        n = ready_count(10.0)
        result = {"available": False, "error": None, "state": None}
        for i in range(n):
            packet = telemetry(i, valid_respiration=False, valid_heart=False, presence=True)
            self.assertIsNone(packet.respiration_rate_bpm)
            self.assertIsNone(packet.heart_rate_bpm)
            self.assertFalse(packet.valid["respiration"])
            self.assertFalse(packet.valid["heart"])
            now = feed(pipeline, manager, packet, i)
            snap = manager.snapshot(now=now, monotonic_now=now)
            mm = snap["sensors"]["mmwave"]
            self.assertNotEqual(mm["status"], "INVALID")
            self.assertTrue(mm["valid"])
            self.assertEqual(mm["status"], "LIVE")
            result = pipeline.evaluate(snap)["ai"]["mmwave"]
        self.assertNotEqual(result.get("error"), "SENSOR_INVALID")
        self.assertNotEqual(result.get("state"), "SENSOR_INVALID")
        self.assertNotIn("SENSOR_INVALID", str(result.get("error") or ""))
        self.assertEqual(pipeline.models["mmwave"].calls, [])
        self.assertGreaterEqual(pipeline._mmwave_b23.buffered_count, 300)
        self.assertIn(
            result["state"],
            PHYSIOLOGY_OK | {"WINDOW_NOT_READY", "QUALITY_SUPPRESSED", "RR_UNAVAILABLE", "PRESENCE_UNAVAILABLE"},
        )
        if result["available"]:
            self.assertEqual(result["source"], "pytorch")

    def test_no_phase_and_no_vendor_scalars_is_fail_closed(self) -> None:
        manager = SensorStateManager()
        pipeline = OnDeviceAIPipeline(manager, {"mmwave": CountingMN9()})
        packet = telemetry(0, valid_respiration=False, valid_heart=False, presence=True)
        object.__setattr__(packet, "breath_phase", None)
        object.__setattr__(packet, "ts_monotonic_ms", None)
        object.__setattr__(packet, "phase_age_ms", None)
        object.__setattr__(packet, "mmwave_sequence", None)
        now = feed(pipeline, manager, packet, 0)
        snap = manager.snapshot(now=now, monotonic_now=now)
        mm = snap["sensors"]["mmwave"]
        self.assertFalse(mm["valid"])
        self.assertEqual(mm["status"], "INVALID")
        result = pipeline.evaluate(snap)["ai"]["mmwave"]
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "SENSOR_INVALID")
        self.assertEqual(pipeline.models["mmwave"].calls, [])


if __name__ == "__main__":
    unittest.main()
