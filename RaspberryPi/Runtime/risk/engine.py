"""Fuse current state and PHASE 5 results using the frozen V4 risk contract."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping

from paths import RISK_CONFIG

CONFIG_PATH = RISK_CONFIG
SENSOR_ORDER = ("mmwave", "co2", "pir", "thermal")


@dataclass(frozen=True)
class RiskComponent:
    sensor_id: str
    available: bool
    score: float | None
    source: str
    state: str
    timestamp: float
    reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sensor_id not in SENSOR_ORDER:
            raise ValueError(f"unknown risk component: {self.sensor_id}")
        if self.source not in {"ai", "rule", "rule_fallback", "unavailable"}:
            raise ValueError(f"unsupported risk component source: {self.source}")
        if self.available == (self.source == "unavailable"):
            raise ValueError("component availability and source are inconsistent")
        if not _finite_number(self.timestamp) or float(self.timestamp) < 0:
            raise ValueError("component timestamp must be finite and non-negative")
        if self.available:
            if not _finite_number(self.score) or not 0.0 <= float(self.score) <= 1.0:
                raise ValueError("available component score must be finite in [0,1]")
        elif self.score is not None:
            raise ValueError("unavailable component score must be null")
        _ensure_json_safe(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskEvaluation:
    timestamp: float
    risk_score: float | None
    risk_level: str | None
    system_health: str
    degraded_mode: bool
    is_emergency: bool
    presence_detected: bool
    presence_source: str
    reasons: tuple[str, ...]
    component_scores: dict[str, float | None]
    component_status: dict[str, str]
    components: dict[str, dict[str, Any]]
    weights: dict[str, float]
    thresholds: dict[str, float]
    config_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SafeNestRiskEngine:
    """Risk calculation with explicit unavailable and degraded states."""

    def __init__(self, config_path: str | Path = CONFIG_PATH) -> None:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        v4_weights = config["risk"]["v4_weights"]
        self.weights = {
            "mmwave": float(v4_weights["S1_mmwave_apnea"]),
            "co2": float(v4_weights["S2_co2_enclosure"]),
            "pir": float(v4_weights["S3_pir_motion"]),
            "thermal": float(v4_weights["S4_thermal_posture"]),
        }
        if not math.isclose(sum(self.weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError("risk weights must sum to one")
        thresholds = config["risk"]["v4_thresholds"]
        self.warning_min = float(thresholds["caution_min"])
        self.danger_min = float(thresholds["danger_min"])
        if not 0 <= self.warning_min < self.danger_min <= 100:
            raise ValueError("invalid risk thresholds")
        self.respiration_min = float(config["respiration"]["normal_min_rpm"])
        self.respiration_max = float(config["respiration"]["normal_max_rpm"])
        self.no_motion_seconds = float(config["motion"]["pir_no_motion_seconds"])
        self.co2_warning = float(config["co2"]["warning_ppm"])
        self.co2_danger = float(config["co2"]["danger_ppm"])
        self.co2_slope_warning = float(config["co2"]["slope_warning_ppm_per_min"])
        self.config_status = str(config.get("status", "UNKNOWN"))
        self._no_motion_started_at: float | None = None
        self._co2_history: deque[tuple[float, float]] = deque(maxlen=30)
        self._last_co2_sequence: int | None = None

    def evaluate(
        self,
        state_snapshot: Mapping[str, Any],
        ai_output: Mapping[str, Any],
    ) -> RiskEvaluation:
        now = _timestamp(state_snapshot.get("timestamp", time.time()))
        sensors = state_snapshot.get("sensors")
        if not isinstance(sensors, Mapping):
            raise ValueError("state snapshot must contain sensors")
        ai_results = ai_output.get("ai")
        if not isinstance(ai_results, Mapping):
            raise ValueError("AI output must contain ai results")

        thermal = self._thermal_component(sensors.get("thermal", {}), ai_results.get("thermal", {}), now)
        mmwave = self._mmwave_component(sensors.get("mmwave", {}), ai_results.get("mmwave", {}), now)
        co2 = self._co2_component(sensors.get("co2", {}), ai_results.get("co2", {}), now)
        presence, presence_source, presence_reasons = self._presence(
            sensors.get("mmwave", {}), thermal
        )
        pir = self._pir_component(sensors.get("pir", {}), presence, now)

        reasons = list(presence_reasons)
        mm_values = _values(sensors.get("mmwave", {}))
        if bool(mm_values.get("presence_available")) and thermal.available:
            mm_presence = bool(mm_values.get("presence"))
            thermal_human = thermal.state in {"HUMAN_NORMAL", "HUMAN_FALL"}
            if mm_presence != thermal_human:
                reasons.append("MMWAVE_THERMAL_MISMATCH")

        return self.fuse(
            {"mmwave": mmwave, "co2": co2, "pir": pir, "thermal": thermal},
            timestamp=now,
            extra_reasons=reasons,
            presence_detected=presence,
            presence_source=presence_source,
        )

    def fuse(
        self,
        components: Mapping[str, RiskComponent],
        *,
        timestamp: float,
        extra_reasons: list[str] | tuple[str, ...] = (),
        presence_detected: bool = False,
        presence_source: str = "UNCONFIRMED",
    ) -> RiskEvaluation:
        now = _timestamp(timestamp)
        missing = set(SENSOR_ORDER) - set(components)
        if missing:
            raise ValueError(f"missing risk components: {sorted(missing)}")
        ordered = {name: components[name] for name in SENSOR_ORDER}
        available = [name for name, item in ordered.items() if item.available]
        unavailable = [name for name, item in ordered.items() if not item.available]
        fallback = [name for name, item in ordered.items() if item.source == "rule_fallback"]

        reasons = list(extra_reasons)
        for item in ordered.values():
            reasons.extend(item.reasons)
        reasons = list(dict.fromkeys(reasons))

        emergency = False
        thermal = ordered["thermal"]
        if (
            thermal.available
            and thermal.state == "HUMAN_FALL"
            and _finite_number(thermal.metadata.get("confidence"))
            and float(thermal.metadata["confidence"]) >= 0.8
        ):
            emergency = True
            reasons.insert(0, "EMERGENCY_HUMAN_FALL")
        mmwave = ordered["mmwave"]
        if mmwave.available and mmwave.state == "APNEA" and mmwave.metadata.get("apnea_verified") is True:
            emergency = True
            reasons.insert(0, "EMERGENCY_VERIFIED_APNEA")
        elif mmwave.available and mmwave.state == "APNEA":
            reasons.append("APNEA_UNVERIFIED_NO_OVERRIDE")

        if not available:
            score = None
            level = None
            health = "FAILED"
            reasons.insert(0, "ALL_RISK_COMPONENTS_UNAVAILABLE")
        elif emergency:
            score = 100.0
            level = "DANGER"
            health = "DEGRADED" if unavailable or fallback else "HEALTHY"
        else:
            valid_weight = sum(self.weights[name] for name in available)
            score = 100.0 * sum(
                float(ordered[name].score) * self.weights[name] / valid_weight
                for name in available
            )
            score = min(100.0, max(0.0, score))
            level = self.classify(score)
            health = "DEGRADED" if unavailable or fallback else "HEALTHY"

        statuses = {
            name: ("UNAVAILABLE" if not item.available else item.source.upper())
            for name, item in ordered.items()
        }
        return RiskEvaluation(
            timestamp=now,
            risk_score=score,
            risk_level=level,
            system_health=health,
            degraded_mode=health != "HEALTHY",
            is_emergency=emergency,
            presence_detected=bool(presence_detected),
            presence_source=str(presence_source),
            reasons=tuple(dict.fromkeys(reasons)),
            component_scores={name: item.score for name, item in ordered.items()},
            component_status=statuses,
            components={name: item.to_dict() for name, item in ordered.items()},
            weights=dict(self.weights),
            thresholds={"warning_min": self.warning_min, "danger_min": self.danger_min},
            config_status=self.config_status,
        )

    def classify(self, score: float) -> str:
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("risk score must be finite")
        if value >= self.danger_min:
            return "DANGER"
        if value >= self.warning_min:
            return "WARNING"
        return "NORMAL"

    def _thermal_component(self, sensor: Any, ai: Any, now: float) -> RiskComponent:
        if _status(sensor) != "LIVE":
            return _unavailable("thermal", now, f"THERMAL_SENSOR_{_status(sensor)}")
        parsed = _valid_ai(ai, now=now, ttl=_ttl(sensor, 3.0))
        if parsed is None:
            return _unavailable("thermal", now, "THERMAL_AI_UNAVAILABLE")
        score, state, timestamp, confidence, metadata = parsed
        if (
            state == "HUMAN_FALL_PROXY"
            and metadata.get("risk_authority") != "LIMITED_POSTURE_PROXY"
        ):
            return _unavailable(
                "thermal",
                now,
                "THERMAL_PROXY_RISK_AUTHORITY_MISSING",
            )
        if (
            metadata.get("safety_authority") is False
            and metadata.get("risk_authority") != "LIMITED_POSTURE_PROXY"
        ):
            return _unavailable(
                "thermal",
                now,
                "THERMAL_MODEL_SAFETY_AUTHORITY_UNVERIFIED",
            )
        return RiskComponent(
            "thermal", True, score, "ai", state, timestamp,
            metadata={**metadata, "confidence": confidence},
        )

    def _mmwave_component(self, sensor: Any, ai: Any, now: float) -> RiskComponent:
        if _status(sensor) != "LIVE":
            return _unavailable("mmwave", now, f"MMWAVE_SENSOR_{_status(sensor)}")
        parsed = _valid_ai(ai, now=now, ttl=_ttl(sensor, 3.0))
        if parsed is not None:
            score, state, timestamp, confidence, metadata = parsed
            return RiskComponent(
                "mmwave", True, score, "ai", state, timestamp,
                metadata={**metadata, "confidence": confidence},
            )
        values = _values(sensor)
        breath = values.get("respiration_rate_bpm")
        if not bool(values.get("respiration_valid")) or not _finite_number(breath) or float(breath) <= 0:
            return _unavailable("mmwave", now, "RESPIRATION_INPUT_UNAVAILABLE")
        normal = self.respiration_min <= float(breath) <= self.respiration_max
        return RiskComponent(
            "mmwave", True, 0.0 if normal else 0.75, "rule_fallback",
            "RESPIRATION_NORMAL" if normal else "RESPIRATION_ABNORMAL", now,
            reasons=() if normal else ("ABNORMAL_RESPIRATION_RPM",),
            metadata={
                "respiration_rate_bpm": float(breath),
                **_ai_debug_metadata(ai),
            },
        )

    def _co2_component(self, sensor: Any, ai: Any, now: float) -> RiskComponent:
        if _status(sensor) != "LIVE":
            return _unavailable("co2", now, f"CO2_SENSOR_{_status(sensor)}")
        values = _values(sensor)
        ppm = values.get("ppm")
        if not _finite_number(ppm) or float(ppm) < 0:
            return _unavailable("co2", now, "CO2_INPUT_UNAVAILABLE")
        sample_time = sensor.get("last_update") if isinstance(sensor, Mapping) else None
        sequence = sensor.get("sequence") if isinstance(sensor, Mapping) else None
        if sequence != self._last_co2_sequence and _finite_number(sample_time):
            self._co2_history.append((float(sample_time), float(ppm)))
            self._last_co2_sequence = sequence
        slope = None
        if len(self._co2_history) >= 2:
            elapsed = (self._co2_history[-1][0] - self._co2_history[0][0]) / 60.0
            if elapsed > 0:
                slope = (self._co2_history[-1][1] - self._co2_history[0][1]) / elapsed
        score = min(1.0, max(0.0, (float(ppm) - 500.0) / 2000.0))
        reasons: list[str] = []
        if float(ppm) >= self.co2_danger:
            reasons.append("HIGH_CO2_DANGER")
        elif float(ppm) >= self.co2_warning:
            reasons.append("HIGH_CO2_WARNING")
        if slope is not None and slope >= self.co2_slope_warning:
            reasons.append("FAST_CO2_RISE")
        ai_parsed = _valid_ai(ai, now=now, ttl=_ttl(sensor, 10.0))
        return RiskComponent(
            "co2", True, score, "rule_fallback" if ai_parsed is None else "rule",
            "CO2_DANGER" if float(ppm) >= self.co2_danger else (
                "CO2_WARNING" if float(ppm) >= self.co2_warning else "CO2_NORMAL"
            ),
            now,
            reasons=tuple(reasons),
            metadata={
                "ppm": float(ppm),
                "slope_ppm_per_min": slope,
                "ai_state": ai_parsed[1] if ai_parsed else None,
                "ai_error": _error(ai),
            },
        )

    def _pir_component(self, sensor: Any, presence: bool, now: float) -> RiskComponent:
        if _status(sensor) != "LIVE":
            self._no_motion_started_at = None
            return _unavailable("pir", now, f"PIR_SENSOR_{_status(sensor)}")
        values = _values(sensor)
        motion = values.get("motion")
        if not isinstance(motion, bool):
            self._no_motion_started_at = None
            return _unavailable("pir", now, "PIR_INPUT_UNAVAILABLE")
        sample_time = sensor.get("last_update", now) if isinstance(sensor, Mapping) else now
        if not _finite_number(sample_time) or float(sample_time) < 0:
            self._no_motion_started_at = None
            return _unavailable("pir", now, "PIR_INVALID_TIMESTAMP")
        timestamp = float(sample_time)
        if motion:
            self._no_motion_started_at = None
            return RiskComponent("pir", True, 0.0, "rule", "MOTION", timestamp)
        if not presence:
            self._no_motion_started_at = None
            return RiskComponent(
                "pir", True, 0.0, "rule", "NO_MOTION", timestamp,
                reasons=("PRESENCE_NOT_CONFIRMED",), metadata={"no_motion_seconds": 0.0},
            )
        if self._no_motion_started_at is None or timestamp < self._no_motion_started_at:
            self._no_motion_started_at = timestamp
        elapsed = max(0.0, timestamp - self._no_motion_started_at)
        long_no_motion = elapsed >= self.no_motion_seconds
        return RiskComponent(
            "pir", True, 1.0 if long_no_motion else 0.5, "rule",
            "LONG_NO_MOTION" if long_no_motion else "NO_MOTION", timestamp,
            reasons=("LONG_NO_MOTION",) if long_no_motion else ("NO_MOTION_DETECTED",),
            metadata={"no_motion_seconds": elapsed, "presence_confirmed": True},
        )

    @staticmethod
    def _presence(
        mmwave_sensor: Any,
        thermal: RiskComponent,
    ) -> tuple[bool, str, tuple[str, ...]]:
        values = _values(mmwave_sensor)
        if _status(mmwave_sensor) == "LIVE" and values.get("presence_available") is True:
            return bool(values.get("presence")), "MMWAVE", ("PRESENCE_FROM_MMWAVE",)
        if thermal.available and thermal.state in {"HUMAN_NORMAL", "HUMAN_FALL"}:
            return True, "THERMAL", ("PRESENCE_FROM_THERMAL",)
        if thermal.available:
            return False, "THERMAL", ()
        return False, "UNCONFIRMED", ("PRESENCE_UNCONFIRMED",)


def _valid_ai(
    ai: Any,
    *,
    now: float | None = None,
    ttl: float | None = None,
) -> tuple[float, str, float, float, dict[str, Any]] | None:
    if not isinstance(ai, Mapping) or ai.get("available") is not True:
        return None
    score = ai.get("score")
    confidence = ai.get("confidence")
    timestamp = ai.get("timestamp")
    if (
        not _finite_number(score)
        or not 0 <= float(score) <= 1
        or not _finite_number(confidence)
        or not 0 <= float(confidence) <= 1
        or not _finite_number(timestamp)
        or float(timestamp) < 0
    ):
        return None
    if now is not None and ttl is not None:
        age = float(now) - float(timestamp)
        if age > float(ttl) or age < -1.0:
            return None
    metadata = ai.get("metadata")
    return float(score), str(ai.get("state", "UNKNOWN")), float(timestamp), float(confidence), (
        dict(metadata) if isinstance(metadata, Mapping) else {}
    )


def _unavailable(sensor_id: str, now: float, reason: str) -> RiskComponent:
    return RiskComponent(sensor_id, False, None, "unavailable", "UNAVAILABLE", now, (reason,))


def _status(sensor: Any) -> str:
    return str(sensor.get("status", "NO_DATA")) if isinstance(sensor, Mapping) else "NO_DATA"


def _values(sensor: Any) -> Mapping[str, Any]:
    values = sensor.get("values", {}) if isinstance(sensor, Mapping) else {}
    return values if isinstance(values, Mapping) else {}


def _error(ai: Any) -> str | None:
    return str(ai.get("error")) if isinstance(ai, Mapping) and ai.get("error") else None


def _ai_debug_metadata(ai: Any) -> dict[str, Any]:
    metadata = ai.get("metadata") if isinstance(ai, Mapping) else None
    if not isinstance(metadata, Mapping):
        metadata = {}
    missing = metadata.get("missing")
    return {
        "ai_error": _error(ai),
        "canonical_window_status": metadata.get("canonical_window_status"),
        "suppression_reason": metadata.get("suppression_reason"),
        "missing": list(missing) if isinstance(missing, list) else missing,
    }


def _ttl(sensor: Any, default: float) -> float:
    value = sensor.get("ttl_seconds") if isinstance(sensor, Mapping) else None
    return float(value) if _finite_number(value) and float(value) > 0 else default


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _timestamp(value: object) -> float:
    if not _finite_number(value) or float(value) < 0:
        raise ValueError("timestamp must be finite and non-negative")
    return float(value)


def _ensure_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _ensure_json_safe(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("metadata key must be a string")
            _ensure_json_safe(item)
        return
    raise ValueError(f"metadata contains non-JSON value {type(value).__name__}")
