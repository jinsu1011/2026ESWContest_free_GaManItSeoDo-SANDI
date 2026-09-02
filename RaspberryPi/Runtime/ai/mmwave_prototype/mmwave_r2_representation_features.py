"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

Bounded R2 feature candidates over the frozen R1 common trace.

R2 is intentionally downstream of R1.  It accepts an R1 ``CommonTraceOutput``
and never inspects D0/D1 channel layout, radar I/Q fields, Six-Port fields, or
MR60 fields.  The implementation is a deterministic candidate generator, not
a selected V2 representation and not a model preprocessor.

The spectral candidate uses a fixed, named 0.10--0.70 Hz engineering band
(6--42 cycles/minute) and a small set of named frequency bins.  This band is a
bounded candidate for M-PV1; it is not a clinical cutoff and is not selected
from validation, held-out data, a respiration reference, or model results.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

import numpy as np
from scipy.signal import welch

from ai.mmwave_prototype.mmwave_r1_sensor_independent_trace import CommonTraceOutput


R2_SCHEMA_VERSION = "R2.1"
R2_CANDIDATE_SET_ID = "MMWAVE_V2_R2_REPRESENTATION_CANDIDATE_SET_V1"
F1_SCHEMA_ID = "MMWAVE_V2_R2_F1_NORMALIZED_SPECTRAL_V1"
F2_SCHEMA_ID = "MMWAVE_V2_R2_F2_SPECTRAL_AUTOCORR_V1"
F3_SCHEMA_ID = "MMWAVE_V2_R2_F3_TRACE_PLUS_QUALITY_DESCRIPTOR_V1"
RESPIRATORY_BAND_HZ = (0.10, 0.70)
SPECTRAL_BAND_BIN_EDGES_HZ = (0.10, 0.25, 0.40, 0.55, 0.70)
MIN_SPECTRAL_SAMPLES = 64
MAX_WELCH_SEGMENT_SAMPLES = 256
WELCH_OVERLAP_FRACTION = 0.5
WELCH_WINDOW = "hann"
WELCH_DETREND = "constant"
WELCH_SCALING = "density"
AUTOCORR_MIN_PERIOD_S = 1.0 / RESPIRATORY_BAND_HZ[1]
AUTOCORR_MAX_PERIOD_S = 1.0 / RESPIRATORY_BAND_HZ[0]


F1_FEATURE_NAMES = (
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
)

F2_AUTOCORR_FEATURE_NAMES = (
    "autocorr_periodicity_peak_strength",
    "autocorr_periodicity_peak_lag_s",
    "autocorr_periodicity_peak_frequency_hz",
    "autocorr_periodicity_lag_mean",
    "autocorr_abs_entropy_normalized",
)

