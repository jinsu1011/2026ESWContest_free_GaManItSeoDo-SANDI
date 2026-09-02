"""Thread-safe latest-value and freshness state for all SafeNest sensors."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import math
import threading
import time
from typing import Final, Mapping

from gateway.protocol import (
    DecodedPacket,
    TelemetryPayload,
    ThermalFrame,
)


SENSOR_IDS: Final = ("mmwave", "thermal", "co2", "pir")
DEFAULT_STALE_SECONDS: Final = {
    "mmwave": 3.0,
    "thermal": 3.0,
    "co2": 10.0,
    "pir": 10.0,
}
DEFAULT_CO2_UPDATE_INTERVAL_SECONDS: Final = 60.0


@dataclass
class _SensorRecord:
    sensor_id: str
    ttl_seconds: float
    connected: bool = False
    valid: bool = False
    peer: str | None = None
    sequence: int | None = None
    source_uptime_ms: int | None = None
    device_id: str | None = None
    boot_id: str | None = None
    last_received_at: float | None = None
    last_received_monotonic: float | None = None
    last_valid_at: float | None = None
    disconnected_at: float | None = None
    error: str | None = None
    values: dict[str, object] = field(default_factory=dict)


class SensorStateManager:
    """Own the latest state without pretending old or invalid values are live.

    Wall-clock Unix seconds are exposed to downstream consumers. Freshness is
    calculated from monotonic receive time so system clock adjustments cannot
    make a stale packet look new.
    """

    def __init__(
        self,
        stale_seconds: Mapping[str, float] | None = None,
        *,
        co2_update_interval_seconds: float = DEFAULT_CO2_UPDATE_INTERVAL_SECONDS,
    ) -> None:
        configured = dict(DEFAULT_STALE_SECONDS)
        if stale_seconds is not None:
            unknown = set(stale_seconds) - set(SENSOR_IDS)
            if unknown:
                raise ValueError(f"unknown sensor TTL keys: {sorted(unknown)}")
            configured.update(stale_seconds)
        for sensor_id, ttl in configured.items():
            if not math.isfinite(float(ttl)) or float(ttl) <= 0:
                raise ValueError(f"{sensor_id} stale TTL must be positive and finite")
        if (
            not math.isfinite(float(co2_update_interval_seconds))
            or float(co2_update_interval_seconds) <= 0
        ):
            raise ValueError("CO2 update interval must be positive and finite")

        self._lock = threading.RLock()
        self._records = {
            sensor_id: _SensorRecord(sensor_id, float(configured[sensor_id]))
            for sensor_id in SENSOR_IDS
        }
        self._latest_thermal_frame: ThermalFrame | None = None
        self._device_health: dict[str, int] | None = None
        self.co2_update_interval_seconds = float(co2_update_interval_seconds)
        self._last_co2_value_monotonic: float | None = None
        self._last_co2_event_key: tuple[str, str, int] | None = None
        self._co2_measurement_event_count = 0
        self._revision = 0

    def ingest(
        self,
        packet: DecodedPacket,
        peer: tuple[str, int],
        *,
        received_at: float | None = None,
        monotonic_at: float | None = None,
    ) -> int:
        wall = time.time() if received_at is None else _finite_time(received_at, "received_at")
        monotonic = (
            time.monotonic()
            if monotonic_at is None
            else _finite_time(monotonic_at, "monotonic_at")
        )
        peer_label = f"{peer[0]}:{peer[1]}"
        with self._lock:
            if isinstance(packet, TelemetryPayload):
                self._ingest_telemetry(packet, peer_label, wall, monotonic)
            elif isinstance(packet, ThermalFrame):
                self._ingest_thermal(packet, peer_label, wall, monotonic)
            else:  # Defensive boundary for callbacks outside the strict decoder.
                raise TypeError(f"unsupported packet object: {type(packet).__name__}")
            self._revision += 1
            return self._revision

    def mark_peer_disconnected(
        self,
        peer: tuple[str, int],
        *,
        disconnected_at: float | None = None,
    ) -> int:
        wall = (
            time.time()
            if disconnected_at is None
            else _finite_time(disconnected_at, "disconnected_at")
        )
        peer_label = f"{peer[0]}:{peer[1]}"
        changed = False
        with self._lock:
            for record in self._records.values():
                if record.peer == peer_label and record.connected:
                    record.connected = False
                    record.disconnected_at = wall
                    changed = True
            if changed:
                self._revision += 1
            return self._revision

    def snapshot(
        self,
        *,
        now: float | None = None,
        monotonic_now: float | None = None,
    ) -> dict[str, object]:
        wall = time.time() if now is None else _finite_time(now, "now")
        monotonic = (
            time.monotonic()
            if monotonic_now is None
            else _finite_time(monotonic_now, "monotonic_now")
        )
        with self._lock:
            sensors = {
                sensor_id: self._snapshot_record(record, monotonic)
                for sensor_id, record in self._records.items()
            }
            statuses = [entry["status"] for entry in sensors.values()]
            live_count = statuses.count("LIVE")
            connected_count = sum(bool(entry["connected"]) for entry in sensors.values())
            if live_count == len(SENSOR_IDS):
                system = "ONLINE"
            elif connected_count > 0:
                system = "DEGRADED"
            else:
                system = "OFFLINE"
            return {
                "timestamp": wall,
                "revision": self._revision,
                "system": system,
                "device_health": copy.deepcopy(self._device_health),
                "sensors": sensors,
            }

    def latest_thermal_frame(self) -> ThermalFrame | None:
        """Return the immutable latest frame; raw bytes stay out of state JSON."""

        with self._lock:
            return self._latest_thermal_frame

    def _ingest_telemetry(
        self,
        packet: TelemetryPayload,
        peer: str,
        wall: float,
        monotonic: float,
    ) -> None:
        self._device_health = copy.deepcopy(packet.health)
        mmwave_valid = _mmwave_record_valid(packet)
        self._update(
            "mmwave",
            peer=peer,
            sequence=packet.header.sequence,
            uptime_ms=packet.uptime_ms,
            wall=wall,
            monotonic=monotonic,
            valid=mmwave_valid,
            error=None if mmwave_valid else "MMWAVE_VALUES_INVALID",
            values={
                # MR60's already-normalized boolean is authoritative when it
                # arrives.  No numeric occupancy threshold is invented here.
                "presence": packet.human_detected_raw,
                "presence_available": isinstance(packet.human_detected_raw, bool),
                "human_detected_raw": packet.human_detected_raw,
                "breath_phase": packet.breath_phase,
                "ts_monotonic_ms": packet.ts_monotonic_ms,
                "phase_age_ms": packet.phase_age_ms,
                "mmwave_sequence": packet.mmwave_sequence,
                "publication_sequence": packet.header.sequence,
                "breath_rate_raw": packet.breath_rate_raw,
                "session_id": packet.session_id,
                "respiration_rate_bpm": packet.respiration_rate_bpm,
                "heart_rate_bpm": packet.heart_rate_bpm,
                "respiration_valid": packet.valid["respiration"],
                "heart_valid": packet.valid["heart"],
            },
            device_id=packet.device_id,
            boot_id=packet.boot_id,
        )
        self._ingest_co2(packet, peer, wall, monotonic)
        self._update(
            "pir",
            peer=peer,
            sequence=packet.header.sequence,
            uptime_ms=packet.uptime_ms,
            wall=wall,
            monotonic=monotonic,
            valid=True,
            error=None,
            values={"motion": packet.pir_motion},
            device_id=packet.device_id,
            boot_id=packet.boot_id,
        )
        pir_record = self._records["pir"]
        pir_record.values.update(
            {
                "event_id": packet.pir_event_id,
                "last_transition_monotonic_ms": packet.pir_last_transition_monotonic_ms,
            }
        )

    def _ingest_co2(
        self,
        packet: TelemetryPayload,
        peer: str,
        wall: float,
        monotonic: float,
    ) -> None:
        """Separate packet reception, physical events, and throttled presentation."""

        record = self._records["co2"]
        record.connected = True
        record.valid = packet.valid["co2"]
        record.peer = peer
        record.device_id = packet.device_id
        record.boot_id = packet.boot_id
        record.last_received_at = wall
        record.last_received_monotonic = monotonic
        record.disconnected_at = None
        record.error = None if packet.valid["co2"] else "CO2_VALUE_INVALID"

        domain = {
            "sensor_model": packet.co2_sensor_model,
            "event_identity_class": packet.co2_event_identity_class,
            "preheat_complete": packet.co2_preheat_complete,
            "abc_enabled": packet.abc_enabled,
            "configured_range_ppm": packet.configured_range_ppm,
        }

        event_key = None
        if (
            packet.co2_measurement_event_valid is True
            and packet.boot_id is not None
            and packet.co2_measurement_event_id is not None
        ):
            event_key = (
                packet.device_id,
                packet.boot_id,
                packet.co2_measurement_event_id,
            )
        if event_key is not None and event_key != self._last_co2_event_key:
            self._last_co2_event_key = event_key
            self._co2_measurement_event_count += 1
            record.values.update(
                {
                    "latest_measurement_ppm": packet.co2_ppm,
                    "measurement_event_id": packet.co2_measurement_event_id,
                    "measurement_monotonic_ms": packet.co2_measurement_monotonic_ms,
                    "measurement_event_valid": True,
                    "measurement_event_count": self._co2_measurement_event_count,
                    **domain,
                }
            )
        elif not record.values:
            record.values = {
                "ppm": None,
                "latest_measurement_ppm": None,
                "measurement_event_id": packet.co2_measurement_event_id,
                "measurement_monotonic_ms": packet.co2_measurement_monotonic_ms,
                "measurement_event_valid": packet.co2_measurement_event_valid,
                "measurement_event_count": self._co2_measurement_event_count,
                **domain,
            }
        else:
            record.values.update(domain)

        if not packet.valid["co2"]:
            return
        due = (
            self._last_co2_value_monotonic is None
            or monotonic - self._last_co2_value_monotonic
            >= self.co2_update_interval_seconds
        )
        if not due:
            return
        record.sequence = packet.header.sequence
        record.source_uptime_ms = packet.uptime_ms
        record.values["ppm"] = packet.co2_ppm
        record.last_valid_at = wall
        self._last_co2_value_monotonic = monotonic

    def _ingest_thermal(
        self,
        packet: ThermalFrame,
        peer: str,
        wall: float,
        monotonic: float,
    ) -> None:
        self._latest_thermal_frame = packet
        self._update(
            "thermal",
            peer=peer,
            sequence=packet.header.sequence,
            uptime_ms=packet.uptime_ms,
            wall=wall,
            monotonic=monotonic,
            valid=True,
            error=None,
            values={
                "width": packet.width,
                "height": packet.height,
                "frame_sequence": packet.frame_sequence,
                "minimum_raw": packet.minimum_raw,
                "maximum_raw": packet.maximum_raw,
                "frame_available": True,
            },
        )

    def _update(
        self,
        sensor_id: str,
        *,
        peer: str,
        sequence: int,
        uptime_ms: int,
        wall: float,
        monotonic: float,
        valid: bool,
        error: str | None,
        values: dict[str, object],
        device_id: str | None = None,
        boot_id: str | None = None,
    ) -> None:
        record = self._records[sensor_id]
        record.connected = True
        record.valid = valid
        record.peer = peer
        record.sequence = sequence
        record.source_uptime_ms = uptime_ms
        record.device_id = device_id
        record.boot_id = boot_id
        record.last_received_at = wall
        record.last_received_monotonic = monotonic
        record.disconnected_at = None
        record.error = error
        record.values = values
        if valid:
            record.last_valid_at = wall

    def _snapshot_record(self, record: _SensorRecord, monotonic_now: float) -> dict[str, object]:
        if record.last_received_monotonic is None:
            age = None
            stale = False
            status = "NO_DATA"
        else:
            age = max(0.0, monotonic_now - record.last_received_monotonic)
            stale = age > record.ttl_seconds
            if not record.connected:
                status = "DISCONNECTED"
            elif stale:
                status = "STALE"
            elif not record.valid:
                status = "INVALID"
            else:
                status = "LIVE"
        values = copy.deepcopy(record.values)
        if record.sensor_id == "mmwave":
            # Compatibility alias; device_health is the single canonical source.
            values["health"] = copy.deepcopy(self._device_health)
        return {
            "sensor_id": record.sensor_id,
            "status": status,
            "connected": record.connected,
            "stale": stale,
            "valid": record.valid,
            "current": status == "LIVE",
            "ttl_seconds": record.ttl_seconds,
            "age_seconds": age,
            # CO2 publishes a usable value once/minute, while reception health
            # remains independently visible through last_received_at/status.
            "last_update": (
                record.last_valid_at
                if record.sensor_id == "co2"
                else record.last_received_at
            ),
            "last_received_at": record.last_received_at,
            "last_valid_at": record.last_valid_at,
            "disconnected_at": record.disconnected_at,
            "peer": record.peer,
            "sequence": record.sequence,
            "device_id": record.device_id,
            "boot_id": record.boot_id,
            "source_uptime_ms": record.source_uptime_ms,
            "last_received_monotonic": record.last_received_monotonic,
            "error": record.error,
            "values": values,
        }


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _mmwave_phase_source_structurally_valid(packet: TelemetryPayload) -> bool:
    """True when parsed B23 phase-source fields are structurally present.

    Freshness, continuity, and boot boundaries remain B23TeamRuntime
    fail-closed checks. This does not subtract phase_age_ms from time.
    """

    return (
        _finite_number(packet.breath_phase)
        and _finite_number(packet.ts_monotonic_ms)
        and _finite_number(packet.phase_age_ms)
        and isinstance(packet.mmwave_sequence, int)
        and not isinstance(packet.mmwave_sequence, bool)
    )


def _mmwave_record_valid(packet: TelemetryPayload) -> bool:
    """mmWave snapshot validity: phase source OR vendor respiration OR vendor heart.

    Vendor scalar telemetry is not mandatory for a valid B23 phase stream.
    """

    return (
        _mmwave_phase_source_structurally_valid(packet)
        or packet.valid["respiration"]
        or packet.valid["heart"]
    )


def _finite_time(value: float, field: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return converted
