"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

R1 sensor-independent relative-motion trace contract.

R1 deliberately consumes an already-established native source trace.  Source
specific decoding belongs to the D0/D1 adapters; this module does not inspect
radar channel layout, range bins, Six-Port detector channels, or MR60 fields.

The bounded R1 candidate implemented here is a common, variable-length 10 Hz
trace with only full-recording median centering.  The native trace and
amplitude descriptors remain separate from the common waveform.  Downsampling
uses a deterministic anti-alias polyphase filter; no physiological band-pass,
gain matching, window-local normalization, sign inversion, or feature
extraction is performed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

import numpy as np
from scipy.signal import resample_poly


R1_SCHEMA_VERSION = "R1.1"
R1_PROFILE_ID = "R1-A_NATIVE_CENTERED_RELATIVE_MOTION_10HZ_V1"
R1_CONTRACT_ID = "R1_SENSOR_INDEPENDENT_TRACE_CONTRACT_V1"
R1_TARGET_SAMPLE_RATE_HZ = 10.0
R1_ANTIALIAS_WINDOW = ("kaiser", 8.6)


class R1TraceError(ValueError):
    """A fail-closed error raised when a native trace cannot be mapped safely."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class NativeTraceInput:
    """A source-adapted trace accepted by the generic R1 layer."""

    source_id: str
    dataset_id: str
    subject_id: str
    recording_id: str
    condition: str
    trace: np.ndarray
    time_s: np.ndarray
    sampling_rate_hz: float
    native_trace_semantics: str
    native_trace_unit: str
    source_scale_metadata: Mapping[str, Any]
    provenance: Mapping[str, Any]
    validity_mask: np.ndarray | None = None
    source_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommonTraceOutput:
    """The R1 common trace plus non-waveform quality/provenance evidence."""

    trace: np.ndarray
    time_s: np.ndarray
    validity_mask: np.ndarray
    metadata: dict[str, Any]


def _finite_vector(values: Any, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64).squeeze()
    if array.ndim != 1 or array.size < 2:
        raise R1TraceError("TRACE_NOT_ONE_DIMENSIONAL", f"{name}: shape={array.shape}")
    if not np.all(np.isfinite(array)):
        raise R1TraceError("NONFINITE_INPUT", name)
    return array


def _finite_positive_rate(value: Any) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise R1TraceError("SOURCE_SAMPLING_RATE_INVALID", str(value)) from exc
    if not math.isfinite(rate) or rate <= 0.0:
        raise R1TraceError("SOURCE_SAMPLING_RATE_INVALID", str(value))
    return rate


def _scale_descriptors(values: np.ndarray) -> dict[str, float]:
    array = _finite_vector(values, "scale_descriptor_input")
    median = float(np.median(array))
    centered = array - median
    mad = float(np.median(np.abs(centered)))
    q05, q95 = np.percentile(array, [5.0, 95.0])
    return {
        "min": float(np.min(array)),
        "max": float(np.max(array)),
        "mean": float(np.mean(array)),
        "std": float(np.std(array)),
        "median": median,
        "mad_about_median": mad,
        "robust_rms_about_median": float(np.sqrt(np.mean(centered * centered))),
        "robust_peak_to_peak_p05_p95": float(q95 - q05),
        "peak_to_peak": float(np.max(array) - np.min(array)),
    }


def _validate_and_regularize_time(
    trace: np.ndarray,
    time_s: np.ndarray,
    sampling_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    expected_dt = 1.0 / sampling_rate_hz
    deltas = np.diff(time_s)
    if np.any(deltas <= 0.0):
        raise R1TraceError("TIMESTAMP_NOT_STRICTLY_INCREASING", "time_s")

    # A gap larger than 2.5 source samples is not treated as a small timing
    # jitter.  R1 refuses the record instead of interpolating a missing region.
    if float(np.max(deltas)) > expected_dt * 2.5 + 1e-12:
        raise R1TraceError(
            "UNRESOLVABLE_TIME_GAP",
            f"max_delta={float(np.max(deltas))}; expected_delta={expected_dt}",
        )

    expected_grid = time_s[0] + np.arange(trace.size, dtype=np.float64) * expected_dt
    timing_error = np.abs(time_s - expected_grid)
    timing_jitter = float(np.max(timing_error)) if timing_error.size else 0.0
    # The R1 sources currently use exact or reconstructed sample-index timing.
    # Small jitter is regularized onto the declared source grid only; no large
    # gap is filled.
    if timing_jitter <= max(expected_dt * 0.25, 1e-9):
        if timing_jitter > 1e-12:
            regularized = np.interp(expected_grid, time_s, trace)
            return regularized, expected_grid, {
                "source_time_regularization": "SMALL_JITTER_LINEAR_REGRID",
                "source_time_jitter_max_s": timing_jitter,
            }
        return trace, time_s, {
            "source_time_regularization": "NONE",
            "source_time_jitter_max_s": timing_jitter,
        }

    raise R1TraceError(
        "TIMESTAMP_GRID_INCONSISTENT",
        f"max_grid_error={timing_jitter}; expected_dt={expected_dt}",
    )


def _resample_to_target(
    trace: np.ndarray,
    source_rate_hz: float,
    source_time_s: np.ndarray,
    target_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    source_rate_rounded = int(round(source_rate_hz))
    target_rate_rounded = int(round(target_rate_hz))
    if not math.isclose(source_rate_hz, source_rate_rounded, rel_tol=0.0, abs_tol=1e-9):
        raise R1TraceError(
            "NON_INTEGER_SOURCE_RATE_UNSUPPORTED",
            f"source_rate_hz={source_rate_hz}",
        )
    if not math.isclose(target_rate_hz, target_rate_rounded, rel_tol=0.0, abs_tol=1e-9):
        raise R1TraceError("NON_INTEGER_TARGET_RATE_UNSUPPORTED", str(target_rate_hz))

    if math.isclose(source_rate_hz, target_rate_hz, rel_tol=0.0, abs_tol=1e-9):
        return trace.copy(), source_time_s.copy(), {
            "resampling_performed": False,
            "resampling_method": "NONE_SOURCE_ALREADY_AT_TARGET_RATE",
            "anti_aliasing": "NOT_REQUIRED",
            "resampling_up": 1,
            "resampling_down": 1,
        }

    if source_rate_hz < target_rate_hz:
        raise R1TraceError(
            "SOURCE_RATE_BELOW_TARGET",
            f"source_rate_hz={source_rate_hz}; target_rate_hz={target_rate_hz}",
        )

    # D0/D1 rates are integer multiples of the 10 Hz candidate.  Keep the
    # explicit ratio in provenance rather than fitting or matching a gain.
    if source_rate_rounded % target_rate_rounded != 0:
        raise R1TraceError(
            "NON_INTEGER_RESAMPLING_RATIO",
            f"source={source_rate_rounded}; target={target_rate_rounded}",
        )
    divisor = math.gcd(target_rate_rounded, source_rate_rounded)
    up = target_rate_rounded // divisor
    down = source_rate_rounded // divisor
    resampled = resample_poly(
        trace,
        up=up,
        down=down,
        window=R1_ANTIALIAS_WINDOW,
        padtype="line",
    ).astype(np.float64, copy=False)
    output_time = source_time_s[0] + np.arange(resampled.size, dtype=np.float64) / target_rate_hz
    return resampled, output_time, {
        "resampling_performed": True,
        "resampling_method": "SCIPY_RESAMPLE_POLY",
        "anti_aliasing": "KAISER_WINDOWED_POLYPHASE_LOW_PASS",
        "anti_alias_window": list(R1_ANTIALIAS_WINDOW),
        "resampling_up": up,
        "resampling_down": down,
        "edge_behavior": "PADTYPE_LINE",
        "gap_behavior": "FAIL_CLOSED_NO_LARGE_GAP_INTERPOLATION",
    }


def adapt_native_trace(
    native: NativeTraceInput,
    *,
    target_rate_hz: float = R1_TARGET_SAMPLE_RATE_HZ,
    profile_id: str = R1_PROFILE_ID,
) -> CommonTraceOutput:
    """Map one source-adapted trace to the bounded R1 common candidate.

    The only waveform operation after optional anti-alias resampling is
    full-recording median centering.  The centered trace is not divided by
    MAD, RMS, or any other per-recording scale.  Native descriptors are
    calculated and retained for later R2/M-PV1 decisions.
    """

    if not native.source_id or not native.recording_id:
        raise R1TraceError("PROVENANCE_ID_MISSING", "source_id and recording_id are required")
    source_rate_hz = _finite_positive_rate(native.sampling_rate_hz)
    raw_trace = _finite_vector(native.trace, "native_trace")
    raw_time = _finite_vector(native.time_s, "time_s")
    if raw_trace.size != raw_time.size:
        raise R1TraceError(
            "TRACE_TIME_LENGTH_MISMATCH",
            f"trace={raw_trace.size}; time={raw_time.size}",
        )
    if native.validity_mask is not None:
        validity = np.asarray(native.validity_mask, dtype=bool).squeeze()
        if validity.shape != raw_trace.shape:
            raise R1TraceError("VALIDITY_MASK_LENGTH_MISMATCH", native.recording_id)
        if not np.all(validity):
            raise R1TraceError(
                "INVALID_SOURCE_REGION",
                "R1 will not zero-fill or interpolate invalid samples",
            )
    regular_trace, regular_time, timing_metadata = _validate_and_regularize_time(
        raw_trace,
        raw_time,
        source_rate_hz,
    )
    native_descriptors = _scale_descriptors(raw_trace)
    resampled, output_time, resampling_metadata = _resample_to_target(
        regular_trace,
        source_rate_hz,
        regular_time,
        target_rate_hz,
    )
    if not np.all(np.isfinite(resampled)):
        raise R1TraceError("NONFINITE_RESAMPLED_OUTPUT", native.recording_id)
    center_offset = float(np.median(resampled))
    centered = resampled - center_offset
    if not np.all(np.isfinite(centered)):
        raise R1TraceError("NONFINITE_CENTERED_OUTPUT", native.recording_id)
    output_validity = np.ones(centered.shape, dtype=bool)
    output_descriptors = _scale_descriptors(centered)

    source_scale_metadata = {
        "native_trace_unit": native.native_trace_unit,
        "native_trace_semantics": native.native_trace_semantics,
        "native_descriptors": native_descriptors,
        "resampled_descriptors_before_centering": _scale_descriptors(resampled),
        "common_trace_descriptors_after_centering": output_descriptors,
        "native_scale_preserved": True,
        "scale_normalization_applied": False,
        "sensor_gain_matching_applied": False,
        "sign_inversion_applied": False,
        "center_offset_common_trace": center_offset,
        "source_metadata_passthrough": dict(native.source_scale_metadata),
    }
    provenance = dict(native.provenance)
    provenance.update(
        {
            "source_id": native.source_id,
            "dataset_id": native.dataset_id,
            "subject_id": native.subject_id,
            "recording_id": native.recording_id,
            "condition": native.condition,
            "original_sampling_rate_hz": source_rate_hz,
            "adapter_identity": native.provenance.get("adapter_identity", "UNVERIFIED"),
            "r1_profile_identity": profile_id,
            "time_range_s": [float(output_time[0]), float(output_time[-1])],
            "native_trace_unit": native.native_trace_unit,
            "common_trace_semantics": "OFFSET_CENTERED_NATIVE_PHASE_LIKE_RELATIVE_MOTION",
            "validity_gap_flags": list(native.source_quality_flags),
        }
    )
    metadata: dict[str, Any] = {
        "schema_version": R1_SCHEMA_VERSION,
        "contract_id": R1_CONTRACT_ID,
        "profile_id": profile_id,
        "source_id": native.source_id,
        "dataset_id": native.dataset_id,
        "subject_id": native.subject_id,
        "recording_id": native.recording_id,
        "condition": native.condition,
        "trace_name": "respiratory_motion_trace",
        "trace_semantics": "OFFSET_CENTERED_NATIVE_PHASE_LIKE_RELATIVE_MOTION",
        "trace_units": "phase_like_radian; absolute displacement equivalence not claimed",
        "sign_policy": "PRESERVE_SOURCE_SIGN; SIGN_ALIGNMENT_UNVERIFIED",
        "offset_policy": "SUBTRACT_FULL_RECORDING_MEDIAN_ONLY",
        "detrending_policy": "NONE_BEYOND_MEDIAN_CENTERING",
        "filtering_policy": "ANTI_ALIAS_ONLY_WHEN_DOWNSAMPLING; NO_PHYSIOLOGICAL_BANDPASS",
        "source_sampling_rate_hz": source_rate_hz,
        "output_sampling_rate_hz": float(target_rate_hz),
        "timestamp_rule": "output_time_s = source_start_time_s + index / target_rate_hz",
        "time_basis": "source-relative-or-source-provided-start-preserved",
        "source_time_metadata": timing_metadata,
        "resampling_metadata": resampling_metadata,
        "native_trace_semantics": native.native_trace_semantics,
        "native_trace_unit": native.native_trace_unit,
        "native_scale_metadata": source_scale_metadata,
        "validity_mask_semantics": "TRUE_ONLY_FOR_FINITE_TRACE_WITH_VALID_TIMING; NO_ZERO_FILL",
        "quality_flags": list(native.source_quality_flags)
        + ["R1_FINITE_OUTPUT", "R1_NATIVE_SCALE_PRESERVED"],
        "provenance": provenance,
        "forbidden_processing_not_applied": [
            "arbitrary_sensor_gain_matching",
            "window_local_MAD_only_normalization",
            "D0_or_D1_cross_domain_scaler",
            "model_score_optimized_gain",
            "feature_extraction",
            "target_encoding",
            "abstention_threshold_selection",
            "D2_access",
            "MR60_supervised_use",
        ],
    }
    return CommonTraceOutput(
        trace=centered,
        time_s=output_time,
        validity_mask=output_validity,
        metadata=metadata,
    )


__all__ = [
    "CommonTraceOutput",
    "NativeTraceInput",
    "R1_ANTIALIAS_WINDOW",
    "R1_CONTRACT_ID",
    "R1_PROFILE_ID",
    "R1_SCHEMA_VERSION",
    "R1_TARGET_SAMPLE_RATE_HZ",
    "R1TraceError",
    "adapt_native_trace",
]
