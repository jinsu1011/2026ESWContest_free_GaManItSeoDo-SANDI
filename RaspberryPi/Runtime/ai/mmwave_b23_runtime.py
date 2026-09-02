"""Active SafeNest mmWave path: SW-01 → M-PROT-3 → R1/R2 → frozen B23.

This is the default team runtime. The old M-N4/M-N9 240-sample path is
legacy/non-active and must not be called from here.

Active path:
  ESP nested telemetry
  → parse breath_phase
  → parse physical ts (ts_monotonic_ms / 1000, no age subtraction)
  → parse nested mmwave.seq
  → boot/presence semantics
  → SW-01 / M-PROT-3 Sample
  → R1 300 @ 10 Hz
  → R2 621
  → B23

No M-N4 ts-age reconstruction. No M-N9 fallback.
"""

from __future__ import annotations

import math
import threading
from typing import Mapping

from ai.mmwave_b23_bridge import (
    json_safe_receipt,
    mprot3_session_id,
    phase_age_is_fresh,
    physical_timestamp_s,
    presence_from_sensor,
)
from ai.mmwave_prototype.mmwave_m_prot_2_b23_runtime import (
    CANDIDATE_ID,
    PANEL_ID,
    PRIMARY_REPRESENTATION,
    SAMPLE_RATE_HZ,
    SCALER_CONTENT_SHA256,
    SOURCE_ARTIFACT_SHA256,
)
from ai.mmwave_prototype.mmwave_m_prot_3_integration_runtime import (
    DEFAULT_MAX_GAP_S,
    MProt3FailClosed,
    MProt3IntegrationRuntime,
)
from ai.mmwave_prototype.mmwave_sw01_interface_checker import Sample, StreamBundle
from ai.result import AIResult
from gateway.protocol import TelemetryPayload
from paths import ONDEVICE_AI_ROOT

SOURCE = "pytorch"
MODEL_ID = CANDIDATE_ID
MODEL_VERSION = "m_prot_b23_pytorch_float32_v1"
# Frozen R1 requires a regular integer-Hz grid. Live MR60 millis() is ~9.8 Hz
# and jittered, so Sample.t handed to the composer is admitted-sample index
# at 10 Hz. Physical ts_monotonic_ms stays freshness + gap/boot only.
R1_INDEX_DT_S = 1.0 / float(SAMPLE_RATE_HZ)
R1_TIMEBASE = "SAMPLE_INDEX_10HZ_AFTER_PHYSICAL_FRESHNESS"


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


