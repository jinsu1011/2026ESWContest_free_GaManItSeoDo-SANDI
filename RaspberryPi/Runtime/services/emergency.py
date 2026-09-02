"""Competition-safe emergency actions layered on the integrated runtime."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import os
import secrets
import threading
import time
from typing import Any, Mapping

from backend.store import RuntimeStore
from services.buzzer import BuzzerProtocol
from services.sms_service import (
    SMSProvider,
    SMSProviderError,
    NaverSensSMSProvider,
    mask_phone,
)


class EmergencyActionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds


class EmergencyActionService:
    """Own user-triggered actions without changing the Risk Engine state."""

    ALLOWED_VOICE_EVENTS = {
        "DANGER": "EMERGENCY_VOICE_TRIGGERED",
        "WARNING": "WARNING_VOICE_TRIGGERED",
        "119_START": "EMERGENCY_VOICE_119_STARTED",
        "119_COMPLETE": "EMERGENCY_VOICE_119_COMPLETED",
        "SMS_SUCCESS": "EMERGENCY_VOICE_SMS_SUCCEEDED",
        "SMS_FAILURE": "EMERGENCY_VOICE_SMS_FAILED",
        "SENSOR_OFFLINE": "SENSOR_OFFLINE_VOICE_TRIGGERED",
        "MUTED": "VOICE_MUTED",
        "UNMUTED": "VOICE_UNMUTED",
    }

    def __init__(
        self,
        store: RuntimeStore,
        *,
        sms_provider: SMSProvider | None = None,
        manager_phone: str | None = None,
        manager_name: str | None = None,
        room: str = "밀폐공간 A-01",
        sms_cooldown_seconds: float = 60.0,
        clock=time.time,
        monotonic=time.monotonic,
        buzzer: BuzzerProtocol | None = None,
    ) -> None:
        if sms_cooldown_seconds <= 0:
            raise ValueError("SMS cooldown must be positive")
        self.store = store
        self.sms_provider = sms_provider or NaverSensSMSProvider.from_env()
        self.manager_phone = manager_phone if manager_phone is not None else os.getenv("MANAGER_PHONE_NUMBER", "")
        self.manager_name = manager_name if manager_name is not None else os.getenv("MANAGER_NAME", "안전 담당자")
        self.room = room
        self.sms_cooldown_seconds = float(sms_cooldown_seconds)
        self.clock = clock
        self.monotonic = monotonic
        self._buzzer = buzzer
        self._lock = threading.RLock()
        self._simulation_token: str | None = None
        self._simulation_started_at: float | None = None
        self._last_sms_success_monotonic: float | None = None
        self._last_sms_attempt_monotonic: float | None = None
        self._last_sms_result: dict[str, Any] | None = None
        self._sms_idempotency: dict[str, dict[str, Any]] = {}
        self._connection_state: dict[str, str] = {}

    def start_119_simulation(self) -> dict[str, Any]:
        self._require_active_danger()
        with self._lock:
            if self._simulation_token is not None:
                raise EmergencyActionError(
                    "SIMULATION_IN_PROGRESS",
                    "119 simulation is already in progress",
                    retry_after_seconds=1.0,
                )
            token = secrets.token_urlsafe(18)
            self._simulation_token = token
            self._simulation_started_at = self.clock()
        event = self.store.record_event(
            "EMERGENCY_SIMULATION_STARTED",
            {"simulation_id": token, "disclaimer": self.disclaimer()},
        )
        return {
            "ok": True,
            "simulation_id": token,
            "status": "COUNTDOWN",
            "disclaimer": self.disclaimer(),
            "event_id": event["event_id"],
        }

    def complete_119_simulation(self, simulation_id: str) -> dict[str, Any]:
        token = str(simulation_id or "")
        with self._lock:
            if not self._simulation_token or not secrets.compare_digest(self._simulation_token, token):
                raise EmergencyActionError("SIMULATION_NOT_FOUND", "119 simulation token is invalid", status_code=404)
            started_at = self._simulation_started_at
            self._simulation_token = None
            self._simulation_started_at = None
        event = self.store.record_event(
            "EMERGENCY_SIMULATION_COMPLETED",
            {
                "simulation_id": token,
                "duration_seconds": max(0.0, self.clock() - float(started_at or self.clock())),
                "disclaimer": self.disclaimer(),
            },
        )
        return {
            "ok": True,
            "status": "COMPLETED",
            "message": "신고 접수 완료",
            "disclaimer": self.disclaimer(),
            "event_id": event["event_id"],
        }

    def acknowledge_alarm(self) -> dict[str, Any]:
        self._require_active_danger()
        snapshot = self.store.acknowledge_alarm()
        return {"ok": True, "emergency": snapshot, "message": "경고가 확인되었습니다."}

    def acknowledge_recovery(self) -> dict[str, Any]:
        try:
            snapshot = self.store.acknowledge_recovery()
        except RuntimeError as error:
            raise EmergencyActionError("RECOVERY_NOT_PENDING", str(error), status_code=409) from error
        return {"ok": True, "emergency": snapshot, "message": "센서 정상 복귀를 확인했습니다."}

    def send_manager_sms(self, *, idempotency_key: str | None = None) -> dict[str, Any]:
        self._require_active_danger()
        key = _clean_idempotency_key(idempotency_key)
        now_mono = self.monotonic()
        with self._lock:
            if key and key in self._sms_idempotency:
                cached = dict(self._sms_idempotency[key])
                cached["deduplicated"] = True
                self.store.record_event("MANAGER_SMS_DUPLICATE_IGNORED", {"idempotency_key": key})
                return cached
            if self._last_sms_success_monotonic is not None:
                elapsed = max(0.0, now_mono - self._last_sms_success_monotonic)
                if elapsed < self.sms_cooldown_seconds:
                    retry_after = self.sms_cooldown_seconds - elapsed
                    self.store.record_event(
                        "MANAGER_SMS_COOLDOWN_REJECTED",
                        {"retry_after_seconds": round(retry_after, 3)},
                    )
                    raise EmergencyActionError(
                        "SMS_COOLDOWN",
                        "manager SMS is cooling down",
                        retry_after_seconds=retry_after,
                    )
            if self._last_sms_attempt_monotonic is not None:
                elapsed = max(0.0, now_mono - self._last_sms_attempt_monotonic)
                if elapsed < 2.0:
                    raise EmergencyActionError(
                        "SMS_REQUEST_IN_PROGRESS",
                        "manager SMS request is being processed",
                        retry_after_seconds=2.0 - elapsed,
                    )
            self._last_sms_attempt_monotonic = now_mono

        self.store.record_event(
            "MANAGER_SMS_REQUESTED",
            {"provider": getattr(self.sms_provider, "name", "unknown")},
        )
        message = self._build_sms_message()
        try:
            delivery = self.sms_provider.send(to=self.manager_phone, message=message)
        except SMSProviderError as error:
            self.store.record_event(
                "MANAGER_SMS_FAILED",
                {"code": error.code, "message": str(error)},
            )
            raise EmergencyActionError(
                error.code,
                str(error),
                status_code=error.status_code,
            ) from error
        except Exception as error:
            self.store.record_event(
                "MANAGER_SMS_FAILED",
                {"code": "SMS_PROVIDER_ERROR", "message": f"{type(error).__name__}: {error}"},
            )
            raise EmergencyActionError(
                "SMS_PROVIDER_ERROR",
                "manager SMS provider failed",
                status_code=502,
            ) from error

        sent_at = float(delivery.sent_at)
        result = {
            "ok": True,
            "sent": True,
            "manager": {
                "name": self.manager_name,
                "phone_masked": mask_phone(self.manager_phone),
            },
            "provider": delivery.provider,
            "provider_request_id": delivery.request_id,
            "sent_at": sent_at,
            "cooldown_seconds": self.sms_cooldown_seconds,
            "deduplicated": False,
        }
        with self._lock:
            self._last_sms_success_monotonic = self.monotonic()
            self._last_sms_result = dict(result)
            if key:
                self._sms_idempotency[key] = dict(result)
        self.store.record_event(
            "MANAGER_SMS_SUCCEEDED",
            {
                "provider": delivery.provider,
                "provider_request_id": delivery.request_id,
                "manager_phone_masked": mask_phone(self.manager_phone),
                "sent_at": sent_at,
            },
        )
        return result

    def record_voice_event(self, action: str) -> dict[str, Any]:
        normalized = str(action or "").strip().upper()
        event_type = self.ALLOWED_VOICE_EVENTS.get(normalized)
        if event_type is None:
            raise EmergencyActionError("VOICE_ACTION_INVALID", "unsupported voice action", status_code=422)
        return {"ok": True, "event": self.store.record_event(event_type, {"action": normalized})}

    def record_client_connection(self, *, source: str, status: str) -> dict[str, Any]:
        source_value = str(source or "").strip().lower()
        status_value = str(status or "").strip().lower()
        if source_value not in {"websocket", "polling"} or status_value not in {"online", "offline"}:
            raise EmergencyActionError("CONNECTION_STATUS_INVALID", "unsupported client connection state", status_code=422)
        with self._lock:
            if self._connection_state.get(source_value) == status_value:
                return {"ok": True, "changed": False}
            self._connection_state[source_value] = status_value
        event_type = "WEBSOCKET_OFFLINE" if status_value == "offline" else "WEBSOCKET_ONLINE"
        event = self.store.record_event(event_type, {"source": source_value})
        return {"ok": True, "changed": True, "event": event}

    def buzzer(self, buzzer: BuzzerProtocol | None) -> None:
        self._buzzer = buzzer
        self.store.attach_buzzer(buzzer)

    def disclaimer(self) -> str:
        return "경진대회 시연용 모의 신고입니다. 실제 119 긴급 서비스와 연결되지 않습니다."

    def _require_active_danger(self) -> None:
        latest = self.store.latest()
        risk = _mapping(latest.get("risk")) if latest else {}
        emergency = self.store.emergency_snapshot()
        if risk.get("risk_level") != "DANGER" and not emergency.get("active"):
            raise EmergencyActionError("DANGER_NOT_ACTIVE", "active DANGER state is required", status_code=409)

    def _build_sms_message(self) -> str:
        latest = self.store.latest() or {}
        state = _mapping(latest.get("state"))
        risk = _mapping(latest.get("risk"))
        sensors = _mapping(state.get("sensors"))
        reasons = risk.get("reasons") if isinstance(risk.get("reasons"), (list, tuple)) else []
        reason_lines = "\n".join(f"- {str(reason)}" for reason in list(dict.fromkeys(reasons))[:4]) or "- 위험 원인 정보 없음"
        mmwave_values = _mapping(_mapping(sensors.get("mmwave")).get("values"))
        co2_values = _mapping(_mapping(sensors.get("co2")).get("values"))
        pir_component = _mapping(_mapping(risk.get("components")).get("pir"))
        no_motion = _mapping(pir_component.get("metadata")).get("no_motion_seconds")
        timestamp = _format_datetime(float(latest.get("timestamp", self.clock())))
        score = risk.get("risk_score")
        score_text = f"{float(score):.0f}" if isinstance(score, (int, float)) else "-"
        respiration = mmwave_values.get("respiration_rate_bpm")
        co2 = co2_values.get("ppm")
        critical = [
            f"호흡수: {float(respiration):.1f} rpm" if isinstance(respiration, (int, float)) else "호흡수: -",
            f"CO₂: {float(co2):.0f} ppm" if isinstance(co2, (int, float)) else "CO₂: -",
            f"무움직임: {float(no_motion):.0f}초" if isinstance(no_motion, (int, float)) else "무움직임: -",
        ]
        return (
            "[SafeNest 긴급 알림]\n\n"
            f"{self.room}에서 위험 상황이 감지되었습니다.\n\n"
            f"위험 단계: {risk.get('risk_level', 'DANGER')}\n"
            f"위험 점수: {score_text} / 100\n"
            f"감지 원인:\n{reason_lines}\n\n"
            + "\n".join(critical)
            + f"\n감지 시각: {timestamp}"
        )


def _clean_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 128:
        raise EmergencyActionError("IDEMPOTENCY_KEY_INVALID", "idempotency key is too long", status_code=422)
    return text


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _format_datetime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
