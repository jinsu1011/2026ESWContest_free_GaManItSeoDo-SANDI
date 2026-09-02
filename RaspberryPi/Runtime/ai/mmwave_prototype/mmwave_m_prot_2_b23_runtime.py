"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

Isolated M-PROT-2 B23 prototype runtime (not the integrated application).

This module freezes the B23 packaging/runtime contract. It does not train,
retune thresholds, convert TFLite, wire live sensors, or select a final model.
Wrong artifact or scaler identity fails closed. There is no fallback model.

Architecture:
  STAGE 0 — Runtime Input Admissibility Gate (stricter than training ingestion)
  STAGE 1 — Canonical B23 Model Preprocessing (R1 → R2 F2-scale/F3-quality → scaler once → 621-d)
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

import numpy as np
import torch

from ai.mmwave_prototype.mmwave_r1_sensor_independent_trace import CommonTraceOutput, R1_PROFILE_ID
from ai.mmwave_prototype.trace_model_support import (
    QUALITY_NAMES,
    SCALE_NAMES,
    TraceModel,
    _canonical_parameter_sha,
    _feature_arrays,
    _feature_matrix,
    _sha256_json,
    InputRecord,
)

CONTRACT_ID = "MMWAVE_V2_M_PROT_2_B23_DEPLOYABLE_RUNTIME_V1"
PROTOTYPE_VERSION_ID = "M_PROT_2_B23_PYTORCH_FLOAT32_V1"
INPUT_CONTRACT_VERSION = "M-PROT-2-B23-INPUT-V1"
PREPROCESS_CONTRACT_VERSION = "M-PROT-2-B23-PREPROCESS-V1"
OUTPUT_CONTRACT_VERSION = "M-PROT-2-B23-OUTPUT-V1"
FAIL_CLOSED_CONTRACT_VERSION = "M-PROT-2-B23-FAIL-CLOSED-V1"

PANEL_ID = "B23"
CANDIDATE_ID = "M-PV2_FAMILY_B_TRACE_TCN_BREATHING_RR_QUALITY"
FAMILY = "family_b"
SEED = 23
CONSTRUCTION_CLASS = "TraceModel"
CONSTRUCTION_SCRIPT = "ai/mmwave_prototype/trace_model_support.py"
SOURCE_ARTIFACT_REL = "models/mmwave/m_prot_b23/candidate_seed_23.pt"
SOURCE_ARTIFACT_SHA256 = "8f7de6f50d6ff62ff9b0ebfaed0b1fccd8d194c7e33781bc5b93366fae251a2c"
CANONICAL_PARAMETER_SHA256 = "6db949c242e25888dd20c3fc8e2305af03448aa229e3ca73e4159216a266d78e"
SOURCE_ARTIFACT_BYTES = 76473
PARAMETER_COUNT = 17915
INPUT_DIM = 621
WINDOW_DURATION_S = 30.0
SAMPLE_RATE_HZ = 10.0
TRACE_SAMPLES = 300
SCALE_DIM = 12
QUALITY_DIM = 9
ASSEMBLED_DTYPE = np.float32

SCALER_REL = "models/mmwave/m_prot_b23/scaler_statistics.json"
SCALER_CONTENT_SHA256 = "5a2583b5b5064be5480b0cf56f2a2c12d40a4a2d005eb087dc8e12106881159c"
SCALER_FILE_SHA256 = "9555c8c954078b80e26fbcd3bc5d5a70b9a2e04620946118709ec95418b2ac36"

R1_ADAPTER_MODULE = "ai/mmwave_prototype/mmwave_r1_sensor_independent_trace.py"
R1_PROFILE = R1_PROFILE_ID
R2_EXTRACTOR_MODULE = "ai/mmwave_prototype/mmwave_r2_representation_features.py"
R2_EXTRACTOR_FUNCTION = "extract_feature_candidates"
TRAINING_FEATURE_MATRIX_FUNCTION = "ai.mmwave_prototype.trace_model_support._feature_matrix"
TRAINING_FEATURE_ARRAYS_FUNCTION = "ai.mmwave_prototype.trace_model_support._feature_arrays"
RUNTIME_ASSEMBLER_FUNCTION = "ai.mmwave_prototype.mmwave_m_prot_2_b23_runtime.assemble_family_b_vector"
RUNTIME_FROM_R1_FUNCTION = "ai.mmwave_prototype.mmwave_m_prot_2_b23_runtime.assemble_from_r1_common_trace"

BREATHING_THRESHOLD = 0.5
QUALITY_THRESHOLD = 0.5
RR_MEAN = 17.12899193548387
RR_STD = 8.948729232744911

PRIMARY_REPRESENTATION = "PYTORCH_FLOAT32_STATE_DICT"
TFLITE_CONVERSION = "NOT_YET_PROVEN"
INT8 = "NOT_AUTHORIZED_IN_M_PROT_2"

MANDATORY_SEMANTICS = (
    "PROTOTYPE_INTEGRATION_ONLY",
    "NOT_FINAL_SELECTED_MODEL",
    "NOT_DEPLOYMENT_VALIDATED",
    "NOT_SAFETY_VALIDATED",
    "NOT_CLINICAL_VALIDATION",
    "SUBJECT_TO_REPLACEMENT",
)

