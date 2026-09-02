#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
inference/thermal_interpreter.py
SafeNest 공용 Thermal Interpreter Wrapper

[역할]
1. models/model_manifest.json에서 공식 모델 경로 및 텐서 스펙 로드
2. Mac TensorFlow와 Raspberry Pi tflite-runtime 이중 호환
3. 입력 shape 검증 및 NaN/Inf 안전 검사
4. Manifest가 가리키는 active thermal selector의 입력 dtype/양자화 처리
5. INT8 출력 역양자화 또는 FP32 출력 처리 (Softmax 이중 적용 방지)
6. 추론 지연시간(latency_ms) 및 모델 버전 반환
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
import hashlib
import json
import time
import numpy as np

BASELINE_PREPROCESSING_IDS = frozenset(
    {
        "",
        "per_frame_minmax",
        "PUBLIC_SDT_BILINEAR_62X80_FRAME_MINMAX_V1",
    }
)
ROBUST_PREPROCESSING_ID = "FRAME_ROBUST_P2_P98_V1"
_ROBUST_EPS = 1e-6

try:
    import ai_edge_litert.interpreter as tflite
except ImportError:
    try:
        import tflite_runtime.interpreter as tflite
    except ImportError:
        try:
            import tensorflow.lite as tflite
        except ImportError:
            import tensorflow as tf
            tflite = tf.lite


@dataclass(frozen=True)
class ThermalPrediction:
    class_index: int
    class_name: str
    confidence: float
    probabilities: list[float]
    latency_ms: float
    model_id: str
    model_version: str
    model_selector: str = ""
    model_sha256: str = ""
    preprocessing_id: str = ""
    overlay_applied: bool = False
    posture_source: str = "MODEL"
    model_class_name: str = ""
    bbox_height: int | None = None
    bbox_width: int | None = None


@dataclass(frozen=True)
class PostureOverride:
    """Model keeps presence; bbox aspect replaces standing vs lying."""

    class_index: int
    class_name: str
    confidence: float
    overlay_applied: bool
    posture_source: str
    model_class_index: int
    model_class_name: str
    bbox_height: int | None = None
    bbox_width: int | None = None


DEFAULT_THERMAL_SELECTOR = "thermal_public_sdt_fp32_active"
HOT_PIXEL_THRESHOLD = 0.5
MIN_HOT_PIXELS_FOR_POSTURE = 20
_NOT_HUMAN_NAMES = frozenset({"NOT_HUMAN", "NO_HUMAN"})


def spatial_from_prepared(prepared: np.ndarray) -> np.ndarray:
    """Take the 62x80 plane from a 0-1 model frame."""

    array = np.asarray(prepared, dtype=np.float32)
    if array.shape == (62, 80):
        return array
    if array.shape == (62, 80, 1):
        return array[:, :, 0]
    if array.shape == (1, 62, 80, 1):
        return array[0, :, :, 0]
    raise ValueError(
        f"prepared thermal frame must have shape (62,80), (62,80,1), or (1,62,80,1), got {array.shape}"
    )


def override_posture_from_bbox(
    class_index: int,
    class_map: Mapping[int, str],
    spatial_01: np.ndarray,
    probabilities: np.ndarray | Sequence[float],
    *,
    hot_threshold: float = HOT_PIXEL_THRESHOLD,
    min_hot_pixels: int = MIN_HOT_PIXELS_FOR_POSTURE,
) -> PostureOverride:
    """Keep model presence; replace standing vs lying with bbox aspect.

    class_map[1] is standing/sitting (HUMAN_NORMAL on C0).
    class_map[2] is lying (HUMAN_FALL_PROXY on C0, HUMAN_FALL on some INT8 maps).
    Square bbox counts as sitting / HUMAN_NORMAL. If the person is present but
    the hot mask is too small for a bbox, pose falls back to standing/sitting
    instead of the model's lying class so risk never uses neural pose.
    """

    index = int(class_index)
    probs = np.asarray(probabilities, dtype=np.float32).reshape(-1)
    original_name = str(class_map.get(index, f"CLASS_{index}"))
    original_conf = float(probs[index]) if 0 <= index < probs.size else 0.0
    standing_index = 1
    standing_name = str(class_map.get(standing_index, "HUMAN_NORMAL"))
    standing_conf = (
        float(probs[standing_index]) if 0 <= standing_index < probs.size else original_conf
    )
    if original_name in _NOT_HUMAN_NAMES:
        return PostureOverride(
            index, original_name, original_conf,
            overlay_applied=False,
            posture_source="NOT_HUMAN",
            model_class_index=index,
            model_class_name=original_name,
        )

    spatial = np.asarray(spatial_01, dtype=np.float32)
    if spatial.ndim != 2:
        spatial = spatial_from_prepared(spatial)
    hot = spatial >= float(hot_threshold)
    if int(np.count_nonzero(hot)) < int(min_hot_pixels):
        return PostureOverride(
            standing_index, standing_name, standing_conf,
            overlay_applied=False,
            posture_source="PRESENCE_ONLY",
            model_class_index=index,
            model_class_name=original_name,
        )

    rows, cols = np.nonzero(hot)
    height = int(rows.max() - rows.min() + 1)
    width = int(cols.max() - cols.min() + 1)
    new_index = standing_index if height >= width else 2
    new_name = str(class_map.get(new_index, f"CLASS_{new_index}"))
    new_conf = float(probs[new_index]) if 0 <= new_index < probs.size else original_conf
    return PostureOverride(
        new_index, new_name, new_conf,
        overlay_applied=True,
        posture_source="BBOX",
        model_class_index=index,
        model_class_name=original_name,
        bbox_height=height,
        bbox_width=width,
    )


