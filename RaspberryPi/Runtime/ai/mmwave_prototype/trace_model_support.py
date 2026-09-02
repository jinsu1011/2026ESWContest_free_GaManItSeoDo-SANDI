"""Runtime-only extract of sheepmeat/test training-script helpers used by B23.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_FILE: scripts/mmwave_m_pv2_candidate_training.py

Copied without behavioral rewrite: TraceModel, feature-name constants,
_feature_arrays/_feature_matrix, InputRecord, canonical parameter hash.
Training loops, sklearn metrics, and dataset loaders are omitted.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from torch import nn

F2_NAMES = (
    "spectral_shape_fraction_0p10_0p25_hz",
    "spectral_shape_fraction_0p25_0p40_hz",
    "spectral_shape_fraction_0p40_0p55_hz",
    "spectral_shape_fraction_0p55_0p70_hz",
    "spectral_shape_centroid_hz",
    "spectral_shape_peak_frequency_hz",
    "spectral_shape_peak_fraction",
    "spectral_shape_entropy_normalized",
    "native_mad_about_median",
    "native_robust_rms_about_median",
    "native_robust_range_p05_p95",
    "native_peak_to_peak",
    "common_trace_mad_about_median",
    "common_trace_robust_rms_about_median",
    "total_signal_energy",
    "total_signal_mean_square",
    "log_total_signal_energy",
    "respiratory_band_power",
    "respiratory_band_energy",
    "log_respiratory_band_energy",
    "autocorr_periodicity_peak_strength",
    "autocorr_periodicity_peak_lag_s",
    "autocorr_periodicity_peak_frequency_hz",
    "autocorr_periodicity_lag_mean",
    "autocorr_abs_entropy_normalized",
)
SCALE_NAMES = (
    "native_mad_about_median",
    "native_robust_rms_about_median",
    "native_robust_range_p05_p95",
    "native_peak_to_peak",
    "common_trace_mad_about_median",
    "common_trace_robust_rms_about_median",
    "total_signal_energy",
    "total_signal_mean_square",
    "log_total_signal_energy",
    "respiratory_band_energy",
    "respiratory_band_power",
    "log_respiratory_band_energy",
)
QUALITY_NAMES = (
    "trace_sample_count",
    "trace_duration_s",
    "trace_mad_about_median",
    "trace_robust_rms_about_median",
    "trace_robust_range_p05_p95",
    "trace_mean_square",
    "trace_is_exact_flat",
    "valid_sample_fraction",
    "source_quality_flag_count",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (not math.isfinite(value)):
        return None
    return value


def _sha256_json(value: Any) -> str:
    payload = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclasses.dataclass
class InputRecord:
    source_id: str
    subject_id: str
    recording_id: str
    model_input_id: str
    split: str
    trace: np.ndarray
    trace_mask: np.ndarray
    f2: np.ndarray
    f2_mask: np.ndarray
    scale: np.ndarray
    quality: np.ndarray
    breathing_label: float
    breathing_mask: float
    rr_bpm: float
    rr_mask: float
    quality_label: float
    quality_mask: float
    breathing_state: str
    rr_target_status: str
    quality_status: str
    provenance: dict[str, Any]
    is_synthetic: bool = False
    corruption_mode: str | None = None


def _vector(mapping: Mapping[str, Any], names: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    values = np.zeros(len(names), dtype=np.float32)
    mask = np.zeros(len(names), dtype=bool)
    for index, name in enumerate(names):
        try:
            value = float(mapping.get(name))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values[index] = value
            mask[index] = True
    return values, mask


def _feature_arrays(common: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    from ai.mmwave_prototype.mmwave_r2_representation_features import extract_feature_candidates

    extracted = extract_feature_candidates(common)
    f2_map = extracted.f2.features if isinstance(extracted.f2.features, Mapping) else {}
    scale_map = f2_map
    quality_map = extracted.f3.features if isinstance(extracted.f3.features, Mapping) else {}
    f2, f2_mask = _vector(f2_map, F2_NAMES)
    scale, scale_mask = _vector(scale_map, SCALE_NAMES)
    quality, quality_mask = _vector(quality_map, QUALITY_NAMES)
    if not np.all(scale_mask):
        scale = np.nan_to_num(scale, nan=0.0, posinf=0.0, neginf=0.0)
    if not np.all(quality_mask):
        quality = np.nan_to_num(quality, nan=0.0, posinf=0.0, neginf=0.0)
    return (
        np.asarray(extracted.trace, dtype=np.float32),
        np.asarray(extracted.validity_mask, dtype=bool),
        f2,
        f2_mask,
        np.concatenate([scale, quality]).astype(np.float32),
    )


def _normalize(values: np.ndarray, spec: Mapping[str, Any]) -> np.ndarray:
    mean = np.asarray(spec["mean"], dtype=np.float32)
    std = np.asarray(spec["std"], dtype=np.float32)
    return (values - mean) / std


def _feature_matrix(records: Sequence[InputRecord], family: str, stats: Mapping[str, Any]) -> np.ndarray:
    vectors: list[np.ndarray] = []
    for record in records:
        f2 = _normalize(record.f2[None, :], stats["f2"])[0]
        f2_mask = record.f2_mask.astype(np.float32)
        scale = _normalize(record.scale[None, :], stats["scale"])[0]
        quality = _normalize(record.quality[None, :], stats["quality"])[0]
        trace = (record.trace.astype(np.float32) - float(stats["trace"]["mean"])) / float(stats["trace"]["std"])
        trace_mask = record.trace_mask.astype(np.float32)
        if family == "family_a":
            vector = np.concatenate([f2, f2_mask, quality])
        elif family == "family_b":
            vector = np.concatenate([trace, trace_mask, scale, quality])
        elif family == "family_c":
            vector = np.concatenate([trace, trace_mask, scale, quality, f2, f2_mask])
        else:
            raise RuntimeError(f"unknown family: {family}")
        vectors.append(np.nan_to_num(vector, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32))
    return np.stack(vectors, axis=0) if vectors else np.zeros((0, 1), dtype=np.float32)


class TraceModel(nn.Module):
    def __init__(self, input_dim: int, family: str):
        super().__init__()
        self.family = family
        self.trace = nn.Sequential(nn.Conv1d(1, 16, 5, padding=2), nn.ReLU(), nn.Conv1d(16, 24, 5, padding=2), nn.ReLU(), nn.AdaptiveAvgPool1d(8))
        scalar_dim = 12 + 9 + (25 + 25 if family == "family_c" else 0)
        self.body = nn.Sequential(nn.Linear(24 * 8 + scalar_dim, 64), nn.ReLU(), nn.Linear(64, 32), nn.ReLU())
        self.breathing_head = nn.Linear(32, 1)
        self.rr_head = nn.Linear(32, 1)
        self.quality_head = nn.Linear(32, 1)

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        trace = x[:, :300]
        mask = x[:, 300:600]
        offset = 600
        scalar = [x[:, offset : offset + 12], x[:, offset + 12 : offset + 21]]
        offset += 21
        if self.family == "family_c":
            scalar.extend([x[:, offset : offset + 25], x[:, offset + 25 : offset + 50]])
        trace_hidden = self.trace((trace * mask).unsqueeze(1)).flatten(1)
        hidden = self.body(torch.cat([trace_hidden, *scalar], dim=1))
        return {
            "breathing": self.breathing_head(hidden).squeeze(-1),
            "rr": self.rr_head(hidden).squeeze(-1),
            "quality": self.quality_head(hidden).squeeze(-1),
        }


def _canonical_parameter_sha(model: nn.Module) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        digest.update(name.encode("utf-8"))
        digest.update(np.asarray(tensor.detach().cpu(), dtype=np.float32).tobytes(order="C"))
    return digest.hexdigest()
