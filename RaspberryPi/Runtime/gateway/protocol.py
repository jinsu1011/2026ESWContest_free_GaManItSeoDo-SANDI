"""Strict decoder for SafeNest TCP protocol v1.

The ESP32 sender owns two independent sequence counters: one for scalar
telemetry and one for thermal frames. Sequence validation must therefore be
performed per packet type and reset for each new TCP connection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import socket
import struct
import time
from typing import Final


MAGIC: Final = b"SNST"
PROTOCOL_VERSION: Final = 1
PACKET_TELEMETRY_JSON: Final = 1
PACKET_THERMAL_U16_BE: Final = 2
HEADER: Final = struct.Struct("!4sBBHII")
THERMAL_META: Final = struct.Struct("!HHIIHH")
THERMAL_WIDTH: Final = 80
THERMAL_HEIGHT: Final = 62
THERMAL_PIXEL_BYTES: Final = THERMAL_WIDTH * THERMAL_HEIGHT * 2
THERMAL_PAYLOAD_BYTES: Final = THERMAL_META.size + THERMAL_PIXEL_BYTES
MAX_TELEMETRY_BYTES: Final = 4_096
MAX_U32: Final = 0xFFFFFFFF
EXPECTED_TELEMETRY_SCHEMA: Final = "safenest.telemetry.v1"
ALLOWED_CO2_RANGE_PPM: Final = frozenset({2000, 5000, 10000})


class ProtocolError(ValueError):
    """The peer sent a frame that violates SafeNest TCP v1."""


class ConnectionClosed(ProtocolError):
    """The peer closed the socket before one complete field was received."""


class ReceiveDeadlineExceeded(ProtocolError):
    """A complete field did not arrive before its total receive deadline."""


class SequenceError(ProtocolError):
    """A packet sequence was duplicated or moved backwards."""


@dataclass(frozen=True)
class PacketHeader:
    packet_type: int
    sequence: int
    payload_length: int


@dataclass(frozen=True)
class TelemetryPayload:
    header: PacketHeader
    device_id: str
    uptime_ms: int
    respiration_rate_bpm: float | None
    heart_rate_bpm: float | None
    co2_ppm: float | None
    pir_motion: bool
    valid: dict[str, bool]
    boot_id: str | None = None
    co2_measurement_event_id: int | None = None
    co2_measurement_monotonic_ms: int | None = None
    co2_measurement_event_valid: bool | None = None
    pir_event_id: int | None = None
    pir_last_transition_monotonic_ms: int | None = None
    health: dict[str, int] | None = None
    # Optional mmWave evidence. Additive to TCP v1: legacy packets remain
    # decodable, but cannot be treated as canonical AI input without these.
    # ts_monotonic_ms is the physical 0x0A13 observation time (ESP millis()).
    # phase_age_ms is freshness metadata only — not subtracted from that time.
    # mmwave_sequence is nested mmwave.seq (phase-event identity), distinct
    # from header.sequence (telemetry publication identity).
    breath_phase: float | None = None
    ts_monotonic_ms: float | None = None
    phase_age_ms: float | None = None
    human_detected_raw: bool | None = None
    session_id: str | None = None
    mmwave_sequence: int | None = None
    breath_rate_raw: float | None = None
    # Optional MH-Z19B device-domain metadata. Legacy packets omit these.
    # Identity is INFERRED_UART_SAMPLE, not SCD40 getDataReady conversion.
    co2_sensor_model: str | None = None
    co2_event_identity_class: str | None = None
    co2_preheat_complete: bool | None = None
    abc_enabled: bool | None = None
    configured_range_ppm: int | None = None


@dataclass(frozen=True)
class ThermalFrame:
    header: PacketHeader
    width: int
    height: int
    frame_sequence: int
    uptime_ms: int
    minimum_raw: int
    maximum_raw: int
    pixel_bytes: bytes


DecodedPacket = TelemetryPayload | ThermalFrame


def recv_exact(
    connection: socket.socket,
    size: int,
    *,
    deadline_seconds: float = 5.0,
    idle_deadline_seconds: float | None = None,
) -> bytes:
    """Receive exactly ``size`` bytes without discarding partial progress.

    The socket may have a short per-recv timeout. Such a timeout does not lose
    bytes already consumed.

    ``idle_deadline_seconds`` bounds the wait while zero bytes have arrived.
    ``None`` waits indefinitely for the first byte so a skipped 1 Hz snapshot
    does not close a live ESP socket. Once any byte is buffered,
    ``deadline_seconds`` bounds completion of this field. Expiry closes the
    connection so the next packet cannot start from a desynchronized stream.
    """

    if size < 0:
        raise ValueError("size must be non-negative")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    if idle_deadline_seconds is not None and idle_deadline_seconds <= 0:
        raise ValueError("idle_deadline_seconds must be positive when set")
    if size == 0:
        return b""

    buffer = bytearray()
    idle_deadline = (
        None
        if idle_deadline_seconds is None
        else time.monotonic() + idle_deadline_seconds
    )
    frame_deadline: float | None = None
    while len(buffer) < size:
        now = time.monotonic()
        if len(buffer) == 0:
            if idle_deadline is not None and now >= idle_deadline:
                raise ReceiveDeadlineExceeded(
                    f"receive deadline exceeded: got 0 of {size} bytes"
                )
        elif frame_deadline is not None and now >= frame_deadline:
            raise ReceiveDeadlineExceeded(
                f"receive deadline exceeded: got {len(buffer)} of {size} bytes"
            )
        try:
            chunk = connection.recv(size - len(buffer))
        except socket.timeout:
            continue
        except OSError as exc:
            raise ConnectionClosed(f"socket receive failed: {exc}") from exc
        if not chunk:
            raise ConnectionClosed(
                f"peer closed connection: got {len(buffer)} of {size} bytes"
            )
        buffer.extend(chunk)
        if frame_deadline is None:
            frame_deadline = time.monotonic() + deadline_seconds
    return bytes(buffer)


def decode_header(data: bytes) -> PacketHeader:
    if len(data) != HEADER.size:
        raise ProtocolError(f"header must be {HEADER.size} bytes, got {len(data)}")
    magic, version, packet_type, flags, sequence, payload_length = HEADER.unpack(data)
    if magic != MAGIC:
        raise ProtocolError(f"invalid magic: {magic!r}")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {version}")
    if flags != 0:
        raise ProtocolError(f"unsupported flags: 0x{flags:04x}")
    if packet_type == PACKET_TELEMETRY_JSON:
        if not 0 < payload_length <= MAX_TELEMETRY_BYTES:
            raise ProtocolError(f"invalid telemetry payload length: {payload_length}")
    elif packet_type == PACKET_THERMAL_U16_BE:
        if payload_length != THERMAL_PAYLOAD_BYTES:
            raise ProtocolError(
                "invalid thermal payload length: "
                f"{payload_length} != {THERMAL_PAYLOAD_BYTES}"
            )
    else:
        raise ProtocolError(f"unsupported packet type: {packet_type}")
    return PacketHeader(packet_type, sequence, payload_length)


def read_packet(
    connection: socket.socket,
    *,
    deadline_seconds: float = 5.0,
    idle_deadline_seconds: float | None = None,
) -> DecodedPacket:
    header = decode_header(
        recv_exact(
            connection,
            HEADER.size,
            deadline_seconds=deadline_seconds,
            idle_deadline_seconds=idle_deadline_seconds,
        )
    )
    payload = recv_exact(
        connection,
        header.payload_length,
        deadline_seconds=deadline_seconds,
        idle_deadline_seconds=deadline_seconds,
    )
    if header.packet_type == PACKET_TELEMETRY_JSON:
        return decode_telemetry(header, payload)
    return decode_thermal(header, payload)


def decode_telemetry(header: PacketHeader, payload: bytes) -> TelemetryPayload:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid telemetry JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("telemetry JSON root must be an object")
    if decoded.get("schema") != EXPECTED_TELEMETRY_SCHEMA:
        raise ProtocolError(f"unsupported telemetry schema: {decoded.get('schema')!r}")

    device_id = decoded.get("device_id")
    if not isinstance(device_id, str) or not device_id or len(device_id) > 64:
        raise ProtocolError("device_id must be a non-empty string up to 64 characters")
    json_sequence = _u32(decoded.get("seq"), "seq")
    if json_sequence != header.sequence:
        raise ProtocolError(
            f"header/json sequence mismatch: {header.sequence} != {json_sequence}"
        )
    uptime_ms = _u32(decoded.get("uptime_ms"), "uptime_ms")
    boot_id = _optional_identifier(decoded.get("boot_id"), "boot_id")

    valid_raw = decoded.get("valid")
    if not isinstance(valid_raw, dict):
        raise ProtocolError("valid must be an object")
    valid: dict[str, bool] = {}
    for key in ("respiration", "heart", "co2"):
        value = valid_raw.get(key)
        if not isinstance(value, bool):
            raise ProtocolError(f"valid.{key} must be boolean")
        valid[key] = value

    respiration = _optional_finite(decoded.get("resp_rate_bpm"), "resp_rate_bpm")
    heart = _optional_finite(decoded.get("heart_rate_bpm"), "heart_rate_bpm")
    co2 = _optional_finite(decoded.get("co2_ppm"), "co2_ppm")
    for key, is_valid, value in (
        ("respiration", valid["respiration"], respiration),
        ("heart", valid["heart"], heart),
        ("co2", valid["co2"], co2),
    ):
        if is_valid != (value is not None):
            raise ProtocolError(f"valid.{key} does not match its telemetry value")

    pir_motion = decoded.get("pir_motion")
    if not isinstance(pir_motion, bool):
        raise ProtocolError("pir_motion must be boolean")

    co2_event_id, co2_event_ms, co2_event_valid = _optional_event_provenance(
        decoded,
        id_field="co2_measurement_event_id",
        time_field="co2_measurement_monotonic_ms",
        valid_field="co2_measurement_event_valid",
        boot_id=boot_id,
    )
    pir_event_id, pir_transition_ms = _optional_transition_provenance(
        decoded,
        id_field="pir_event_id",
        time_field="pir_last_transition_monotonic_ms",
        boot_id=boot_id,
    )
    health = _optional_health(decoded.get("health"))
    nested_mmwave = _optional_mmwave_object(decoded.get("mmwave"))
    # Live ESP packets nest the phase observation under `mmwave`. Top-level
    # keys win when both are present so v1 fixtures stay authoritative.
    # Nested `mmwave.seq` is NOT the outer JSON `seq` (publication identity).
    breath_phase = _promoted_optional_finite(decoded, nested_mmwave, "breath_phase")
    ts_monotonic_ms = _promoted_optional_finite(decoded, nested_mmwave, "ts_monotonic_ms")
    phase_age_ms = _promoted_optional_finite(decoded, nested_mmwave, "phase_age_ms")
    human_detected_raw = _promoted_optional_bool(
        decoded, nested_mmwave, "human_detected_raw"
    )
    session_id = _promoted_optional_identifier(decoded, nested_mmwave, "session_id")
    mmwave_sequence = _optional_mmwave_sequence(decoded, nested_mmwave)
    breath_rate_raw = _promoted_optional_finite(decoded, nested_mmwave, "breath_rate_raw")
    co2_sensor_model = _optional_identifier(
        decoded.get("co2_sensor_model"), "co2_sensor_model"
    )
    co2_event_identity_class = _optional_identifier(
        decoded.get("co2_event_identity_class"), "co2_event_identity_class"
    )
    co2_preheat_complete = _optional_co2_preheat_complete(decoded)
    abc_enabled = _optional_bool(decoded.get("abc_enabled"), "abc_enabled")
    configured_range_ppm = _optional_configured_range_ppm(decoded.get("configured_range_ppm"))

    return TelemetryPayload(
        header=header,
        device_id=device_id,
        uptime_ms=uptime_ms,
        respiration_rate_bpm=respiration,
        heart_rate_bpm=heart,
        co2_ppm=co2,
        pir_motion=pir_motion,
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
        session_id=session_id,
        mmwave_sequence=mmwave_sequence,
        breath_rate_raw=breath_rate_raw,
        co2_sensor_model=co2_sensor_model,
        co2_event_identity_class=co2_event_identity_class,
        co2_preheat_complete=co2_preheat_complete,
        abc_enabled=abc_enabled,
        configured_range_ppm=configured_range_ppm,
    )


def decode_thermal(header: PacketHeader, payload: bytes) -> ThermalFrame:
    if len(payload) != THERMAL_PAYLOAD_BYTES:
        raise ProtocolError(
            f"thermal payload must be {THERMAL_PAYLOAD_BYTES} bytes, got {len(payload)}"
        )
    width, height, frame_sequence, uptime_ms, minimum_raw, maximum_raw = (
        THERMAL_META.unpack_from(payload)
    )
    if (width, height) != (THERMAL_WIDTH, THERMAL_HEIGHT):
        raise ProtocolError(f"invalid thermal dimensions: {width}x{height}")
    if frame_sequence != header.sequence:
        raise ProtocolError(
            f"header/thermal sequence mismatch: {header.sequence} != {frame_sequence}"
        )
    if minimum_raw > maximum_raw:
        raise ProtocolError("thermal minimum_raw exceeds maximum_raw")

    pixel_bytes = payload[THERMAL_META.size :]
    actual_min = 0xFFFF
    actual_max = 0
    for (pixel,) in struct.iter_unpack("!H", pixel_bytes):
        actual_min = min(actual_min, pixel)
        actual_max = max(actual_max, pixel)
    if (minimum_raw, maximum_raw) != (actual_min, actual_max):
        raise ProtocolError(
            "thermal min/max metadata mismatch: "
            f"metadata=({minimum_raw},{maximum_raw}) "
            f"pixels=({actual_min},{actual_max})"
        )

    return ThermalFrame(
        header=header,
        width=width,
        height=height,
        frame_sequence=frame_sequence,
        uptime_ms=uptime_ms,
        minimum_raw=minimum_raw,
        maximum_raw=maximum_raw,
        pixel_bytes=pixel_bytes,
    )


class SequenceTracker:
    """Track monotonic uint32 sequences independently for each packet type."""

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def accept(self, header: PacketHeader) -> int:
        previous = self._last.get(header.packet_type)
        self._last[header.packet_type] = header.sequence
        if previous is None:
            return 0
        delta = (header.sequence - previous) & MAX_U32
        if delta == 0:
            raise SequenceError(
                f"duplicate sequence for type {header.packet_type}: {header.sequence}"
            )
        if delta >= 0x80000000:
            raise SequenceError(
                f"backward sequence for type {header.packet_type}: "
                f"{previous} -> {header.sequence}"
            )
        return delta - 1


def _u32(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{field} must be an integer")
    if not 0 <= value <= MAX_U32:
        raise ProtocolError(f"{field} must be in uint32 range")
    return value


def _optional_finite(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{field} must be a number or null")
    converted = float(value)
    if not math.isfinite(converted):
        raise ProtocolError(f"{field} must be finite")
    return converted


def _optional_mmwave_object(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError("mmwave must be an object when present")
    return value


def _optional_mmwave_sequence(
    decoded: dict[str, object],
    nested: dict[str, object] | None,
) -> int | None:
    """Parse physical phase-event sequence.

    Accepts explicit payload field ``mmwave_sequence`` or nested ``mmwave.seq``.
    Never reads outer JSON ``seq`` — that is the publication packet identity
    already validated against the binary header.
    """

    if "mmwave_sequence" in decoded:
        value = decoded["mmwave_sequence"]
        if value is None:
            return None
        return _u32(value, "mmwave_sequence")
    if nested is not None and "seq" in nested:
        value = nested["seq"]
        if value is None:
            return None
        return _u32(value, "mmwave.seq")
    return None


def _promoted_optional_finite(
    decoded: dict[str, object],
    nested: dict[str, object] | None,
    field: str,
) -> float | None:
    if field in decoded:
        return _optional_finite(decoded.get(field), field)
    if nested is not None and field in nested:
        return _optional_finite(nested.get(field), f"mmwave.{field}")
    return None


def _promoted_optional_bool(
    decoded: dict[str, object],
    nested: dict[str, object] | None,
    field: str,
) -> bool | None:
    if field in decoded:
        value = decoded[field]
        path = field
    elif nested is not None and field in nested:
        value = nested[field]
        path = f"mmwave.{field}"
    else:
        return None
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ProtocolError(f"{path} must be boolean when present")
    return value


def _promoted_optional_identifier(
    decoded: dict[str, object],
    nested: dict[str, object] | None,
    field: str,
) -> str | None:
    if field in decoded:
        return _optional_identifier(decoded.get(field), field)
    if nested is not None and field in nested:
        return _optional_identifier(nested.get(field), f"mmwave.{field}")
    return None


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ProtocolError(f"{field} must be boolean when present")
    return value


def _optional_co2_preheat_complete(document: dict[str, object]) -> bool | None:
    """Canonical ``co2_preheat_complete``, with firmware alias ``co2_preheat``.

    Parallel MH-Z19B firmware currently emits ``co2_preheat``. The Pi ingest
    contract name is ``co2_preheat_complete``. Canonical wins when both exist.
    """

    if "co2_preheat_complete" in document:
        return _optional_bool(document.get("co2_preheat_complete"), "co2_preheat_complete")
    if "co2_preheat" in document:
        return _optional_bool(document.get("co2_preheat"), "co2_preheat")
    return None


def _optional_configured_range_ppm(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError("configured_range_ppm must be an integer when present")
    if value not in ALLOWED_CO2_RANGE_PPM:
        raise ProtocolError("configured_range_ppm must be 2000, 5000, or 10000 when present")
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ProtocolError(f"{field} must be a non-empty string up to 64 characters")
    if any(not (character.isalnum() or character in "-_.:") for character in value):
        raise ProtocolError(f"{field} contains unsupported characters")
    return value


def _optional_event_provenance(
    document: dict[str, object],
    *,
    id_field: str,
    time_field: str,
    valid_field: str,
    boot_id: str | None,
) -> tuple[int | None, int | None, bool | None]:
    present = tuple(field in document for field in (id_field, time_field, valid_field))
    if not any(present):
        return None, None, None
    if not all(present):
        raise ProtocolError(f"{id_field}, {time_field}, and {valid_field} must appear together")
    event_id = _u32(document[id_field], id_field)
    event_ms = _u32(document[time_field], time_field)
    event_valid = document[valid_field]
    if not isinstance(event_valid, bool):
        raise ProtocolError(f"{valid_field} must be boolean")
    if event_valid:
        if event_id == 0:
            raise ProtocolError(f"{id_field} must be non-zero when {valid_field} is true")
        if boot_id is None:
            raise ProtocolError(f"boot_id is required when {valid_field} is true")
    elif event_id != 0 or event_ms != 0:
        raise ProtocolError(f"invalid {id_field}/{time_field} must both be zero")
    return event_id, event_ms, event_valid


def _optional_transition_provenance(
    document: dict[str, object],
    *,
    id_field: str,
    time_field: str,
    boot_id: str | None,
) -> tuple[int | None, int | None]:
    present = (id_field in document, time_field in document)
    if not any(present):
        return None, None
    if not all(present):
        raise ProtocolError(f"{id_field} and {time_field} must appear together")
    event_id = _u32(document[id_field], id_field)
    event_ms = _u32(document[time_field], time_field)
    if event_id == 0:
        if event_ms != 0:
            raise ProtocolError(f"{time_field} must be zero before the first transition")
    elif boot_id is None:
        raise ProtocolError(f"boot_id is required when {id_field} is non-zero")
    return event_id, event_ms


def _optional_health(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError("health must be an object")
    result: dict[str, int] = {}
    for field in (
        "telemetry_queue_overwrites",
        "thermal_queue_overwrites",
        "tcp_connection_failures",
        "tcp_send_failures",
        "thermal_udp_frames_sent",
        "thermal_udp_send_failures",
        "co2_data_ready_query_failures",
        "co2_read_failures",
        "thermal_status_query_failures",
    ):
        if field in value:
            result[field] = _u32(value[field], f"health.{field}")
    return result
