"""SafeNest risk formula v1 - first pass authored for the current runtime.

This deliberately does not reuse ``sources/ondevice_ai/risk/risk_config.json``
(the frozen legacy V4 contract).  It is calibrated against what this repository
can actually prove today:

* CO2 is the only continuously trustworthy live signal and is the primary
  enclosed-space hazard, so it carries a full share.
* The active public-SDT FP32 thermal model decides person vs empty only.
  Standing vs lying for risk is the bbox overlay on that presence bit.
  ``HUMAN_FALL_PROXY`` scores a bounded 0.4 and cannot assert a real fall
  or emergency.
* mmWave M-N9 is ``DEVICE_VALIDATED: NO`` and emits an ``APNEA-proxy`` class, so
  it carries a reduced share. Unverified proxy cannot raise caution and never
  raises DANGER by itself.
* PIR is corroborating only, and becomes *unavailable* rather than 0.0 when
  presence is unconfirmed - scoring it 0.0 would silently lower the total.
* WARNING is not a band of R. It is a separate CO2-only caution formula: after
  the room baseline locks, a plus-only rise of 500 ppm from that first value,
  or a canonical 150 s slope of 50 ppm/min, publishes WARNING. Neither trip
  fires while the room is still localizing. Other sensors cannot raise caution.

Three properties the legacy weighted sum lacked and this one has:

1. ``escalation floors`` - one severe signal cannot be diluted to NORMAL by
   three calm ones. DANGER/EMERGENCY floors stay on the overall formula;
   WARNING floors belong only to the CO2 caution formula.
2. ``evidence sufficiency`` - NORMAL is only published when the available
   components carry at least ``minimum_effective_weight`` of the total weight;
   otherwise the level is ``INDETERMINATE``.
3. ``decisiveness gating`` - a 3-class INT8 head whose top two probabilities are
   within ``minimum_top_two_margin`` is treated as no decision at all, instead
   of being scored as if it had decided.
4. ``occupancy gate`` - DANGER/EMERGENCY require ``presence_detected is True``.
   Vacant or unconfirmed rooms keep only NORMAL and CO2 WARNING. C-B6 occupancy
   does not drive this gate. An empty-room 5000 ppm trip is demoted to WARNING.

The output document is a superset of the legacy one, so ``backend.store``,
``backend.views`` and ``database.repository`` consume it unchanged.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from risk.engine import RiskComponent, _ensure_json_safe, _finite_number, _timestamp

CONFIG_PATH = Path(__file__).resolve().parent / "risk_formula_v1.json"
SENSOR_ORDER = ("mmwave", "co2", "pir", "thermal")
LEVEL_ORDER = ("NORMAL", "WARNING", "DANGER")
CAUTION_FLOOR_LABELS = frozenset({"co2_relative_warning", "co2_fast_rise"})
PERSON_DANGER_FLOOR_LABELS = frozenset({
    "thermal_fall_confident",
    "mmwave_apnea_hardware_verified",
    "mmwave_apnea_proxy_sustained",
    "co2_immediate_danger",
})


@dataclass(frozen=True)
class RiskEvaluationV1:
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
    # v1 additions
    formula_id: str
    formula_version: str
    score_level: str | None
    level_source: str
    effective_weight: float
    evidence_sufficient: bool
    escalation_floors: tuple[str, ...] = ()
    caution_formula_id: str = ""
    caution_active: bool = False
    caution_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SafeNestRiskFormulaV1:
    """Overall fusion (NORMAL/DANGER/EMERGENCY) plus a CO2-only caution formula."""

    def __init__(self, config_path: str | Path = CONFIG_PATH) -> None:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.config = config
        self.formula_id = str(config["formula_id"])
        self.formula_version = str(config["formula_version"])
        self.config_status = str(config["status"])
        self.weights = {name: float(config["weights"][name]) for name in SENSOR_ORDER}
        if not math.isclose(sum(self.weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError("risk weights must sum to one")
        self.warning_min = float(config["thresholds"]["warning_min"])
        self.danger_min = float(config["thresholds"]["danger_min"])
        if not 0 <= self.warning_min < self.danger_min <= 100:
            raise ValueError("invalid risk thresholds")
        self.minimum_effective_weight = float(config["evidence"]["minimum_effective_weight"])
        self.insufficient_level = str(config["evidence"]["insufficient_evidence_level"])
        self.min_confidence = float(config["ai_acceptance"]["minimum_confidence"])
        self.mmwave_neural_trust = str(config["mmwave"].get("neural_trust", "TRUSTED"))
        if self.mmwave_neural_trust not in {"OBSERVE_ONLY", "TRUSTED"}:
            raise ValueError("mmwave.neural_trust must be OBSERVE_ONLY or TRUSTED")
        self.min_margin = float(config["ai_acceptance"]["minimum_top_two_margin"])

        self._thermal = config["thermal"]
        self._mmwave = config["mmwave"]
        self._co2 = config["co2"]
        self._caution = config.get("caution_formula") or {}
        self._pir = config["pir"]
        self._floors = config["escalation_floors"]
        self._emergency = config["emergency_overrides"]
        self.caution_formula_id = str(
            self._caution.get("formula_id", "SAFENEST_CAUTION_CO2_V1")
        )
        self.caution_delta_enter = float(
            self._caution.get(
                "baseline_delta_enter_ppm",
                self._co2["baseline_delta_warning_ppm"],
            )
        )
        self.caution_slope_ppm_per_min = float(
            self._caution.get(
                "slope_caution_ppm_per_min",
                self._co2["slope_danger_ppm_per_min"],
            )
        )
        self.caution_requires_baseline_lock = bool(
            self._caution.get("requires_baseline_lock", True)
        )
        caution_sensors = tuple(self._caution.get("sensors", ("co2",)))
        if caution_sensors != ("co2",):
            raise ValueError("caution formula may only use CO2")
        warning_floors = {name for name, level in self._floors.items() if level == "WARNING"}
        if warning_floors - CAUTION_FLOOR_LABELS:
            raise ValueError("WARNING floors must belong to the CO2 caution formula")

        self._co2_curve: list[tuple[float, float]] = [
            (float(ppm), float(score)) for ppm, score in self._co2["curve_ppm_to_score"]
        ]
        self._co2_history: deque[tuple[float, float]] = deque(
            maxlen=int(self._co2["history_samples"])
        )
        self._last_co2_sequence: int | None = None
        self._no_motion_started_at: float | None = None
        self._apnea_streak = 0
        self._respiration_abnormal_streak = 0

    # ----------------------------------------------------------------- public
    def evaluate(
        self,
        state_snapshot: Mapping[str, Any],
        ai_output: Mapping[str, Any],
    ) -> RiskEvaluationV1:
        now = _timestamp(state_snapshot.get("timestamp", time.time()))
        sensors = state_snapshot.get("sensors")
        if not isinstance(sensors, Mapping):
            raise ValueError("state snapshot must contain sensors")
        ai_results = ai_output.get("ai")
        if not isinstance(ai_results, Mapping):
            raise ValueError("AI output must contain ai results")

        floors: list[str] = []
        emergencies: list[str] = []

        thermal = self._thermal_component(
            sensors.get("thermal", {}), ai_results.get("thermal", {}), now, floors, emergencies
        )
        mmwave = self._mmwave_component(
            sensors.get("mmwave", {}), ai_results.get("mmwave", {}), now, floors, emergencies
        )
        co2 = self._co2_component(
            sensors.get("co2", {}), ai_results.get("co2", {}), now, floors, emergencies
        )
        presence, presence_source, presence_reasons = self._presence(
            sensors.get("mmwave", {}), thermal
        )
        pir = self._pir_component(sensors.get("pir", {}), presence, now, floors)

        reasons = list(presence_reasons)
        mm_values = _values(sensors.get("mmwave", {}))
        if mm_values.get("presence_available") is True and thermal.available:
            if bool(mm_values.get("presence")) != (
                thermal.state in tuple(self._thermal["human_states"])
            ):
                reasons.append("MMWAVE_THERMAL_MISMATCH")

        return self.fuse(
            {"mmwave": mmwave, "co2": co2, "pir": pir, "thermal": thermal},
            timestamp=now,
            extra_reasons=reasons,
            presence_detected=presence,
            presence_source=presence_source,
            floors=floors,
            emergencies=emergencies,
        )

    def fuse(
        self,
        components: Mapping[str, RiskComponent],
        *,
        timestamp: float,
        extra_reasons: Sequence[str] = (),
        presence_detected: bool = False,
        presence_source: str = "UNCONFIRMED",
        floors: Sequence[str] = (),
        emergencies: Sequence[str] = (),
    ) -> RiskEvaluationV1:
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

        occupied = presence_detected is True
        working_floors = list(floors)
        working_emergencies = list(emergencies)
        vacant_co2_extreme = False
        if not occupied:
            vacant_co2_extreme = "co2_immediate_danger" in working_floors
            stripped = [
                label for label in working_floors if label in PERSON_DANGER_FLOOR_LABELS
            ]
            working_floors = [
                label for label in working_floors if label not in PERSON_DANGER_FLOOR_LABELS
            ]
            working_emergencies = []
            if stripped:
                reasons.append("VACANT_DANGER_SUPPRESSED")

        effective_weight = sum(self.weights[name] for name in available)
        evidence_sufficient = effective_weight >= self.minimum_effective_weight

        overall_floor = None
        caution_level = None
        caution_reasons: list[str] = []
        for label in working_floors:
            candidate = self._floors.get(label)
            if candidate == "WARNING":
                caution_level = "WARNING"
                caution_reasons.append(label)
                reasons.append(f"FLOOR_{label.upper()}")
            elif candidate in LEVEL_ORDER:
                overall_floor = _max_level(overall_floor, candidate)
                reasons.append(f"FLOOR_{label.upper()}")

        if vacant_co2_extreme:
            caution_level = "WARNING"
            caution_reasons.append("co2_immediate_danger")
            reasons.append("VACANT_CO2_EMERGENCY_DEMOTED")

        emergency = bool(working_emergencies)
        for label in working_emergencies:
            reasons.insert(0, f"EMERGENCY_{label.upper()}")

        if not available:
            score: float | None = None
            score_level: str | None = None
            overall_level = overall_floor
            level = _max_level(overall_level, caution_level)
            if overall_floor:
                level_source = "FLOOR"
            elif caution_level:
                level_source = "CAUTION"
            else:
                level_source = "NO_COMPONENTS"
            health = "FAILED"
            reasons.insert(0, "ALL_RISK_COMPONENTS_UNAVAILABLE")
        else:
            score = 100.0 * sum(
                float(ordered[name].score) * self.weights[name] / effective_weight
                for name in available
            )
            score = round(min(100.0, max(0.0, score)), 4)
            score_level = self.classify(score)
            if not occupied and score_level == "DANGER":
                score_level = "NORMAL"
                reasons.append("VACANT_SCORE_DANGER_SUPPRESSED")
            overall_level = _max_level(score_level, overall_floor)
            level = _max_level(overall_level, caution_level)
            if overall_floor and overall_floor == level and overall_floor != score_level:
                level_source = "FLOOR"
            elif caution_level and caution_level == level and overall_level != "DANGER":
                level_source = "CAUTION"
            else:
                level_source = "SCORE"
            health = "DEGRADED" if unavailable or fallback else "HEALTHY"

        if emergency:
            score = 100.0
            level = "DANGER"
            score_level = score_level or "DANGER"
            level_source = "EMERGENCY"

        if level is not None and not evidence_sufficient and level == "NORMAL":
            # Never claim NORMAL from a minority of the sensor set.
            level = self.insufficient_level
            level_source = "INSUFFICIENT_EVIDENCE"
            reasons.append("INSUFFICIENT_EVIDENCE_FOR_NORMAL")
            health = "DEGRADED"

        statuses = {
            name: ("UNAVAILABLE" if not item.available else item.source.upper())
            for name, item in ordered.items()
        }
        return RiskEvaluationV1(
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
            formula_id=self.formula_id,
            formula_version=self.formula_version,
            score_level=score_level,
            level_source=level_source,
            effective_weight=round(effective_weight, 4),
            evidence_sufficient=evidence_sufficient,
            escalation_floors=tuple(dict.fromkeys(
                [*working_floors, *(("co2_immediate_danger",) if vacant_co2_extreme else ())]
            )),
            caution_formula_id=self.caution_formula_id,
            caution_active=caution_level == "WARNING",
            caution_reasons=tuple(dict.fromkeys(caution_reasons)),
        )

    def classify(self, score: float) -> str:
        """Overall score bands: NORMAL or DANGER. WARNING is not a band of R."""

        value = float(score)
        if not math.isfinite(value):
            raise ValueError("risk score must be finite")
        if value >= self.danger_min:
            return "DANGER"
        return "NORMAL"

    # ------------------------------------------------------------- components
    def _thermal_component(
        self,
        sensor: Any,
        ai: Any,
        now: float,
        floors: list[str],
        emergencies: list[str],
    ) -> RiskComponent:
        status = _status(sensor)
        if status != "LIVE":
            return _unavailable("thermal", now, f"THERMAL_SENSOR_{status}")
        decision = self._decision(ai, now=now, ttl=_ttl(sensor, 3.0))
        if decision is None:
            return _unavailable("thermal", now, _ai_block_reason(ai, "THERMAL"))
        state, confidence, timestamp, metadata = decision
        limited_proxy = metadata.get("risk_authority") == "LIMITED_POSTURE_PROXY"
        if metadata.get("safety_authority") is False and not limited_proxy:
            return _unavailable(
                "thermal",
                now,
                "THERMAL_MODEL_SAFETY_AUTHORITY_UNVERIFIED",
            )
        reasons: list[str] = []
        if state == "HUMAN_FALL_PROXY":
            if not limited_proxy:
                return _unavailable(
                    "thermal",
                    now,
                    "THERMAL_PROXY_RISK_AUTHORITY_MISSING",
                )
            score = float(self._thermal["class_scores"][state])
            reasons.append("THERMAL_FALL_PROXY_LIMITED_RISK_NO_EMERGENCY")
            return RiskComponent(
                "thermal", True, score, "ai", state, timestamp,
                reasons=tuple(reasons),
                metadata={**metadata, "confidence": confidence},
            )
        if state == "HUMAN_FALL":
            if confidence >= float(self._thermal["fall_high_confidence_min"]):
                score = float(self._thermal["fall_score_high_confidence"])
                floors.append("thermal_fall_confident")
                if self._emergency.get("thermal_fall_confident"):
                    emergencies.append("human_fall")
            elif confidence >= float(self._thermal["fall_medium_confidence_min"]):
                score = float(self._thermal["fall_score_medium_confidence"])
                reasons.append("THERMAL_FALL_MEDIUM_CONFIDENCE")
            else:
                score = float(self._thermal["fall_score_low_confidence"])
                reasons.append("THERMAL_FALL_LOW_CONFIDENCE")
        else:
            mapped = self._thermal["class_scores"].get(state)
            if mapped is None:
                return _unavailable("thermal", now, f"THERMAL_AI_UNKNOWN_CLASS_{state}")
            score = float(mapped)
        return RiskComponent(
            "thermal", True, score, "ai", state, timestamp,
            reasons=tuple(reasons),
            metadata={**metadata, "confidence": confidence},
        )

    def _mmwave_component(
        self,
        sensor: Any,
        ai: Any,
        now: float,
        floors: list[str],
        emergencies: list[str],
    ) -> RiskComponent:
        status = _status(sensor)
        if status != "LIVE":
            self._apnea_streak = 0
            self._respiration_abnormal_streak = 0
            return _unavailable("mmwave", now, f"MMWAVE_SENSOR_{status}")

        decision = self._decision(ai, now=now, ttl=_ttl(sensor, 3.0))
        observed_state, observed_confidence = None, None
        if decision is not None and self.mmwave_neural_trust != "TRUSTED":
            state, confidence, _, metadata = decision
            hardware_verified = (
                state in tuple(self._mmwave["apnea_states"])
                and metadata.get("apnea_verified") is True
            )
            if not hardware_verified:
                # Observe-only: record the class, do not let it score. See
                # mmwave.neural_trust_reason in risk_formula_v1.json.
                # A hardware-verified apnea is device provenance, not a model
                # opinion, so it is never suppressed by this switch.
                observed_state, observed_confidence = state, confidence
                decision = None
        if decision is not None:
            state, confidence, timestamp, metadata = decision
            mapped = self._mmwave["class_scores"].get(state)
            if mapped is None:
                return _unavailable("mmwave", now, f"MMWAVE_AI_UNKNOWN_CLASS_{state}")
            reasons: list[str] = []
            apnea_states = tuple(self._mmwave["apnea_states"])
            if state in apnea_states:
                self._apnea_streak += 1
                hardware_verified = metadata.get("apnea_verified") is True
                if hardware_verified and self._emergency.get("mmwave_apnea_hardware_verified"):
                    floors.append("mmwave_apnea_hardware_verified")
                    emergencies.append("hardware_verified_apnea")
                elif self._apnea_streak >= int(self._mmwave["apnea_warning_persistence"]):
                    # M-N9 is DEVICE_VALIDATED=NO: escalate, but never to DANGER alone.
                    floors.append("mmwave_apnea_proxy_sustained")
                    reasons.append("APNEA_PROXY_UNVERIFIED_NO_EMERGENCY")
                else:
                    reasons.append("APNEA_PROXY_AWAITING_PERSISTENCE")
            else:
                self._apnea_streak = 0
            self._respiration_abnormal_streak = 0
            return RiskComponent(
                "mmwave", True, float(mapped), "ai", state, timestamp,
                reasons=tuple(reasons),
                metadata={**metadata, "confidence": confidence, "apnea_streak": self._apnea_streak},
            )

        self._apnea_streak = 0
        values = _values(sensor)
        # Prefer the spectral readout of the canonical window over the MR60's own
        # breath_rate_raw. On the committed 20260817 capture the spectral estimate
        # holds mean 20.56 rpm (sd 4.42) while the MR60 scalar reports mean 10.21
        # (sd 9.17) and bottoms out at 0.00 rpm on the same windows.
        rate, rate_source, spectral_detail = self._respiration_rate(ai, values)
        if rate is None:
            self._respiration_abnormal_streak = 0
            return _unavailable("mmwave", now, "RESPIRATION_INPUT_UNAVAILABLE")
        normal = (
            float(self._mmwave["respiration_normal_min_rpm"])
            <= rate
            <= float(self._mmwave["respiration_normal_max_rpm"])
        )
        if normal:
            self._respiration_abnormal_streak = 0
            score = 0.0
            reasons = ()
            state = "RESPIRATION_NORMAL"
        else:
            self._respiration_abnormal_streak += 1
            sustained = self._respiration_abnormal_streak >= int(
                self._mmwave["respiration_abnormal_persistence"]
            )
            score = float(
                self._mmwave["respiration_abnormal_sustained_score"]
                if sustained
                else self._mmwave["respiration_abnormal_score"]
            )
            reasons = ("ABNORMAL_RESPIRATION_RPM_SUSTAINED",) if sustained else ("ABNORMAL_RESPIRATION_RPM",)
            state = "RESPIRATION_ABNORMAL"
        return RiskComponent(
            "mmwave", True, score, "rule_fallback", state, now,
            reasons=reasons,
            metadata={
                "respiration_rate_bpm": rate,
                "respiration_rate_source": rate_source,
                "abnormal_streak": self._respiration_abnormal_streak,
                "neural_trust": self.mmwave_neural_trust,
                "observed_neural_state": observed_state,
                "observed_neural_confidence": observed_confidence,
                **spectral_detail,
                **_ai_debug_metadata(ai),
            },
        )

    def _respiration_rate(
        self, ai: Any, values: Mapping[str, Any]
    ) -> tuple[float | None, str, dict[str, Any]]:
        """Spectral canonical-window rate first, MR60 scalar only as a last resort."""

        metadata = ai.get("metadata") if isinstance(ai, Mapping) else None
        metadata = metadata if isinstance(metadata, Mapping) else {}
        spectral_rate = metadata.get("spectral_rate_rpm")
        fraction = metadata.get("spectral_band_power_fraction")
        detail = {
            "spectral_status": metadata.get("spectral_status"),
            "spectral_band_power_fraction": fraction,
            "spectral_contradicts_apnea": metadata.get("spectral_contradicts_apnea"),
            "mr60_breath_rate_raw": values.get("respiration_rate_bpm"),
        }
        if (
            metadata.get("spectral_status") == "SPECTRAL_ESTIMATE_READY"
            and _finite_number(spectral_rate)
            and float(spectral_rate) > 0
        ):
            return float(spectral_rate), "SPECTRAL_CANONICAL_WINDOW", detail

        breath = values.get("respiration_rate_bpm")
        if not bool(values.get("respiration_valid")) or not _finite_number(breath) or float(breath) <= 0:
            return None, "UNAVAILABLE", detail
        return float(breath), "MR60_BREATH_RATE_RAW", detail

    def _co2_component(
        self,
        sensor: Any,
        ai: Any,
        now: float,
        floors: list[str],
        emergencies: list[str],
    ) -> RiskComponent:
        status = _status(sensor)
        if status != "LIVE":
            return _unavailable("co2", now, f"CO2_SENSOR_{status}")
        values = _values(sensor)
        ppm = values.get("ppm")
        if not _finite_number(ppm) or float(ppm) < 0:
            return _unavailable("co2", now, "CO2_INPUT_UNAVAILABLE")
        ppm = float(ppm)

        # Prefer the canonical CO2_SLOPE_FEATURE_PROFILE_001 slope produced by the
        # AI pipeline so there is exactly one slope definition in the runtime.
        slope, slope_source = self._canonical_slope(ai)
        if slope is None:
            slope, slope_source = self._local_slope(sensor, ppm), "RISK_LOCAL_ENDPOINT"

        score = _piecewise(self._co2_curve, ppm)
        reasons: list[str] = []
        baseline = _baseline_metadata(ai)
        locked = baseline.get("co2_baseline_status") == "CO2_BASELINE_LOCKED"
        delta = baseline.get("co2_delta_plus_ppm")
        if ppm >= float(self._co2["immediate_danger_ppm"]):
            reasons.append("CO2_IMMEDIATE_DANGER")
            floors.append("co2_immediate_danger")
            if self._emergency.get("co2_immediate_danger"):
                emergencies.append("co2_immediate_danger")
            state = "CO2_IMMEDIATE_DANGER"
        elif locked and (
            baseline.get("co2_relative_warning") is True
            or (_finite_number(delta) and float(delta) >= self.caution_delta_enter)
        ):
            reasons.append("CO2_RELATIVE_RISE")
            floors.append("co2_relative_warning")
            state = "CO2_RELATIVE_WARNING"
        elif locked:
            state = "CO2_NORMAL"
        elif baseline.get("co2_baseline_status"):
            reasons.append("CO2_BASELINE_UNLOCKED")
            state = "CO2_LOCALIZING"
        else:
            state = "CO2_NORMAL"

        if slope is not None:
            if slope >= self.caution_slope_ppm_per_min:
                score += float(self._co2["slope_danger_bonus"])
                reasons.append("VERY_FAST_CO2_RISE")
                if self._slope_raises_caution(locked, slope_source):
                    floors.append("co2_fast_rise")
            elif slope >= float(self._co2["slope_warning_ppm_per_min"]):
                score += float(self._co2["slope_warning_bonus"])
                reasons.append("FAST_CO2_RISE")
        score = min(1.0, max(0.0, score))

        # Occupancy from the C-B6 head is informational only: its class_map
        # declares risk_semantic NONE and safety_semantic NONE, and SCD40 domain
        # alignment is still an open phase, so it never becomes a hazard weight.
        occupancy = _occupancy_metadata(ai)

        return RiskComponent(
            "co2", True, score, "rule", state, now,
            reasons=tuple(reasons),
            metadata={
                "ppm": ppm,
                "slope_ppm_per_min": slope,
                "slope_source": slope_source,
                "slope_unit": "ppm/min",
                **baseline,
                **occupancy,
            },
        )

    def _slope_raises_caution(self, locked: bool, slope_source: str) -> bool:
        """WARNING from slope needs a locked room and the canonical 150 s slope.

        The short RISK_LOCAL_ENDPOINT fallback is score-only. It is the 15 s
        display-tick slope that produced field false cautions during warmup.
        """

        if self.caution_requires_baseline_lock and not locked:
            return False
        return slope_source != "RISK_LOCAL_ENDPOINT"

    @staticmethod
    def _canonical_slope(ai: Any) -> tuple[float | None, str]:
        metadata = ai.get("metadata") if isinstance(ai, Mapping) else None
        if not isinstance(metadata, Mapping):
            return None, "UNAVAILABLE"
        value = metadata.get("co2_slope_ppm_per_min")
        if _finite_number(value):
            profile = metadata.get("slope_profile_id")
            return float(value), str(profile or "CANONICAL")
        return None, "UNAVAILABLE"

    def _local_slope(self, sensor: Any, ppm: float) -> float | None:
        """Endpoint-difference fallback when the canonical slope is warming up."""

        sample_time = sensor.get("last_update") if isinstance(sensor, Mapping) else None
        sequence = sensor.get("sequence") if isinstance(sensor, Mapping) else None
        if sequence != self._last_co2_sequence and _finite_number(sample_time):
            self._co2_history.append((float(sample_time), ppm))
            self._last_co2_sequence = sequence
        if len(self._co2_history) < 2:
            return None
        elapsed = (self._co2_history[-1][0] - self._co2_history[0][0]) / 60.0
        if elapsed <= 0:
            return None
        return (self._co2_history[-1][1] - self._co2_history[0][1]) / elapsed

    def _pir_component(
        self, sensor: Any, presence: bool, now: float, floors: list[str]
    ) -> RiskComponent:
        status = _status(sensor)
        if status != "LIVE":
            self._no_motion_started_at = None
            return _unavailable("pir", now, f"PIR_SENSOR_{status}")
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
            # Without confirmed presence, "no motion" carries no information.
            # Scoring it 0.0 would silently pull the fused score down.
            self._no_motion_started_at = None
            return _unavailable("pir", now, "PIR_PRESENCE_UNCONFIRMED")
        if self._no_motion_started_at is None or timestamp < self._no_motion_started_at:
            self._no_motion_started_at = timestamp
        elapsed = max(0.0, timestamp - self._no_motion_started_at)
        grace = float(self._pir["no_motion_grace_seconds"])
        danger = float(self._pir["no_motion_danger_seconds"])
        if elapsed <= grace:
            score, state, reasons = 0.0, "NO_MOTION", ()
        elif elapsed >= danger:
            score, state, reasons = 1.0, "LONG_NO_MOTION", ("LONG_NO_MOTION",)
            floors.append("pir_long_no_motion")
        else:
            score = (elapsed - grace) / (danger - grace)
            state, reasons = "NO_MOTION_RISING", ("NO_MOTION_DETECTED",)
        return RiskComponent(
            "pir", True, round(score, 4), "rule", state, timestamp,
            reasons=reasons,
            metadata={
                "no_motion_seconds": round(elapsed, 3),
                "presence_confirmed": True,
                "grace_seconds": grace,
                "danger_seconds": danger,
            },
        )

    def _presence(
        self, mmwave_sensor: Any, thermal: RiskComponent
    ) -> tuple[bool, str, tuple[str, ...]]:
        values = _values(mmwave_sensor)
        if _status(mmwave_sensor) == "LIVE" and values.get("presence_available") is True:
            return bool(values.get("presence")), "MMWAVE", ("PRESENCE_FROM_MMWAVE",)
        human_states = tuple(self._thermal["human_states"])
        if thermal.available and thermal.state in human_states:
            return True, "THERMAL", ("PRESENCE_FROM_THERMAL",)
        if thermal.available:
            return False, "THERMAL", ()
        return False, "UNCONFIRMED", ("PRESENCE_UNCONFIRMED",)

    # ------------------------------------------------------------- AI gating
    def _decision(
        self, ai: Any, *, now: float, ttl: float
    ) -> tuple[str, float, float, dict[str, Any]] | None:
        """Accept an AI result only when it is fresh, confident and decisive."""

        if not isinstance(ai, Mapping) or ai.get("available") is not True:
            return None
        confidence = ai.get("confidence")
        timestamp = ai.get("timestamp")
        if (
            not _finite_number(confidence)
            or not 0.0 <= float(confidence) <= 1.0
            or not _finite_number(timestamp)
            or float(timestamp) < 0
        ):
            return None
        age = float(now) - float(timestamp)
        if age > float(ttl) or age < -1.0:
            return None
        if float(confidence) < self.min_confidence:
            return None
        metadata = ai.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        probabilities = metadata.get("probabilities")
        if isinstance(probabilities, (list, tuple)) and len(probabilities) >= 2:
            values = sorted(
                (float(p) for p in probabilities if _finite_number(p)), reverse=True
            )
            if len(values) >= 2 and (values[0] - values[1]) < self.min_margin:
                return None
        return str(ai.get("state", "UNKNOWN")), float(confidence), float(timestamp), metadata


# ------------------------------------------------------------------ helpers
def _piecewise(curve: list[tuple[float, float]], x: float) -> float:
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def _max_level(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if LEVEL_ORDER.index(candidate) > LEVEL_ORDER.index(current) else current


def _unavailable(sensor_id: str, now: float, reason: str) -> RiskComponent:
    return RiskComponent(sensor_id, False, None, "unavailable", "UNAVAILABLE", now, (reason,))


def _ai_block_reason(ai: Any, prefix: str) -> str:
    if isinstance(ai, Mapping) and ai.get("available") is True:
        return f"{prefix}_AI_OUTPUT_INDECISIVE"
    return f"{prefix}_AI_UNAVAILABLE"


def _status(sensor: Any) -> str:
    return str(sensor.get("status", "NO_DATA")) if isinstance(sensor, Mapping) else "NO_DATA"


def _values(sensor: Any) -> Mapping[str, Any]:
    values = sensor.get("values", {}) if isinstance(sensor, Mapping) else {}
    return values if isinstance(values, Mapping) else {}


def _error(ai: Any) -> str | None:
    return str(ai.get("error")) if isinstance(ai, Mapping) and ai.get("error") else None


def _baseline_metadata(ai: Any) -> dict[str, Any]:
    """Room-air localization fields. Occupancy is a separate, non-hazard signal."""

    if not isinstance(ai, Mapping):
        return {}
    metadata = ai.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    status = metadata.get("co2_baseline_status")
    if not status:
        return {}
    baseline_ppm = metadata.get("co2_baseline_ppm")
    delta = metadata.get("co2_delta_plus_ppm")
    return {
        "co2_baseline_status": str(status),
        "co2_baseline_ppm": float(baseline_ppm) if _finite_number(baseline_ppm) else None,
        "co2_delta_plus_ppm": float(delta) if _finite_number(delta) else None,
        "co2_relative_warning": metadata.get("co2_relative_warning") is True,
        "co2_baseline_reason": metadata.get("co2_baseline_reason"),
        "co2_baseline_plus_only": True,
    }


def _occupancy_metadata(ai: Any) -> dict[str, Any]:
    """Surface C-B6 occupancy without letting it act as a hazard weight."""

    if not isinstance(ai, Mapping):
        return {"occupancy_state": None, "occupancy_available": False}
    metadata = ai.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    available = ai.get("available") is True
    probability = metadata.get("occupancy_probability")
    return {
        "occupancy_state": str(ai.get("state")) if available else None,
        "occupancy_available": available,
        "occupancy_probability": float(probability) if _finite_number(probability) else None,
        "occupancy_risk_semantic": str(metadata.get("risk_semantic", "NONE")),
        "occupancy_contract_id": metadata.get("contract_id"),
        "co2_ai_error": _error(ai),
        "co2_slope_status": str(ai.get("state")) if not available else None,
    }


def _ai_debug_metadata(ai: Any) -> dict[str, Any]:
    metadata = ai.get("metadata") if isinstance(ai, Mapping) else None
    if not isinstance(metadata, Mapping):
        metadata = {}
    missing = metadata.get("missing")
    return {
        "ai_error": _error(ai),
        "ai_state": str(ai.get("state")) if isinstance(ai, Mapping) and ai.get("state") else None,
        "canonical_window_status": metadata.get("canonical_window_status"),
        "suppression_reason": metadata.get("suppression_reason"),
        "missing": list(missing) if isinstance(missing, list) else missing,
    }


def _ttl(sensor: Any, default: float) -> float:
    value = sensor.get("ttl_seconds") if isinstance(sensor, Mapping) else None
    return float(value) if _finite_number(value) and float(value) > 0 else default


__all__ = ["SafeNestRiskFormulaV1", "RiskEvaluationV1", "CONFIG_PATH"]
