"""LEGACY / NON-ACTIVE M-N4 window reconstruction (M-N9 240-sample path).

Default team runtime is B23: ``ai/mmwave_b23_runtime.py``. This module is
retained for isolated contract tests and must not be called from
``OnDeviceAIPipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import math
from pathlib import Path
import sys
import threading
from typing import Any, Mapping

import numpy as np


def _canonical_module():
    from paths import ONDEVICE_AI_ROOT
    path = ONDEVICE_AI_ROOT / "scripts" / "mmwave_m_n4_canonical.py"
    spec = importlib.util.spec_from_file_location("_safenest_m_n4_contract", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load canonical contract: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


M4 = _canonical_module()
CONTRACT_ID = "MMWAVE_MR60_COMPAT_INPUT_DATASET_V1"


@dataclass(frozen=True)
class CanonicalRuntimeResult:
    status: str
    reason: str | None
    tensor: np.ndarray | None
    metadata: dict[str, Any]


class MR60CanonicalWindowBuilder:
    """Accumulate freshness-aware MR60 events and build the newest 30 s window.

    ``ingest`` is driven by the receiver thread at wire rate while ``latest`` is
    read by the publication thread, so the accumulator state is lock-protected.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[tuple[float, float, str | None, str | None, int | None]] = []
        self._last_update_ms: float | None = None
        self._last_source_key: tuple[str | None, str | None, int | None] | None = None
        self._boot_id: str | None = None
        self._session_id: str | None = None
        self._republication_count = 0
        self._freshness_missing = False

    def reset(self, reason: str = "EXPLICIT_STREAM_RESET") -> None:
        with self._lock:
            self._events.clear()
            self._last_update_ms = None
            self._last_source_key = None
            self._republication_count = 0
            self._freshness_missing = reason == "CANONICAL_FRESHNESS_METADATA_MISSING"

    def ingest(self, sensor: Mapping[str, object]) -> None:
        with self._lock:
            self._ingest_locked(sensor)

    def _ingest_locked(self, sensor: Mapping[str, object]) -> None:
        values = sensor.get("values")
        if not isinstance(values, Mapping):
            self._freshness_missing = True
            return
        boot_id = _string_or_none(sensor.get("boot_id"))
        session_id = _string_or_none(values.get("session_id"))
        if self._boot_id is not None and boot_id is not None and boot_id != self._boot_id:
            self.reset("BOOT_BOUNDARY")
        elif self._session_id is not None and session_id is not None and session_id != self._session_id:
            self.reset("SESSION_BOUNDARY")
        self._boot_id = boot_id
        self._session_id = session_id

        phase = values.get("breath_phase")
        ts_ms = values.get("ts_monotonic_ms")
        age_ms = values.get("phase_age_ms")
        if not (_finite(phase) and _finite(ts_ms) and _finite(age_ms)):
            self._freshness_missing = True
            return
        self._freshness_missing = False
        sequence = _integer_or_none(sensor.get("sequence"))
        source_key = (boot_id, session_id, sequence)
        if source_key == self._last_source_key:
            return
        self._last_source_key = source_key
        update_ms = float(ts_ms) - float(age_ms)
        if self._last_update_ms is not None and update_ms <= self._last_update_ms + M4.UPDATE_ADVANCE_TOLERANCE_MS:
            self._republication_count += 1
            return
        self._events.append((update_ms, float(phase), boot_id, session_id, sequence))
        self._last_update_ms = update_ms
        # The current candidate needs at most 30 s plus the frozen edge hold.
        cutoff = update_ms - (M4.WINDOW_SECONDS + M4.EDGE_HOLD_MAX_SECONDS + 1.0) * 1000.0
        self._events = [event for event in self._events if event[0] >= cutoff]

    def latest(self) -> CanonicalRuntimeResult:
        with self._lock:
            return self._latest_locked()

    def _latest_locked(self) -> CanonicalRuntimeResult:
        base = {
            "contract_id": CONTRACT_ID,
            "accepted_update_count": len(self._events),
            "republication_count": self._republication_count,
            "source_boot_id": self._boot_id,
            "source_session_id": self._session_id,
            "required_span_ms": 30_000,
        }
        if self._freshness_missing:
            return CanonicalRuntimeResult("WINDOW_UNAVAILABLE", "CANONICAL_FRESHNESS_METADATA_MISSING", None, base)
        if len(self._events) < 2:
            return CanonicalRuntimeResult("RESPIRATORY_WINDOW_WARMING_UP", "INSUFFICIENT_CONTINUOUS_DURATION", None, base)
        span_ms = self._events[-1][0] - self._events[0][0]
        base["continuous_span_ms"] = span_ms
        if span_ms < 30_000:
            return CanonicalRuntimeResult("RESPIRATORY_WINDOW_WARMING_UP", "INSUFFICIENT_CONTINUOUS_DURATION", None, base)
        t_ms = np.asarray([event[0] for event in self._events], dtype=np.float64)
        phase = np.asarray([event[1] for event in self._events], dtype=np.float64)
        boots = np.asarray([event[2] for event in self._events], dtype=object)
        t_start_s = float(t_ms[-1] / 1000.0 - M4.WINDOW_SECONDS)
        try:
            window = M4.form_canonical_window(t_ms / 1000.0, phase, t_start_s, boot_ids=boots)
        except M4.CanonicalContractError as error:
            return CanonicalRuntimeResult("WINDOW_UNAVAILABLE", str(error), None, base)
        base.update({
            "window_start": window.t_start_s,
            "window_end": window.t_start_s + M4.WINDOW_SECONDS,
            "median_update_dt_ms": window.median_update_dt_s * 1000.0,
            "gap_threshold_ms": window.gap_threshold_s * 1000.0,
            "MAD": window.mad,
            "mad_collapsed": window.collapsed,
            "input_shape": [1, 240, 1],
            "input_dtype": "float32",
        })
        return CanonicalRuntimeResult("CANONICAL_WINDOW_READY", None, window.values.reshape(1, 240, 1), base)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _integer_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
