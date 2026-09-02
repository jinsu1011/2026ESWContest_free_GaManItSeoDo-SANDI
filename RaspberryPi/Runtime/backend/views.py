"""Pure response builders kept testable without importing FastAPI."""

from __future__ import annotations

import copy
import time
from typing import Any, Mapping

from backend.runtime_status import runtime_status_document


ROUTE_CONTRACTS = {
    "GET /display": "integrated LCD display page",
    "GET /admin": "integrated administrator login and management UI",
    "POST /api/auth/login": "administrator credential exchange for a signed token",
    "GET/POST /api/spaces": "authenticated space registry",
    "GET/PATCH/DELETE /api/spaces/{space_id}": "authenticated space management",
    "GET /guest/dashboard/{space_id}": "public read-only QR dashboard",
    "GET /api/guest/spaces/{space_id}": "public single-space live sensor view",
    "GET /api/thermal/{space_id}": "latest 80x62 thermal frame binary",
    "GET /api/qr/{space_id}.png": "QR code for the public single-space dashboard",
    "GET /api/portal/events": "authenticated event list for the administrator UI",
    "GET /dashboard": "responsive same-origin live monitoring dashboard",
    "GET /api/status": "full current system, risk, and sensor view",
    "GET /api/sensors": "sensor state with AI and risk component overlays",
    "GET /api/events": "bounded newest-first transition events",
    "GET /api/history": "newest-first persisted sensor and risk snapshots",
    "GET /api/state": "read-only LCD projection of current runtime state",
    "GET /api/emergency/state": "current alarm latch and buzzer state",
    "POST /api/emergency/contact": "server-side configured manager SMS request",
    "POST /api/emergency/acknowledge": "silence alarm without clearing risk",
    "POST /api/emergency/recovery/acknowledge": "dismiss recovered alarm banner without changing risk",
    "POST /api/emergency/voice": "log local voice guidance action",
    "POST /api/client-connection": "log dashboard connection state transitions",
    "GET /health": "process liveness and runtime readiness",
    "WS /ws": "current status publication stream",
}

DEMO_ROUTE_CONTRACTS = {
    "GET /control": "manual LCD demo control page",
    "GET /lcd/assets/{asset}": "LCD demo static asset",
    "POST /api/state": "manual LCD demo state update",
    "POST /api/emergency/119/simulation/start": "competition-only mock 119 countdown start",
    "POST /api/emergency/119/simulation/complete": "competition-only mock 119 completion",
}


def status_document(publication: Mapping[str, Any] | None) -> dict[str, Any]:
    if publication is None:
        runtime_status = runtime_status_document({}, {})
        return {
            "schema": "safenest.api.status.v1",
            "timestamp": time.time(),
            "revision": None,
            "system": "OFFLINE",
            "system_health": "FAILED",
            "risk": None,
            "emergency": _empty_emergency(),
            "offline": True,
            "device_health": None,
            "mmwave": None,
            "thermal": None,
            "co2": None,
            "pir": None,
            "ready": False,
            "runtime_status": runtime_status,
        }
    state = _mapping(publication.get("state"))
    risk = _mapping(publication.get("risk"))
    emergency = _mapping(publication.get("emergency"))
    ai = _mapping(_mapping(publication.get("ai")).get("ai"))
    sensors = _mapping(state.get("sensors"))
    components = _mapping(risk.get("components"))
    runtime_status = runtime_status_document(state, ai)
    document: dict[str, Any] = {
        "schema": "safenest.api.status.v1",
        "timestamp": publication.get("timestamp"),
        "revision": state.get("revision"),
        "publication_revision": publication.get("publication_revision"),
        "system": state.get("system"),
        "system_health": risk.get("system_health"),
        "device_health": copy.deepcopy(state.get("device_health")),
        "risk": copy.deepcopy(dict(risk)),
        "emergency": copy.deepcopy(dict(emergency)) if emergency else _empty_emergency(),
        "offline": state.get("system") != "ONLINE" or risk.get("system_health") == "FAILED",
        "ready": True,
        "runtime_status": copy.deepcopy(runtime_status),
    }
    for sensor_id in ("mmwave", "thermal", "co2", "pir"):
        document[sensor_id] = {
            "state": copy.deepcopy(dict(_mapping(sensors.get(sensor_id)))),
            "ai": copy.deepcopy(dict(_mapping(ai.get(sensor_id)))),
            "risk_component": copy.deepcopy(dict(_mapping(components.get(sensor_id)))),
            "runtime_status": copy.deepcopy(runtime_status["sensors"][sensor_id]),
        }
    return document


def sensors_document(publication: Mapping[str, Any] | None) -> dict[str, Any]:
    status = status_document(publication)
    return {
        "schema": "safenest.api.sensors.v1",
        "timestamp": status["timestamp"],
        "revision": status["revision"],
        "system": status["system"],
        "device_health": copy.deepcopy(status["device_health"]),
        "runtime_status": copy.deepcopy(status["runtime_status"]),
        "sensors": {
            sensor_id: status[sensor_id]
            for sensor_id in ("mmwave", "thermal", "co2", "pir")
        },
    }


def legacy_state_document(
    publication: Mapping[str, Any] | None,
    *,
    room: str,
) -> dict[str, Any]:
    status = status_document(publication)
    risk = status.get("risk") if isinstance(status.get("risk"), Mapping) else {}
    level = risk.get("risk_level")
    emergency_active = bool(risk.get("is_emergency")) or bool(status.get("emergency", {}).get("active"))
    if emergency_active:
        display_state = "emergency"
    elif level == "DANGER":
        display_state = "danger"
    elif level == "WARNING":
        display_state = "warning"
    elif level == "NORMAL":
        display_state = "normal-occupied" if bool(risk.get("presence_detected")) else "normal-empty"
    else:
        display_state = "offline"
    return {
        "state": display_state,
        "room": room,
        "revision": status.get("revision") or 0,
        "updated_at": int(float(status["timestamp"])),
        "sensors": sensors_document(publication)["sensors"],
        "risk": copy.deepcopy(dict(risk)),
        "emergency": copy.deepcopy(dict(_mapping(status.get("emergency")))),
        "runtime_status": copy.deepcopy(status["runtime_status"]),
    }


def events_document(
    events: list[dict[str, Any]],
    *,
    persistent: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "safenest.api.events.v1",
        "count": len(events),
        "events": copy.deepcopy(events),
        "persistence": "sqlite" if persistent else "memory_only_phase7",
    }


def history_document(history: list[dict[str, Any]], *, persistent: bool) -> dict[str, Any]:
    return {
        "schema": "safenest.api.history.v1",
        "count": len(history),
        "history": copy.deepcopy(history),
        "persistence": "sqlite" if persistent else "latest_only_memory",
    }


def health_document(
    diagnostics: Mapping[str, Any],
    receiver_stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "ready": bool(diagnostics.get("ready")),
        "publication_revision": diagnostics.get("publication_revision", 0),
        "event_count": diagnostics.get("event_count", 0),
        "last_error": copy.deepcopy(diagnostics.get("last_error")),
        "receiver": copy.deepcopy(dict(receiver_stats or {})),
        "database": copy.deepcopy(diagnostics.get("database")),
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _empty_emergency() -> dict[str, Any]:
    return {
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
        "buzzer": {
            "mode": "unconfigured",
            "available": False,
            "simulated": True,
            "active": False,
        },
    }
