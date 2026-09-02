"""Live CO2_slope reconstruction for the C-B6 reduced-feature runtime.

Implements ``CO2_SLOPE_FEATURE_PROFILE_001``
(``sources/ondevice_ai/models/rp_x0_b_complete/co2/co2_slope_feature_profile.json``)
exactly as declared:

* ``feature_unit``           ppm/min
* ``slope_method``           ENDPOINT_DIFFERENCE
* ``formula``                (co2_now - co2_history_start) / (elapsed_s / 60.0)
* endpoint selection         earliest past sample whose age >= history 150 s
* ``causality``              PAST_ONLY - no future or centred windows
* ``timestamp_basis``        SOURCE_ACQUISITION_CLOCK, i.e. the ESP's
                             ``co2_measurement_monotonic_ms`` physical
                             measurement clock, never the Pi wall clock
* ``max_internal_gap_seconds`` 90 s, ``gap_policy`` RESTART_HISTORY_AFTER_FORBIDDEN_GAP
* ``interpolation_allowed``  false
* ``calculation_precision``  float64
* ``nonfinite_policy``       FAIL_CLOSED_STATUS_NO_CANONICAL_SLOPE

Status codes are the profile's own vocabulary, so an unavailable slope is never
silently reported as 0.0 ppm/min:

* ``CO2_SLOPE_READY``
* ``FEATURE_UNAVAILABLE_WARMUP``          (warm_up_status)
* ``FEATURE_UNAVAILABLE_GAP_RESTART``     (gap_restart_status)
* ``NO_CANONICAL_SLOPE``                  (nonfinite_policy)
* ``CO2_MEASUREMENT_CLOCK_UNAVAILABLE``   (no source clock to anchor on)

Only physical measurement events advance the history. The runtime republishes the
last CO2 reading on every telemetry packet and additionally throttles the
presentation value to once per minute, so keying on anything other than
``measurement_event_id`` would fabricate a flat slope out of repeated values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import statistics
import threading
from typing import Any, Mapping

PROFILE_PATH = (
    (__import__("paths", fromlist=["ONDEVICE_AI_ROOT"]).ONDEVICE_AI_ROOT
    / "models/rp_x0_b_complete/co2/co2_slope_feature_profile.json")
)
PROFILE_ID = "CO2_SLOPE_FEATURE_PROFILE_001"


@dataclass(frozen=True)
class CO2SlopeResult:
    status: str
    reason: str | None
    ppm: float | None
    slope_ppm_per_min: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status == "CO2_SLOPE_READY"


def _load_profile() -> dict[str, Any]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if profile.get("profile_id") != PROFILE_ID:
        raise ValueError("CO2_SLOPE_PROFILE_IDENTITY_MISMATCH")
    if profile.get("feature_unit") != "ppm/min":
        raise ValueError("CO2_SLOPE_PROFILE_UNIT_MISMATCH")
    if profile.get("slope_method") != "ENDPOINT_DIFFERENCE":
        raise ValueError("CO2_SLOPE_PROFILE_METHOD_MISMATCH")
    if profile.get("causality") != "PAST_ONLY":
        raise ValueError("CO2_SLOPE_PROFILE_CAUSALITY_MISMATCH")
    if profile.get("interpolation_allowed") is not False:
        raise ValueError("CO2_SLOPE_PROFILE_INTERPOLATION_MISMATCH")
    if profile.get("future_samples_allowed") is not False:
        raise ValueError("CO2_SLOPE_PROFILE_FUTURE_SAMPLES_MISMATCH")
    return profile


class CO2SlopeWindowBuilder:
    """Accumulate CO2 measurement events and derive the canonical ppm/min slope.

    ``observe`` runs on the receiver thread while ``latest`` is read by the
    publication thread, so the history is lock-protected.
    """

    def __init__(self, profile: Mapping[str, Any] | None = None) -> None:
        self._profile = dict(profile) if profile is not None else _load_profile()
        self.history_seconds = float(self._profile["history_duration_seconds"])
        self.minimum_elapsed_seconds = float(self._profile["minimum_elapsed_seconds"])
        self.minimum_samples = int(self._profile["minimum_source_samples"])
        self.max_internal_gap_seconds = float(self._profile["max_internal_gap_seconds"])
        self._lock = threading.RLock()
        # (source_clock_seconds, ppm) in float64, oldest first, PAST_ONLY.
        self._samples: list[tuple[float, float]] = []
        self._last_event_key: tuple[Any, Any, Any] | None = None
        self._boot_id: str | None = None
        self._gap_restarts = 0
        self._gap_restart_pending = False
        self._accepted_events = 0

    def reset(self, reason: str) -> None:
        with self._lock:
            self._samples.clear()
            self._last_event_key = None
            if reason == "GAP":
                self._gap_restarts += 1
                self._gap_restart_pending = True

    def observe(self, sensor: Mapping[str, Any]) -> None:
        """Ingest one CO2 sensor record; only new measurement events advance."""

        with self._lock:
            self._observe_locked(sensor)

    def _observe_locked(self, sensor: Mapping[str, Any]) -> None:
        values = sensor.get("values")
        if not isinstance(values, Mapping):
            return
        if values.get("measurement_event_valid") is not True:
            return

        boot_id = sensor.get("boot_id")
        boot_id = boot_id if isinstance(boot_id, str) and boot_id else None
        if self._boot_id is not None and boot_id is not None and boot_id != self._boot_id:
            # boundary_policy: DERIVED_TEMPORAL_FEATURES_MUST_NOT_CROSS_BLOCK_BOUNDARIES
            self.reset("BOOT_BOUNDARY")
        self._boot_id = boot_id

        event_id = values.get("measurement_event_id")
        event_key = (sensor.get("device_id"), boot_id, event_id)
        if event_id is None or event_key == self._last_event_key:
            return

        clock_ms = values.get("measurement_monotonic_ms")
        ppm = values.get("latest_measurement_ppm")
        if not _finite(clock_ms) or not _finite(ppm):
            return
        clock_s = float(clock_ms) / 1000.0
        ppm = float(ppm)

        if self._samples:
            gap = clock_s - self._samples[-1][0]
            if gap <= 0.0:
                # Non-monotonic source clock cannot anchor a PAST_ONLY feature.
                self.reset("GAP")
            elif gap > self.max_internal_gap_seconds:
                self.reset("GAP")

        self._last_event_key = event_key
        self._samples.append((clock_s, ppm))
        self._accepted_events += 1
        # Keep just enough past history to select the >= 150 s endpoint.
        horizon = clock_s - (self.history_seconds * 2.0 + self.max_internal_gap_seconds)
        while len(self._samples) > 2 and self._samples[1][0] < horizon:
            self._samples.pop(0)

    def latest(self) -> CO2SlopeResult:
        with self._lock:
            return self._latest_locked()

    def _latest_locked(self) -> CO2SlopeResult:
        base: dict[str, Any] = {
            "slope_profile_id": PROFILE_ID,
            "slope_method": "ENDPOINT_DIFFERENCE",
            "slope_unit": "ppm/min",
            "timestamp_basis": "SOURCE_ACQUISITION_CLOCK",
            "required_history_seconds": self.history_seconds,
            "max_internal_gap_seconds": self.max_internal_gap_seconds,
            "accepted_measurement_events": self._accepted_events,
            "gap_restarts": self._gap_restarts,
            "retained_samples": len(self._samples),
            "gap_restart_pending": self._gap_restart_pending,
            "source_boot_id": self._boot_id,
        }
        if not self._samples:
            return CO2SlopeResult(
                "CO2_MEASUREMENT_CLOCK_UNAVAILABLE",
                "NO_VALID_MEASUREMENT_EVENT",
                None,
                None,
                base,
            )

        now_s, now_ppm = self._samples[-1]
        base["ppm"] = now_ppm
        if len(self._samples) < self.minimum_samples:
            return self._unrecovered(
                "INSUFFICIENT_SOURCE_SAMPLES", now_ppm, base
            )

        # Earliest past observation whose source-clock age is at least the
        # configured history duration. No interpolation, no future samples.
        endpoint: tuple[float, float] | None = None
        for sample in self._samples[:-1]:
            if now_s - sample[0] >= self.history_seconds:
                endpoint = sample
                break
        base["available_history_seconds"] = round(now_s - self._samples[0][0], 3)
        if endpoint is None:
            return self._unrecovered("INSUFFICIENT_ELAPSED_HISTORY", now_ppm, base)

        elapsed_s = now_s - endpoint[0]
        if elapsed_s < self.minimum_elapsed_seconds or elapsed_s <= 0.0:
            return self._unrecovered("INSUFFICIENT_ELAPSED_HISTORY", now_ppm, base)
        slope = (now_ppm - endpoint[1]) / (elapsed_s / 60.0)
        if not math.isfinite(slope):
            return CO2SlopeResult(
                "NO_CANONICAL_SLOPE", "NONFINITE_SLOPE", now_ppm, None, base
            )
        base.update(
            {
                "endpoint_span_seconds": round(elapsed_s, 3),
                "endpoint_ppm": endpoint[1],
            }
        )
        self._gap_restart_pending = False
        base["gap_restart_pending"] = False
        return CO2SlopeResult("CO2_SLOPE_READY", None, now_ppm, slope, base)

    def _unrecovered(
        self, reason: str, ppm: float | None, base: dict[str, Any]
    ) -> CO2SlopeResult:
        """Distinguish a cold start from an unrecovered forbidden-gap restart."""

        if self._gap_restart_pending:
            return CO2SlopeResult(
                "FEATURE_UNAVAILABLE_GAP_RESTART",
                f"GAP_RESTART_{reason}",
                ppm,
                None,
                base,
            )
        return CO2SlopeResult("FEATURE_UNAVAILABLE_WARMUP", reason, ppm, None, base)


@dataclass(frozen=True)
class CO2BaselineResult:
    """Room-air localization: a locked enclosed-space ppm, not occupancy."""

    status: str
    reason: str | None
    ppm: float | None
    baseline_ppm: float | None
    delta_plus_ppm: float | None
    relative_warning: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def locked(self) -> bool:
        return self.status == "CO2_BASELINE_LOCKED"

    def as_metadata(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        payload["co2_baseline_status"] = self.status
        payload["co2_baseline_ppm"] = self.baseline_ppm
        payload["co2_delta_plus_ppm"] = (
            None if self.delta_plus_ppm is None else round(float(self.delta_plus_ppm), 3)
        )
        payload["co2_relative_warning"] = self.relative_warning
        if self.reason:
            payload["co2_baseline_reason"] = self.reason
        return payload


class CO2BaselineLock:
    """Lock the enclosed-room CO2 baseline from physical measurement events.

    Same event identity, source clock, boot boundary, and 90 s gap policy as
    ``CO2SlopeWindowBuilder``. Occupancy (C-B6) is a different signal and is
    not computed here.

    After ``lock_seconds`` of source-clock span, B is the median of those
    warmup samples and then frozen. Delta is plus-only: a drop below B is
    ventilation, not a hazard. Relative warning enters at +delta_enter ppm
    and clears at +delta_exit ppm.
    """

    def __init__(
        self,
        *,
        lock_seconds: float = 180.0,
        minimum_samples: int = 3,
        max_internal_gap_seconds: float = 90.0,
        delta_enter_ppm: float = 500.0,
        delta_exit_ppm: float = 350.0,
    ) -> None:
        if lock_seconds <= 0 or minimum_samples < 1:
            raise ValueError("invalid CO2 baseline lock window")
        if delta_exit_ppm < 0 or delta_enter_ppm < delta_exit_ppm:
            raise ValueError("relative warning hysteresis must have enter >= exit >= 0")
        self.lock_seconds = float(lock_seconds)
        self.minimum_samples = int(minimum_samples)
        self.max_internal_gap_seconds = float(max_internal_gap_seconds)
        self.delta_enter_ppm = float(delta_enter_ppm)
        self.delta_exit_ppm = float(delta_exit_ppm)
        self._lock = threading.RLock()
        self._samples: list[tuple[float, float]] = []
        self._last_event_key: tuple[Any, Any, Any] | None = None
        self._boot_id: str | None = None
        self._gap_restarts = 0
        self._gap_restart_pending = False
        self._accepted_events = 0
        self._baseline_ppm: float | None = None
        self._relative_warning = False

    @classmethod
    def from_risk_config(cls, path: Path | None = None) -> "CO2BaselineLock":
        config_path = path or (
            Path(__file__).resolve().parent.parent / "risk" / "risk_formula_v1.json"
        )
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        co2 = payload["co2"]
        caution = payload.get("caution_formula") or {}
        return cls(
            lock_seconds=float(co2["baseline_lock_seconds"]),
            minimum_samples=int(co2["baseline_minimum_samples"]),
            max_internal_gap_seconds=float(co2["baseline_max_internal_gap_seconds"]),
            delta_enter_ppm=float(
                caution.get("baseline_delta_enter_ppm", co2["baseline_delta_warning_ppm"])
            ),
            delta_exit_ppm=float(
                caution.get("baseline_delta_clear_ppm", co2["baseline_delta_clear_ppm"])
            ),
        )

    def reset(self, reason: str) -> None:
        with self._lock:
            self._samples.clear()
            self._last_event_key = None
            self._baseline_ppm = None
            self._relative_warning = False
            if reason == "GAP":
                self._gap_restarts += 1
                self._gap_restart_pending = True

    def observe(self, sensor: Mapping[str, Any]) -> None:
        with self._lock:
            self._observe_locked(sensor)

    def _observe_locked(self, sensor: Mapping[str, Any]) -> None:
        values = sensor.get("values")
        if not isinstance(values, Mapping):
            return
        if values.get("measurement_event_valid") is not True:
            return

        boot_id = sensor.get("boot_id")
        boot_id = boot_id if isinstance(boot_id, str) and boot_id else None
        if self._boot_id is not None and boot_id is not None and boot_id != self._boot_id:
            self.reset("BOOT_BOUNDARY")
        self._boot_id = boot_id

        event_id = values.get("measurement_event_id")
        event_key = (sensor.get("device_id"), boot_id, event_id)
        if event_id is None or event_key == self._last_event_key:
            return

        clock_ms = values.get("measurement_monotonic_ms")
        ppm = values.get("latest_measurement_ppm")
        if not _finite(clock_ms) or not _finite(ppm):
            return
        clock_s = float(clock_ms) / 1000.0
        ppm = float(ppm)

        if self._samples:
            gap = clock_s - self._samples[-1][0]
            if gap <= 0.0 or gap > self.max_internal_gap_seconds:
                self.reset("GAP")

        self._last_event_key = event_key
        self._samples.append((clock_s, ppm))
        self._accepted_events += 1
        if self._baseline_ppm is None:
            span = self._samples[-1][0] - self._samples[0][0]
            if (
                span >= self.lock_seconds
                and len(self._samples) >= self.minimum_samples
            ):
                self._baseline_ppm = float(statistics.median(p for _, p in self._samples))
                self._gap_restart_pending = False
        else:
            # Keep a short tail so latest() has a current ppm after lock.
            horizon = clock_s - (self.lock_seconds + self.max_internal_gap_seconds)
            while len(self._samples) > 2 and self._samples[1][0] < horizon:
                self._samples.pop(0)

        if self._baseline_ppm is not None:
            delta_plus = max(0.0, ppm - self._baseline_ppm)
            if self._relative_warning:
                if delta_plus < self.delta_exit_ppm:
                    self._relative_warning = False
            elif delta_plus >= self.delta_enter_ppm:
                self._relative_warning = True

    def latest(self) -> CO2BaselineResult:
        with self._lock:
            return self._latest_locked()

    def _latest_locked(self) -> CO2BaselineResult:
        base: dict[str, Any] = {
            "co2_baseline_lock_seconds": self.lock_seconds,
            "co2_baseline_minimum_samples": self.minimum_samples,
            "co2_baseline_delta_enter_ppm": self.delta_enter_ppm,
            "co2_baseline_delta_exit_ppm": self.delta_exit_ppm,
            "co2_baseline_plus_only": True,
            "timestamp_basis": "SOURCE_ACQUISITION_CLOCK",
            "accepted_measurement_events": self._accepted_events,
            "gap_restarts": self._gap_restarts,
            "gap_restart_pending": self._gap_restart_pending,
            "source_boot_id": self._boot_id,
            "retained_samples": len(self._samples),
        }
        if not self._samples:
            return CO2BaselineResult(
                "CO2_BASELINE_UNLOCKED_NO_CLOCK",
                "NO_VALID_MEASUREMENT_EVENT",
                None, None, None, False, base,
            )
        now_ppm = self._samples[-1][1]
        span = self._samples[-1][0] - self._samples[0][0]
        base["ppm"] = now_ppm
        base["available_history_seconds"] = round(span, 3)
        if self._baseline_ppm is None:
            reason = "INSUFFICIENT_ELAPSED_HISTORY"
            if len(self._samples) < self.minimum_samples:
                reason = "INSUFFICIENT_SOURCE_SAMPLES"
            status = (
                "CO2_BASELINE_UNLOCKED_GAP_RESTART"
                if self._gap_restart_pending
                else "CO2_BASELINE_UNLOCKED_WARMUP"
            )
            return CO2BaselineResult(
                status, reason, now_ppm, None, None, False, base,
            )
        delta_plus = max(0.0, now_ppm - self._baseline_ppm)
        base.update(
            {
                "co2_baseline_status": "CO2_BASELINE_LOCKED",
                "co2_baseline_ppm": self._baseline_ppm,
                "co2_delta_plus_ppm": round(delta_plus, 3),
                "co2_relative_warning": self._relative_warning,
            }
        )
        return CO2BaselineResult(
            "CO2_BASELINE_LOCKED",
            None,
            now_ppm,
            self._baseline_ppm,
            delta_plus,
            self._relative_warning,
            base,
        )


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def h150_model_input_eligible(sensor: Mapping[str, Any]) -> bool:
    """Whether a CO2 record may enter C-B6 H150 history.

    Does not change slope math. Callers must not invent ``measurement_event_id``
    from packet ``seq``, transport ``fresh``, or ``age_seconds``.

    Preheat: missing is unknown, not complete. Only ``preheat_complete is True``
    is model-eligible. MH-Z19B identity is inferred UART sample, not SCD40
    getDataReady conversion.
    """

    values = sensor.get("values")
    if not isinstance(values, Mapping):
        return False
    if values.get("measurement_event_valid") is not True:
        return False
    event_id = values.get("measurement_event_id")
    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id == 0:
        return False
    if not _finite(values.get("measurement_monotonic_ms")):
        return False
    if not _finite(values.get("latest_measurement_ppm")):
        return False
    if values.get("preheat_complete") is not True:
        return False
    return True


__all__ = [
    "CO2SlopeWindowBuilder",
    "CO2SlopeResult",
    "CO2BaselineLock",
    "CO2BaselineResult",
    "PROFILE_ID",
    "PROFILE_PATH",
    "h150_model_input_eligible",
]
