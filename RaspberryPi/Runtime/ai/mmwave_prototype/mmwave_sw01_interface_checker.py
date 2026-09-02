"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

SW-01 non-campaign mmWave interface checker (acquisition interface only).

Does not load ROLE_L checkpoints, does not create D1 membership, does not capture
governed ABSENT campaign evidence. Reuses stream fail-closed semantics aligned with
adapters/mmwave_stream_adapter.py (monotonic timestamps, finite values, gap/dropout).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "MMWAVE_V2_D1_SW01_INTERFACE_CHECK_RECEIPT_V1"
CHECK_ID = "MMWAVE_V2_D1_SW01_NON_CAMPAIGN_INTERFACE_CHECKER"

# Nominal 10 Hz MR60-class sampling; align with stream adapter default max gap.
DEFAULT_MAX_GAP_SECONDS = 0.5
DEFAULT_NOMINAL_DT = 0.1

STATUS_PASS = "PASS_NON_CAMPAIGN_INTERFACE_CHECK"
STATUS_FAIL_FIELD = "FAIL_REQUIRED_FIELD_MISSING"
STATUS_FAIL_NON_MONO = "FAIL_NON_MONOTONIC_TIMESTAMP"
STATUS_FAIL_CONTINUITY = "FAIL_CONTINUITY_UNOBSERVABLE"
STATUS_FAIL_HEALTH = "FAIL_HEALTH_UNAVAILABLE"
STATUS_FAIL_RAW = "FAIL_RAW_OR_NEAR_RAW_UNAVAILABLE"
STATUS_LIVE_UNAVAILABLE = "LIVE_TARGET_UNAVAILABLE"
STATUS_BACKEND = "BACKEND_UNAVAILABLE"
STATUS_SCALAR_ONLY = "FAIL_SCALAR_TELEMETRY_ONLY"
STATUS_SOURCE_DECODE = "FAIL_SOURCE_DECODE"
MODE_FIXTURE = "FIXTURE_OFFLINE_VALIDATION"
MODE_EXTERNAL_STREAM = "EXTERNAL_STREAM_NON_CAMPAIGN_CHECK"
MODE_LIVE_HARDWARE = "LIVE_HARDWARE_NON_CAMPAIGN_CHECK"


FIELD_OK = "VERIFIED"
FIELD_MISSING = "MISSING"
FIELD_FAIL = "FAILED"
FIELD_NA = "NOT_APPLICABLE"
FIELD_UNAVAIL = "UNAVAILABLE"
FIELD_SCALAR = "SCALAR_VENDOR_ONLY"


@dataclass
class Sample:
    t: float | None
    phase: float | None = None
    seq: int | None = None
    health_ok: bool | None = None
    fault_code: str | None = None
    session_id: str | None = None
    reset_flag: bool = False
    scalar_rr: float | None = None


@dataclass
class StreamBundle:
    device_identity: str | None = None
    interface_identity: str | None = None
    configuration_identity: str | None = None
    observation_kind: str | None = None  # near_raw_phase | scalar_vendor_rr | unknown
    samples: list[Sample] = field(default_factory=list)
    backend_error: str | None = None
    source_faults: list[str] = field(default_factory=list)


def _finite(x: Any) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def load_fixture(path: Path) -> StreamBundle:
    raw = json.loads(path.read_text())
    meta = raw.get("metadata") or {}
    samples: list[Sample] = []
    for row in raw.get("samples") or []:
        health = row.get("health") or {}
        samples.append(
            Sample(
                t=row.get("t", row.get("timestamp")),
                phase=row.get("phase", row.get("resp_phase")),
                seq=row.get("seq", row.get("sequence")),
                health_ok=health.get("ok", row.get("health_ok")),
                fault_code=health.get("fault_code", row.get("fault_code")),
                session_id=row.get("session_id"),
                reset_flag=bool(row.get("reset") or row.get("reset_flag")),
                scalar_rr=row.get("rr_bpm", row.get("scalar_rr")),
            )
        )
    return StreamBundle(
        device_identity=meta.get("device_identity"),
        interface_identity=meta.get("interface_identity"),
        configuration_identity=meta.get("configuration_identity"),
        observation_kind=meta.get("observation_kind"),
        samples=samples,
        backend_error=raw.get("backend_error"),
    )


