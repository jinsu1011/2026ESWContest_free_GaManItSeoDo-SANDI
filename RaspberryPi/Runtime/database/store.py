"""RuntimeStore that mirrors publications and events to SQLite safely."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Mapping

from backend.store import RuntimeStore
from database.repository import SQLiteRepository


class PersistentRuntimeStore(RuntimeStore):
    def __init__(
        self,
        database_path: str | Path,
        *,
        event_capacity: int = 500,
        repository: SQLiteRepository | None = None,
        buzzer: Any | None = None,
    ) -> None:
        super().__init__(event_capacity=event_capacity, buzzer=buzzer)
        self._database_error: str | None = None
        try:
            self.repository = repository or SQLiteRepository(database_path)
        except Exception as error:
            self._database_error = f"{type(error).__name__}: {error}"
            self.repository = _UnavailableRepository(database_path, self._database_error)
        try:
            self._publication_revision = self.repository.last_publication_revision()
            self._event_sequence = self.repository.last_event_sequence()
            history = self.repository.fetch_history(1)
            if history:
                self._latest = _publication_baseline(history[0])
                self.restore_emergency(_mapping(self._latest.get("emergency")))
        except Exception as error:
            self._database_error = f"{type(error).__name__}: {error}"
        self._last_persisted_event_sequence = self._event_sequence
        self._persistence_lock = threading.RLock()

    def publish(
        self,
        state: Mapping[str, Any],
        ai: Mapping[str, Any],
        risk: Mapping[str, Any],
    ) -> dict[str, Any]:
        publication = super().publish(state, ai, risk)
        self._persist(publication)
        return publication

    def record_runtime_error(self, source: str, error: Exception | str) -> dict[str, Any]:
        event = super().record_runtime_error(source, error)
        latest = self.latest()
        if latest is not None and source != "sqlite":
            self._persist(latest)
        return event

    def record_event(
        self,
        event_type: str,
        details: Mapping[str, Any] | None = None,
        *,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        event = super().record_event(event_type, details, timestamp=timestamp)
        latest = self.latest()
        if latest is not None:
            self._persist(latest)
            try:
                self.repository.update_emergency_state(
                    int(latest["publication_revision"]),
                    self.emergency_snapshot(),
                )
            except Exception as error:
                self._database_error = f"{type(error).__name__}: {error}"
        return event

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return self.repository.fetch_events(limit)
        except Exception as error:
            self._database_error = f"{type(error).__name__}: {error}"
            return super().events(limit)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return self.repository.fetch_history(limit)
        except Exception as error:
            self._database_error = f"{type(error).__name__}: {error}"
            latest = self.latest()
            return [] if latest is None else [latest]

    def diagnostics(self) -> dict[str, Any]:
        result = super().diagnostics()
        try:
            counts = self.repository.counts()
        except Exception as error:
            self._database_error = f"{type(error).__name__}: {error}"
            counts = None
        result["database"] = {
            "path": self.repository.database_path,
            "available": self._database_error is None,
            "error": self._database_error,
            "counts": counts,
            "schema_version": "2",
        }
        return result

    def close(self) -> None:
        self.repository.close()

    def _persist(self, publication: Mapping[str, Any]) -> None:
        with self._persistence_lock:
            try:
                memory_events = super().events(200)
                new_events = [
                    event
                    for event in reversed(memory_events)
                    if int(event.get("sequence", 0)) > self._last_persisted_event_sequence
                ]
                self.repository.persist(publication, new_events)
                if new_events:
                    self._last_persisted_event_sequence = max(
                        int(event["sequence"]) for event in new_events
                    )
                self._database_error = None
            except Exception as error:
                self._database_error = f"{type(error).__name__}: {error}"
                super().record_runtime_error("sqlite", self._database_error)


class _UnavailableRepository:
    def __init__(self, database_path: str | Path, error: str) -> None:
        self.database_path = str(database_path)
        self.error = error

    def _raise(self, *_args, **_kwargs):
        raise RuntimeError(self.error)

    persist = fetch_events = fetch_history = counts = last_publication_revision = last_event_sequence = update_emergency_state = _raise

    def close(self) -> None:
        return None


def _publication_baseline(row: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = float(row["timestamp"])
    return {
        "timestamp": timestamp,
        "publication_revision": int(row["publication_revision"]),
        "state": {
            "timestamp": timestamp,
            "revision": row.get("state_revision"),
            "system": row.get("system"),
            "sensors": {
                sensor_id: {"status": row.get(f"{sensor_id}_status"), "values": {}}
                for sensor_id in ("mmwave", "thermal", "co2", "pir")
            },
        },
        "ai": {"timestamp": timestamp, "ai": {}},
        "risk": {
            "timestamp": timestamp,
            "risk_score": row.get("risk_score"),
            "risk_level": row.get("risk_level"),
            "system_health": row.get("system_health"),
            "is_emergency": bool(row.get("is_emergency")),
            "components": {},
        },
        "emergency": {
            "active": bool(row.get("emergency_active")) or row.get("risk_level") == "DANGER",
            "transition_id": row.get("danger_transition_id"),
            "entered_at": row.get("danger_entered_at"),
            "acknowledged": bool(row.get("alarm_acknowledged")),
            "acknowledged_at": row.get("alarm_acknowledged_at"),
            "buzzer_active": bool(row.get("buzzer_active")),
            "latched_while_offline": bool(row.get("latched_while_offline")),
        },
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
