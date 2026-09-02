"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

M-PROT-3 integration runtime wiring.

Composes (does not reimplement):
  SW-01 source validation
    → causal temporal composer (time-coverage / past-only)
    → NativeTraceInput
    → R1 adapt_native_trace (owns resampling)
    → M-PROT-2 B23 runtime

Semantics:
  PROTOTYPE_INTEGRATION_ONLY / NOT_FINAL_SELECTED_MODEL / SUBJECT_TO_REPLACEMENT
  PROVISIONAL_INTEGRATION_FREEZE = true
  No M-N9 fallback. No UART protocol invention.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ai.mmwave_prototype.mmwave_m_prot_2_b23_runtime import (
    CANONICAL_PARAMETER_SHA256,
    CANDIDATE_ID,
    PANEL_ID,
    PRIMARY_REPRESENTATION,
    SAMPLE_RATE_HZ,
    SCALER_CONTENT_SHA256,
    SOURCE_ARTIFACT_REL,
    SOURCE_ARTIFACT_SHA256,
    TRACE_SAMPLES,
    PrototypeFailClosed,
    resolve_verified_runtime,
    run_prototype_inference,
)
from ai.mmwave_prototype.mmwave_r1_sensor_independent_trace import (
    R1_PROFILE_ID,
    NativeTraceInput,
    R1TraceError,
    adapt_native_trace,
)
from ai.mmwave_prototype.mmwave_sw01_interface_checker import (
    DEFAULT_MAX_GAP_SECONDS,
    STATUS_PASS,
    Sample,
    StreamBundle,
    evaluate_stream,
)

PHASE_ID = "M-PROT-3"
RUNTIME_MODULE = "ai/mmwave_prototype/mmwave_m_prot_3_integration_runtime.py"
WINDOW_CONTRACT = "M_PROT_3_CAUSAL_30S_10HZ_300_V1"
PRODUCTION_INFERENCE_CADENCE = "NOT_GOVERNED_IN_M_PROT_3"

MANDATORY_SEMANTICS = (
    "PROTOTYPE_INTEGRATION_ONLY",
    "NOT_FINAL_SELECTED_MODEL",
    "NOT_DEPLOYMENT_VALIDATED",
    "NOT_SAFETY_VALIDATED",
    "NOT_CLINICAL_VALIDATION",
    "SUBJECT_TO_REPLACEMENT",
)

# Composer / cross-bundle boundary: reuse SW-01 governed max gap.
DEFAULT_MAX_GAP_S = float(DEFAULT_MAX_GAP_SECONDS)
# Nominal indexed span for 300 samples @ 10 Hz (indices 0..299).
TARGET_SPAN_S = (TRACE_SAMPLES - 1) / SAMPLE_RATE_HZ  # 29.9 s

def _team_root() -> Path:
    from paths import ONDEVICE_AI_ROOT
    return ONDEVICE_AI_ROOT