ALLOWED_LINEAGE_CLASSES = frozenset({"FIXTURE_NON_CAMPAIGN", "DEBUG_CAPTURE"})
LineageClass = Literal["FIXTURE_NON_CAMPAIGN", "DEBUG_CAPTURE"]

SCALE_FEATURE_NAMES = tuple(SCALE_NAMES)
QUALITY_FEATURE_NAMES = tuple(QUALITY_NAMES)

from paths import ONDEVICE_AI_ROOT as _REPO_ROOT  # team Ondevice_AI root


class PrototypeFailClosed(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PrototypeReceipt:
    status: str
    fail_closed_code: str | None
    breathing_logit: float | None
    breathing_probability: float | None
    breathing_decision: str | None
    rr_raw: float | None
    rr_bpm: float | None
    rr_status: str | None
    quality_logit: float | None
    quality_probability: float | None
    quality_decision: str | None
    artifact_sha256: str | None
    scaler_content_sha256: str | None
    representation: str
    prototype_version_id: str
    mandatory_semantics: tuple[str, ...]
    apnea_emitted: bool
    lineage_class: str
    identities_verified: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "M-PROT-2-INFERENCE-RECEIPT-V1",
            "status": self.status,
            "fail_closed_code": self.fail_closed_code,
            "panel_id": PANEL_ID,
            "candidate_id": CANDIDATE_ID,
            "prototype_version_id": self.prototype_version_id,
            "representation": self.representation,
            "artifact_sha256": self.artifact_sha256,
            "scaler_content_sha256": self.scaler_content_sha256,
            "identities_verified": self.identities_verified,
            "breathing_logit": self.breathing_logit,
            "breathing_probability": self.breathing_probability,
            "breathing_decision": self.breathing_decision,
            "rr_raw": self.rr_raw,
            "rr_bpm": self.rr_bpm,
            "rr_status": self.rr_status,
            "quality_logit": self.quality_logit,
            "quality_probability": self.quality_probability,
            "quality_decision": self.quality_decision,
            "apnea_emitted": self.apnea_emitted,
            "mandatory_semantics": list(self.mandatory_semantics),
            "lineage_class": self.lineage_class,
            "PROTOTYPE_INTEGRATION_ONLY": True,
            "FINAL_GOVERNED_EVALUATION": False,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_lineage_class(value: Any) -> str:
    if value is None:
        return "FIXTURE_NON_CAMPAIGN"
    text = str(value)
    if text in {"FINAL_GOVERNED_EVALUATION", "DEVICE_DOMAIN_DEVELOPMENT"}:
        raise PrototypeFailClosed(
            "LINEAGE_CLASS_FORBIDDEN",
            f"{text} is not an authorized M-PROT-2 caller lineage class",
        )
    if text not in ALLOWED_LINEAGE_CLASSES:
        raise PrototypeFailClosed("LINEAGE_CLASS_FORBIDDEN", f"unsupported lineage_class={text}")
    return text


def verify_scaler_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Independently verify an in-memory scaler mapping against the frozen identity."""
    if not isinstance(payload, Mapping):
        raise PrototypeFailClosed("SCALER_SHA_MISMATCH", "scaler payload is not a mapping")
    content = {key: value for key, value in payload.items() if key != "sha256"}
    content_sha = _sha256_json(content)
    declared = payload.get("sha256")
    if content_sha != SCALER_CONTENT_SHA256 or declared != SCALER_CONTENT_SHA256:
        raise PrototypeFailClosed(
            "SCALER_SHA_MISMATCH",
            f"scaler content sha {content_sha} != {SCALER_CONTENT_SHA256}",
        )
    if tuple(payload.get("scale", {}).get("names", ())) != SCALE_FEATURE_NAMES:
        raise PrototypeFailClosed("SCALER_FEATURE_ORDER_MISMATCH", "scale feature order drifted")
    if tuple(payload.get("quality", {}).get("names", ())) != QUALITY_FEATURE_NAMES:
        raise PrototypeFailClosed("SCALER_FEATURE_ORDER_MISMATCH", "quality feature order drifted")
    for group in ("scale", "quality"):
        block = payload[group]
        if len(block["mean"]) != len(SCALE_FEATURE_NAMES if group == "scale" else QUALITY_FEATURE_NAMES):
            raise PrototypeFailClosed("SCALER_SHA_MISMATCH", f"{group} mean length drifted")
        if len(block["std"]) != len(block["mean"]):
            raise PrototypeFailClosed("SCALER_SHA_MISMATCH", f"{group} std length drifted")
    return dict(payload)


def verify_scaler(root: Path | None = None, scaler_path: Path | None = None) -> dict[str, Any]:
    root = Path(root or _REPO_ROOT)
    path = Path(scaler_path) if scaler_path is not None else root / SCALER_REL
    if not path.is_file():
        raise PrototypeFailClosed("SCALER_MISSING", f"missing scaler: {path.as_posix()}")
    file_sha = sha256_file(path)
    if file_sha != SCALER_FILE_SHA256:
        raise PrototypeFailClosed(
            "SCALER_SHA_MISMATCH",
            f"scaler file sha {file_sha} != {SCALER_FILE_SHA256}",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return verify_scaler_payload(payload)


def verify_artifact(root: Path | None = None, artifact_path: Path | None = None) -> bytes:
    root = Path(root or _REPO_ROOT)
    path = Path(artifact_path) if artifact_path is not None else root / SOURCE_ARTIFACT_REL
    if not path.is_file():
        raise PrototypeFailClosed("ARTIFACT_MISSING", f"missing artifact: {path.as_posix()}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_ARTIFACT_SHA256:
        raise PrototypeFailClosed(
            "ARTIFACT_SHA_MISMATCH",
            f"artifact sha {digest} != {SOURCE_ARTIFACT_SHA256}",
        )
    if len(data) != SOURCE_ARTIFACT_BYTES:
        raise PrototypeFailClosed(
            "ARTIFACT_SHA_MISMATCH",
            f"artifact bytes {len(data)} != {SOURCE_ARTIFACT_BYTES}",
        )
    return data


def verify_model_identity(model: torch.nn.Module) -> str:
    """Reject any injected model whose parameters are not exact B23."""
    if not isinstance(model, TraceModel):
        raise PrototypeFailClosed(
            "MODEL_IDENTITY_MISMATCH",
            f"model type {type(model).__name__} is not TraceModel",
        )
    sha = _canonical_parameter_sha(model)
    if sha != CANONICAL_PARAMETER_SHA256:
        raise PrototypeFailClosed(
            "MODEL_IDENTITY_MISMATCH",
            f"canonical parameter sha {sha} != {CANONICAL_PARAMETER_SHA256}",
        )
    return sha


def load_b23_model(root: Path | None = None, artifact_path: Path | None = None) -> torch.nn.Module:
    root = Path(root or _REPO_ROOT)
    path = Path(artifact_path) if artifact_path is not None else root / SOURCE_ARTIFACT_REL
    verify_artifact(root, path)
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or "state_dict" not in payload:
        raise PrototypeFailClosed("ARTIFACT_SHA_MISMATCH", "payload is not a B23 state_dict package")
    if payload.get("family") != FAMILY or int(payload.get("seed", -1)) != SEED:
        raise PrototypeFailClosed("ARTIFACT_SHA_MISMATCH", "payload family/seed is not B23")
    model = TraceModel(INPUT_DIM, FAMILY)
    model.load_state_dict(payload["state_dict"], strict=True)
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    verify_model_identity(model)
    return model


def resolve_verified_runtime(
    *,
    root: Path | None = None,
    model: torch.nn.Module | None = None,
    scaler: Mapping[str, Any] | None = None,
    artifact_path: Path | None = None,
    scaler_path: Path | None = None,
) -> tuple[torch.nn.Module, dict[str, Any]]:
    """Always verify scaler + model identities before inference.

    Injected objects are allowed only after independent identity checks.
    Caller assertions are never trusted.
    """
    root = Path(root or _REPO_ROOT)
    if scaler is not None:
        scaler_payload = verify_scaler_payload(scaler)
    else:
        scaler_payload = verify_scaler(root, scaler_path)
    if model is not None:
        verify_model_identity(model)
        if artifact_path is not None:
            verify_artifact(root, artifact_path)
        loaded = model
    else:
        loaded = load_b23_model(root, artifact_path)
    return loaded, scaler_payload


def _as_float32_vector(name: str, value: Any, expected: int, code: str) -> np.ndarray:
    if value is None:
        raise PrototypeFailClosed("INCOMPLETE_INPUT", f"missing {name}")
    try:
        array = np.asarray(value)
        if array.dtype.kind not in {"b", "i", "u", "f"}:
            raise PrototypeFailClosed("WRONG_DTYPE", f"{name} dtype {array.dtype} is not numeric")
        array = np.asarray(array, dtype=np.float32).reshape(-1)
    except PrototypeFailClosed:
        raise
    except (TypeError, ValueError) as exc:
        raise PrototypeFailClosed("WRONG_DTYPE", f"{name} is not safely convertible to float32") from exc
    if array.size != expected:
        raise PrototypeFailClosed(code, f"{name} length {array.size} != {expected}")
    return array


def _window_scale_descriptors(trace: np.ndarray) -> dict[str, float]:
    centered = trace - float(np.median(trace))
    return {
        "mad_about_median": float(np.median(np.abs(centered))),
        "robust_rms_about_median": float(np.sqrt(np.mean(centered * centered))),
        "robust_peak_to_peak_p05_p95": float(np.percentile(trace, 95.0) - np.percentile(trace, 5.0)),
        "peak_to_peak": float(np.max(trace) - np.min(trace)),
    }


def build_r1_common_trace_from_window(
    trace: Any,
    trace_mask: Any | None = None,
    *,
    quality_flags: list[str] | None = None,
) -> CommonTraceOutput:
    """Build an accepted R1 CommonTraceOutput for a frozen 30 s / 10 Hz window.

    This is the M-PROT-2 upstream handoff identity for descriptor extraction.
    Live MR60 resampling remains M-PROT-3; this function freezes the already-
    windowed R1 contract surface.
    """
    trace_v = _as_float32_vector("trace", trace, TRACE_SAMPLES, "WRONG_DIMENSION")
    if np.any(~np.isfinite(trace_v)):
        raise PrototypeFailClosed("NON_FINITE_INPUT", "non-finite values in R1 window trace")
    if trace_mask is None:
        mask_bool = np.ones(TRACE_SAMPLES, dtype=bool)
    else:
        mask_v = _as_float32_vector("trace_mask", trace_mask, TRACE_SAMPLES, "MISSING_TRACE_MASK")
        if np.any(~np.isfinite(mask_v)):
            raise PrototypeFailClosed("NON_FINITE_INPUT", "non-finite values in R1 window mask")
        if np.any((mask_v != 0.0) & (mask_v != 1.0)):
            raise PrototypeFailClosed("MISSING_TRACE_MASK", "trace_mask must be 0/1")
        mask_bool = mask_v.astype(bool)
    descriptor = _window_scale_descriptors(trace_v.astype(np.float64))
    flags = list(quality_flags or ["R1_FINITE_OUTPUT", "M_PROT_2_WINDOW_CONTEXT"])
    metadata = {
        "schema_version": "R1.1",
        "contract_id": "R1_SENSOR_INDEPENDENT_TRACE_CONTRACT_V1",
        "profile_id": R1_PROFILE,
        "source_id": "M_PROT_2_FIXTURE",
        "dataset_id": "m_prot_2_deterministic_fixture",
        "subject_id": "FIXTURE",
        "recording_id": "M_PROT_2_R1_WINDOW",
        "condition": "FIXTURE_NON_CAMPAIGN",
        "trace_name": "respiratory_motion_trace",
        "trace_units": "phase_like_radian; absolute displacement equivalence not claimed",
        "sign_policy": "PRESERVE_SOURCE_SIGN; SIGN_ALIGNMENT_UNVERIFIED",
        "output_sampling_rate_hz": SAMPLE_RATE_HZ,
        "source_sampling_rate_hz": SAMPLE_RATE_HZ,
        "native_scale_metadata": {
            "native_descriptors": descriptor,
            "common_trace_descriptors_after_centering": descriptor,
            "native_scale_preserved": True,
            "scale_normalization_applied": False,
            "sensor_gain_matching_applied": False,
            "sign_inversion_applied": False,
        },
        "quality_flags": flags,
        "provenance": {
            "m_prot_2_window": True,
            "context_derivation": "FIXED_30S_10HZ_WINDOW",
        },
        "validity_mask_semantics": "TRUE_ONLY_FOR_FINITE_TRACE_WITH_VALID_TIMING; NO_ZERO_FILL",
    }
    return CommonTraceOutput(
        trace=trace_v.astype(np.float64),
        time_s=np.arange(TRACE_SAMPLES, dtype=np.float64) / SAMPLE_RATE_HZ,
        validity_mask=mask_bool,
        metadata=metadata,
    )


def extract_profile_b_descriptors(common: CommonTraceOutput) -> dict[str, np.ndarray]:
    """Canonical R2 extraction used by M-PV2 training for Family B scale/quality."""
    trace, mask, _f2, _f2_mask, descriptors = _feature_arrays(common)
    scale = np.asarray(descriptors[:SCALE_DIM], dtype=np.float32)
    quality = np.asarray(descriptors[SCALE_DIM:], dtype=np.float32)
    if scale.size != SCALE_DIM or quality.size != QUALITY_DIM:
        raise PrototypeFailClosed("WRONG_DIMENSION", "descriptor extraction did not yield 12+9 features")
    return {
        "trace": np.asarray(trace, dtype=np.float32),
        "trace_mask": np.asarray(mask, dtype=np.float32),
        "scale": scale,
        "quality": quality,
    }


def stage0_runtime_admissibility(
    *,
    trace: Any,
    trace_mask: Any,
    scale: Any,
    quality: Any,
    already_zscored: bool = False,
    window_valid: Any = True,
    sample_count: Any = None,
    presence_available: Any = True,
    availability_state: Any = None,
) -> dict[str, np.ndarray]:
    """STAGE 0 — reject invalid runtime inputs before model preprocessing.

    Runtime admissibility is stricter than historical training ingestion:
    non-finite values fail closed here instead of nan_to_num fill.
    """
    if already_zscored:
        raise PrototypeFailClosed("DOUBLE_ZSCORE_FORBIDDEN", "runtime must apply the TRAIN scaler once")
    if window_valid is False:
        raise PrototypeFailClosed("INCOMPLETE_INPUT", "window_valid is false")
    if sample_count is not None and int(sample_count) < TRACE_SAMPLES:
        raise PrototypeFailClosed("INCOMPLETE_INPUT", "fewer than 300 required samples")
    if presence_available is False:
        raise PrototypeFailClosed("PRESENCE_UNAVAILABLE", "presence is unavailable")
    if availability_state in {"INPUT_UNAVAILABLE", "PRESENCE_SUPPRESSED"}:
        raise PrototypeFailClosed(str(availability_state), f"availability_state={availability_state}")
    trace_v = _as_float32_vector("trace", trace, TRACE_SAMPLES, "WRONG_DIMENSION")
    mask_v = _as_float32_vector("trace_mask", trace_mask, TRACE_SAMPLES, "MISSING_TRACE_MASK")
    scale_v = _as_float32_vector("scale", scale, SCALE_DIM, "WRONG_SCALE_DIM")
    quality_v = _as_float32_vector("quality", quality, QUALITY_DIM, "WRONG_QUALITY_DIM")
    if np.any(~np.isfinite(trace_v)) or np.any(~np.isfinite(mask_v)) or np.any(~np.isfinite(scale_v)) or np.any(~np.isfinite(quality_v)):
        raise PrototypeFailClosed("NON_FINITE_INPUT", "non-finite values rejected by admissibility gate")
    if np.any((mask_v != 0.0) & (mask_v != 1.0)):
        raise PrototypeFailClosed("MISSING_TRACE_MASK", "trace_mask must be 0/1")
    return {
        "trace": trace_v,
        "trace_mask": mask_v,
        "scale": scale_v,
        "quality": quality_v,
    }


def stage1_canonical_preprocess(
    accepted: Mapping[str, np.ndarray],
    scaler: Mapping[str, Any],
) -> np.ndarray:
    """STAGE 1 — canonical B23 preprocessing for already-admitted finite inputs.

    Matches Family B layout of training ``_feature_matrix`` for finite values.
    Historical training filled non-finite assembled values with zeros; Stage 0
    already rejected those inputs, so Stage 1 does not fill zeros.
    """
    verify_scaler_payload(scaler)
    trace_z = (accepted["trace"] - np.float32(scaler["trace"]["mean"])) / np.float32(scaler["trace"]["std"])
    scale_z = (accepted["scale"] - np.asarray(scaler["scale"]["mean"], dtype=np.float32)) / np.asarray(
        scaler["scale"]["std"], dtype=np.float32
    )
    quality_z = (accepted["quality"] - np.asarray(scaler["quality"]["mean"], dtype=np.float32)) / np.asarray(
        scaler["quality"]["std"], dtype=np.float32
    )
    vector = np.concatenate([trace_z, accepted["trace_mask"], scale_z, quality_z]).astype(np.float32, copy=False)
    if vector.size != INPUT_DIM:
        raise PrototypeFailClosed("WRONG_DIMENSION", f"assembled dim {vector.size} != {INPUT_DIM}")
    if np.any(~np.isfinite(vector)):
        raise PrototypeFailClosed("NON_FINITE_INPUT", "non-finite values after TRAIN scaler")
    return vector


def assemble_family_b_vector(
    *,
    trace: Any,
    trace_mask: Any,
    scale: Any,
    quality: Any,
    scaler: Mapping[str, Any],
    already_zscored: bool = False,
) -> np.ndarray:
    accepted = stage0_runtime_admissibility(
        trace=trace,
        trace_mask=trace_mask,
        scale=scale,
        quality=quality,
        already_zscored=already_zscored,
    )
    return stage1_canonical_preprocess(accepted, scaler)


def assemble_from_r1_common_trace(
    common: CommonTraceOutput,
    scaler: Mapping[str, Any],
) -> np.ndarray:
    """Canonical R1 CommonTrace → R2 descriptors → Stage0/1 → 621-d vector."""
    descriptors = extract_profile_b_descriptors(common)
    return assemble_family_b_vector(
        trace=descriptors["trace"],
        trace_mask=descriptors["trace_mask"],
        scale=descriptors["scale"],
        quality=descriptors["quality"],
        scaler=scaler,
        already_zscored=False,
    )


def training_side_family_b_vector(
    common: CommonTraceOutput,
    scaler: Mapping[str, Any],
) -> np.ndarray:
    """Exact M-PV2 training-side Family B vector for parity comparison.

    Uses ``_feature_arrays`` + ``_feature_matrix`` (including historical
    training non-finite fill). For finite accepted fixtures this should match Stage 1.
    """
    verify_scaler_payload(scaler)
    trace, mask, f2, f2_mask, descriptors = _feature_arrays(common)
    record = InputRecord(
        source_id="M_PROT_2_PARITY",
        subject_id="FIXTURE",
        recording_id="PARITY",
        model_input_id="M_PROT_2_PARITY",
        split="TRAIN",
        trace=np.asarray(trace, dtype=np.float32),
        trace_mask=np.asarray(mask, dtype=bool),
        f2=np.asarray(f2, dtype=np.float32),
        f2_mask=np.asarray(f2_mask, dtype=bool),
        scale=np.asarray(descriptors[:SCALE_DIM], dtype=np.float32),
        quality=np.asarray(descriptors[SCALE_DIM:], dtype=np.float32),
        breathing_label=0.0,
        breathing_mask=0.0,
        rr_bpm=float("nan"),
        rr_mask=0.0,
        quality_label=0.0,
        quality_mask=0.0,
        breathing_state="PARITY",
        rr_target_status="UNUSED",
        quality_status="CLEAN",
        provenance={"parity": True},
        is_synthetic=True,
        corruption_mode=None,
    )
    matrix = _feature_matrix([record], FAMILY, scaler)
    return np.asarray(matrix[0], dtype=np.float32)


def sigmoid(logit: float) -> float:
    return float(1.0 / (1.0 + math.exp(-float(logit))))


def decode_rr(rr_raw: float) -> tuple[float | None, str]:
    if not math.isfinite(rr_raw):
        return None, "UNAVAILABLE_INVALID_DECODE"
    rr_bpm = float(rr_raw) * RR_STD + RR_MEAN
    if not math.isfinite(rr_bpm) or rr_bpm <= 0.0:
        return None, "UNAVAILABLE_INVALID_DECODE"
    return rr_bpm, "EMITTED"


def _unavailable_receipt(
    code: str,
    *,
    lineage_class: str,
    identities_verified: bool,
    artifact_sha: str | None,
    scaler_sha: str | None,
) -> PrototypeReceipt:
    return PrototypeReceipt(
        status="UNAVAILABLE",
        fail_closed_code=code,
        breathing_logit=None,
        breathing_probability=None,
        breathing_decision=None,
        rr_raw=None,
        rr_bpm=None,
        rr_status="UNAVAILABLE",
        quality_logit=None,
        quality_probability=None,
        quality_decision=None,
        artifact_sha256=artifact_sha if identities_verified else None,
        scaler_content_sha256=scaler_sha if identities_verified else None,
        representation=PRIMARY_REPRESENTATION,
        prototype_version_id=PROTOTYPE_VERSION_ID,
        mandatory_semantics=MANDATORY_SEMANTICS,
        apnea_emitted=False,
        lineage_class=lineage_class,
        identities_verified=identities_verified,
    )


def run_prototype_inference(
    fixture: Mapping[str, Any],
    *,
    root: Path | None = None,
    model: torch.nn.Module | None = None,
    scaler: Mapping[str, Any] | None = None,
    artifact_path: Path | None = None,
    scaler_path: Path | None = None,
    lineage_class: LineageClass | str | None = None,
) -> PrototypeReceipt:
    """Authoritative B23 prototype inference.

    Injected ``model`` / ``scaler`` objects are independently identity-checked.
    Canonical B23/scaler receipt fields are emitted only after verification.
    """
    root = Path(root or _REPO_ROOT)
    try:
        lineage = _normalize_lineage_class(
            lineage_class if lineage_class is not None else fixture.get("lineage_class", "FIXTURE_NON_CAMPAIGN")
        )
    except PrototypeFailClosed as exc:
        return _unavailable_receipt(
            exc.code,
            lineage_class="FIXTURE_NON_CAMPAIGN",
            identities_verified=False,
            artifact_sha=None,
            scaler_sha=None,
        )
    identities_verified = False
    try:
        loaded, scaler_payload = resolve_verified_runtime(
            root=root,
            model=model,
            scaler=scaler,
            artifact_path=artifact_path,
            scaler_path=scaler_path,
        )
        identities_verified = True
        if "common_trace" in fixture:
            common = fixture["common_trace"]
            if not isinstance(common, CommonTraceOutput):
                raise PrototypeFailClosed("INCOMPLETE_INPUT", "common_trace must be CommonTraceOutput")
            # Admissibility for presence/availability still applies around R1 path.
            if fixture.get("presence_available") is False:
                raise PrototypeFailClosed("PRESENCE_UNAVAILABLE", "presence is unavailable")
            if fixture.get("availability_state") in {"INPUT_UNAVAILABLE", "PRESENCE_SUPPRESSED"}:
                raise PrototypeFailClosed(str(fixture["availability_state"]), "availability fail")
            if bool(fixture.get("already_zscored")):
                raise PrototypeFailClosed("DOUBLE_ZSCORE_FORBIDDEN", "do not pass z-scored tensors")
            vector = assemble_from_r1_common_trace(common, scaler_payload)
        else:
            accepted = stage0_runtime_admissibility(
                trace=fixture.get("trace"),
                trace_mask=fixture.get("trace_mask"),
                scale=fixture.get("scale"),
                quality=fixture.get("quality"),
                already_zscored=bool(fixture.get("already_zscored", False)),
                window_valid=fixture.get("window_valid", True),
                sample_count=fixture.get("sample_count"),
                presence_available=fixture.get("presence_available", True),
                availability_state=fixture.get("availability_state"),
            )
            vector = stage1_canonical_preprocess(accepted, scaler_payload)
        with torch.no_grad():
            tensor = torch.from_numpy(vector[None, :])
            outputs = loaded(tensor)
        breathing_logit = float(outputs["breathing"].reshape(-1)[0].cpu())
        rr_raw = float(outputs["rr"].reshape(-1)[0].cpu())
        quality_logit = float(outputs["quality"].reshape(-1)[0].cpu())
        breathing_probability = sigmoid(breathing_logit)
        quality_probability = sigmoid(quality_logit)
        if not math.isfinite(breathing_probability) or not math.isfinite(quality_probability):
            raise PrototypeFailClosed("NON_FINITE_INPUT", "non-finite head outputs")
        breathing_decision = "PRESENT" if breathing_probability >= BREATHING_THRESHOLD else "ABSENT"
        if breathing_decision == "ABSENT":
            quality_decision = "BELOW_PROTOTYPE_THRESHOLD" if quality_probability < QUALITY_THRESHOLD else "ABOVE_PROTOTYPE_THRESHOLD"
            return PrototypeReceipt(
                status="ABSENT",
                fail_closed_code=None,
                breathing_logit=breathing_logit,
                breathing_probability=breathing_probability,
                breathing_decision="ABSENT",
                rr_raw=None,
                rr_bpm=None,
                rr_status="SUPPRESSED_ABSENT",
                quality_logit=quality_logit,
                quality_probability=quality_probability,
                quality_decision=quality_decision,
                artifact_sha256=SOURCE_ARTIFACT_SHA256,
                scaler_content_sha256=SCALER_CONTENT_SHA256,
                representation=PRIMARY_REPRESENTATION,
                prototype_version_id=PROTOTYPE_VERSION_ID,
                mandatory_semantics=MANDATORY_SEMANTICS,
                apnea_emitted=False,
                lineage_class=lineage,
                identities_verified=True,
            )
        if quality_probability < QUALITY_THRESHOLD:
            return PrototypeReceipt(
                status="QUALITY_SUPPRESSED",
                fail_closed_code="QUALITY_FAIL",
                breathing_logit=breathing_logit,
                breathing_probability=breathing_probability,
                breathing_decision="PRESENT",
                rr_raw=None,
                rr_bpm=None,
                rr_status="SUPPRESSED_QUALITY",
                quality_logit=quality_logit,
                quality_probability=quality_probability,
                quality_decision="BELOW_PROTOTYPE_THRESHOLD",
                artifact_sha256=SOURCE_ARTIFACT_SHA256,
                scaler_content_sha256=SCALER_CONTENT_SHA256,
                representation=PRIMARY_REPRESENTATION,
                prototype_version_id=PROTOTYPE_VERSION_ID,
                mandatory_semantics=MANDATORY_SEMANTICS,
                apnea_emitted=False,
                lineage_class=lineage,
                identities_verified=True,
            )
        rr_bpm, rr_status = decode_rr(rr_raw)
        if rr_status != "EMITTED":
            return PrototypeReceipt(
                status="RR_UNAVAILABLE",
                fail_closed_code="UNAVAILABLE_INVALID_DECODE",
                breathing_logit=breathing_logit,
                breathing_probability=breathing_probability,
                breathing_decision="PRESENT",
                rr_raw=rr_raw,
                rr_bpm=None,
                rr_status=rr_status,
                quality_logit=quality_logit,
                quality_probability=quality_probability,
                quality_decision="ABOVE_PROTOTYPE_THRESHOLD",
                artifact_sha256=SOURCE_ARTIFACT_SHA256,
                scaler_content_sha256=SCALER_CONTENT_SHA256,
                representation=PRIMARY_REPRESENTATION,
                prototype_version_id=PROTOTYPE_VERSION_ID,
                mandatory_semantics=MANDATORY_SEMANTICS,
                apnea_emitted=False,
                lineage_class=lineage,
                identities_verified=True,
            )
        return PrototypeReceipt(
            status="PHYSIOLOGY_ELIGIBLE",
            fail_closed_code=None,
            breathing_logit=breathing_logit,
            breathing_probability=breathing_probability,
            breathing_decision="PRESENT",
            rr_raw=rr_raw,
            rr_bpm=rr_bpm,
            rr_status="EMITTED",
            quality_logit=quality_logit,
            quality_probability=quality_probability,
            quality_decision="ABOVE_PROTOTYPE_THRESHOLD",
            artifact_sha256=SOURCE_ARTIFACT_SHA256,
            scaler_content_sha256=SCALER_CONTENT_SHA256,
            representation=PRIMARY_REPRESENTATION,
            prototype_version_id=PROTOTYPE_VERSION_ID,
            mandatory_semantics=MANDATORY_SEMANTICS,
            apnea_emitted=False,
            lineage_class=lineage,
            identities_verified=True,
        )
    except PrototypeFailClosed as exc:
        return _unavailable_receipt(
            exc.code,
            lineage_class=lineage,
            identities_verified=identities_verified,
            artifact_sha=SOURCE_ARTIFACT_SHA256 if identities_verified else None,
            scaler_sha=SCALER_CONTENT_SHA256 if identities_verified else None,
        )


def valid_fixture_from_scaler(scaler: Mapping[str, Any], *, seed: int = 23) -> dict[str, Any]:
    """Deterministic software-only fixture for reference decode / integrity tests.

    Trace is a fixed sine; scale/quality use TRAIN scaler means so the three
    heads and decode path execute without live or evaluation data. This is
    ``FIXTURE_NON_CAMPAIGN`` evidence, not a claim that means equal R2 output.
    Canonical R1→R2 descriptor parity uses ``valid_r1_parity_fixture``.
    """
    verify_scaler_payload(scaler)
    del seed  # reserved for deterministic variants; default path is fixed.
    t = np.arange(TRACE_SAMPLES, dtype=np.float32) / np.float32(SAMPLE_RATE_HZ)
    trace = np.sin(2.0 * np.pi * np.float32(0.25) * t).astype(np.float32)
    return {
        "trace": trace.tolist(),
        "trace_mask": np.ones(TRACE_SAMPLES, dtype=np.float32).tolist(),
        "scale": [float(x) for x in scaler["scale"]["mean"]],
        "quality": [float(x) for x in scaler["quality"]["mean"]],
        "window_valid": True,
        "presence_available": True,
        "sample_count": TRACE_SAMPLES,
        "already_zscored": False,
        "lineage_class": "FIXTURE_NON_CAMPAIGN",
    }


def valid_r1_parity_fixture(*, seed: int = 23) -> CommonTraceOutput:
    """Deterministic R1 CommonTraceOutput for canonical descriptor/parity tests."""
    rng = np.random.default_rng(seed)
    t = np.arange(TRACE_SAMPLES, dtype=np.float64) / SAMPLE_RATE_HZ
    amp = 0.42 + 0.08 * float(rng.random())
    freq = 0.22 + 0.06 * float(rng.random())
    trace = (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float64)
    # Mild non-stationary envelope keeps spectral descriptors well-defined.
    trace = trace * (0.85 + 0.15 * np.sin(2.0 * np.pi * 0.03 * t))
    return build_r1_common_trace_from_window(trace)


FIXTURE_OVERLAY_CONTRACT_VERSION = "M-PROT-2-FIXTURE-OVERLAY-V1"
GOVERNED_FIXTURE_DIR_REL = (
    "datasets/mmwave/manifests/M_PROT_2_deployable_artifact_runtime_contract/fixtures"
)


def capture_runtime_environment() -> dict[str, Any]:
    """Record the CPU software environment that produced a reference receipt."""
    import platform
    import sys

    return {
        "schema_version": "M-PROT-2-REFERENCE-RECEIPT-ENVIRONMENT-V1",
        "python_version": sys.version.split()[0],
        "torch_version": torch.__version__,
        "numpy_version": np.__version__,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "processor": platform.processor() or "UNAVAILABLE",
        "device": "CPU",
        "REFERENCE_RECEIPT_ENVIRONMENT_BOUND": True,
        "BIT_EXACT_OUTPUT_ACROSS_DIFFERENT_TORCH_VERSIONS": "NOT_GUARANTEED",
        "SAME_FROZEN_RUNTIME_ENVIRONMENT": "deterministic result expected",
        "CROSS_RUNTIME_SEMANTIC_MATCH": "DESCRIPTIVE_ONLY",
    }


def _safe_fixture_path(path: Path, fixture_root: Path) -> Path:
    resolved = path.resolve()
    root = fixture_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PrototypeFailClosed(
            "FIXTURE_PATH_ESCAPE",
            f"fixture path escapes governed directory: {resolved.as_posix()}",
        ) from exc
    return resolved


def resolve_fixture_document(
    fixture_path: Path,
    *,
    fixture_root: Path | None = None,
    _stack: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Load a fixture JSON, resolving optional base+overrides overlays.

    Overlay contract (``M-PROT-2-FIXTURE-OVERLAY-V1``):
    - ``base`` is a filename relative to the governed fixtures directory only
    - ``overrides`` shallow-merge onto the resolved base mapping
    - missing base / path escape / cyclic base chains fail closed
    - standalone fixtures without ``base`` load as-is
    """
    fixture_root = Path(fixture_root or (Path(fixture_path).resolve().parent))
    path = _safe_fixture_path(Path(fixture_path), fixture_root)
    if path in _stack:
        chain = " -> ".join(item.name for item in (_stack + (path,)))
        raise PrototypeFailClosed("FIXTURE_BASE_CYCLE", f"cyclic fixture base chain: {chain}")
    if not path.is_file():
        raise PrototypeFailClosed("FIXTURE_MISSING", f"missing fixture: {path.as_posix()}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrototypeFailClosed("FIXTURE_INVALID", f"cannot parse fixture JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PrototypeFailClosed("FIXTURE_INVALID", "fixture root must be a JSON object")
    if "base" not in payload:
        return payload
    base_name = payload.get("base")
    if not isinstance(base_name, str) or not base_name or "/" in base_name or "\\" in base_name or base_name in {".", ".."}:
        raise PrototypeFailClosed(
            "FIXTURE_BASE_INVALID",
            "base must be a plain filename inside the governed fixtures directory",
        )
    overrides = payload.get("overrides", {})
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise PrototypeFailClosed("FIXTURE_INVALID", "overrides must be a JSON object")
    reserved = {key for key in payload if key not in {"base", "overrides", "expected_fail_closed_code", "notes"}}
    if reserved:
        raise PrototypeFailClosed(
            "FIXTURE_INVALID",
            f"overlay fixture may only contain base/overrides(+metadata); unexpected keys={sorted(reserved)}",
        )
    base_path = _safe_fixture_path(fixture_root / base_name, fixture_root)
    if not base_path.is_file():
        raise PrototypeFailClosed("FIXTURE_BASE_MISSING", f"missing base fixture: {base_name}")
    merged = dict(resolve_fixture_document(base_path, fixture_root=fixture_root, _stack=_stack + (path,)))
    merged.update(overrides)
    return merged