def evaluate_stream(
    bundle: StreamBundle,
    *,
    mode: str,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    check_source: str | None = None,
) -> dict[str, Any]:
    """Evaluate a stream bundle into a machine-readable SW-01 receipt."""
    faults: list[str] = []
    dropouts: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []

    if bundle.backend_error:
        err = bundle.backend_error
        if err == "MR60_UART_PROTOCOL_UNPROVEN":
            overall = STATUS_BACKEND
        elif str(err).startswith("SOURCE_") or err in (
            "SOURCE_RECORD_INVALID",
            "SOURCE_STREAM_TRUNCATED",
            "SOURCE_REQUIRED_FIELD_MISSING",
            "SOURCE_SCHEMA_MISMATCH",
        ):
            overall = STATUS_SOURCE_DECODE
        else:
            overall = STATUS_BACKEND
        return _receipt(
            mode=mode,
            check_source=check_source,
            overall=overall,
            device=FIELD_UNAVAIL,
            interface=FIELD_UNAVAIL,
            config=FIELD_UNAVAIL,
            raw=FIELD_UNAVAIL,
            ts=FIELD_UNAVAIL,
            mono=FIELD_UNAVAIL,
            cont=FIELD_UNAVAIL,
            drop=FIELD_UNAVAIL,
            health=FIELD_UNAVAIL,
            sample_count=len(bundle.samples),
            duration=None,
            delta_summary=None,
            faults=[err] + list(bundle.source_faults or []),
            dropouts=[],
            resets=[],
            observed_fields=[],
            source_faults=list(bundle.source_faults or []),
        )

    device_status = FIELD_OK if bundle.device_identity else FIELD_MISSING
    interface_status = FIELD_OK if bundle.interface_identity else FIELD_MISSING
    config_status = FIELD_OK if bundle.configuration_identity else FIELD_MISSING
    if device_status == FIELD_MISSING:
        faults.append("device_identity_missing")
    if interface_status == FIELD_MISSING:
        faults.append("interface_identity_missing")
    if config_status == FIELD_MISSING:
        faults.append("configuration_identity_missing")

    kind = (bundle.observation_kind or "").strip() or None
    samples = bundle.samples
    observed_fields = sorted(
        {
            k
            for k, present in (
                ("device_identity", bool(bundle.device_identity)),
                ("interface_identity", bool(bundle.interface_identity)),
                ("configuration_identity", bool(bundle.configuration_identity)),
                ("observation_kind", bool(kind)),
                ("t", any(s.t is not None for s in samples)),
                ("phase", any(s.phase is not None for s in samples)),
                ("seq", any(s.seq is not None for s in samples)),
                ("health_ok", any(s.health_ok is not None for s in samples)),
                ("scalar_rr", any(s.scalar_rr is not None for s in samples)),
            )
            if present
        }
    )

    # Raw vs scalar
    has_phase = any(_finite(s.phase) for s in samples)
    has_scalar_only = (not has_phase) and any(_finite(s.scalar_rr) for s in samples)
    if kind == "scalar_vendor_rr" or has_scalar_only:
        raw_status = FIELD_SCALAR
        faults.append("scalar_vendor_telemetry_only")
    elif has_phase:
        raw_status = FIELD_OK
    else:
        raw_status = FIELD_MISSING
        faults.append("raw_or_near_raw_missing")

    # Timestamps
    ts_values = [s.t for s in samples]
    if not samples:
        ts_status = FIELD_MISSING
        mono_status = FIELD_MISSING
        faults.append("no_samples")
    elif any(not _finite(t) for t in ts_values):
        ts_status = FIELD_FAIL
        mono_status = FIELD_FAIL
        faults.append("timestamp_non_finite_or_missing")
    else:
        ts_status = FIELD_OK
        mono_status = FIELD_OK
        for i in range(1, len(ts_values)):
            prev, cur = float(ts_values[i - 1]), float(ts_values[i])  # type: ignore[arg-type]
            if cur <= prev:
                mono_status = FIELD_FAIL
                faults.append(f"non_monotonic_at_index_{i}")
                break

    # Continuity / dropout via seq and/or time gaps
    cont_status = FIELD_OK
    drop_status = FIELD_OK
    if not samples:
        cont_status = FIELD_MISSING
        drop_status = FIELD_MISSING
    else:
        # sequence continuity when seq present
        seqs = [s.seq for s in samples]
        if any(s is None for s in seqs):
            # time-gap based continuity only
            if mono_status == FIELD_OK and ts_status == FIELD_OK:
                for i in range(1, len(samples)):
                    dt = float(samples[i].t) - float(samples[i - 1].t)  # type: ignore[arg-type]
                    if dt > max_gap_seconds:
                        dropouts.append({"index": i, "dt": dt, "kind": "timestamp_gap"})
                        cont_status = FIELD_FAIL
                        drop_status = FIELD_OK  # observable
                        faults.append(f"timestamp_gap_at_index_{i}")
            else:
                cont_status = FIELD_FAIL
                drop_status = FIELD_FAIL
                faults.append("continuity_unobservable_without_valid_timestamps")
        else:
            for i in range(1, len(seqs)):
                prev, cur = int(seqs[i - 1]), int(seqs[i])  # type: ignore[arg-type]
                if cur < prev:
                    cont_status = FIELD_FAIL
                    faults.append(f"sequence_regression_at_index_{i}")
                elif cur > prev + 1:
                    gap = cur - prev - 1
                    dropouts.append({"index": i, "missing_count": gap, "kind": "sequence_gap"})
                    cont_status = FIELD_FAIL
                    drop_status = FIELD_OK
                    faults.append(f"sequence_gap_at_index_{i}_missing_{gap}")
            if mono_status == FIELD_OK and ts_status == FIELD_OK:
                for i in range(1, len(samples)):
                    dt = float(samples[i].t) - float(samples[i - 1].t)  # type: ignore[arg-type]
                    if dt > max_gap_seconds:
                        dropouts.append({"index": i, "dt": dt, "kind": "timestamp_gap"})
                        cont_status = FIELD_FAIL
                        faults.append(f"timestamp_gap_at_index_{i}")

    # Session reset / discontinuity
    prev_session: str | None = None
    for i, s in enumerate(samples):
        if s.reset_flag:
            resets.append({"index": i, "kind": "reset_flag"})
        if s.session_id is not None:
            if prev_session is not None and s.session_id != prev_session:
                resets.append({"index": i, "kind": "session_id_change", "from": prev_session, "to": s.session_id})
            prev_session = s.session_id

    # Health: must be explicit; packet presence ≠ healthy
    health_vals = [s.health_ok for s in samples]
    if not samples or all(h is None for h in health_vals):
        health_status = FIELD_MISSING
        faults.append("health_telemetry_missing")
    elif any(h is False for h in health_vals):
        health_status = FIELD_FAIL
        for i, s in enumerate(samples):
            if s.health_ok is False:
                faults.append(f"health_fault_at_index_{i}:{s.fault_code or 'UNSPECIFIED'}")
    else:
        health_status = FIELD_OK

    # Duration / deltas
    duration = None
    delta_summary = None
    if ts_status == FIELD_OK and len(samples) >= 2:
        duration = float(samples[-1].t) - float(samples[0].t)  # type: ignore[arg-type]
        deltas = [
            float(samples[i].t) - float(samples[i - 1].t)  # type: ignore[arg-type]
            for i in range(1, len(samples))
            if float(samples[i].t) > float(samples[i - 1].t)  # type: ignore[arg-type]
        ]
        if deltas:
            delta_summary = {
                "count": len(deltas),
                "min": min(deltas),
                "max": max(deltas),
                "mean": sum(deltas) / len(deltas),
                "max_gap_seconds_threshold": max_gap_seconds,
            }

    overall = STATUS_PASS
    if device_status != FIELD_OK or interface_status != FIELD_OK or config_status != FIELD_OK:
        overall = STATUS_FAIL_FIELD
    if raw_status == FIELD_SCALAR:
        overall = STATUS_SCALAR_ONLY
    elif raw_status != FIELD_OK:
        overall = STATUS_FAIL_RAW
    if ts_status != FIELD_OK:
        overall = STATUS_FAIL_FIELD
    if mono_status != FIELD_OK:
        overall = STATUS_FAIL_NON_MONO
    if cont_status != FIELD_OK or drop_status == FIELD_FAIL:
        # prefer continuity code when continuity failed after other checks
        if cont_status != FIELD_OK:
            overall = STATUS_FAIL_CONTINUITY
    if health_status == FIELD_MISSING or health_status == FIELD_FAIL:
        overall = STATUS_FAIL_HEALTH

    # Priority: backend already handled; order fail codes by severity already applied last-wins —
    # recompute with priority list for clarity.
    overall = _prioritize_overall(
        device_status,
        interface_status,
        config_status,
        raw_status,
        ts_status,
        mono_status,
        cont_status,
        drop_status,
        health_status,
    )

    return _receipt(
        mode=mode,
        check_source=check_source,
        overall=overall,
        device=device_status,
        interface=interface_status,
        config=config_status,
        raw=raw_status,
        ts=ts_status,
        mono=mono_status,
        cont=cont_status,
        drop=drop_status,
        health=health_status,
        sample_count=len(samples),
        duration=duration,
        delta_summary=delta_summary,
        faults=faults,
        dropouts=dropouts,
        resets=resets,
        observed_fields=observed_fields,
        observation_kind=kind,
        identities={
            "device_identity": bundle.device_identity,
            "interface_identity": bundle.interface_identity,
            "configuration_identity": bundle.configuration_identity,
        },
    )