class B23TeamRuntime:
    """Thread-safe wrapper around the frozen M-PROT-3 composer."""

    def __init__(self, root=None) -> None:
        self._lock = threading.RLock()
        self._runtime = MProt3IntegrationRuntime(root=root or ONDEVICE_AI_ROOT)
        self._last_ingest_error: str | None = None
        self._wire_observed = False
        self._last_nested_seq: int | None = None
        self._last_boot_id: str | None = None
        self._phase_seq_prev: int | None = None
        self._phase_seq_curr: int | None = None
        self._phase_seq_delta: int | None = None
        self._missing_phase_event_count = 0
        self._republish_skip_count = 0
        # 결측/비유한 phase 로 "건너뛴" tick 수. 창을 지운 횟수가 아닙니다.
        self._skipped_phase_tick_count = 0
        # 유효 샘플 사이의 물리적 간격이 DEFAULT_MAX_GAP_S 를 넘은 횟수.
        # 크면 창의 시간축이 실시간보다 압축되어 있다는 뜻입니다.
        self._bridged_gap_count = 0
        # 마지막으로 샘플을 admit 한 뒤 null 로 건너뛴 tick 이 있었는가.
        # 있으면 다음 유효 샘플까지의 시간 공백은 "설명된 공백"입니다.
        self._skipped_since_admit = 0
        self._r1_index_n = 0
        self._last_physical_t: float | None = None
        # Last explicit ESP occupancy bool. null does not clear this.
        self._latched_presence: bool | None = None

    @property
    def wire_observed(self) -> bool:
        return self._wire_observed

    @property
    def buffered_count(self) -> int:
        with self._lock:
            return self._runtime.composer.buffered_count

    @property
    def phase_seq_monitor(self) -> dict[str, int | None]:
        with self._lock:
            return self._monitor_locked()

    def observe_packet(self, packet: TelemetryPayload) -> None:
        with self._lock:
            self._wire_observed = True
            self._latch_occupancy(packet.human_detected_raw)
            self._admit(
                phase=packet.breath_phase,
                ts_monotonic_ms=packet.ts_monotonic_ms,
                phase_age_ms=packet.phase_age_ms,
                nested_seq=_int_or_none(packet.mmwave_sequence),
                boot_id=packet.boot_id if isinstance(packet.boot_id, str) else None,
                packet_session_id=packet.session_id,
                device_id=packet.device_id,
            )

    def observe_sensor(self, sensor: Mapping[str, object]) -> None:
        with self._lock:
            self._admit_from_sensor(sensor)

    def evaluate(self, sensor: Mapping[str, object], now: float) -> AIResult:
        with self._lock:
            if not self._wire_observed:
                self._admit_from_sensor(sensor)
            extras = self._metadata_extras()
            if self._runtime.composer.buffered_count == 0:
                reason = self._last_ingest_error or "WINDOW_NOT_READY"
                extras["fail_closed_code"] = reason
                return _unavailable(now, "WINDOW_NOT_READY", reason, extras)
            if self._last_ingest_error is not None:
                extras["fail_closed_code"] = self._last_ingest_error
                return _unavailable(
                    now,
                    self._last_ingest_error,
                    self._last_ingest_error,
                    extras,
                )
            presence_available, presence_true = self._presence_for_infer(sensor)
            extras["occupancy_latch"] = self._latched_presence
            extras["occupancy_null_coerced_to_false"] = False
            extras["r1_timebase"] = R1_TIMEBASE
            receipt = self._runtime.try_infer(
                presence_available=presence_available and presence_true,
                lineage_class="FIXTURE_NON_CAMPAIGN",
            )
            return map_receipt_to_ai_result(
                receipt,
                now=now,
                presence_available=presence_available,
                presence_true=presence_true,
                extras=extras,
            )

    def _admit_from_sensor(self, sensor: Mapping[str, object]) -> None:
        values = sensor.get("values") if isinstance(sensor.get("values"), Mapping) else {}
        if not isinstance(values, Mapping):
            values = {}
        boot = sensor.get("boot_id")
        raw = values.get("human_detected_raw")
        if not isinstance(raw, bool):
            raw = values.get("presence")
        self._latch_occupancy(raw)
        self._admit(
            phase=values.get("breath_phase"),
            ts_monotonic_ms=values.get("ts_monotonic_ms"),
            phase_age_ms=values.get("phase_age_ms"),
            nested_seq=_int_or_none(values.get("mmwave_sequence")),
            boot_id=boot if isinstance(boot, str) else None,
            packet_session_id=values.get("session_id") if isinstance(values.get("session_id"), str) else None,
            device_id=sensor.get("device_id") if isinstance(sensor.get("device_id"), str) else None,
        )

    def _invalidate_source(self) -> None:
        """Drop any previously ready causal window. Runtime stays alive."""

        self._runtime.reset()
        self._r1_index_n = 0
        self._last_physical_t = None

    def _latch_occupancy(self, raw: object) -> None:
        """Keep last explicit bool. null/omitted does not become false."""

        if isinstance(raw, bool):
            self._latched_presence = raw

    def _presence_for_infer(self, sensor: Mapping[str, object]) -> tuple[bool, bool]:
        snapshot_available, snapshot_true = presence_from_sensor(sensor)
        if snapshot_available:
            self._latched_presence = snapshot_true
        if self._latched_presence is None:
            return False, False
        return True, bool(self._latched_presence)

    def _admit(
        self,
        *,
        phase: object,
        ts_monotonic_ms: object,
        phase_age_ms: object,
        nested_seq: int | None,
        boot_id: str | None,
        packet_session_id: str | None,
        device_id: str | None,
    ) -> None:
        boot_changed = (
            boot_id is not None
            and self._last_boot_id is not None
            and boot_id != self._last_boot_id
        )
        if boot_changed:
            # Harder than ordinary stale: reset BEFORE inspecting the first
            # packet of the new boot. Invalid new-boot phase must not keep
            # the previous boot's ready window.
            self._invalidate_source()
            self._last_nested_seq = None
            self._missing_phase_event_count = 0
            self._republish_skip_count = 0
            self._skipped_phase_tick_count = 0
            self._bridged_gap_count = 0
            self._skipped_since_admit = 0
            self._phase_seq_prev = None
            self._phase_seq_curr = None
            self._phase_seq_delta = None
            self._latched_presence = None
            self._last_ingest_error = "BOOT_BOUNDARY"
        if boot_id is not None:
            self._last_boot_id = boot_id

        if not _finite(phase):
            # null / 결측 / NaN / inf 는 "건너뛴 tick" 이지 세션 단절이 아닙니다.
            #
            # 이전 구현은 여기서 _invalidate_source() 를 불러 299개까지 모은 창도
            # 통째로 버렸습니다. MR60 의 phase 리포트는 원래 간헐적이라, 10 Hz 로
            # 발행하면 상당수 패킷의 phase 가 null 입니다. 그래서 창이 실제로는
            # 절대 300 에 도달하지 못했습니다(현장에서 ESP 가 phase 를 2,785개
            # 만들었는데 Pi 창은 0 에서 안 올라가는 것으로 관측됨).
            #
            # 이제는 아무것도 건드리지 않고 그냥 돌아갑니다. 버퍼, write index,
            # 마지막 admit seq, 마지막 physical timestamp 모두 그대로 둡니다.
            # 다음에 오는 유한한 phase 가 같은 창에 이어서 쌓입니다.
            #
            # 0-fill / hold-last / 보간은 하지 않습니다. null 은 슬롯을 차지하지
            # 않고, 오직 실제로 도착한 유한 값만 300 에 계상됩니다.
            if boot_changed:
                self._last_ingest_error = "BOOT_BOUNDARY"
                return
            self._skipped_phase_tick_count += 1
            self._skipped_since_admit += 1
            # 버퍼가 비어 있으면 창은 여전히 not ready 입니다. 그건 evaluate()
            # 가 buffered_count == 0 으로 판단하므로 여기서 오류를 남길 필요가
            # 없습니다. 비어있지 않은 창을 PHASE_MISSING 으로 오염시키면
            # 추론이 막히는데, 그것이 바로 고치려는 증상입니다.
            return
        if not phase_age_is_fresh(phase_age_ms):
            if not boot_changed:
                self._invalidate_source()
            self._last_ingest_error = "BOOT_BOUNDARY" if boot_changed else "PHASE_STALE"
            return
        t_physical = physical_timestamp_s(ts_monotonic_ms)
        if t_physical is None:
            if not boot_changed:
                self._invalidate_source()
            self._last_ingest_error = "BOOT_BOUNDARY" if boot_changed else "TIMESTAMP_INVALID"
            return
        if nested_seq is None:
            if not boot_changed:
                self._invalidate_source()
            self._last_ingest_error = "BOOT_BOUNDARY" if boot_changed else "PHASE_SEQUENCE_MISSING"
            return

        if (
            not boot_changed
            and self._last_nested_seq is not None
            and nested_seq == self._last_nested_seq
        ):
            self._republish_skip_count += 1
            self._note_seq(nested_seq)
            return

        if (
            not boot_changed
            and self._last_physical_t is not None
        ):
            physical_dt = float(t_physical) - float(self._last_physical_t)
            if physical_dt <= 0.0:
                # 소스 시계가 뒤로 갔습니다. 이건 공백이 아니라 세션 단절이므로
                # 그대로 창을 버립니다.
                self._invalidate_source()
                self._last_nested_seq = None
            elif physical_dt > float(DEFAULT_MAX_GAP_S):
                # 공백의 원인을 구분합니다.
                #
                #  (a) 그 사이 null phase tick 이 실제로 도착했다면, 노드는 살아서
                #      계속 발행 중이었고 레이더만 phase 를 못 낸 것입니다. 이건
                #      "설명된 공백"이라 창을 유지합니다. 그래야 null 을 건너뛰기로
                #      한 결정이 의미를 갖습니다(널 6틱=600 ms 면 리셋되던 문제).
                #
                #  (b) 아무 패킷도 없이 시간만 흘렀다면 소스가 실제로 멈춘 것입니다.
                #      M-PROT-6 계약대로 창을 버립니다. 2초 정지를 10 Hz 격자에
                #      이어붙이면 주파수가 왜곡되어 호흡수 추정이 틀어집니다.
                if self._skipped_since_admit > 0:
                    self._bridged_gap_count += 1
                else:
                    self._invalidate_source()
                    self._last_nested_seq = None

        if self._last_nested_seq is not None:
            delta = int(nested_seq) - int(self._last_nested_seq)
            if delta > 1:
                self._missing_phase_event_count += delta - 1

        t_index = float(self._r1_index_n) * R1_INDEX_DT_S
        bundle = StreamBundle(
            device_identity=device_id or "safenest-mmwave",
            interface_identity="safenest.telemetry.v1",
            configuration_identity="mr60_tcp_v1_phase_waveform",
            observation_kind="near_raw_phase",
            samples=[
                Sample(
                    t=t_index,
                    phase=float(phase),
                    seq=int(nested_seq),
                    health_ok=True,
                    session_id=mprot3_session_id(
                        boot_id=boot_id,
                        packet_session_id=packet_session_id,
                    ),
                    reset_flag=boot_changed,
                    scalar_rr=None,
                )
            ],
        )
        try:
            self._runtime.ingest_bundle(bundle)
            self._last_ingest_error = None
            self._last_nested_seq = nested_seq
            self._last_physical_t = float(t_physical)
            self._skipped_since_admit = 0
            self._r1_index_n += 1
            self._note_seq(nested_seq)
        except MProt3FailClosed as exc:
            self._last_ingest_error = exc.code
            self._last_nested_seq = None
            self._last_physical_t = None

    def _note_seq(self, nested_seq: int) -> None:
        prev = self._phase_seq_curr
        self._phase_seq_prev = prev
        self._phase_seq_curr = int(nested_seq)
        if prev is None:
            self._phase_seq_delta = None
        else:
            self._phase_seq_delta = int(nested_seq) - int(prev)

    def _monitor_locked(self) -> dict[str, int | None]:
        return {
            "previous_nested_phase_seq": self._phase_seq_prev,
            "current_nested_phase_seq": self._phase_seq_curr,
            "delta": self._phase_seq_delta,
            "missing_phase_event_count": self._missing_phase_event_count,
            "republish_skip_count": self._republish_skip_count,
            "skipped_phase_tick_count": self._skipped_phase_tick_count,
            "bridged_gap_count": self._bridged_gap_count,
        }

    def _metadata_extras(self) -> dict:
        return {
            "live_phase_seq_monitor": self._monitor_locked(),
            "live_phase_seq_jump_monitor": "PREPARED_FOR_M_PROT_5C",
            "physical_timestamp_semantic": "ts_monotonic_ms_is_physical_observation_timestamp",
            "phase_age_usage": "FRESHNESS_ONLY",
            "r1_timebase": R1_TIMEBASE,
            "occupancy_latch": self._latched_presence,
            "occupancy_null_coerced_to_false": False,
            "b23_source_sequence": "NESTED_MMWAVE_SEQ",
            "outer_sequence_role": "TRANSPORT_PUBLICATION_ONLY",
            "vendor_rr_model_input": False,
            "vendor_rr_validity_gates_b23": False,
            "m_n9_fallback": False,
            "double_age_subtraction": "NOT_PRESENT_IN_NEW_B23_PATH",
            "mprot3_session_mapping": "boot:{boot_id}",
        }


