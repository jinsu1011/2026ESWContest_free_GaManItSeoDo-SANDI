"""Strict C-B6 reduced-feature CO2 occupancy interpreter (CO2 + CO2_slope only).

Supersedes the historical 3-feature ``co2_interpreter.py``
(``co2_slope_humidity_co2_ppm``). Humidity is not merely unused here - the locked
input contract lists it under ``forbidden_additional_inputs``, so this adapter
refuses to accept it.

Feature order is ``["CO2", "CO2_slope"]`` (ppm, ppm/min) - note that this is the
reverse of the historical v0.1.0 ordering, which put slope first.

Slope itself is produced outside this adapter by the runtime window builder that
implements ``CO2_SLOPE_FEATURE_PROFILE_001``; this class only standardises,
quantises and invokes.

Semantics are room occupancy only. ``class_map.json`` declares
``risk_semantic: NONE`` and ``safety_semantic: NONE``, so the output must never
be fused into the safety risk score as if it were a hazard signal.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

import numpy as np

try:
    from ai_edge_litert.interpreter import Interpreter
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        from tensorflow.lite import Interpreter


MODEL_ID = "C_B6_REDUCED_CO2_SLOPE_CANDIDATE_001"
MODEL_SHA256 = "c5969b367f5b5e28c4d27f1bdd6220f7d02da92e99604e3f85c9f1291e98dd3b"
CONTRACT_ID = "C_B6_REDUCED_CO2_SLOPE_CANDIDATE_001"
SCALER_PROFILE_ID = "CO2_C_B6_TRAIN_ONLY_STANDARD_SCALER_001"
SCALER_FINGERPRINT = "a92123ad37e9b284929ba0fe53179126345d54d487ec4b3a73c910d00490a462"
FEATURE_ORDER = ("CO2", "CO2_slope")
FORBIDDEN_INPUTS = ("Temperature", "Humidity", "Light", "time_of_day")
CLASS_MAP = {0: "VACANT", 1: "OCCUPIED"}
POSITIVE_CLASS = "OCCUPIED"

INPUT_SHAPE = [1, 2]
INPUT_QUANTIZATION = (0.03921568766236305, 0)
OUTPUT_SHAPE = [1, 1]
OUTPUT_QUANTIZATION = (0.00390625, -128)


@dataclass(frozen=True)
class CB6Prediction:
    class_index: int
    class_name: str
    confidence: float
    probabilities: list[float]
    latency_ms: float
    occupancy_probability: float
    threshold: float
    standardized_features: list[float]
    model_id: str = MODEL_ID
    model_version: str = "c_b6_reduced_int8_v1"
    model_sha256: str = MODEL_SHA256
    contract_id: str = CONTRACT_ID
    fallback_used: bool = False
    risk_semantic: str = "NONE"
    safety_semantic: str = "NONE"


class CB6Interpreter:
    def __init__(self, project_root: str | Path | None = None) -> None:
        root = (
            Path(project_root).resolve()
            if project_root
            else Path(__file__).resolve().parent.parent
        )
        manifest = json.loads(
            (root / "models/model_manifest.json").read_text(encoding="utf-8")
        )["models"]["co2_occupancy_c_b6"]
        if manifest.get("model_id") != MODEL_ID:
            raise ValueError("C_B6_ARTIFACT_IDENTITY_MISMATCH")

        self.model_path = root / manifest["path"]
        if hashlib.sha256(self.model_path.read_bytes()).hexdigest() != MODEL_SHA256:
            raise ValueError("C_B6_ARTIFACT_IDENTITY_MISMATCH")

        contract_dir = self.model_path.parent
        self.input_contract = json.loads(
            (contract_dir / "input_contract.json").read_text(encoding="utf-8")
        )
        self._validate_input_contract()

        threshold_contract = json.loads(
            (contract_dir / "threshold_contract.json").read_text(encoding="utf-8")
        )
        if threshold_contract.get("candidate_id") != CONTRACT_ID:
            raise ValueError("C_B6_THRESHOLD_CONTRACT_MISMATCH")
        self.threshold = float(threshold_contract["threshold"])
        if not 0.0 < self.threshold < 1.0:
            raise ValueError("C_B6_THRESHOLD_OUT_OF_RANGE")
        self.threshold_source = str(threshold_contract["threshold_source"])

        # The contract points scaler_path at datasets/co2/manifests/... which is
        # not part of this snapshot; the co-located copy is the available source
        # of truth and is pinned by fingerprint instead of by path.
        scaler = json.loads(
            (contract_dir / "scaler_metadata.json").read_text(encoding="utf-8")
        )
        if scaler.get("scaler_profile_id") != SCALER_PROFILE_ID:
            raise ValueError("C_B6_SCALER_PROFILE_MISMATCH")
        if scaler.get("fingerprint") != SCALER_FINGERPRINT:
            raise ValueError("C_B6_SCALER_FINGERPRINT_MISMATCH")
        if tuple(scaler.get("feature_order", ())) != FEATURE_ORDER:
            raise ValueError("C_B6_SCALER_FEATURE_ORDER_MISMATCH")
        if int(scaler.get("locked_test_fit_rows", -1)) != 0:
            raise ValueError("C_B6_SCALER_FIT_LEAKAGE")
        # float64 throughout: calculation_precision in the slope profile.
        self.means = np.asarray(scaler["mean"], dtype=np.float64).reshape(-1)
        self.scales = np.asarray(scaler["scale"], dtype=np.float64).reshape(-1)
        if self.means.size != 2 or not np.all(np.isfinite(self.means)):
            raise ValueError("C_B6_SCALER_MEAN_INVALID")
        if self.scales.size != 2 or not np.all(np.isfinite(self.scales)) or np.any(self.scales <= 0):
            raise ValueError("C_B6_SCALER_SCALE_INVALID")

        class_map = json.loads((contract_dir / "class_map.json").read_text(encoding="utf-8"))
        if {int(k): v for k, v in class_map["labels"].items()} != CLASS_MAP:
            raise ValueError("C_B6_CLASS_MAP_MISMATCH")
        if class_map.get("positive_class") != POSITIVE_CLASS:
            raise ValueError("C_B6_POSITIVE_CLASS_MISMATCH")
        self.risk_semantic = str(class_map.get("risk_semantic", "NONE"))
        self.safety_semantic = str(class_map.get("safety_semantic", "NONE"))
        if self.risk_semantic != "NONE" or self.safety_semantic != "NONE":
            raise ValueError("C_B6_UNEXPECTED_SAFETY_SEMANTIC")

        self.interpreter = Interpreter(model_path=str(self.model_path), num_threads=1)
        self.interpreter.allocate_tensors()
        self.input_detail = self.interpreter.get_input_details()[0]
        self.output_detail = self.interpreter.get_output_details()[0]
        self._validate_details()

    def _validate_input_contract(self) -> None:
        contract = self.input_contract
        if contract.get("candidate_id") != CONTRACT_ID:
            raise ValueError("C_B6_INPUT_CONTRACT_IDENTITY_MISMATCH")
        if int(contract.get("feature_count", -1)) != 2:
            raise ValueError("C_B6_FEATURE_COUNT_MISMATCH")
        if tuple(contract.get("feature_order", ())) != FEATURE_ORDER:
            raise ValueError("C_B6_FEATURE_ORDER_MISMATCH")
        if contract.get("humidity_included") is not False:
            raise ValueError("C_B6_HUMIDITY_MUST_BE_EXCLUDED")
        if contract.get("temperature_included") is not False:
            raise ValueError("C_B6_TEMPERATURE_MUST_BE_EXCLUDED")
        if contract.get("causality") != "PAST_ONLY":
            raise ValueError("C_B6_CAUSALITY_MISMATCH")
        if contract.get("slope_method") != "ENDPOINT_DIFFERENCE":
            raise ValueError("C_B6_SLOPE_METHOD_MISMATCH")
        forbidden = set(contract.get("forbidden_additional_inputs", ()))
        if not set(FORBIDDEN_INPUTS) <= forbidden:
            raise ValueError("C_B6_FORBIDDEN_INPUT_LIST_MISMATCH")
        self.history_seconds = float(contract["history_seconds"])
        self.max_internal_gap_seconds = float(contract["max_internal_gap_seconds"])

    def _validate_details(self) -> None:
        if (
            list(self.input_detail["shape"]) != INPUT_SHAPE
            or np.dtype(self.input_detail["dtype"]) != np.dtype(np.int8)
        ):
            raise ValueError("C_B6_INPUT_CONTRACT_MISMATCH")
        if (
            list(self.output_detail["shape"]) != OUTPUT_SHAPE
            or np.dtype(self.output_detail["dtype"]) != np.dtype(np.int8)
        ):
            raise ValueError("C_B6_OUTPUT_CONTRACT_MISMATCH")
        if tuple(self.input_detail["quantization"]) != INPUT_QUANTIZATION:
            raise ValueError("C_B6_INPUT_QUANTIZATION_MISMATCH")
        if tuple(self.output_detail["quantization"]) != OUTPUT_QUANTIZATION:
            raise ValueError("C_B6_OUTPUT_QUANTIZATION_MISMATCH")

    def standardize(self, co2_ppm: float, co2_slope_ppm_per_min: float) -> np.ndarray:
        raw = np.asarray([co2_ppm, co2_slope_ppm_per_min], dtype=np.float64)
        if not np.all(np.isfinite(raw)):
            # nonfinite_policy: FAIL_CLOSED_STATUS_NO_CANONICAL_SLOPE
            raise ValueError("C_B6_NONFINITE_FEATURE")
        return (raw - self.means) / self.scales

    def predict(self, co2_ppm: float, co2_slope_ppm_per_min: float) -> CB6Prediction:
        """Occupancy from ppm and ppm/min only. No humidity, no temperature."""

        started = time.perf_counter()
        standardized = self.standardize(float(co2_ppm), float(co2_slope_ppm_per_min))
        scale, zero = self.input_detail["quantization"]
        quantized = (
            np.clip(np.rint(standardized / scale + zero), -128, 127)
            .astype(np.int8)
            .reshape(1, 2)
        )
        self.interpreter.set_tensor(self.input_detail["index"], quantized)
        self.interpreter.invoke()
        raw = self.interpreter.get_tensor(self.output_detail["index"]).reshape(-1)
        out_scale, out_zero = self.output_detail["quantization"]
        occupied = float((float(raw[0]) - out_zero) * out_scale)
        occupied = min(1.0, max(0.0, occupied))
        index = 1 if occupied >= self.threshold else 0
        probabilities = [1.0 - occupied, occupied]
        return CB6Prediction(
            class_index=index,
            class_name=CLASS_MAP[index],
            confidence=float(probabilities[index]),
            probabilities=probabilities,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            occupancy_probability=occupied,
            threshold=self.threshold,
            standardized_features=[float(value) for value in standardized],
            risk_semantic=self.risk_semantic,
            safety_semantic=self.safety_semantic,
        )
