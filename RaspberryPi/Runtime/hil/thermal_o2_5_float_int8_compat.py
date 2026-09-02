"""RP-X0 O2.5 FLOAT vs INT8 real-domain quantization compatibility.

Offline diagnostic only. Does not implement the runtime adapter, retrain,
modify models, or activate production Thermal.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from hil.thermal_o2_real_snapshot_replay import (
    CLASS_MAP_PATH,
    EXPECTED_INPUT_SCALE,
    EXPECTED_INPUT_ZERO_POINT,
    EXPECTED_SHA256,
    HEIGHT,
    P1_MEAN,
    P1_STD,
    THERMAL_ARTIFACT,
    WIDTH,
    apply_p1,
    celsius_from_raw_uint16,
    invoke_int8,
    load_locked_t_b5,
    make_interpreter,
    quantize_int8,
    quantization,
    sha256_file,
    validate_npz_contract,
)

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_FP32_SHA256 = "fbe891520f07e0534b1a7074dc819d8ed44bca58b27e35c78916c3ddae73a779"
EXPECTED_FP32_SIZE = 1252048
EXPECTED_ARCHITECTURE = "SMALL_CNN_BASELINE_V1"
EXPECTED_P1_PROFILE = "P1_TRAIN_FITTED_GLOBAL_ZSCORE"
# T-B4: measured equivalence, no pre-existing absolute acceptance threshold.
EQUIVALENCE_CONTRACT = "EQUIVALENCE_MEASURED_NO_PREEXISTING_ABSOLUTE_ACCEPTANCE_THRESHOLD"

O2_SELECTED_FRAMES = (
    {
        "role": "early_field_capture",
        "filename": "20260817_065524_939278_0000007852-0000007864.npz",
        "sha256": "09dc0e3b70beb357951dc7ea1fc4057babaada9c46e9ae673bdc59769af5b2d6",
        "frame_index": 0,
    },
    {
        "role": "middle_field_capture",
        "filename": "20260817_082122_819638_0000006423-0000006434.npz",
        "sha256": "dde0240f086f2a59da24b73237fde4fcfd5369935cd1b1453aa48f8e8fb2e009",
        "frame_index": 0,
    },
    {
        "role": "late_field_capture",
        "filename": "20260817_093125_194406_0000030438-0000030438.npz",
        "sha256": "c378a964a315c55ee1b8d3093f8fa52f4387ac5428e6bb2311cb4cd86f8b02cb",
        "frame_index": 0,
    },
    {
        "role": "low_temperature_looking_frame",
        "filename": "20260817_081844_396547_0000005431-0000005442.npz",
        "sha256": "77ef9787155b434c7c8836ce6fba80de83843d75869803b1722e8ca6ff71c46e",
        "frame_index": 2,
    },
    {
        "role": "high_temperature_looking_frame",
        "filename": "20260817_084720_900175_0000015484-0000015496.npz",
        "sha256": "0dd2e560172b4234e6bccafc5aa079c0ac906068e09faa2d664c843057169300",
        "frame_index": 5,
    },
)

LABELS = {0: "NOT_HUMAN", 1: "HUMAN_NORMAL", 2: "HUMAN_FALL"}


def int8_q_minus_128_represented_celsius(
    mean: float = P1_MEAN,
    std: float = P1_STD,
    scale: float = EXPECTED_INPUT_SCALE,
    zero_point: int = EXPECTED_INPUT_ZERO_POINT,
) -> float:
    """Physical value *represented by* q=-128 after dequantization. Not the clip edge."""
    z = (-128 - zero_point) * scale
    return float(z * std + mean)


def int8_low_clip_threshold_celsius(
    mean: float = P1_MEAN,
    std: float = P1_STD,
    scale: float = EXPECTED_INPUT_SCALE,
    zero_point: int = EXPECTED_INPUT_ZERO_POINT,
) -> float:
    """Largest Celsius that still saturates to -128 under numpy rint then clip.

    unclipped = rint(z / scale + zp). Saturation occurs when unclipped <= -129,
    which for numpy rint is x < -128.5 (half-even at exact .5 is ignored as the
    physical boundary of interest).
    """
    z = ((-128.5) - zero_point) * scale
    return float(z * std + mean)


def ranking(vector: np.ndarray) -> list[int]:
    return [int(i) for i in np.argsort(np.asarray(vector, dtype=np.float64).reshape(-1))[::-1]]


def compare_outputs(float_probs: np.ndarray, int8_probs: np.ndarray) -> dict[str, Any]:
    float_vec = np.asarray(float_probs, dtype=np.float64).reshape(-1)
    int8_vec = np.asarray(int8_probs, dtype=np.float64).reshape(-1)
    if float_vec.shape != (3,) or int8_vec.shape != (3,):
        raise ValueError("expected 3-class probability vectors")
    float_rank = ranking(float_vec)
    int8_rank = ranking(int8_vec)
    abs_err = np.abs(float_vec - int8_vec)
    float_margin = float(float_vec[float_rank[0]] - float_vec[float_rank[1]])
    int8_margin = float(int8_vec[int8_rank[0]] - int8_vec[int8_rank[1]])
    return {
        "top1_agree": float_rank[0] == int8_rank[0],
        "ranking_agree": float_rank == int8_rank,
        "float_class_index": float_rank[0],
        "int8_class_index": int8_rank[0],
        "float_class": LABELS[float_rank[0]],
        "int8_class": LABELS[int8_rank[0]],
        "float_margin": float_margin,
        "int8_margin": int8_margin,
        "probability_l1": float(abs_err.sum()),
        "probability_mae": float(abs_err.mean()),
        "probability_max_abs": float(abs_err.max()),
        "float_ranking": float_rank,
        "int8_ranking": int8_rank,
    }


def load_float_t_b5(path: Path) -> dict[str, Any]:
    digest = sha256_file(path)
    if digest != EXPECTED_FP32_SHA256:
        raise ValueError(f"FLOAT T-B5 SHA-256 mismatch: {digest}")
    if path.stat().st_size != EXPECTED_FP32_SIZE:
        raise ValueError(f"FLOAT T-B5 size mismatch: {path.stat().st_size}")
    interpreter = make_interpreter(path)
    inp = interpreter.get_input_details()[0]
    out = interpreter.get_output_details()[0]
    if list(inp["shape"]) != [1, HEIGHT, WIDTH, 1]:
        raise ValueError(f"unexpected FLOAT input shape {inp['shape']}")
    if np.dtype(inp["dtype"]).name != "float32":
        raise ValueError(f"unexpected FLOAT input dtype {inp['dtype']}")
    if list(out["shape"]) != [1, 3]:
        raise ValueError(f"unexpected FLOAT output shape {out['shape']}")
    if np.dtype(out["dtype"]).name != "float32":
        raise ValueError(f"unexpected FLOAT output dtype {out['dtype']}")
    in_scale, in_zero = quantization(inp)
    if in_scale not in (0.0, 0) and abs(float(in_scale)) > 0:
        raise ValueError(f"FLOAT input must be unquantized, got scale={in_scale}")
    return {
        "interpreter": interpreter,
        "input": inp,
        "output": out,
        "sha256": digest,
        "path": str(path),
    }


def invoke_float(runtime: dict[str, Any], z: np.ndarray) -> dict[str, Any]:
    tensor = np.asarray(z, dtype=np.float32).reshape(1, HEIGHT, WIDTH, 1)
    interpreter = runtime["interpreter"]
    inp = runtime["input"]
    out = runtime["output"]
    started = time.perf_counter()
    interpreter.set_tensor(inp["index"], tensor)
    interpreter.invoke()
    raw = np.array(interpreter.get_tensor(out["index"]))
    latency_ms = (time.perf_counter() - started) * 1000.0
    probs = raw.astype(np.float64).reshape(-1)
    class_index = int(np.argmax(probs))
    return {
        "raw_output": [float(v) for v in probs.tolist()],
        "probabilities": [float(v) for v in probs.tolist()],
        "class_index": class_index,
        "class_name": LABELS[class_index],
        "latency_ms": float(latency_ms),
        "output_shape": list(raw.shape),
        "finite": bool(np.all(np.isfinite(probs))),
    }


def load_o2_frame(snapshot: Path, spec: dict[str, Any]) -> np.ndarray:
    path = snapshot / "data" / "thermal" / spec["filename"]
    digest = sha256_file(path)
    if digest != spec["sha256"]:
        raise ValueError(f"NPZ hash mismatch for {spec['filename']}: {digest}")
    with np.load(path, allow_pickle=False) as payload:
        validate_npz_contract(payload, path)
        frames = payload["frames"]
        index = int(spec["frame_index"])
        raw = np.array(frames[index], copy=True)
    if raw.dtype != np.uint16 or raw.shape != (HEIGHT, WIDTH):
        raise ValueError(f"unexpected frame contract {raw.dtype} {raw.shape}")
    return raw


def classify_saturation_relationship(rows: list[dict[str, Any]]) -> str:
    """Five-sample descriptive class. Not a causal claim."""
    if len(rows) < 5:
        return "INSUFFICIENT_EVIDENCE"
    disagreements = [row for row in rows if not row["comparison"]["top1_agree"]]
    if not disagreements:
        return "NO_OBVIOUS_EFFECT_OBSERVED"
    disagree_sat = [float(row["int8"]["low_saturation_fraction"]) for row in disagreements]
    agree_sat = [float(row["int8"]["low_saturation_fraction"]) for row in rows if row["comparison"]["top1_agree"]]
    # A higher-saturation agreeing frame blocks a saturation-causes-flip claim.
    if agree_sat and max(agree_sat) >= max(disagree_sat):
        return "INSUFFICIENT_EVIDENCE"
    if max(disagree_sat) >= 0.5 and max(row["comparison"]["probability_mae"] for row in disagreements) >= 0.3:
        return "POSSIBLE_QUANTIZATION_DISTORTION"
    return "INSUFFICIENT_EVIDENCE"


def decide_classification(rows: list[dict[str, Any]], saturation_rel: str) -> str:
    """Conservative one-of-four decision. No post-hoc accuracy threshold.

    T-B4 contract is measurement-only
    (``EQUIVALENCE_MEASURED_NO_PREEXISTING_ABSOLUTE_ACCEPTANCE_THRESHOLD``).
    Extreme near-one-hot FLOAT *and* INT8 outputs on unlabeled MI48 frames are
    treated as a device-domain gap, not as INT8-only failure.
    """
    n_agree = sum(1 for row in rows if row["comparison"]["top1_agree"])
    float_extreme = all(float(row["comparison"]["float_margin"]) >= 0.8 for row in rows)
    int8_extreme = all(float(row["comparison"]["int8_margin"]) >= 0.8 for row in rows)
    if float_extreme and int8_extreme:
        return "DEVICE_DOMAIN_GAP_LIKELY"
    if n_agree == len(rows) and saturation_rel == "NO_OBVIOUS_EFFECT_OBSERVED":
        return "INT8_COMPATIBLE_FOR_ADAPTER_IMPLEMENTATION"
    if saturation_rel in {"POSSIBLE_QUANTIZATION_DISTORTION", "CLEAR_QUANTIZATION_DISTORTION"}:
        return "INT8_QUANTIZATION_REVIEW_REQUIRED"
    return "INT8_QUANTIZATION_REVIEW_REQUIRED"


def run_audit(snapshot: Path, float_artifact: Path, int8_artifact: Path = THERMAL_ARTIFACT) -> dict[str, Any]:
    class_map = json.loads(CLASS_MAP_PATH.read_text(encoding="utf-8"))
    float_rt = load_float_t_b5(float_artifact)
    int8_rt = load_locked_t_b5(int8_artifact)
    rows = []
    for spec in O2_SELECTED_FRAMES:
        raw = load_o2_frame(snapshot, spec)
        celsius = celsius_from_raw_uint16(raw)
        z = apply_p1(celsius)
        clipped, unclipped = quantize_int8(z, int8_rt["input_scale"], int8_rt["input_zero_point"])
        low = unclipped.reshape(-1) < -128
        high = unclipped.reshape(-1) > 127
        float_out = invoke_float(float_rt, z)
        int8_out = invoke_int8(int8_rt, clipped.reshape(1, HEIGHT, WIDTH, 1))
        comparison = compare_outputs(float_out["probabilities"], int8_out["dequantized_output"])
        rows.append(
            {
                "role": spec["role"],
                "filename": spec["filename"],
                "sha256": spec["sha256"],
                "frame_index": spec["frame_index"],
                "celsius": {
                    "min": float(np.min(celsius)),
                    "median": float(np.median(celsius)),
                    "max": float(np.max(celsius)),
                },
                "int8": {
                    "low_saturation_fraction": float(low.mean()),
                    "high_saturation_fraction": float(high.mean()),
                    "unique_value_count": int(np.unique(clipped).size),
                    "int8_min": int(clipped.min()),
                    "int8_max": int(clipped.max()),
                    "raw_output": int8_out["raw_output"],
                    "dequantized_output": int8_out["dequantized_output"],
                    "class_name": int8_out["class_name"],
                    "latency_ms": round(float(int8_out["latency_ms"]), 3),
                },
                "float": {
                    "probabilities": float_out["probabilities"],
                    "class_name": float_out["class_name"],
                    "latency_ms": round(float(float_out["latency_ms"]), 3),
                    "finite": float_out["finite"],
                },
                "comparison": comparison,
            }
        )
    saturation_rel = classify_saturation_relationship(rows)
    classification = decide_classification(rows, saturation_rel)
    o3 = classification == "INT8_COMPATIBLE_FOR_ADAPTER_IMPLEMENTATION"
    n_agree = sum(1 for row in rows if row["comparison"]["top1_agree"])
    n_rank = sum(1 for row in rows if row["comparison"]["ranking_agree"])
    return {
        "document_id": "RP-X0-O2-5-THERMAL-FLOAT-INT8-COMPAT-01",
        "stacked_on_o2_commit": "925330c1c54eb0f5762ae56b6ff6f6a81897aad5",
        "o2_pr": "https://github.com/yuname121/integration/pull/14",
        "equivalence_contract": EQUIVALENCE_CONTRACT,
        "lineage": {
            "architecture": EXPECTED_ARCHITECTURE,
            "same_architecture": True,
            "p1_profile": EXPECTED_P1_PROFILE,
            "same_p1_contract": True,
            "same_weights_before_quantization": True,
            "float_role": "TFLITE_FP32 true unquantized reference from T-B4 conversion chain",
            "int8_role": "FULL_INT8 selected T-B5 candidate",
            "authority": "sheepmeat/test T-B4/T-B5",
            "float_sha256": EXPECTED_FP32_SHA256,
            "int8_sha256": EXPECTED_SHA256,
            "class_map_restriction": class_map.get("semantic_restriction"),
        },
        "quantization_range": {
            "q_minus_125_represented_celsius": P1_MEAN,
            "q_minus_128_represented_celsius": int8_q_minus_128_represented_celsius(),
            "low_clip_threshold_celsius_rint": int8_low_clip_threshold_celsius(),
            "note": "q=-128 represented Celsius is not the clipping boundary.",
        },
        "frames": rows,
        "aggregate": {
            "frame_count": len(rows),
            "top1_agree_count": n_agree,
            "ranking_agree_count": n_rank,
            "probability_mae_min": min(row["comparison"]["probability_mae"] for row in rows),
            "probability_mae_max": max(row["comparison"]["probability_mae"] for row in rows),
            "low_saturation_min": min(row["int8"]["low_saturation_fraction"] for row in rows),
            "low_saturation_max": max(row["int8"]["low_saturation_fraction"] for row in rows),
            "high_saturation_max": max(row["int8"]["high_saturation_fraction"] for row in rows),
        },
        "saturation_relationship": saturation_rel,
        "classification": classification,
        "o3_adapter_implementation": o3,
        "production_thermal_activation": False,
        "snapshot_modified": False,
        "models_modified": False,
        "gate": "PASS_WITH_LIMITATIONS",
    }


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.generic):
        return obj.item()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP-X0 O2.5 FLOAT vs INT8 compatibility")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--float-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_audit(args.snapshot.resolve(), args.float_artifact.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    print(report["classification"], report["saturation_relationship"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
