"""JSON-safe AI result contract shared by model and rule outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any


@dataclass(frozen=True)
class AIResult:
    sensor_id: str
    timestamp: float
    available: bool
    source: str
    state: str
    score: float | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    model_id: str | None = None
    model_version: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in {"tflite", "rule", "unavailable", "pytorch"}:
            raise ValueError(f"unsupported AI result source: {self.source}")
        if self.available and self.source == "unavailable":
            raise ValueError("an available result cannot use the unavailable source")
        for name in ("timestamp", "score", "confidence", "latency_ms"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        for name in ("score", "confidence"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
        _ensure_json_safe(self.metadata, "metadata")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_json_safe(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _ensure_json_safe(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")