class ThermalInterpreter:
    def __init__(
        self,
        project_root: str | Path | None = None,
        manifest_path: str = "models/model_manifest.json",
        model_key: str | None = None,
    ) -> None:
        if project_root is None:
            self.project_root = Path(__file__).resolve().parent.parent
        else:
            self.project_root = Path(project_root).resolve()

        manifest_file = self.project_root / manifest_path
        if not manifest_file.is_file():
            raise FileNotFoundError(f"Manifest file not found: {manifest_file}")

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        models = manifest.get("models", {})
        selectors = manifest.get("active_runtime_selectors", {})
        selector = model_key or selectors.get("thermal") or DEFAULT_THERMAL_SELECTOR
        if selector not in models and model_key is None and "thermal" in models and not selectors:
            # Keep old standalone snapshots loadable when they predate the
            # explicit selector map. Current manifests must resolve exactly.
            selector = "thermal"
        if selector not in models:
            raise KeyError(f"thermal model selector missing from manifest: {selector}")
        self.model_selector = selector
        self.model_meta = models[selector]
        self.preprocessing_id = str(self.model_meta.get("preprocessing_id") or "per_frame_minmax")
        self.class_map = {
            int(key): value
            for key, value in self.model_meta["class_map"].items()
        }

        self.model_path = self.project_root / self.model_meta["path"]
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Model file not found: {self.model_path}")

        self.sha256_hash = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        expected_sha256 = self.model_meta.get("sha256")
        self.sha256_matches = bool(expected_sha256 and self.sha256_hash == expected_sha256)
        if not self.sha256_matches:
            raise ValueError(
                "thermal model SHA-256 mismatch: "
                f"expected={expected_sha256}, actual={self.sha256_hash}"
            )

        self.interpreter = tflite.Interpreter(model_path=str(self.model_path))
        self.interpreter.allocate_tensors()

        self.input_info = self.interpreter.get_input_details()[0]
        self.output_info = self.interpreter.get_output_details()[0]

        self._validate_contract()

    def _validate_contract(self) -> None:
        expected_input = self.model_meta["input"]
        expected_output = self.model_meta["output"]

        actual_input_shape = self.input_info["shape"].tolist()
        actual_output_shape = self.output_info["shape"].tolist()
        actual_input_dtype = self.input_info["dtype"].__name__
        actual_output_dtype = self.output_info["dtype"].__name__

        if actual_input_shape != expected_input["shape"]:
            raise ValueError(
                f"input shape mismatch: {actual_input_shape} != {expected_input['shape']}"
            )
        if actual_output_shape != expected_output["shape"]:
            raise ValueError(
                f"output shape mismatch: {actual_output_shape} != {expected_output['shape']}"
            )
        if actual_input_dtype != expected_input["dtype"]:
            raise ValueError(
                f"input dtype mismatch: {actual_input_dtype} != {expected_input['dtype']}"
            )
        if actual_output_dtype != expected_output["dtype"]:
            raise ValueError(
                f"output dtype mismatch: {actual_output_dtype} != {expected_output['dtype']}"
            )

        for label, actual, expected in (
            ("input", self.input_info, expected_input),
            ("output", self.output_info, expected_output),
        ):
            expected_scale = expected.get("scale")
            expected_zero_point = expected.get("zero_point")
            if expected_scale is None:
                continue
            actual_scale, actual_zero_point = actual["quantization"]
            if not np.isclose(float(actual_scale), float(expected_scale), rtol=0, atol=1e-12):
                raise ValueError(
                    f"{label} scale mismatch: {actual_scale} != {expected_scale}"
                )
            if int(actual_zero_point) != int(expected_zero_point):
                raise ValueError(
                    f"{label} zero_point mismatch: {actual_zero_point} != {expected_zero_point}"
                )

    @staticmethod
    def _prepare_float_frame(frame: np.ndarray) -> np.ndarray:
        array = np.asarray(frame, dtype=np.float32)

        if array.shape == (62, 80):
            array = array[None, ..., None]
        elif array.shape == (62, 80, 1):
            array = array[None, ...]
        elif array.shape != (1, 62, 80, 1):
            raise ValueError(
                f"thermal frame must have shape (62,80), (62,80,1), or (1,62,80,1), got {array.shape}"
            )

        if not np.all(np.isfinite(array)):
            raise ValueError("thermal frame contains NaN or infinity")

        min_value = float(array.min())
        max_value = float(array.max())
        if min_value < 0.0 or max_value > 1.0:
            # Min-Max normalize array to [0.0, 1.0] safely if unnormalized
            range_val = max_value - min_value
            if range_val > 0:
                array = (array - min_value) / range_val
            else:
                array = np.clip(array, 0.0, 1.0)

        return array

    @staticmethod
    def _prepare_robust_p2_p98_frame(frame: np.ndarray) -> np.ndarray:
        array = np.asarray(frame, dtype=np.float32)

        if array.shape == (62, 80):
            spatial = array
        elif array.shape == (62, 80, 1):
            spatial = array[:, :, 0]
        elif array.shape == (1, 62, 80, 1):
            spatial = array[0, :, :, 0]
        else:
            raise ValueError(
                f"thermal frame must have shape (62,80), (62,80,1), or (1,62,80,1), got {array.shape}"
            )

        if not np.all(np.isfinite(spatial)):
            raise ValueError("thermal frame contains NaN or infinity")

        p2 = float(np.percentile(spatial, 2.0))
        p98 = float(np.percentile(spatial, 98.0))
        denom = max(p98 - p2, _ROBUST_EPS)
        normalized = np.clip((spatial - p2) / denom, 0.0, 1.0)
        return normalized.reshape(1, 62, 80, 1).astype(np.float32)

    def _prepare_model_frame(self, frame: np.ndarray) -> np.ndarray:
        preprocessing_id = str(self.model_meta.get("preprocessing_id") or "")
        if preprocessing_id in BASELINE_PREPROCESSING_IDS:
            return self._prepare_float_frame(frame)
        if preprocessing_id == ROBUST_PREPROCESSING_ID:
            return self._prepare_robust_p2_p98_frame(frame)
        raise ValueError(
            f"unsupported thermal preprocessing_id: {preprocessing_id!r}"
        )

    def _encode_input(self, frame: np.ndarray) -> np.ndarray:
        float_input = self._prepare_model_frame(frame)
        dtype = self.input_info["dtype"]

        if np.issubdtype(dtype, np.integer):
            scale, zero_point = self.input_info["quantization"]
            if scale <= 0:
                raise ValueError("invalid input quantization scale")

            quantized = np.rint(float_input / scale + zero_point)
            limits = np.iinfo(dtype)
            quantized = np.clip(quantized, limits.min, limits.max)
            return quantized.astype(dtype)

        return float_input.astype(dtype)

    def _decode_output(self, raw_output: np.ndarray) -> np.ndarray:
        dtype = self.output_info["dtype"]

        if np.issubdtype(dtype, np.integer):
            scale, zero_point = self.output_info["quantization"]
            if scale <= 0:
                raise ValueError("invalid output quantization scale")
            probabilities = (
                raw_output.astype(np.float32) - zero_point
            ) * scale
        else:
            probabilities = raw_output.astype(np.float32)

        probabilities = probabilities[0]
        if not np.all(np.isfinite(probabilities)):
            raise ValueError("model output contains NaN or infinity")

        probabilities = np.clip(probabilities, 0.0, None)
        total = float(probabilities.sum())
        if total <= 0.0:
            return np.array([0.333, 0.333, 0.334], dtype=np.float32)

        return probabilities / total

    def predict(self, frame: np.ndarray) -> ThermalPrediction:
        prepared = self._prepare_model_frame(frame)
        input_tensor = self._encode_input(frame)

        started = time.perf_counter()
        self.interpreter.set_tensor(
            self.input_info["index"],
            input_tensor,
        )
        self.interpreter.invoke()
        raw_output = self.interpreter.get_tensor(
            self.output_info["index"]
        )
        latency_ms = (time.perf_counter() - started) * 1000.0

        probabilities = self._decode_output(raw_output)
        class_index = int(np.argmax(probabilities))
        override = override_posture_from_bbox(
            class_index,
            self.class_map,
            spatial_from_prepared(prepared),
            probabilities,
        )

        return ThermalPrediction(
            class_index=override.class_index,
            class_name=override.class_name,
            confidence=override.confidence,
            probabilities=[float(value) for value in probabilities],
            latency_ms=float(latency_ms),
            model_id=self.model_meta["model_id"],
            model_version=self.model_meta["version"],
            model_selector=self.model_selector,
            model_sha256=self.sha256_hash,
            preprocessing_id=self.preprocessing_id,
            overlay_applied=override.overlay_applied,
            posture_source=override.posture_source,
            model_class_name=override.model_class_name,
            bbox_height=override.bbox_height,
            bbox_width=override.bbox_width,
        )


def prepare_frame_robust_p2_p98_v1(frame: np.ndarray) -> np.ndarray:
    """RELATIVE_THERMAL_APPEARANCE_V1 / FRAME_ROBUST_P2_P98_V1 for Candidate A/B."""
    return ThermalInterpreter._prepare_robust_p2_p98_frame(frame)