class MProt3FailClosed(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass
class ValidatedSourceBinding:
    """Latest SW-01 PASS evidence for the active temporal admission epoch."""

    overall_status: str
    receipt_sha256: str
    device_identity: str | None
    interface_identity: str | None
    configuration_identity: str | None
    observation_kind: str | None
    admission_id: int


@dataclass
class StreamBoundaryCursor:
    """Last admitted sample identity for cross-bundle continuity checks."""

    last_t: float
    last_seq: int | None
    last_session_id: str | None


@dataclass
class WiringReceipt:
    """Portable M-PROT-3 composition receipt (one inference attempt)."""

    schema_version: str = "M-PROT-3-WIRING-RECEIPT-V3"
    phase: str = PHASE_ID
    status: str = "UNAVAILABLE"
    fail_closed_code: str | None = None
    panel_id: str = PANEL_ID
    candidate_id: str = CANDIDATE_ID
    artifact_rel: str = SOURCE_ARTIFACT_REL
    artifact_sha256: str = SOURCE_ARTIFACT_SHA256
    parameter_sha256: str = CANONICAL_PARAMETER_SHA256
    scaler_content_sha256: str = SCALER_CONTENT_SHA256
    representation: str = PRIMARY_REPRESENTATION
    r1_profile: str = R1_PROFILE_ID
    window_contract: str = WINDOW_CONTRACT
    source_validation_status: str | None = None
    # Latest contributing PASS receipt (compatibility); not full multi-bundle proof alone.
    sw01_receipt_sha256: str | None = None
    # Ordered PASS receipt SHAs for samples in the selected causal window only.
    sw01_receipt_sha256_chain: tuple[str, ...] = ()
    device_identity: str | None = None
    interface_identity: str | None = None
    configuration_identity: str | None = None
    observation_kind: str | None = None
    window_ready: bool = False
    window_start_s: float | None = None
    window_end_s: float | None = None
    source_sample_count: int | None = None
    r1_sample_count: int | None = None
    assembled_dim: int | None = None
    session_id: str | None = None
    presence_status: str = "PRESENCE_UNAVAILABLE"
    # True only when an external governed presence signal opens the gate.
    presence_gate_satisfied: bool = False
    lineage_class: str = "FIXTURE_NON_CAMPAIGN"
    provisional_integration_freeze: bool = True
    replacement_requires_control_tower_decision: bool = True
    mandatory_semantics: tuple[str, ...] = MANDATORY_SEMANTICS
    production_inference_cadence: str = PRODUCTION_INFERENCE_CADENCE
    m_n9_fallback: bool = False
    prototype_receipt: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mandatory_semantics"] = list(self.mandatory_semantics)
        payload["sw01_receipt_sha256_chain"] = list(self.sw01_receipt_sha256_chain)
        payload["PROTOTYPE_INTEGRATION_ONLY"] = True
        payload["NOT_FINAL_SELECTED_MODEL"] = True
        payload["FINAL_GOVERNED_EVALUATION"] = False
        payload["presence_available"] = self.presence_gate_satisfied
        return payload


@dataclass
class _BufferedSample:
    t: float
    phase: float
    session_id: str | None
    admission_id: int
    receipt_sha256: str


@dataclass
class CausalTemporalComposer:
    """Past-only causal composer selecting source-domain time coverage for R1."""

    max_gap_s: float = DEFAULT_MAX_GAP_S
    target_rate_hz: float = SAMPLE_RATE_HZ
    target_samples: int = TRACE_SAMPLES
    target_span_s: float = TARGET_SPAN_S
    _buf: list[_BufferedSample] = field(default_factory=list)
    _session_id: str | None = None

    def flush(self) -> None:
        self._buf.clear()
        self._session_id = None

    @property
    def buffered_count(self) -> int:
        return len(self._buf)

    def push(
        self,
        sample: Sample,
        *,
        admission_id: int,
        receipt_sha256: str,
    ) -> str | None:
        """Push one sample already admitted under a validated SW-01 binding."""
        if sample.reset_flag:
            self.flush()
            self._session_id = sample.session_id
        if sample.session_id is not None and self._session_id is not None:
            if sample.session_id != self._session_id:
                self.flush()
                self._session_id = sample.session_id
        elif sample.session_id is not None:
            self._session_id = sample.session_id

        if sample.t is None or not np.isfinite(sample.t):
            return "TIMESTAMP_INVALID"
        if sample.phase is None or not np.isfinite(sample.phase):
            return "PHASE_MISSING"
        t = float(sample.t)
        phase = float(sample.phase)
        if self._buf:
            prev = self._buf[-1]
            dt = t - prev.t
            if dt <= 0:
                self.flush()
                return "TIMESTAMP_NON_MONOTONIC"
            if dt > self.max_gap_s:
                self.flush()
                self._buf.append(
                    _BufferedSample(
                        t=t,
                        phase=phase,
                        session_id=sample.session_id,
                        admission_id=admission_id,
                        receipt_sha256=receipt_sha256,
                    )
                )
                return "LARGE_GAP_FLUSH"
        self._buf.append(
            _BufferedSample(
                t=t,
                phase=phase,
                session_id=sample.session_id,
                admission_id=admission_id,
                receipt_sha256=receipt_sha256,
            )
        )
        return None

    def ready(self) -> bool:
        if not self._buf:
            return False
        t_end = self._buf[-1].t
        t_need = t_end - self.target_span_s
        return any(s.t <= t_need + 1e-12 for s in self._buf)

    def select_causal_source_suffix(self) -> list[_BufferedSample]:
        """Minimal past-only source suffix covering [T_end - TARGET_SPAN_S, T_end]."""
        if not self.ready():
            raise MProt3FailClosed(
                "WINDOW_NOT_READY",
                f"have_count={len(self._buf)} need_span_s={self.target_span_s}",
            )
        t_end = self._buf[-1].t
        t_need = t_end - self.target_span_s
        start_idx: int | None = None
        for i, sample in enumerate(self._buf):
            if sample.t <= t_need + 1e-12:
                start_idx = i
        if start_idx is None:
            raise MProt3FailClosed("WINDOW_NOT_READY", f"t_need={t_need}")
        window = self._buf[start_idx:]
        sessions = {s.session_id for s in window if s.session_id is not None}
        if len(sessions) > 1:
            raise MProt3FailClosed("CROSS_SESSION_WINDOW", str(sessions))
        admission_ids = {s.admission_id for s in window}
        if len(admission_ids) != 1:
            raise MProt3FailClosed("CROSS_ADMISSION_WINDOW", str(admission_ids))
        for i in range(1, len(window)):
            dt = window[i].t - window[i - 1].t
            if dt <= 0 or dt > self.max_gap_s:
                raise MProt3FailClosed("WINDOW_INTERNAL_GAP", f"index={i} dt={dt}")
        span = float(window[-1].t - window[0].t)
        if span + 1e-9 < self.target_span_s * 0.98:
            raise MProt3FailClosed("WINDOW_SPAN_TOO_SHORT", f"span={span}")
        return window

    @staticmethod
    def contributing_receipt_chain(window: list[_BufferedSample]) -> tuple[str, ...]:
        """Ordered unique receipt SHAs for samples in the selected window."""
        chain: list[str] = []
        for sample in window:
            if not chain or chain[-1] != sample.receipt_sha256:
                chain.append(sample.receipt_sha256)
        return tuple(chain)

    def compose_native_window(
        self,
        *,
        source_binding: ValidatedSourceBinding,
    ) -> tuple[NativeTraceInput, tuple[str, ...]]:
        window = self.select_causal_source_suffix()
        if window[0].admission_id != source_binding.admission_id:
            raise MProt3FailClosed(
                "VALIDATION_BINDING_MISMATCH",
                "selected window is not bound to the active SW-01 PASS admission",
            )
        receipt_chain = self.contributing_receipt_chain(window)
        times = np.asarray([s.t for s in window], dtype=np.float64)
        phases = np.asarray([s.phase for s in window], dtype=np.float64)
        t0 = float(times[0])
        time_s = times - t0
        dts = np.diff(time_s)
        med_dt = float(np.median(dts)) if dts.size else (1.0 / self.target_rate_hz)
        rate = 1.0 / med_dt if med_dt > 0 else self.target_rate_hz
        session = window[-1].session_id or "UNKNOWN_SESSION"
        latest_receipt = receipt_chain[-1] if receipt_chain else source_binding.receipt_sha256
        native = NativeTraceInput(
            source_id="M_PROT_3_SW01",
            dataset_id="m_prot_3_integration",
            subject_id="PROTOTYPE",
            recording_id=str(session),
            condition="FIXTURE_NON_CAMPAIGN",
            trace=phases,
            time_s=time_s,
            sampling_rate_hz=float(rate),
            native_trace_semantics="phase_like_radian",
            native_trace_unit="radian_like",
            source_scale_metadata={
                "composer": WINDOW_CONTRACT,
                "device_identity": source_binding.device_identity,
                "interface_identity": source_binding.interface_identity,
                "configuration_identity": source_binding.configuration_identity,
                "observation_kind": source_binding.observation_kind,
                "sw01_receipt_sha256": latest_receipt,
                "sw01_receipt_sha256_chain": list(receipt_chain),
            },
            provenance={
                "m_prot_3": True,
                "window_contract": WINDOW_CONTRACT,
                "window_readiness_basis": "CAUSAL_TIME_COVERAGE",
                "target_span_s": self.target_span_s,
                "source_sample_count": len(window),
                "window_start_s": float(times[0]),
                "window_end_s": float(times[-1]),
                "session_id": session,
                "device_identity": source_binding.device_identity,
                "interface_identity": source_binding.interface_identity,
                "configuration_identity": source_binding.configuration_identity,
                "observation_kind": source_binding.observation_kind,
                "sw01_overall_status": source_binding.overall_status,
                "sw01_receipt_sha256": latest_receipt,
                "sw01_receipt_sha256_chain": list(receipt_chain),
                "admission_id": source_binding.admission_id,
            },
            validity_mask=np.ones(len(window), dtype=bool),
            source_quality_flags=("M_PROT_3_CAUSAL_WINDOW",),
        )
        return native, receipt_chain


class MProt3IntegrationRuntime:
    """SW-01 → composer → R1 → M-PROT-2 B23 composition."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else _team_root()
        self.composer = CausalTemporalComposer()
        self._model = None
        self._scaler = None
        self._validated_binding: ValidatedSourceBinding | None = None
        self._admission_seq = 0
        self._boundary: StreamBoundaryCursor | None = None

    def ensure_runtime(self) -> None:
        """Resolve frozen B23 + scaler. Call only after non-model gates pass."""
        if self._model is None or self._scaler is None:
            try:
                self._model, self._scaler = resolve_verified_runtime(root=self.root)
            except PrototypeFailClosed:
                raise
            except Exception as exc:  # noqa: BLE001 — map target dependency gaps
                raise MProt3FailClosed(
                    "TARGET_RUNTIME_DEPENDENCY_UNAVAILABLE",
                    str(exc),
                ) from exc

    def reset(self) -> None:
        self._invalidate_admission()

    def _invalidate_admission(self) -> None:
        """Flush temporal state and clear validated binding / boundary cursor."""
        self.composer.flush()
        self._validated_binding = None
        self._boundary = None

    def _identity_compatible(self, bundle: StreamBundle) -> bool:
        binding = self._validated_binding
        if binding is None:
            return False
        return (
            binding.device_identity == bundle.device_identity
            and binding.interface_identity == bundle.interface_identity
            and binding.configuration_identity == bundle.configuration_identity
            and binding.observation_kind == bundle.observation_kind
        )

    def _boundary_allows_continuation(self, bundle: StreamBundle) -> bool:
        """Cross-bundle continuity beyond per-bundle SW-01 validation."""
        if self._boundary is None or not bundle.samples:
            return False
        first = bundle.samples[0]
        if first.reset_flag:
            return False
        if first.session_id is not None and self._boundary.last_session_id is not None:
            if first.session_id != self._boundary.last_session_id:
                return False
        if first.t is None or not np.isfinite(first.t):
            return False
        dt = float(first.t) - self._boundary.last_t
        if dt <= 0:
            return False
        if dt > self.composer.max_gap_s:
            return False
        if first.seq is not None and self._boundary.last_seq is not None:
            seq_delta = int(first.seq) - int(self._boundary.last_seq)
            if seq_delta <= 0:
                return False
            # seq_delta > 1 is a short hole. Timestamp already passed the
            # positive / max-gap checks, so keep the causal buffer. Missing
            # nested seq is a diagnostic on the live B23 adapter, not a flush.
        return True

    def _set_boundary_from_sample(self, sample: Sample) -> None:
        if sample.t is None or not np.isfinite(sample.t):
            self._boundary = None
            return
        self._boundary = StreamBoundaryCursor(
            last_t=float(sample.t),
            last_seq=None if sample.seq is None else int(sample.seq),
            last_session_id=sample.session_id,
        )

    def ingest_bundle(self, bundle: StreamBundle, *, mode: str = "FIXTURE_OFFLINE_VALIDATION") -> dict[str, Any]:
        """Validate SW-01 then admit samples. Production path has no bypass."""
        source_receipt = evaluate_stream(bundle, mode=mode, check_source="m_prot_3")
        if source_receipt.get("overall_status") != STATUS_PASS:
            # Subsequent SW-01 failure must invalidate any prior ready state.
            self._invalidate_admission()
            raise MProt3FailClosed(
                "SOURCE_VALIDATION_FAILED",
                str(source_receipt.get("overall_status")),
            )
        if (bundle.observation_kind or "") == "scalar_vendor_rr":
            self._invalidate_admission()
            raise MProt3FailClosed("SCALAR_RR_NOT_MODEL_INPUT", "scalar_rr cannot feed B23")
        for sample in bundle.samples:
            if sample.phase is None and sample.scalar_rr is not None:
                self._invalidate_admission()
                raise MProt3FailClosed("SCALAR_RR_NOT_MODEL_INPUT", "missing phase; scalar_rr ignored")

        receipt_sha = str(source_receipt.get("receipt_sha256") or "")
        if not receipt_sha:
            self._invalidate_admission()
            raise MProt3FailClosed("SOURCE_RECEIPT_MISSING", "SW-01 receipt_sha256 required")

        continue_stream = self._identity_compatible(bundle) and self._boundary_allows_continuation(bundle)
        if continue_stream:
            admission_id = self._validated_binding.admission_id  # type: ignore[union-attr]
        else:
            # Discontinuity or new identity: do not bridge prior temporal history.
            self.composer.flush()
            self._admission_seq += 1
            admission_id = self._admission_seq

        self._validated_binding = ValidatedSourceBinding(
            overall_status=str(source_receipt["overall_status"]),
            receipt_sha256=receipt_sha,
            device_identity=bundle.device_identity,
            interface_identity=bundle.interface_identity,
            configuration_identity=bundle.configuration_identity,
            observation_kind=bundle.observation_kind,
            admission_id=admission_id,
        )

        last_admitted: Sample | None = None
        for sample in bundle.samples:
            reason = self.composer.push(
                sample,
                admission_id=admission_id,
                receipt_sha256=receipt_sha,
            )
            if reason in {"TIMESTAMP_INVALID", "PHASE_MISSING", "TIMESTAMP_NON_MONOTONIC"}:
                # Internal push failure after PASS — fail closed and invalidate.
                self._invalidate_admission()
                raise MProt3FailClosed("SOURCE_ADMISSION_REJECTED", reason)
            if self.composer.buffered_count > 0:
                last_admitted = sample

        if last_admitted is None or self.composer.buffered_count == 0:
            self._boundary = None
        else:
            self._set_boundary_from_sample(last_admitted)
        return source_receipt

    def _base_receipt(self, *, lineage_class: str) -> WiringReceipt:
        binding = self._validated_binding
        return WiringReceipt(
            source_validation_status=None if binding is None else binding.overall_status,
            sw01_receipt_sha256=None if binding is None else binding.receipt_sha256,
            device_identity=None if binding is None else binding.device_identity,
            interface_identity=None if binding is None else binding.interface_identity,
            configuration_identity=None if binding is None else binding.configuration_identity,
            observation_kind=None if binding is None else binding.observation_kind,
            lineage_class=lineage_class,
            session_id=self.composer._session_id,
            source_sample_count=self.composer.buffered_count,
        )

    def try_infer(
        self,
        *,
        presence_gate_satisfied: bool | None = None,
        presence_available: bool | None = None,
        lineage_class: str = "FIXTURE_NON_CAMPAIGN",
    ) -> WiringReceipt:
        """Caller-triggered inference when validated window is ready."""
        base = self._base_receipt(lineage_class=lineage_class)

        if self._validated_binding is None or self._validated_binding.overall_status != STATUS_PASS:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = "SW01_ADMISSION_REQUIRED"
            return base
        if self.composer.buffered_count == 0:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = "SW01_ADMISSION_REQUIRED"
            return base
        if any(s.admission_id != self._validated_binding.admission_id for s in self.composer._buf):
            base.status = "UNAVAILABLE"
            base.fail_closed_code = "VALIDATION_BINDING_MISMATCH"
            return base

        if presence_gate_satisfied is None:
            presence_gate_satisfied = False if presence_available is None else bool(presence_available)
        base.presence_gate_satisfied = bool(presence_gate_satisfied)
        base.presence_status = (
            "PRESENCE_GATE_SATISFIED" if presence_gate_satisfied else "PRESENCE_UNAVAILABLE"
        )

        if not self.composer.ready():
            base.status = "WINDOW_NOT_READY"
            base.fail_closed_code = "WINDOW_NOT_READY"
            base.window_ready = False
            return base

        try:
            native, receipt_chain = self.composer.compose_native_window(
                source_binding=self._validated_binding
            )
        except MProt3FailClosed as exc:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = exc.code
            return base

        base.window_ready = True
        base.window_start_s = float(native.provenance.get("window_start_s"))
        base.window_end_s = float(native.provenance.get("window_end_s"))
        base.source_sample_count = int(native.provenance.get("source_sample_count"))
        base.session_id = str(native.provenance.get("session_id"))
        base.sw01_receipt_sha256_chain = receipt_chain
        base.sw01_receipt_sha256 = receipt_chain[-1] if receipt_chain else None

        if not presence_gate_satisfied:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = "PRESENCE_UNAVAILABLE"
            return base

        try:
            common = adapt_native_trace(native)
        except R1TraceError as exc:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = f"R1_{exc.code}"
            return base

        r1_n = int(np.asarray(common.trace).shape[0])
        base.r1_sample_count = r1_n
        if r1_n != TRACE_SAMPLES:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = "R1_SAMPLE_COUNT_MISMATCH"
            return base

        try:
            self.ensure_runtime()
        except MProt3FailClosed as exc:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = exc.code
            return base
        except PrototypeFailClosed as exc:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = exc.code
            return base

        try:
            proto = run_prototype_inference(
                {
                    "common_trace": common,
                    "presence_available": True,
                    "lineage_class": lineage_class,
                },
                root=self.root,
                model=self._model,
                scaler=self._scaler,
            )
        except PrototypeFailClosed as exc:
            base.status = "UNAVAILABLE"
            base.fail_closed_code = exc.code
            return base

        base.status = proto.status
        base.fail_closed_code = proto.fail_closed_code
        base.assembled_dim = TRACE_SAMPLES + TRACE_SAMPLES + 12 + 9  # 621
        base.prototype_receipt = proto.to_json()
        return base


def assert_no_mn9_imports() -> None:
    """Static guard used by tests — M-PROT-3 must not depend on M-N9."""
    import ast

    tree = ast.parse(Path(__file__).read_text())
    forbidden_modules = {"inference.mmwave_interpreter", "tensorflow.lite"}
    forbidden_names = {"MMWaveInterpreter", "MN9Interpreter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules or alias.name.startswith("tensorflow.lite"):
                    raise RuntimeError(f"forbidden_import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in forbidden_modules or mod.startswith("inference.mmwave"):
                raise RuntimeError(f"forbidden_import_from:{mod}")
            for alias in node.names:
                if alias.name in forbidden_names:
                    raise RuntimeError(f"forbidden_import_name:{alias.name}")


__all__ = [
    "CausalTemporalComposer",
    "MProt3FailClosed",
    "MProt3IntegrationRuntime",
    "StreamBoundaryCursor",
    "ValidatedSourceBinding",
    "WiringReceipt",
    "PHASE_ID",
    "WINDOW_CONTRACT",
    "PRODUCTION_INFERENCE_CADENCE",
    "TARGET_SPAN_S",
    "DEFAULT_MAX_GAP_S",
    "assert_no_mn9_imports",
]
