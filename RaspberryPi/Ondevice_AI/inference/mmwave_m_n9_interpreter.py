"""Strict M-N9 FULL_INT8 interpreter; preprocessing is supplied by M-N4 runtime."""

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


MODEL_ID = "MMWAVE_M_N9_FULL_INT8_V1"
MODEL_SHA256 = "3b008af4be0facc4037c2afd3fe39292fb794208eb4370dbe6916b2d15aa38a4"
CLASS_MAP = {0: "NORMAL", 1: "RAPID_OR_ABNORMAL", 2: "APNEA-proxy"}


@dataclass(frozen=True)
class MN9Prediction:
    class_index: int
    class_name: str
    confidence: float
    probabilities: list[float]
    latency_ms: float
    model_id: str = MODEL_ID
    model_version: str = "m_n9_full_int8_v1"
    model_sha256: str = MODEL_SHA256
    fallback_used: bool = False


class MN9Interpreter:
    def __init__(self, project_root: str | Path | None = None) -> None:
        root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
        manifest = json.loads((root / "models/model_manifest.json").read_text(encoding="utf-8"))["models"]["mmwave"]
        if manifest.get("model_id") != MODEL_ID:
            raise ValueError("M_N9_ARTIFACT_IDENTITY_MISMATCH")
        self.model_path = root / manifest["path"]
        actual_hash = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        if actual_hash != MODEL_SHA256:
            raise ValueError("M_N9_ARTIFACT_IDENTITY_MISMATCH")
        self.interpreter = Interpreter(model_path=str(self.model_path), num_threads=1)
        self.interpreter.allocate_tensors()
        self.input_detail = self.interpreter.get_input_details()[0]
        self.output_detail = self.interpreter.get_output_details()[0]
        self._validate_details()

    def _validate_details(self) -> None:
        if list(self.input_detail["shape"]) != [1, 240, 1] or np.dtype(self.input_detail["dtype"]) != np.dtype(np.int8):
            raise ValueError("M_N9_INPUT_CONTRACT_MISMATCH")
        if list(self.output_detail["shape"]) != [1, 3] or np.dtype(self.output_detail["dtype"]) != np.dtype(np.int8):
            raise ValueError("M_N9_OUTPUT_CONTRACT_MISMATCH")
        if tuple(self.input_detail["quantization"]) != (0.5623255372047424, 4):
            raise ValueError("M_N9_INPUT_QUANTIZATION_MISMATCH")
        if tuple(self.output_detail["quantization"]) != (0.00390625, -128):
            raise ValueError("M_N9_OUTPUT_QUANTIZATION_MISMATCH")

    def predict(self, canonical: object) -> MN9Prediction:
        values = np.asarray(canonical, dtype=np.float32)
        if values.shape != (1, 240, 1) or not np.all(np.isfinite(values)):
            raise ValueError("M_N9_CANONICAL_INPUT_REQUIRED")
        started = time.perf_counter()
        scale, zero = self.input_detail["quantization"]
        quantized = np.clip(np.rint(values / scale + zero), -128, 127).astype(np.int8)
        self.interpreter.set_tensor(self.input_detail["index"], quantized)
        self.interpreter.invoke()
        output = self.interpreter.get_tensor(self.output_detail["index"])[0].astype(np.int8)
        out_scale, out_zero = self.output_detail["quantization"]
        scores = ((output.astype(np.float32) - out_zero) * out_scale).tolist()
        index = int(np.argmax(output))
        return MN9Prediction(index, CLASS_MAP[index], float(scores[index]), scores, (time.perf_counter() - started) * 1000.0)