def map_receipt_to_ai_result(
    receipt: object,
    *,
    now: float,
    presence_available: bool,
    presence_true: bool,
    extras: dict | None = None,
) -> AIResult:
    payload = json_safe_receipt(receipt)
    proto = payload.get("prototype_receipt") if isinstance(payload.get("prototype_receipt"), dict) else {}
    metadata = {
        "runtime": "M_PROT_B23",
        "panel_id": PANEL_ID,
        "candidate_id": CANDIDATE_ID,
        "representation": PRIMARY_REPRESENTATION,
        "artifact_sha256": payload.get("artifact_sha256") or SOURCE_ARTIFACT_SHA256,
        "scaler_content_sha256": payload.get("scaler_content_sha256") or SCALER_CONTENT_SHA256,
        "window_ready": bool(payload.get("window_ready")),
        "r1_sample_count": payload.get("r1_sample_count"),
        "assembled_dim": payload.get("assembled_dim"),
        "fail_closed_code": payload.get("fail_closed_code"),
        "b23_status": payload.get("status"),
        "breathing_probability": proto.get("breathing_probability"),
        "breathing_decision": proto.get("breathing_decision"),
        "rr_bpm": proto.get("rr_bpm"),
        "rr_status": proto.get("rr_status"),
        "quality_probability": proto.get("quality_probability"),
        "quality_decision": proto.get("quality_decision"),
        "prototype_semantics": True,
        "PROTOTYPE_INTEGRATION_ONLY": True,
        "NOT_FINAL_SELECTED_MODEL": True,
        "risk_contribution_deferred": True,
        "apnea_emitted": False,
        "m_n9_fallback": False,
        "spectral_fallback": False,
        "vendor_rr_model_input": False,
        "PI_TORCH_NOT_LIVE_VERIFIED": True,
        "LIVE_HARDWARE_EXECUTED": False,
        "receipt": payload,
    }
    if extras:
        metadata.update(extras)
    status = str(payload.get("status") or "UNAVAILABLE")
    fail = payload.get("fail_closed_code")

    if status == "WINDOW_NOT_READY" or fail == "WINDOW_NOT_READY":
        return _unavailable(now, "WINDOW_NOT_READY", "WINDOW_NOT_READY", metadata)
    if payload.get("window_ready") and presence_available and not presence_true:
        metadata["fail_closed_code"] = "PRESENCE_FALSE"
        return _unavailable(now, "PRESENCE_FALSE", "PRESENCE_FALSE", metadata)
    if status == "UNAVAILABLE" and fail == "PRESENCE_UNAVAILABLE":
        return _unavailable(now, "PRESENCE_UNAVAILABLE", "PRESENCE_UNAVAILABLE", metadata)
    if status == "QUALITY_SUPPRESSED" or fail == "QUALITY_FAIL":
        return _unavailable(now, "QUALITY_SUPPRESSED", fail or "QUALITY_FAIL", metadata)
    if status == "RR_UNAVAILABLE" or fail == "UNAVAILABLE_INVALID_DECODE":
        return _unavailable(now, "RR_UNAVAILABLE", fail or "UNAVAILABLE_INVALID_DECODE", metadata)
    if status == "PHYSIOLOGY_ELIGIBLE":
        confidence = proto.get("breathing_probability")
        return AIResult(
            sensor_id="mmwave",
            timestamp=now,
            available=True,
            source=SOURCE,
            state="PHYSIOLOGY_ELIGIBLE",
            score=0.0,
            confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
            model_id=MODEL_ID,
            model_version=MODEL_VERSION,
            metadata=metadata,
        )
    if status == "ABSENT":
        confidence = proto.get("breathing_probability")
        return AIResult(
            sensor_id="mmwave",
            timestamp=now,
            available=True,
            source=SOURCE,
            state="ABSENT",
            score=0.0,
            confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
            model_id=MODEL_ID,
            model_version=MODEL_VERSION,
            metadata={**metadata, "absent_is_not_apnea": True},
        )
    return _unavailable(now, status if status != "UNAVAILABLE" else "UNAVAILABLE", fail or status, metadata)


def _unavailable(now: float, state: str, error: str, metadata: dict | None = None) -> AIResult:
    return AIResult(
        sensor_id="mmwave",
        timestamp=now,
        available=False,
        source="unavailable",
        state=state,
        error=error,
        metadata=metadata or {},
    )
