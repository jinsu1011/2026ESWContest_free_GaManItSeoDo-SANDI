"""Thread-safe latest publication and bounded PHASE 7 transition events."""

from __future__ import annotations

from collections import deque
import copy
import json
import math
import threading
import time
import uuid
from typing import Any, Mapping


class RuntimeStore:
    def __init__(self, event_capacity: int = 500, *, buzzer: Any | None = None) -> None:
        if isinstance(event_capacity, bool) or not isinstance(event_capacity, int) or event_capacity <= 0:
            raise ValueError("event_capacity must be a positive integer")
        self._lock = threading.RLock()
        self._latest: dict[str, Any] | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=event_capacity)
        self._publication_revision = 0
        self._event_sequence = 0
        self._last_error: dict[str, Any] | None = None
        self._buzzer = buzzer
        self._emergency_state: dict[str, Any] = {
            "active": False,
            "transition_id": None,
            "entered_at": None,
            "acknowledged": False,
            "acknowledged_at": None,
            "cleared_at": None,
            "cleared_to": None,
            "recovery_pending": False,
            "recovery_acknowledged_at": None,
            "buzzer_active": False,
            "latched_while_offline": False,
        }

    def publish(
        self,
        state: Mapping[str, Any],
        ai: Mapping[str, Any],
        risk: Mapping[str, Any],
    ) -> dict[str, Any]:
        timestamp = _finite_timestamp(state.get("timestamp"))
        candidate = {
            "timestamp": timestamp,
            "state": copy.deepcopy(dict(state)),
            "ai": copy.deepcopy(dict(ai)),
            "risk": copy.deepcopy(dict(risk)),
        }
        _strict_json(candidate)
        with self._lock:
            previous = self._latest
            self._publication_revision += 1
            candidate["publication_revision"] = self._publication_revision
            self._record_transitions(previous, candidate)
            self._apply_emergency_transition(previous, candidate)
            candidate["emergency"] = self.emergency_snapshot()
            _strict_json(candidate)
            self._latest = candidate
            return copy.deepcopy(candidate)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._latest)

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("event limit must be between 1 and 200")
        with self._lock:
            return copy.deepcopy(list(reversed(self._events))[:limit])

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("history limit must be between 1 and 200")
        latest = self.latest()
        return [] if latest is None else [latest]

    def record_runtime_error(self, source: str, error: Exception | str) -> dict[str, Any]:
        detail = str(error)
        with self._lock:
            timestamp = time.time()
            event = self._append_event(
                timestamp,
                "RUNTIME_ERROR",
                {"source": str(source), "detail": detail[:1000]},
            )
            self._last_error = copy.deepcopy(event)
            return copy.deepcopy(event)

    def record_event(
        self,
        event_type: str,
        details: Mapping[str, Any] | None = None,
        *,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Append a user or integration event without changing risk state."""

        with self._lock:
            event = self._append_event(
                time.time() if timestamp is None else _finite_timestamp(timestamp),
                str(event_type),
                dict(details or {}),
            )
            latest_risk = _mapping(self._mapping_from_latest("risk"))
            event["risk_level"] = latest_risk.get("risk_level")
            event["risk_score"] = latest_risk.get("risk_score")
            _strict_json(event)
            return copy.deepcopy(event)

    def acknowledge_alarm(self) -> dict[str, Any]:
        """Silence the alarm only; the Risk Engine publication is untouched."""

        with self._lock:
            if not self._emergency_state["active"]:
                raise RuntimeError("no active DANGER alarm to acknowledge")
            was_active = bool(self._emergency_state["buzzer_active"])
            self._silence_buzzer()
            self._emergency_state["acknowledged"] = True
            self._emergency_state["acknowledged_at"] = time.time()
            event = self.record_event(
                "ALARM_ACKNOWLEDGED",
                {
                    "buzzer_was_active": was_active,
                    "transition_id": self._emergency_state["transition_id"],
                },
            )
            snapshot = self.emergency_snapshot()
            if self._latest is not None:
                self._latest["emergency"] = copy.deepcopy(snapshot)
            return snapshot | {"event_id": event["event_id"]}

    def acknowledge_recovery(self) -> dict[str, Any]:
        """Dismiss the recovered-sensor banner without changing risk state."""

        with self._lock:
            if self._emergency_state["active"]:
                raise RuntimeError("active DANGER alarm cannot be dismissed as recovered")
            if not self._emergency_state["recovery_pending"]:
                raise RuntimeError("no recovered DANGER alarm to acknowledge")
            acknowledged_at = time.time()
            self._emergency_state["recovery_pending"] = False
            self._emergency_state["recovery_acknowledged_at"] = acknowledged_at
            event = self.record_event(
                "EMERGENCY_RECOVERY_ACKNOWLEDGED",
                {"transition_id": self._emergency_state["transition_id"]},
                timestamp=acknowledged_at,
            )
            snapshot = self.emergency_snapshot()
            if self._latest is not None:
                self._latest["emergency"] = copy.deepcopy(snapshot)
            return snapshot | {"event_id": event["event_id"]}

    def attach_buzzer(self, buzzer: Any | None) -> None:
        """Attach GPIO/mock hardware after the store has been constructed."""

        with self._lock:
            self._buzzer = buzzer
            if self._emergency_state["active"] and not self._emergency_state["acknowledged"]:
                self._activate_buzzer()
                if self._latest is not None:
                    self._latest["emergency"] = self.emergency_snapshot()

    def restore_emergency(self, state: Mapping[str, Any] | None) -> None:
        """Restore the persisted acknowledgement/latch state on process start."""

        if not isinstance(state, Mapping):
            return
        with self._lock:
            for key in self._emergency_state:
                if key in state:
                    self._emergency_state[key] = copy.deepcopy(state[key])

    def emergency_snapshot(self) -> dict[str, Any]:
        with self._lock:
            buzzer = self._buzzer_status()
            snapshot = copy.deepcopy(self._emergency_state)
            snapshot["buzzer"] = buzzer
            snapshot["buzzer_active"] = bool(snapshot["buzzer_active"])
            return snapshot

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self._latest is not None,
                "publication_revision": self._publication_revision,
                "event_count": len(self._events),
                "last_error": copy.deepcopy(self._last_error),
                "emergency": self.emergency_snapshot(),
            }

    def _record_transitions(
        self,
        previous: Mapping[str, Any] | None,
        current: Mapping[str, Any],
    ) -> None:
        timestamp = float(current["timestamp"])
        current_risk = _mapping(current.get("risk"))
        if previous is None:
            self._append_event(
                timestamp,
                "SNAPSHOT_INITIALIZED",
                {
                    "risk_level": current_risk.get("risk_level"),
                    "system_health": current_risk.get("system_health"),
                },
            )
            return
        previous_risk = _mapping(previous.get("risk"))
        for field, event_type in (
            ("risk_level", "RISK_LEVEL_CHANGED"),
            ("system_health", "SYSTEM_HEALTH_CHANGED"),
        ):
            before = previous_risk.get(field)
            after = current_risk.get(field)
            if before != after:
                self._append_event(timestamp, event_type, {"from": before, "to": after})
        before_level = previous_risk.get("risk_level")
        after_level = current_risk.get("risk_level")
        if after_level == "WARNING" and before_level != "WARNING":
            self._append_event(timestamp, "WARNING_ENTERED", {"from": before_level, "to": after_level})
        if after_level == "NORMAL" and before_level in {"WARNING", "DANGER"}:
            self._append_event(timestamp, "NORMAL_RESTORED", {"from": before_level, "to": after_level})
        before_emergency = bool(previous_risk.get("is_emergency"))
        after_emergency = bool(current_risk.get("is_emergency"))
        if before_emergency != after_emergency:
            self._append_event(
                timestamp,
                "EMERGENCY_STARTED" if after_emergency else "EMERGENCY_CLEARED",
                {"is_emergency": after_emergency},
            )
        previous_state = _mapping(previous.get("state"))
        current_state = _mapping(current.get("state"))
        before_system = previous_state.get("system")
        after_system = current_state.get("system")
        if before_system != after_system:
            if after_system == "OFFLINE":
                gateway_event = "GATEWAY_OFFLINE"
            elif after_system == "ONLINE":
                gateway_event = "GATEWAY_ONLINE"
            elif after_system == "DEGRADED":
                gateway_event = "GATEWAY_DEGRADED"
            else:
                gateway_event = "GATEWAY_STATUS_CHANGED"
            self._append_event(timestamp, gateway_event, {"from": before_system, "to": after_system})
        previous_sensors = _mapping(previous_state.get("sensors"))
        current_sensors = _mapping(current_state.get("sensors"))
        for sensor_id in ("mmwave", "thermal", "co2", "pir"):
            before = _mapping(previous_sensors.get(sensor_id)).get("status")
            after = _mapping(current_sensors.get(sensor_id)).get("status")
            if before != after:
                self._append_event(
                    timestamp,
                    "SENSOR_STATUS_CHANGED",
                    {"sensor_id": sensor_id, "from": before, "to": after},
                )
                if before == "LIVE" and after in {"DISCONNECTED", "STALE", "INVALID"}:
                    self._append_event(
                        timestamp,
                        "SENSOR_OFFLINE",
                        {"sensor_id": sensor_id, "status": after},
                    )
                elif after == "LIVE" and before not in {None, "LIVE"}:
                    self._append_event(
                        timestamp,
                        "SENSOR_RECOVERED",
                        {"sensor_id": sensor_id, "from": before},
                    )

    def _apply_emergency_transition(
        self,
        previous: Mapping[str, Any] | None,
        current: Mapping[str, Any],
    ) -> None:
        timestamp = float(current["timestamp"])
        risk = _mapping(current.get("risk"))
        current_level = risk.get("risk_level")
        previous_risk = _mapping(previous.get("risk")) if previous else {}
        previous_level = previous_risk.get("risk_level")
        active = bool(self._emergency_state["active"])

        if current_level == "DANGER":
            if not active:
                transition_id = str(uuid.uuid4())
                self._emergency_state.update(
                    {
                        "active": True,
                        "transition_id": transition_id,
                        "entered_at": timestamp,
                        "acknowledged": False,
                        "acknowledged_at": None,
                        "cleared_at": None,
                        "cleared_to": None,
                        "recovery_pending": False,
                        "recovery_acknowledged_at": None,
                        "latched_while_offline": False,
                    }
                )
                self._activate_buzzer()
                self._append_event(
                    timestamp,
                    "DANGER_ENTERED",
                    {
                        "transition_id": transition_id,
                        "risk_score": risk.get("risk_score"),
                        "reasons": list(risk.get("reasons", ()))[:8],
                    },
                )
            return

        if current_level in {"WARNING", "NORMAL"} and active:
            transition_id = self._emergency_state.get("transition_id")
            self._silence_buzzer()
            self._emergency_state.update(
                {
                    "active": False,
                    "cleared_at": timestamp,
                    "cleared_to": current_level,
                    "recovery_pending": True,
                    "recovery_acknowledged_at": None,
                    "latched_while_offline": False,
                }
            )
            self._append_event(
                timestamp,
                "DANGER_CLEARED",
                {"transition_id": transition_id, "to": current_level, "from": previous_level or "DANGER"},
            )
            return

        if current_level is None and active:
            # Missing data is not a recovery. Keep the alarm latched and make
            # the offline condition visible until a live WARNING/NORMAL output.
            self._emergency_state["latched_while_offline"] = True

    def _append_event(
        self,
        timestamp: float,
        event_type: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._event_sequence += 1
        event = {
            "event_id": str(uuid.uuid4()),
            "sequence": self._event_sequence,
            "timestamp": float(timestamp),
            "event_type": event_type,
            "details": copy.deepcopy(dict(details)),
        }
        _strict_json(event)
        self._events.append(event)
        return event

    def _activate_buzzer(self) -> None:
        if self._buzzer is None:
            self._emergency_state["buzzer_active"] = False
            self._append_event(time.time(), "BUZZER_UNAVAILABLE", {"mode": "unconfigured"})
            return
        try:
            self._buzzer.activate()
            self._emergency_state["buzzer_active"] = True
            status = self._buzzer_status()
            self._append_event(
                time.time(),
                "BUZZER_ACTIVATED",
                {"mode": status.get("mode"), "simulated": status.get("simulated", False)},
            )
        except Exception as error:
            self._emergency_state["buzzer_active"] = False
            self._append_event(
                time.time(),
                "BUZZER_ERROR",
                {"message": f"{type(error).__name__}: {error}"},
            )

    def _silence_buzzer(self) -> None:
        if self._buzzer is not None:
            try:
                self._buzzer.silence()
            except Exception as error:
                self._append_event(
                    time.time(),
                    "BUZZER_ERROR",
                    {"message": f"{type(error).__name__}: {error}"},
                )
        self._emergency_state["buzzer_active"] = False

    def _buzzer_status(self) -> dict[str, Any]:
        if self._buzzer is None:
            return {
                "mode": "unconfigured",
                "available": False,
                "simulated": True,
                "active": bool(self._emergency_state["buzzer_active"]),
            }
        try:
            status = self._buzzer.status()
            return dict(status) if isinstance(status, Mapping) else {"mode": "unknown"}
        except Exception as error:
            return {"mode": "error", "available": False, "error": f"{type(error).__name__}: {error}"}

    def _mapping_from_latest(self, key: str) -> object:
        return self._latest.get(key) if self._latest else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError("publication timestamp must be finite and non-negative")
    return float(value)


def _strict_json(value: object) -> None:
    json.dumps(value, ensure_ascii=False, allow_nan=False)