F3_FEATURE_NAMES = (
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


class R2FeatureError(ValueError):
    """Fail-closed error for an invalid or incomplete R1 handoff."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class FeatureCandidate:
    """One bounded candidate's scalar descriptors and typed availability."""

    schema_id: str
    candidate_id: str
    status: str
    features: dict[str, float]
    feature_units: dict[str, str]
    unavailable_reasons: tuple[str, ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "candidate_id": self.candidate_id,
            "status": self.status,
            "features": dict(sorted(self.features.items())),
            "feature_units": dict(sorted(self.feature_units.items())),
            "unavailable_reasons": list(self.unavailable_reasons),
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class FeatureExtractionResult:
    """F1/F2 scalar candidates plus the unmodified R1 trace for F3 runtime use."""

    f1: FeatureCandidate
    f2: FeatureCandidate
    f3: FeatureCandidate
    trace: np.ndarray
    time_s: np.ndarray
    validity_mask: np.ndarray
    provenance: dict[str, Any]

    def to_dict(self, *, include_trace: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "f1": self.f1.to_dict(),
            "f2": self.f2.to_dict(),
            "f3": self.f3.to_dict(),
            "provenance": self.provenance,
            "trace_retained_in_runtime": True,
            "trace_persisted_in_this_record": bool(include_trace),
        }
        if include_trace:
            result["trace"] = [float(value) for value in self.trace]
            result["time_s"] = [float(value) for value in self.time_s]
            result["validity_mask"] = [bool(value) for value in self.validity_mask]
        return result


def _finite_float(value: Any, name: str) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise R2FeatureError("NONNUMERIC_METADATA", name) from exc
    if not math.isfinite(converted):
        raise R2FeatureError("NONFINITE_METADATA", name)
    return converted


def _finite_feature(value: Any, name: str) -> float:
    converted = _finite_float(value, name)
    return converted


def _validate_r1_output(output: CommonTraceOutput) -> tuple[np.ndarray, np.ndarray, float, dict[str, Any]]:
    trace = np.asarray(output.trace, dtype=np.float64).squeeze()
    time_s = np.asarray(output.time_s, dtype=np.float64).squeeze()
    validity = np.asarray(output.validity_mask, dtype=bool).squeeze()
    if trace.ndim != 1 or trace.size < 2:
        raise R2FeatureError("TRACE_NOT_ONE_DIMENSIONAL", f"shape={trace.shape}")
    if time_s.shape != trace.shape or validity.shape != trace.shape:
        raise R2FeatureError("R1_HANDOFF_LENGTH_MISMATCH", output.metadata.get("recording_id", ""))
    if not np.all(np.isfinite(trace)) or not np.all(np.isfinite(time_s)):
        raise R2FeatureError("NONFINITE_R1_HANDOFF", output.metadata.get("recording_id", ""))
    if np.any(np.diff(time_s) <= 0.0):
        raise R2FeatureError("R1_TIMESTAMP_NOT_STRICTLY_INCREASING", output.metadata.get("recording_id", ""))
    if not np.all(validity):
        raise R2FeatureError("R1_INVALID_SAMPLE_MASK", output.metadata.get("recording_id", ""))

    metadata = output.metadata
    if metadata.get("contract_id") != "R1_SENSOR_INDEPENDENT_TRACE_CONTRACT_V1":
        raise R2FeatureError("R1_CONTRACT_ID_MISMATCH", str(metadata.get("contract_id")))
    fs = _finite_float(metadata.get("output_sampling_rate_hz"), "output_sampling_rate_hz")
    if fs <= 0.0:
        raise R2FeatureError("R1_OUTPUT_RATE_INVALID", str(fs))
    expected_dt = 1.0 / fs
    if not np.allclose(np.diff(time_s), expected_dt, rtol=0.0, atol=max(1e-10, expected_dt * 1e-9)):
        raise R2FeatureError("R1_TIMESTAMP_GRID_INCONSISTENT", output.metadata.get("recording_id", ""))
    scale_metadata = metadata.get("native_scale_metadata")
    if not isinstance(scale_metadata, Mapping):
        raise R2FeatureError("R1_SCALE_METADATA_MISSING", output.metadata.get("recording_id", ""))
    if scale_metadata.get("native_scale_preserved") is not True:
        raise R2FeatureError("R1_NATIVE_SCALE_NOT_PRESERVED", output.metadata.get("recording_id", ""))
    if scale_metadata.get("scale_normalization_applied") is not False:
        raise R2FeatureError("R1_SCALE_NORMALIZATION_PRESENT", output.metadata.get("recording_id", ""))
    return trace, time_s, fs, dict(metadata)


def _descriptor_group(scale_metadata: Mapping[str, Any], key: str, required: tuple[str, ...]) -> dict[str, float]:
    group = scale_metadata.get(key)
    if not isinstance(group, Mapping):
        raise R2FeatureError("R1_SCALE_DESCRIPTOR_GROUP_MISSING", key)
    values: dict[str, float] = {}
    for field in required:
        if field not in group:
            raise R2FeatureError("R1_SCALE_DESCRIPTOR_MISSING", f"{key}.{field}")
        values[field] = _finite_feature(group[field], f"{key}.{field}")
    return values


def _scale_features(metadata: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    scale_metadata = metadata["native_scale_metadata"]
    native = _descriptor_group(
        scale_metadata,
        "native_descriptors",
        ("mad_about_median", "robust_rms_about_median", "robust_peak_to_peak_p05_p95", "peak_to_peak"),
    )
    common = _descriptor_group(
        scale_metadata,
        "common_trace_descriptors_after_centering",
        ("mad_about_median", "robust_rms_about_median"),
    )
    features = {
        "native_mad_about_median": native["mad_about_median"],
        "native_robust_rms_about_median": native["robust_rms_about_median"],
        "native_robust_range_p05_p95": native["robust_peak_to_peak_p05_p95"],
        "native_peak_to_peak": native["peak_to_peak"],
        "common_trace_mad_about_median": common["mad_about_median"],
        "common_trace_robust_rms_about_median": common["robust_rms_about_median"],
    }
    units = {
        "native_mad_about_median": "native_trace_unit",
        "native_robust_rms_about_median": "native_trace_unit",
        "native_robust_range_p05_p95": "native_trace_unit",
        "native_peak_to_peak": "native_trace_unit",
        "common_trace_mad_about_median": "phase_like_radian",
        "common_trace_robust_rms_about_median": "phase_like_radian",
    }
    return features, units


def _base_trace_features(trace: np.ndarray, metadata: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    scale_features, scale_units = _scale_features(metadata)
    total_energy = float(np.sum(trace * trace))
    total_mean_square = float(np.mean(trace * trace))
    features = {
        **scale_features,
        "total_signal_energy": total_energy,
        "total_signal_mean_square": total_mean_square,
    }
    units = {
        **scale_units,
        "total_signal_energy": "phase_like_radian_squared_samples",
        "total_signal_mean_square": "phase_like_radian_squared",
    }
    if total_energy > 0.0:
        features["log_total_signal_energy"] = _finite_feature(math.log(total_energy), "log_total_signal_energy")
        units["log_total_signal_energy"] = "natural_log_of_positive_total_signal_energy"
    return features, units


def _empty_candidate(
    schema_id: str,
    candidate_id: str,
    status: str,
    features: dict[str, float],
    feature_units: dict[str, str],
    reasons: list[str],
    diagnostics: dict[str, Any],
) -> FeatureCandidate:
    for name, value in features.items():
        _finite_feature(value, name)
    return FeatureCandidate(
        schema_id=schema_id,
        candidate_id=candidate_id,
        status=status,
        features=features,
        feature_units=feature_units,
        unavailable_reasons=tuple(dict.fromkeys(reasons)),
        diagnostics=diagnostics,
    )


def _welch_descriptors(
    trace: np.ndarray,
    fs: float,
    base_features: dict[str, float],
    base_units: dict[str, str],
) -> tuple[FeatureCandidate, dict[str, Any]]:
    candidate_id = "F1_NORMALIZED_SPECTRAL"
    flat = bool(np.all(trace == trace[0]))
    diagnostics: dict[str, Any] = {
        "respiratory_band_hz": list(RESPIRATORY_BAND_HZ),
        "frequency_bin_edges_hz": list(SPECTRAL_BAND_BIN_EDGES_HZ),
        "flat_trace_detected": flat,
        "sample_count": int(trace.size),
        "sampling_rate_hz": fs,
        "welch": {
            "method": "scipy.signal.welch",
            "window": WELCH_WINDOW,
            "detrend": WELCH_DETREND,
            "scaling": WELCH_SCALING,
            "nperseg": min(MAX_WELCH_SEGMENT_SAMPLES, int(trace.size)),
            "noverlap": None,
            "nfft": min(MAX_WELCH_SEGMENT_SAMPLES, int(trace.size)),
            "zero_padding": False,
            "short_trace_rule": f"n<{MIN_SPECTRAL_SAMPLES} => typed unavailable; no zero padding",
        },
    }
    if flat:
        diagnostics["welch"]["noverlap"] = None
        return (
            _empty_candidate(
                F1_SCHEMA_ID,
                candidate_id,
                "FEATURE_UNAVAILABLE_FLAT_TRACE",
                base_features,
                base_units,
                ["EXACT_FLAT_TRACE"],
                diagnostics,
            ),
            diagnostics,
        )
    if trace.size < MIN_SPECTRAL_SAMPLES:
        diagnostics["welch"]["noverlap"] = None
        return (
            _empty_candidate(
                F1_SCHEMA_ID,
                candidate_id,
                "FEATURE_UNAVAILABLE_SHORT_TRACE",
                base_features,
                base_units,
                ["INSUFFICIENT_SAMPLES_FOR_DETERMINISTIC_WELCH"],
                diagnostics,
            ),
            diagnostics,
        )

    nperseg = min(MAX_WELCH_SEGMENT_SAMPLES, int(trace.size))
    noverlap = int(math.floor(nperseg * WELCH_OVERLAP_FRACTION))
    diagnostics["welch"]["noverlap"] = noverlap
    frequencies, psd = welch(
        trace,
        fs=fs,
        window=WELCH_WINDOW,
        nperseg=nperseg,
        noverlap=noverlap,
        nfft=nperseg,
        detrend=WELCH_DETREND,
        return_onesided=True,
        scaling=WELCH_SCALING,
    )
    frequencies = np.asarray(frequencies, dtype=np.float64)
    psd = np.asarray(psd, dtype=np.float64)
    if not np.all(np.isfinite(frequencies)) or not np.all(np.isfinite(psd)) or np.any(psd < 0.0):
        raise R2FeatureError("NONFINITE_OR_NEGATIVE_WELCH_OUTPUT", "welch")
    band_low, band_high = RESPIRATORY_BAND_HZ
    band_mask = (frequencies >= band_low) & (frequencies <= band_high)
    if not np.any(band_mask):
        diagnostics["welch"]["frequency_resolution_hz"] = float(fs / nperseg)
        return (
            _empty_candidate(
                F1_SCHEMA_ID,
                candidate_id,
                "FEATURE_UNAVAILABLE_NO_RESPIRATORY_BAND_BINS",
                base_features,
                base_units,
                ["RESPIRATORY_BAND_NOT_RESOLVED_ON_FREQUENCY_GRID"],
                diagnostics,
            ),
            diagnostics,
        )
    df = float(frequencies[1] - frequencies[0]) if frequencies.size > 1 else float(fs / nperseg)
    band_psd = psd[band_mask]
    band_frequencies = frequencies[band_mask]
    band_power = float(np.sum(band_psd) * df)
    diagnostics["welch"].update(
        {
            "frequency_resolution_hz": df,
            "frequency_bin_count_in_band": int(np.count_nonzero(band_mask)),
            "band_power_integration": "sum(psd_in_band) * frequency_resolution_hz",
        }
    )
    base_features = dict(base_features)
    base_units = dict(base_units)
    base_features["respiratory_band_power"] = _finite_feature(band_power, "respiratory_band_power")
    base_units["respiratory_band_power"] = "phase_like_radian_squared"
    respiratory_energy = band_power * float(trace.size) / fs
    base_features["respiratory_band_energy"] = _finite_feature(
        respiratory_energy, "respiratory_band_energy"
    )
    base_units["respiratory_band_energy"] = "phase_like_radian_squared_seconds"
    if respiratory_energy <= 0.0:
        return (
            _empty_candidate(
                F1_SCHEMA_ID,
                candidate_id,
                "FEATURE_UNAVAILABLE_NO_RESPIRATORY_BAND_ENERGY",
                base_features,
                base_units,
                ["RESPIRATORY_BAND_POWER_IS_ZERO"],
                diagnostics,
            ),
            diagnostics,
        )

    probabilities = band_psd / float(np.sum(band_psd))
    fractions: dict[str, float] = {}
    bin_names = (
        "spectral_shape_fraction_0p10_0p25_hz",
        "spectral_shape_fraction_0p25_0p40_hz",
        "spectral_shape_fraction_0p40_0p55_hz",
        "spectral_shape_fraction_0p55_0p70_hz",
    )
    for index, name in enumerate(bin_names):
        low = SPECTRAL_BAND_BIN_EDGES_HZ[index]
        high = SPECTRAL_BAND_BIN_EDGES_HZ[index + 1]
        if index == len(bin_names) - 1:
            mask = (band_frequencies >= low) & (band_frequencies <= high)
        else:
            mask = (band_frequencies >= low) & (band_frequencies < high)
        fractions[name] = _finite_feature(float(np.sum(band_psd[mask]) * df / band_power), name)
    peak_index = int(np.argmax(band_psd))
    shape_entropy = 0.0
    if probabilities.size > 1:
        shape_entropy = float(-np.sum(probabilities * np.log(probabilities)) / math.log(probabilities.size))
    spectral_features = {
        **fractions,
        "spectral_shape_centroid_hz": float(np.sum(band_frequencies * probabilities)),
        "spectral_shape_peak_frequency_hz": float(band_frequencies[peak_index]),
        "spectral_shape_peak_fraction": float(probabilities[peak_index]),
        "spectral_shape_entropy_normalized": shape_entropy,
    }
    spectral_units = {
        name: "dimensionless_normalized_band_power_fraction" for name in fractions
    }
    spectral_units.update(
        {
            "spectral_shape_centroid_hz": "Hz",
            "spectral_shape_peak_frequency_hz": "Hz",
            "spectral_shape_peak_fraction": "dimensionless_normalized_band_power_fraction",
            "spectral_shape_entropy_normalized": "dimensionless_normalized_entropy",
            "log_respiratory_band_energy": "natural_log_of_positive_respiratory_band_energy",
        }
    )
    base_features["log_respiratory_band_energy"] = _finite_feature(
        math.log(respiratory_energy), "log_respiratory_band_energy"
    )
    base_units["log_respiratory_band_energy"] = "natural_log_of_positive_respiratory_band_energy"
    features = {**spectral_features, **base_features}
    units = {**spectral_units, **base_units}
    diagnostics["band_power_positive"] = True
    return (
        _empty_candidate(
            F1_SCHEMA_ID,
            candidate_id,
            "AVAILABLE",
            features,
            units,
            [],
            diagnostics,
        ),
        diagnostics,
    )


def _autocorr_candidate(
    trace: np.ndarray,
    fs: float,
    f1: FeatureCandidate,
    spectral_diagnostics: Mapping[str, Any],
) -> FeatureCandidate:
    features = dict(f1.features)
    units = dict(f1.feature_units)
    diagnostics: dict[str, Any] = {
        "normalization": "autocorrelation divided by exact zero-lag value only",
        "sign_behavior": "autocorrelation is invariant to global trace sign; no sign flip applied",
        "lag_period_range_s": [AUTOCORR_MIN_PERIOD_S, AUTOCORR_MAX_PERIOD_S],
        "spectral_diagnostics_inherited": dict(spectral_diagnostics),
    }
    if np.all(trace == trace[0]):
        return _empty_candidate(
            F2_SCHEMA_ID,
            "F2_SPECTRAL_AUTOCORR",
            "FEATURE_UNAVAILABLE_FLAT_TRACE",
            features,
            units,
            ["EXACT_FLAT_TRACE", "AUTOCORRELATION_ZERO_LAG_IS_ZERO"],
            diagnostics,
        )
    centered = trace - float(np.mean(trace))
    zero_lag = float(np.dot(centered, centered))
    if zero_lag <= 0.0 or not math.isfinite(zero_lag):
        return _empty_candidate(
            F2_SCHEMA_ID,
            "F2_SPECTRAL_AUTOCORR",
            "FEATURE_UNAVAILABLE_ZERO_LAG_ENERGY",
            features,
            units,
            ["AUTOCORRELATION_ZERO_LAG_IS_ZERO"],
            diagnostics,
        )
    if trace.size < MIN_SPECTRAL_SAMPLES:
        return _empty_candidate(
            F2_SCHEMA_ID,
            "F2_SPECTRAL_AUTOCORR",
            "FEATURE_UNAVAILABLE_SHORT_TRACE",
            features,
            units,
            ["INSUFFICIENT_SAMPLES_FOR_BOUNDED_AUTOCORRELATION"],
            diagnostics,
        )
    autocorrelation = np.correlate(centered, centered, mode="full")[trace.size - 1 :]
    normalized = autocorrelation / zero_lag
    min_lag = max(1, int(math.ceil(fs * AUTOCORR_MIN_PERIOD_S)))
    max_lag = min(trace.size - 1, int(math.floor(fs * AUTOCORR_MAX_PERIOD_S)))
    if max_lag < min_lag:
        return _empty_candidate(
            F2_SCHEMA_ID,
            "F2_SPECTRAL_AUTOCORR",
            "FEATURE_UNAVAILABLE_NO_AUTOCORR_LAG_RANGE",
            features,
            units,
            ["RESPIRATORY_PERIOD_RANGE_NOT_RESOLVED_BY_TRACE_LENGTH"],
            diagnostics,
        )
    lag_slice = normalized[min_lag : max_lag + 1]
    peak_offset = int(np.argmax(lag_slice))
    peak_lag = min_lag + peak_offset
    abs_values = np.abs(lag_slice)
    abs_sum = float(np.sum(abs_values))
    autocorr_entropy = 0.0
    if abs_sum > 0.0 and abs_values.size > 1:
        probabilities = abs_values / abs_sum
        autocorr_entropy = float(-np.sum(probabilities * np.log(probabilities)) / math.log(probabilities.size))
    features.update(
        {
            "autocorr_periodicity_peak_strength": float(normalized[peak_lag]),
            "autocorr_periodicity_peak_lag_s": float(peak_lag / fs),
            "autocorr_periodicity_peak_frequency_hz": float(fs / peak_lag),
            "autocorr_periodicity_lag_mean": float(np.mean(lag_slice)),
            "autocorr_abs_entropy_normalized": autocorr_entropy,
        }
    )
    units.update(
        {
            "autocorr_periodicity_peak_strength": "dimensionless_zero_lag_normalized_autocorrelation",
            "autocorr_periodicity_peak_lag_s": "seconds",
            "autocorr_periodicity_peak_frequency_hz": "Hz",
            "autocorr_periodicity_lag_mean": "dimensionless_zero_lag_normalized_autocorrelation",
            "autocorr_abs_entropy_normalized": "dimensionless_normalized_entropy",
        }
    )
    diagnostics.update(
        {
            "zero_lag_energy": zero_lag,
            "min_lag_samples": min_lag,
            "max_lag_samples": max_lag,
            "lag_count": int(lag_slice.size),
        }
    )
    return _empty_candidate(
        F2_SCHEMA_ID,
        "F2_SPECTRAL_AUTOCORR",
        "AVAILABLE" if f1.status == "AVAILABLE" else f1.status,
        features,
        units,
        list(f1.unavailable_reasons),
        diagnostics,
    )


def _f3_candidate(
    trace: np.ndarray,
    time_s: np.ndarray,
    validity_mask: np.ndarray,
    fs: float,
    metadata: Mapping[str, Any],
) -> FeatureCandidate:
    centered = trace - float(np.median(trace))
    p05, p95 = np.percentile(trace, [5.0, 95.0])
    flat = bool(np.all(trace == trace[0]))
    quality_flags = metadata.get("quality_flags", [])
    if not isinstance(quality_flags, list):
        quality_flags = list(quality_flags) if isinstance(quality_flags, tuple) else []
    features = {
        "trace_sample_count": float(trace.size),
        "trace_duration_s": float((trace.size - 1) / fs),
        "trace_mad_about_median": float(np.median(np.abs(centered))),
        "trace_robust_rms_about_median": float(np.sqrt(np.mean(centered * centered))),
        "trace_robust_range_p05_p95": float(p95 - p05),
        "trace_mean_square": float(np.mean(trace * trace)),
        "trace_is_exact_flat": float(flat),
        "valid_sample_fraction": float(np.mean(validity_mask)),
        "source_quality_flag_count": float(len(quality_flags)),
    }
    units = {
        "trace_sample_count": "samples",
        "trace_duration_s": "seconds",
        "trace_mad_about_median": "phase_like_radian",
        "trace_robust_rms_about_median": "phase_like_radian",
        "trace_robust_range_p05_p95": "phase_like_radian",
        "trace_mean_square": "phase_like_radian_squared",
        "trace_is_exact_flat": "boolean_encoded_as_0_or_1",
        "valid_sample_fraction": "dimensionless_fraction",
        "source_quality_flag_count": "count",
    }
    return _empty_candidate(
        F3_SCHEMA_ID,
        "F3_TRACE_PLUS_QUALITY",
        "FEATURE_UNAVAILABLE_FLAT_TRACE" if flat else "AVAILABLE",
        features,
        units,
        ["EXACT_FLAT_TRACE"] if flat else [],
        {
            "trace_retained": True,
            "trace_persisted_by_extractor": False,
            "quality_flags": list(quality_flags),
            "r1_sign_policy": metadata.get("sign_policy", "UNVERIFIED"),
            "r1_validity_mask_semantics": metadata.get("validity_mask_semantics", "UNVERIFIED"),
        },
    )


def extract_feature_candidates(output: CommonTraceOutput) -> FeatureExtractionResult:
    """Extract the bounded R2 F1/F2/F3 candidates from one R1 output.

    No scale division, sign inversion, cross-domain calibration, reference
    matching, label construction, or model-dependent transformation occurs.
    """

    trace, time_s, fs, metadata = _validate_r1_output(output)
    base_features, base_units = _base_trace_features(trace, metadata)
    f1, spectral_diagnostics = _welch_descriptors(trace, fs, base_features, base_units)
    f2 = _autocorr_candidate(trace, fs, f1, spectral_diagnostics)
    f3 = _f3_candidate(trace, time_s, np.asarray(output.validity_mask, dtype=bool), fs, metadata)
    provenance = {
        "source_id": metadata.get("source_id", "UNVERIFIED"),
        "dataset_id": metadata.get("dataset_id", "UNVERIFIED"),
        "subject_id": metadata.get("subject_id", "UNVERIFIED"),
        "recording_id": metadata.get("recording_id", "UNVERIFIED"),
        "condition": metadata.get("condition", "UNVERIFIED"),
        "r1_profile_identity": metadata.get("profile_id", "UNVERIFIED"),
        "r1_trace_name": metadata.get("trace_name", "UNVERIFIED"),
        "r1_trace_units": metadata.get("trace_units", "UNVERIFIED"),
        "output_sampling_rate_hz": fs,
        "time_range_s": [float(time_s[0]), float(time_s[-1])],
        "source_provenance": metadata.get("provenance", {}),
        "reference_used_for_feature_selection": False,
        "source_sign_flipped": False,
        "cross_domain_gain_matching": False,
        "window_local_scale_normalization": False,
    }
    return FeatureExtractionResult(
        f1=f1,
        f2=f2,
        f3=f3,
        trace=trace.copy(),
        time_s=time_s.copy(),
        validity_mask=np.asarray(output.validity_mask, dtype=bool).copy(),
        provenance=provenance,
    )


__all__ = [
    "AUTOCORR_MAX_PERIOD_S",
    "AUTOCORR_MIN_PERIOD_S",
    "F1_FEATURE_NAMES",
    "F1_SCHEMA_ID",
    "F2_AUTOCORR_FEATURE_NAMES",
    "F2_SCHEMA_ID",
    "F3_FEATURE_NAMES",
    "F3_SCHEMA_ID",
    "FeatureCandidate",
    "FeatureExtractionResult",
    "MIN_SPECTRAL_SAMPLES",
    "R2_CANDIDATE_SET_ID",
    "R2_SCHEMA_VERSION",
    "R2FeatureError",
    "RESPIRATORY_BAND_HZ",
    "SPECTRAL_BAND_BIN_EDGES_HZ",
    "extract_feature_candidates",
]
