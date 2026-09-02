"""Admin/guest portal adapters for the competition web UI."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import struct
import threading
import time
from typing import Any, Mapping

from gateway.protocol import ThermalFrame


DEFAULT_SPACES = (
    {"id": "A01", "name": "밀폐공간 A-01", "nodeId": "SN-A01", "host": "127.0.0.1", "port": "8000"},
)
SPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class PortalStore:
    """Small JSON-backed space registry with atomic in-process updates."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, str]]:
        with self._lock:
            return copy.deepcopy(self._read())

    def get(self, space_id: str) -> dict[str, str] | None:
        return next((item for item in self.list() if item["id"] == space_id), None)

    def create(self, payload: Mapping[str, object]) -> dict[str, str]:
        name = _required(payload, "name", 80)
        node_id = _required(payload, "nodeId", 64)
        host = _required(payload, "host", 255)
        port = _port(payload.get("port"))
        requested_id = str(payload.get("id", "")).strip().upper()
        space_id = requested_id or _space_id(node_id)
        if not SPACE_ID_PATTERN.fullmatch(space_id):
            raise ValueError("공간 ID는 영문, 숫자, 밑줄, 하이픈만 사용할 수 있습니다.")
        with self._lock:
            spaces = self._read()
            if any(item["id"] == space_id or item["nodeId"].lower() == node_id.lower() for item in spaces):
                raise ValueError("이미 등록된 공간 ID 또는 센서 노드 ID입니다.")
            item = {"id": space_id, "name": name, "nodeId": node_id, "host": host, "port": port}
            spaces.append(item)
            self._write(spaces)
            return copy.deepcopy(item)

    def update(self, space_id: str, payload: Mapping[str, object]) -> dict[str, str]:
        with self._lock:
            spaces = self._read()
            for item in spaces:
                if item["id"] != space_id:
                    continue
                if "name" in payload:
                    item["name"] = _required(payload, "name", 80)
                self._write(spaces)
                return copy.deepcopy(item)
        raise KeyError(space_id)

    def delete(self, space_id: str) -> None:
        with self._lock:
            spaces = self._read()
            if len(spaces) <= 1:
                raise ValueError("최소 한 개의 공간은 유지해야 합니다.")
            updated = [item for item in spaces if item["id"] != space_id]
            if len(updated) == len(spaces):
                raise KeyError(space_id)
            self._write(updated)

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return [dict(item) for item in DEFAULT_SPACES]
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [dict(item) for item in DEFAULT_SPACES]
        if not isinstance(value, list):
            return [dict(item) for item in DEFAULT_SPACES]
        valid = [item for item in value if isinstance(item, dict) and SPACE_ID_PATTERN.fullmatch(str(item.get("id", "")))]
        return valid or [dict(item) for item in DEFAULT_SPACES]

    def _write(self, spaces: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(spaces, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class PortalAuth:
    """HMAC-signed short-lived admin tokens; no database dependency required.

    Fail-closed by design: the administrator credential exists only in the
    environment. There is no built-in default account, so a deployment that
    forgets ``SAFENEST_ADMIN_ID`` / ``SAFENEST_ADMIN_PASSWORD`` cannot be signed
    into with a publicly known password, and an empty submission cannot match an
    empty configured value either. The rest of the runtime keeps serving.
    """

    def __init__(self, *, admin_id: str | None = None, password: str | None = None, secret: str | None = None) -> None:
        self.admin_id = (admin_id if admin_id is not None else os.getenv("SAFENEST_ADMIN_ID", "")).strip()
        self.password = password if password is not None else os.getenv("SAFENEST_ADMIN_PASSWORD", "")
        auth_secret = secret or os.getenv("SAFENEST_AUTH_SECRET")
        self.secret = (auth_secret.encode("utf-8") if auth_secret else secrets.token_bytes(32))

    @property
    def configured(self) -> bool:
        """True only when both the administrator id and password are set."""
        return bool(self.admin_id) and bool(self.password)

    def login(self, admin_id: object, password: object) -> str | None:
        if not self.configured:
            return None
        if not isinstance(admin_id, str) or not isinstance(password, str):
            return None
        if not (hmac.compare_digest(admin_id, self.admin_id) and hmac.compare_digest(password, self.password)):
            return None
        expiry = int(time.time()) + 12 * 60 * 60
        body = f"{self.admin_id}:{expiry}".encode("utf-8")
        signature = hmac.new(self.secret, body, hashlib.sha256).digest()
        return _b64(body + b"." + signature)

    def verify(self, token: str) -> bool:
        if not self.configured:
            return False
        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if len(decoded) < 34 or decoded[-33:-32] != b".":
                return False
            body, signature = decoded[:-33], decoded[-32:]
            admin_id, expiry = body.decode("utf-8").rsplit(":", 1)
            expected = hmac.new(self.secret, body, hashlib.sha256).digest()
            return hmac.compare_digest(signature, expected) and admin_id == self.admin_id and int(expiry) >= int(time.time())
        except (ValueError, UnicodeDecodeError):
            return False


def portal_space(space: Mapping[str, str], status: Mapping[str, Any], *, offline_after_seconds: float = 30.0) -> dict[str, Any]:
    """Map the canonical status document to the legacy competition UI contract."""

    is_live_space = space.get("id") == "A01"
    if not is_live_space:
        return _offline_space(space)
    mmwave = _sensor_state(status, "mmwave")
    thermal = _sensor_state(status, "thermal")
    co2 = _sensor_state(status, "co2")
    pir = _sensor_state(status, "pir")
    values_seen = any(_values(item) for item in (mmwave, thermal, co2, pir))
    ages = [float(item["age_seconds"]) for item in (mmwave, thermal, co2, pir) if isinstance(item.get("age_seconds"), (int, float))]
    recently_seen = bool(ages) and min(ages) <= offline_after_seconds
    connected = values_seen and (recently_seen or any(bool(item.get("connected")) for item in (mmwave, thermal, co2, pir)))
    risk = status.get("risk") if isinstance(status.get("risk"), Mapping) else {}
    risk_score = risk.get("risk_score") if isinstance(risk.get("risk_score"), (int, float)) else 0
    mode = _mode(risk.get("risk_level"), bool(risk.get("is_emergency")), connected, _presence(status, pir))
    thermal_values = _values(thermal)
    max_raw = thermal_values.get("maximum_raw")
    min_raw = thermal_values.get("minimum_raw")
    return {
        **dict(space),
        "status": mode,
        "risk": round(float(risk_score)),
        "lastSeen": _last_seen((mmwave, thermal, co2, pir)),
        "reading": {
            "occupied": _presence(status, pir),
            "breathRate": _values(mmwave).get("respiration_rate_bpm"),
            "heartRate": _values(mmwave).get("heart_rate_bpm"),
            "co2": _values(co2).get("ppm") if _values(co2).get("ppm") is not None else _values(co2).get("latest_measurement_ppm"),
            "motion": bool(_values(pir).get("motion", False)),
            "motionlessSeconds": 0,
            "bodyTemperature": _raw_celsius(max_raw),
            "thermal": {
                "fresh": thermal.get("status") == "LIVE",
                "minC": _raw_celsius(min_raw),
                "maxC": _raw_celsius(max_raw),
                "sequence": thermal_values.get("frame_sequence"),
            },
        },
        "bridge": {
            "fresh": connected,
            "deviceId": mmwave.get("device_id") or space.get("nodeId"),
        },
    }


def portal_event(event: Mapping[str, Any], *, space_id: str = "A01") -> dict[str, Any]:
    event_type = str(event.get("event_type", "EVENT"))
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    level = "info"
    if "DANGER" in event_type or "EMERGENCY" in event_type:
        level = "emergency"
    elif "WARNING" in event_type:
        level = "warning"
    elif "OFFLINE" in event_type or "ERROR" in event_type:
        level = "offline"
    elif "NORMAL" in event_type or "ONLINE" in event_type:
        level = "normal"
    timestamp = event.get("timestamp")
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(timestamp))) if isinstance(timestamp, (int, float)) else time.strftime("%Y-%m-%dT%H:%M:%S"),
        "spaceId": space_id,
        "level": level,
        "message": event_type.replace("_", " "),
        "detail": " · ".join(f"{key}: {value}" for key, value in details.items()) or "SafeNest 시스템 이벤트",
    }


