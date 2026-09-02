"""Small transactional sqlite3 repository with an explicit schema contract."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2"
SCHEMA_PATH = Path(__file__).resolve().with_name("schema.sql")


class SQLiteRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._apply_schema()

    def persist(
        self,
        publication: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        _json(dict(publication))
        _json([dict(event) for event in events])
        snapshot_row = _snapshot_row(publication)
        event_rows = [_event_row(event, publication) for event in events]
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO sensor_snapshots (
                        timestamp, state_revision, publication_revision,
                        system, system_health,
                        mmwave_status, thermal_status, co2_status, pir_status,
                        mmwave_presence,
                        respiration_rate_bpm, heart_rate_bpm,
                        thermal_max_raw, thermal_max_temp_c,
                        thermal_human_probability, thermal_ai_state,
                        co2_ppm, pir_motion, risk_score, risk_level,
                        is_emergency, emergency_active, danger_transition_id,
                        danger_entered_at, alarm_acknowledged, alarm_acknowledged_at,
                        buzzer_active, latched_while_offline,
                        event_type, risk_reasons_json
                    ) VALUES (
                        :timestamp, :state_revision, :publication_revision,
                        :system, :system_health,
                        :mmwave_status, :thermal_status, :co2_status, :pir_status,
                        :mmwave_presence,
                        :respiration_rate_bpm, :heart_rate_bpm,
                        :thermal_max_raw, :thermal_max_temp_c,
                        :thermal_human_probability, :thermal_ai_state,
                        :co2_ppm, :pir_motion, :risk_score, :risk_level,
                        :is_emergency, :emergency_active, :danger_transition_id,
                        :danger_entered_at, :alarm_acknowledged, :alarm_acknowledged_at,
                        :buzzer_active, :latched_while_offline,
                        :event_type, :risk_reasons_json
                    )
                    ON CONFLICT(publication_revision) DO NOTHING
                    """,
                    snapshot_row,
                )
                self._connection.executemany(
                    """
                    INSERT INTO risk_events (
                        event_id, sequence, timestamp, event_type,
                        publication_revision, risk_score, risk_level,
                        system_health, details_json
                    ) VALUES (
                        :event_id, :sequence, :timestamp, :event_type,
                        :publication_revision, :risk_score, :risk_level,
                        :system_health, :details_json
                    )
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    event_rows,
                )

    def fetch_history(self, limit: int = 100) -> list[dict[str, Any]]:
        checked = _limit(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM sensor_snapshots ORDER BY id DESC LIMIT ?", (checked,)
            ).fetchall()
        return [_decode_snapshot(row) for row in rows]

    def fetch_events(self, limit: int = 100) -> list[dict[str, Any]]:
        checked = _limit(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM risk_events ORDER BY sequence DESC, rowid DESC LIMIT ?",
                (checked,),
            ).fetchall()
        return [_decode_event(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            self._ensure_open()
            snapshots = self._connection.execute(
                "SELECT COUNT(*) FROM sensor_snapshots"
            ).fetchone()[0]
            events = self._connection.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0]
        return {"snapshots": int(snapshots), "events": int(events)}

    def last_publication_revision(self) -> int:
        with self._lock:
            self._ensure_open()
            value = self._connection.execute(
                "SELECT COALESCE(MAX(publication_revision), 0) FROM sensor_snapshots"
            ).fetchone()[0]
        return int(value)

    def last_event_sequence(self) -> int:
        with self._lock:
            self._ensure_open()
            value = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM risk_events"
            ).fetchone()[0]
        return int(value)

    def update_emergency_state(
        self,
        publication_revision: int,
        emergency: Mapping[str, Any],
    ) -> None:
        state = _mapping(emergency)
        row = {
            "publication_revision": _integer_required(publication_revision, "publication_revision"),
            "emergency_active": int(bool(state.get("active"))),
            "danger_transition_id": _text_optional(state.get("transition_id")),
            "danger_entered_at": _finite_optional(state.get("entered_at")),
            "alarm_acknowledged": int(bool(state.get("acknowledged"))),
            "alarm_acknowledged_at": _finite_optional(state.get("acknowledged_at")),
            "buzzer_active": int(bool(state.get("buzzer_active"))),
            "latched_while_offline": int(bool(state.get("latched_while_offline"))),
        }
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    UPDATE sensor_snapshots
                    SET emergency_active = :emergency_active,
                        danger_transition_id = :danger_transition_id,
                        danger_entered_at = :danger_entered_at,
                        alarm_acknowledged = :alarm_acknowledged,
                        alarm_acknowledged_at = :alarm_acknowledged_at,
                        buzzer_active = :buzzer_active,
                        latched_while_offline = :latched_while_offline
                    WHERE publication_revision = :publication_revision
                    """,
                    row,
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _apply_schema(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connection:
            self._connection.executescript(schema)
            self._ensure_emergency_columns()
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        found = None if row is None else str(row[0])
        if found == "1":
            with self._connection:
                self._connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            found = SCHEMA_VERSION
        if found != SCHEMA_VERSION:
            self._connection.close()
            self._closed = True
            raise RuntimeError(
                f"unsupported SafeNest database schema: expected={SCHEMA_VERSION}, found={found}"
            )

    def _ensure_emergency_columns(self) -> None:
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(sensor_snapshots)").fetchall()
        }
        columns = {
            "emergency_active": "INTEGER NOT NULL DEFAULT 0",
            "danger_transition_id": "TEXT",
            "danger_entered_at": "REAL",
            "alarm_acknowledged": "INTEGER NOT NULL DEFAULT 0",
            "alarm_acknowledged_at": "REAL",
            "buzzer_active": "INTEGER NOT NULL DEFAULT 0",
            "latched_while_offline": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE sensor_snapshots ADD COLUMN {name} {definition}"
                )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLite repository is closed")


def _snapshot_row(publication: Mapping[str, Any]) -> dict[str, Any]:
    state = _mapping(publication.get("state"))
    sensors = _mapping(state.get("sensors"))
    ai = _mapping(_mapping(publication.get("ai")).get("ai"))
    risk = _mapping(publication.get("risk"))
    mmwave = _mapping(_mapping(sensors.get("mmwave")).get("values"))
    thermal = _mapping(_mapping(sensors.get("thermal")).get("values"))
    co2 = _mapping(_mapping(sensors.get("co2")).get("values"))
    pir = _mapping(_mapping(sensors.get("pir")).get("values"))
    thermal_ai = _mapping(ai.get("thermal"))
    emergency = _mapping(publication.get("emergency"))

    presence = None
    if mmwave.get("presence_available") is True and isinstance(mmwave.get("presence"), bool):
        presence = int(mmwave["presence"])
    motion = int(pir["motion"]) if isinstance(pir.get("motion"), bool) else None
    probabilities = _mapping_or_list(_mapping(thermal_ai.get("metadata")).get("probabilities"))
    human_probability = None
    if isinstance(probabilities, list) and len(probabilities) == 3:
        values = [_finite_optional(value) for value in probabilities]
        if all(value is not None and 0 <= value <= 1 for value in values):
            human_probability = min(1.0, float(values[1]) + float(values[2]))

    reasons = risk.get("reasons")
    reasons_list = list(reasons) if isinstance(reasons, (list, tuple)) else []
    row = {
        "timestamp": _finite_required(publication.get("timestamp"), "timestamp"),
        "state_revision": _integer_optional(state.get("revision")),
        "publication_revision": _integer_required(
            publication.get("publication_revision"), "publication_revision"
        ),
        "system": _text_optional(state.get("system")),
        "system_health": _text_optional(risk.get("system_health")),
        "mmwave_status": _sensor_status(sensors, "mmwave"),
        "thermal_status": _sensor_status(sensors, "thermal"),
        "co2_status": _sensor_status(sensors, "co2"),
        "pir_status": _sensor_status(sensors, "pir"),
        "mmwave_presence": presence,
        "respiration_rate_bpm": _finite_optional(mmwave.get("respiration_rate_bpm")),
        "heart_rate_bpm": _finite_optional(mmwave.get("heart_rate_bpm")),
        "thermal_max_raw": _integer_optional(thermal.get("maximum_raw")),
        "thermal_max_temp_c": None,
        "thermal_human_probability": human_probability,
        "thermal_ai_state": _text_optional(thermal_ai.get("state")),
        "co2_ppm": _finite_optional(co2.get("ppm")),
        "pir_motion": motion,
        "risk_score": _finite_optional(risk.get("risk_score")),
        "risk_level": _text_optional(risk.get("risk_level")),
        "is_emergency": int(bool(risk.get("is_emergency"))),
        "emergency_active": int(bool(emergency.get("active"))),
        "danger_transition_id": _text_optional(emergency.get("transition_id")),
        "danger_entered_at": _finite_optional(emergency.get("entered_at")),
        "alarm_acknowledged": int(bool(emergency.get("acknowledged"))),
        "alarm_acknowledged_at": _finite_optional(emergency.get("acknowledged_at")),
        "buzzer_active": int(bool(emergency.get("buzzer_active"))),
        "latched_while_offline": int(bool(emergency.get("latched_while_offline"))),
        "event_type": "SNAPSHOT",
        "risk_reasons_json": _json(reasons_list),
    }
    return row


def _event_row(event: Mapping[str, Any], publication: Mapping[str, Any]) -> dict[str, Any]:
    risk = _mapping(publication.get("risk"))
    return {
        "event_id": str(event.get("event_id", "")),
        "sequence": _integer_required(event.get("sequence"), "event sequence"),
        "timestamp": _finite_required(event.get("timestamp"), "event timestamp"),
        "event_type": str(event.get("event_type", "UNKNOWN")),
        "publication_revision": _integer_optional(publication.get("publication_revision")),
        "risk_score": _finite_optional(risk.get("risk_score")),
        "risk_level": _text_optional(risk.get("risk_level")),
        "system_health": _text_optional(risk.get("system_health")),
        "details_json": _json(dict(_mapping(event.get("details")))),
    }


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["mmwave_presence"] = _database_bool(result["mmwave_presence"])
    result["pir_motion"] = _database_bool(result["pir_motion"])
    result["is_emergency"] = bool(result["is_emergency"])
    result["emergency_active"] = bool(result["emergency_active"])
    result["alarm_acknowledged"] = bool(result["alarm_acknowledged"])
    result["buzzer_active"] = bool(result["buzzer_active"])
    result["latched_while_offline"] = bool(result["latched_while_offline"])
    result["risk_reasons"] = json.loads(result.pop("risk_reasons_json"))
    return result


def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["details"] = json.loads(result.pop("details_json"))
    return result


def _database_bool(value: object) -> bool | None:
    return None if value is None else bool(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_or_list(value: object) -> object:
    return value if isinstance(value, (Mapping, list)) else None


def _finite_required(value: object, field: str) -> float:
    converted = _finite_optional(value)
    if converted is None or converted < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return converted


def _finite_optional(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _integer_required(value: object, field: str) -> int:
    converted = _integer_optional(value)
    if converted is None or converted < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return converted


def _integer_optional(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_optional(value: object) -> str | None:
    return str(value) if value is not None else None


def _sensor_status(sensors: Mapping[str, Any], sensor_id: str) -> str | None:
    return _text_optional(_mapping(sensors.get(sensor_id)).get("status"))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ValueError("database query limit must be between 1 and 200")
    return value
