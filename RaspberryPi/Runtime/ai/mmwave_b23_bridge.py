"""Team telemetry → SW-01 StreamBundle semantic bridge (M-PROT-5B).

Does not invent UART decoding. Maps existing SafeNest TCP v1 / snapshot
fields onto frozen SW-01 Sample semantics.

Current ESP producer (do not modify ESP firmware in M-PROT-5B):

  mmwave.breath_phase     = real MR60 0x0A13 breath-phase observation
  mmwave.seq              = physical phase-event sequence
  outer packet seq        = telemetry publication identity (~10 Hz)
  mmwave.ts_monotonic_ms  = ESP millis() when the physical observation is consumed
  mmwave.phase_age_ms     = send time minus that physical timestamp (freshness only)
  boot_id                 = ESP boot/reset boundary
  human_detected_raw      = tri-state presence (true / false / null)
  breath_rate_raw         = vendor diagnostic scalar; never B23 model input

Sample.t = ts_monotonic_ms / 1000.0 directly. Do not subtract phase_age_ms.
Sample.seq = nested mmwave.seq, never the outer publication sequence.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from ai.mmwave_prototype.mmwave_sw01_interface_checker import Sample, StreamBundle
from gateway.protocol import TelemetryPayload

INTERFACE_IDENTITY = "safenest.telemetry.v1"
CONFIGURATION_IDENTITY = "mr60_tcp_v1_phase_waveform"
OBSERVATION_KIND = "near_raw_phase"

# Freshness bound for the ESP 100 ms latest-only publisher of an ~8.4–10 Hz
# 0x0A13 stream. Used only to admit/reject; never to reconstruct event time.
PHASE_AGE_MAX_MS = 1000.0


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def physical_timestamp_s(ts_monotonic_ms: object) -> float | None:
    """B23 source time: ESP physical observation timestamp, in seconds."""

    if not _finite(ts_monotonic_ms):
        return None
    return float(ts_monotonic_ms) / 1000.0


def observation_timestamp_s(ts_monotonic_ms: object, phase_age_ms: object = None) -> float | None:
    """B23 Sample.t.

    ``phase_age_ms`` is accepted for call-site compatibility with the legacy
    M-N4 two-argument form. It is **not** subtracted. The current ESP already
    stores the physical observation time in ``ts_monotonic_ms``.
    """

    return physical_timestamp_s(ts_monotonic_ms)


def phase_age_is_fresh(phase_age_ms: object, *, max_age_ms: float = PHASE_AGE_MAX_MS) -> bool:
    """True when phase_age_ms can be used as freshness evidence."""

    if not _finite(phase_age_ms):
        return False
    age = float(phase_age_ms)
    return 0.0 <= age <= float(max_age_ms)


def mprot3_session_id(*, boot_id: object, packet_session_id: object = None) -> str | None:
    """Map ESP boot epoch onto the M-PROT-3 session/reset boundary.

    ``boot_id`` is the hard source identity. M-PROT-3 ``session_id`` becomes
    ``boot:{boot_id}`` when boot_id is present. A separate ESP ``session_id``
    is not an independent sensor history: it is used only when boot_id is
    absent (offline fixtures).
    """

    if isinstance(boot_id, str) and boot_id:
        return f"boot:{boot_id}"
    if isinstance(packet_session_id, str) and packet_session_id:
        return packet_session_id
    return None


def bundle_from_sensor(
    sensor: Mapping[str, object],
    *,
    device_identity: str | None = None,
    reset_flag: bool = False,
) -> StreamBundle:
    values = sensor.get("values") if isinstance(sensor.get("values"), Mapping) else {}
    if not isinstance(values, Mapping):
        values = {}
    phase = values.get("breath_phase")
    t = physical_timestamp_s(values.get("ts_monotonic_ms"))
    seq = _int_or_none(values.get("mmwave_sequence"))
    session = mprot3_session_id(
        boot_id=sensor.get("boot_id"),
        packet_session_id=values.get("session_id"),
    )
    # SW-01 requires explicit health_ok. This is waveform/source health, not
    # vendor scalar respiration-rate validity.
    health_ok = True
    device = device_identity or _string(sensor.get("device_id")) or "safenest-mmwave"
    sample = Sample(
        t=t,
        phase=float(phase) if _finite(phase) else None,
        seq=seq,
        health_ok=health_ok,
        session_id=session,
        reset_flag=reset_flag,
        scalar_rr=None,
    )
    return StreamBundle(
        device_identity=device,
        interface_identity=INTERFACE_IDENTITY,
        configuration_identity=CONFIGURATION_IDENTITY,
        observation_kind=OBSERVATION_KIND,
        samples=[sample],
    )


def bundle_from_packet(packet: TelemetryPayload, *, reset_flag: bool = False) -> StreamBundle:
    t = physical_timestamp_s(packet.ts_monotonic_ms)
    sample = Sample(
        t=t,
        phase=float(packet.breath_phase) if _finite(packet.breath_phase) else None,
        seq=_int_or_none(packet.mmwave_sequence),
        health_ok=True,
        session_id=mprot3_session_id(
            boot_id=packet.boot_id,
            packet_session_id=packet.session_id,
        ),
        reset_flag=reset_flag,
        scalar_rr=None,
    )
    return StreamBundle(
        device_identity=packet.device_id,
        interface_identity=INTERFACE_IDENTITY,
        configuration_identity=CONFIGURATION_IDENTITY,
        observation_kind=OBSERVATION_KIND,
        samples=[sample],
    )


def presence_from_sensor(sensor: Mapping[str, object]) -> tuple[bool, bool]:
    """Return (presence_available, presence_gate_satisfied).

    Presence is taken only from the team explicit occupancy field.
    null must not collapse to false. It is never inferred from RR,
    breathing probability, quality, or amplitude.
    """

    values = sensor.get("values") if isinstance(sensor.get("values"), Mapping) else {}
    if not isinstance(values, Mapping):
        return False, False
    available = values.get("presence_available") is True
    presence = values.get("presence")
    if not available or not isinstance(presence, bool):
        return False, False
    return True, bool(presence)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def json_safe_receipt(receipt: Any) -> dict[str, Any]:
    payload = receipt.to_json() if hasattr(receipt, "to_json") else dict(receipt)
    return _json_safe(payload)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)