def _prioritize_overall(
    device: str,
    interface: str,
    config: str,
    raw: str,
    ts: str,
    mono: str,
    cont: str,
    drop: str,
    health: str,
) -> str:
    if device != FIELD_OK or interface != FIELD_OK or config != FIELD_OK or ts == FIELD_MISSING:
        if raw == FIELD_SCALAR:
            # still report scalar distinctly if identities ok but scalar-only? prefer field missing first
            pass
        if device != FIELD_OK or interface != FIELD_OK or config != FIELD_OK or ts == FIELD_MISSING:
            return STATUS_FAIL_FIELD
    if raw == FIELD_SCALAR:
        return STATUS_SCALAR_ONLY
    if raw != FIELD_OK:
        return STATUS_FAIL_RAW
    if ts != FIELD_OK:
        return STATUS_FAIL_FIELD
    if mono != FIELD_OK:
        return STATUS_FAIL_NON_MONO
    if cont != FIELD_OK or drop == FIELD_FAIL:
        return STATUS_FAIL_CONTINUITY
    if health != FIELD_OK:
        return STATUS_FAIL_HEALTH
    return STATUS_PASS


def _receipt(
    *,
    mode: str,
    check_source: str | None,
    overall: str,
    device: str,
    interface: str,
    config: str,
    raw: str,
    ts: str,
    mono: str,
    cont: str,
    drop: str,
    health: str,
    sample_count: int,
    duration: float | None,
    delta_summary: dict | None,
    faults: list[str],
    dropouts: list[dict],
    resets: list[dict],
    observed_fields: list[str],
    observation_kind: str | None = None,
    identities: dict | None = None,
    source_faults: list[str] | None = None,
    transport_status: str | None = None,
    source_backend_status: str | None = None,
    parser_status: str | None = None,
    pipeline_semantics: dict | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "check_id": CHECK_ID,
        "mode": mode,
        "check_source": check_source,
        "probe_class": "NON_CAMPAIGN_INTERFACE_PROBE",
        "device_identity_status": device,
        "interface_identity_status": interface,
        "configuration_identity_status": config,
        "raw_observation_status": raw,
        "timestamp_status": ts,
        "monotonicity_status": mono,
        "continuity_status": cont,
        "dropout_observability_status": drop,
        "sensor_health_status": health,
        "observation_kind": observation_kind,
        "identities": identities or {},
        "observed_required_field_inventory": observed_fields,
        "observed_sample_count": sample_count,
        "observed_duration": duration,
        "timestamp_delta_summary": delta_summary,
        "faults": faults,
        "dropouts": dropouts,
        "resets": resets,
        "source_faults": list(source_faults or []),
        "transport_status": transport_status,
        "source_backend_status": source_backend_status,
        "parser_status": parser_status,
        "pipeline_semantics": pipeline_semantics or {},
        "campaign_data_created": False,
        "d1_admissible": False,
        "campaign_slot_consumed": False,
        "dataset_admissible": False,
        "not_d1": True,
        "model_inference": False,
        "role_l_loaded": False,
        "overall_status": overall,
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return receipt


def live_target_unavailable_receipt(*, reason: str, serial_candidates: Sequence[str] | None = None) -> dict[str, Any]:
    return _receipt(
        mode=MODE_LIVE_HARDWARE,
        check_source="live_probe",
        overall=STATUS_LIVE_UNAVAILABLE,
        device=FIELD_UNAVAIL,
        interface=FIELD_UNAVAIL,
        config=FIELD_UNAVAIL,
        raw=FIELD_UNAVAIL,
        ts=FIELD_UNAVAIL,
        mono=FIELD_UNAVAIL,
        cont=FIELD_UNAVAIL,
        drop=FIELD_UNAVAIL,
        health=FIELD_UNAVAIL,
        sample_count=0,
        duration=None,
        delta_summary=None,
        faults=[reason],
        dropouts=[],
        resets=[],
        observed_fields=[],
        observation_kind=None,
        identities={"serial_candidates": list(serial_candidates or [])},
    )


def inventory_serial_ports() -> list[str]:
    import glob

    ports: list[str] = []
    for p in sorted(glob.glob("/dev/tty.*") + glob.glob("/dev/cu.*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        name = Path(p).name.lower()
        if any(x in name for x in ("bluetooth", "debug-console", "razer")):
            continue
        ports.append(p)
    return ports


def annotate_receipt(
    receipt: dict[str, Any],
    *,
    transport_status: str | None = None,
    source_backend_status: str | None = None,
    parser_status: str | None = None,
    pipeline_semantics: dict | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    if mode is not None:
        receipt["mode"] = mode
    if transport_status is not None:
        receipt["transport_status"] = transport_status
    if source_backend_status is not None:
        receipt["source_backend_status"] = source_backend_status
    if parser_status is not None:
        receipt["parser_status"] = parser_status
    if pipeline_semantics is not None:
        receipt["pipeline_semantics"] = pipeline_semantics
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return receipt


def run_source_pipeline(source, *, mode: str, check_source: str | None = None) -> dict[str, Any]:
    """Open source → StreamBundle → evaluate_stream (shared external/live path)."""
    opened = False
    try:
        source.open()
        opened = True
    except Exception as exc:
        # UART stub raises on open
        from ai.mmwave_prototype.mmwave_sw01_source import SourceDecodeError

        if isinstance(exc, SourceDecodeError):
            bundle = StreamBundle(
                backend_error=exc.code,
                source_faults=[f"{exc.code}:{exc.detail}" if exc.detail else exc.code],
            )
            receipt = evaluate_stream(bundle, mode=mode, check_source=check_source)
            ident = source.source_identity()
            return annotate_receipt(
                receipt,
                transport_status=None,
                source_backend_status=ident.get("status"),
                parser_status="PARSER_BACKEND_UNAVAILABLE",
                pipeline_semantics={
                    "software_pipeline_validated": False,
                    "live_hardware_verified": False,
                    "source_backend": ident.get("backend"),
                },
            )
        raise
    try:
        bundle = source.read_bundle()
        receipt = evaluate_stream(bundle, mode=mode, check_source=check_source)
        ident = source.source_identity()
        soft_ok = (
            mode == MODE_EXTERNAL_STREAM
            and receipt.get("overall_status") == STATUS_PASS
            and not bundle.backend_error
        )
        return annotate_receipt(
            receipt,
            source_backend_status=ident.get("status"),
            parser_status="PARSER_APPLIED" if not bundle.backend_error else "PARSER_BACKEND_UNAVAILABLE",
            pipeline_semantics={
                "software_pipeline_validated": soft_ok,
                "live_hardware_verified": False,
                "source_backend": ident.get("backend"),
                "d1_admissible": False,
            },
        )
    finally:
        if opened:
            source.close()