def thermal_payload(frame: ThermalFrame) -> bytes:
    """Return the exact binary layout consumed by thermal-client.js."""

    return struct.pack("!HHIIHH", frame.width, frame.height, frame.frame_sequence, frame.uptime_ms, frame.minimum_raw, frame.maximum_raw) + frame.pixel_bytes


def _sensor_state(status: Mapping[str, Any], sensor_id: str) -> Mapping[str, Any]:
    item = status.get(sensor_id)
    return item.get("state", {}) if isinstance(item, Mapping) and isinstance(item.get("state"), Mapping) else {}


def _values(state: Mapping[str, Any]) -> Mapping[str, Any]:
    return state.get("values", {}) if isinstance(state.get("values"), Mapping) else {}


def _presence(status: Mapping[str, Any], pir: Mapping[str, Any]) -> bool:
    thermal = status.get("thermal")
    ai = thermal.get("ai", {}) if isinstance(thermal, Mapping) and isinstance(thermal.get("ai"), Mapping) else {}
    if ai.get("state") in {"HUMAN_NORMAL", "HUMAN_FALL_PROXY", "HUMAN_FALL"}:
        return True
    mmwave = _sensor_state(status, "mmwave")
    presence = _values(mmwave).get("presence")
    return bool(presence) if presence is not None else bool(_values(pir).get("motion", False))


def _mode(level: object, emergency: bool, connected: bool, occupied: bool) -> str:
    if not connected:
        return "offline"
    if emergency:
        return "emergency"
    if level == "DANGER":
        return "danger"
    if level == "WARNING":
        return "warning"
    return "normal-occupied" if occupied else "normal-empty"


def _offline_space(space: Mapping[str, str]) -> dict[str, Any]:
    return {**dict(space), "status": "offline", "risk": None, "lastSeen": None, "reading": {}, "bridge": {"fresh": False, "deviceId": space.get("nodeId")}}


def _last_seen(states: tuple[Mapping[str, Any], ...]) -> str | None:
    timestamps = [float(item["last_received_at"]) for item in states if isinstance(item.get("last_received_at"), (int, float))]
    if not timestamps:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(max(timestamps)))


def _raw_celsius(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) * 0.1 - 273.15, 1)


def _required(payload: Mapping[str, object], key: str, maximum: int) -> str:
    value = str(payload.get(key, "")).strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{key} 값을 확인하세요.")
    return value


def _port(value: object) -> str:
    try:
        port = int(str(value))
    except ValueError as error:
        raise ValueError("port 값을 확인하세요.") from error
    if not 1 <= port <= 65535:
        raise ValueError("port는 1~65535여야 합니다.")
    return str(port)


def _space_id(node_id: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]", "", node_id).upper()
    if candidate.startswith("SN-"):
        candidate = candidate[3:]
    return candidate[:32] or f"N{int(time.time())}"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
