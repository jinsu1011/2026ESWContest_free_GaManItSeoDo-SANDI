"""RP-X0 O2 offline Thermal replay: uint16 → Celsius → P1 → INT8 → T-B5.

This module is an offline pipeline-compatibility tool. It does not select
T-B5 in production, modify the Pi snapshot, or claim fall-detection accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from paths import ONDEVICE_AI_ROOT, RUNTIME_ROOT

ROOT = RUNTIME_ROOT
ONDEVICE = ONDEVICE_AI_ROOT
THERMAL_ARTIFACT = (
    ONDEVICE / "models" / "rp_x0_b_complete" / "thermal" / "SMALL_CNN_BASELINE_V1_P1_full_int8.tflite"
)
CLASS_MAP_PATH = ONDEVICE / "models" / "rp_x0_b_complete" / "thermal" / "class_map.json"
P1_LOCK_PATH = ONDEVICE / "models" / "rp_x0_b_complete" / "thermal" / "p1_lock.json"

EXPECTED_SHA256 = "fa9730c29535477a3994c11e664474a0ca0116afaaa172889f47446ab2ac46be"
P1_MEAN = 22.769290618485442
P1_STD = 2.8684523405441222
EXPECTED_INPUT_SCALE = 0.31791284680366516
EXPECTED_INPUT_ZERO_POINT = -125
HEIGHT = 62
WIDTH = 80
PIXELS = HEIGHT * WIDTH

# Investigation threshold only. Not a sanitization / invalid-pixel policy.
EXTREME_CELSIUS_LT = -40.0
EXTREME_CELSIUS_GT = 80.0


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Path):
        return obj.name
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def celsius_from_raw_uint16(raw: np.ndarray) -> np.ndarray:
    """O1 contract: physical_C = raw_uint16 / 10.0 - 273.15. No clamp."""
    array = np.asarray(raw)
    if array.dtype != np.uint16:
        raise ValueError(f"expected uint16, got {array.dtype}")
    return array.astype(np.float32) / np.float32(10.0) - np.float32(273.15)


def apply_p1(celsius: np.ndarray, mean: float = P1_MEAN, std: float = P1_STD) -> np.ndarray:
    if std <= 0:
        raise ValueError("P1 std must be positive")
    return (np.asarray(celsius, dtype=np.float64) - float(mean)) / float(std)


def quantize_int8(z: np.ndarray, scale: float, zero_point: int) -> tuple[np.ndarray, np.ndarray]:
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("invalid INT8 scale")
    unclipped = np.rint(np.asarray(z, dtype=np.float64) / float(scale) + int(zero_point))
    clipped = np.clip(unclipped, -128, 127).astype(np.int8)
    return clipped, unclipped


def percentile_stats(values: np.ndarray) -> dict[str, float]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    finite = np.isfinite(flat)
    data = flat[finite]
    if data.size == 0:
        raise ValueError("no finite values")
    return {
        "min": float(data.min()),
        "p1": float(np.percentile(data, 1)),
        "p5": float(np.percentile(data, 5)),
        "median": float(np.median(data)),
        "p95": float(np.percentile(data, 95)),
        "p99": float(np.percentile(data, 99)),
        "max": float(data.max()),
        "finite_count": int(finite.sum()),
        "nonfinite_count": int((~finite).sum()),
    }


def representable_celsius_range(
    mean: float = P1_MEAN,
    std: float = P1_STD,
    scale: float = EXPECTED_INPUT_SCALE,
    zero_point: int = EXPECTED_INPUT_ZERO_POINT,
) -> dict[str, float]:
    def dequant(q: int) -> float:
        z = (q - zero_point) * scale
        return z * std + mean

    return {
        "celsius_at_int8_minus_128": float(dequant(-128)),
        "celsius_at_int8_zero_point": float(dequant(zero_point)),
        "celsius_at_int8_127": float(dequant(127)),
    }


def make_interpreter(model_path: Path):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tensorflow.lite.python.interpreter import Interpreter
        except ImportError:
            from tflite_runtime.interpreter import Interpreter
    interpreter = Interpreter(model_path=str(model_path), num_threads=1)
    interpreter.allocate_tensors()
    return interpreter


def quantization(details: dict) -> tuple[float, int]:
    params = details.get("quantization_parameters") or {}
    scales = params.get("scales")
    zeros = params.get("zero_points")
    if scales is not None and len(scales) == 1:
        return float(scales[0]), int(zeros[0])
    scale, zero = details["quantization"]
    return float(scale), int(zero)


def load_locked_t_b5(artifact: Path = THERMAL_ARTIFACT) -> dict[str, Any]:
    digest = sha256_file(artifact)
    if digest != EXPECTED_SHA256:
        raise ValueError(f"T-B5 SHA-256 mismatch: {digest}")
    interpreter = make_interpreter(artifact)
    inp = interpreter.get_input_details()[0]
    out = interpreter.get_output_details()[0]
    in_scale, in_zero = quantization(inp)
    out_scale, out_zero = quantization(out)
    if list(inp["shape"]) != [1, HEIGHT, WIDTH, 1]:
        raise ValueError(f"unexpected input shape {inp['shape']}")
    if np.dtype(inp["dtype"]).name != "int8":
        raise ValueError(f"unexpected input dtype {inp['dtype']}")
    if not np.isclose(in_scale, EXPECTED_INPUT_SCALE, rtol=0, atol=1e-12):
        raise ValueError(f"input scale mismatch {in_scale}")
    if in_zero != EXPECTED_INPUT_ZERO_POINT:
        raise ValueError(f"input zero_point mismatch {in_zero}")
    class_map = json.loads(CLASS_MAP_PATH.read_text(encoding="utf-8"))
    labels = {int(k): v for k, v in class_map.items() if k.isdigit()}
    return {
        "interpreter": interpreter,
        "input": inp,
        "output": out,
        "input_scale": in_scale,
        "input_zero_point": in_zero,
        "output_scale": out_scale,
        "output_zero_point": out_zero,
        "labels": labels,
        "sha256": digest,
    }


def invoke_int8(runtime: dict[str, Any], tensor: np.ndarray) -> dict[str, Any]:
    interpreter = runtime["interpreter"]
    inp = runtime["input"]
    out = runtime["output"]
    if tensor.shape != tuple(inp["shape"]):
        raise ValueError(f"input tensor shape {tensor.shape} != {tuple(inp['shape'])}")
    started = time.perf_counter()
    interpreter.set_tensor(inp["index"], tensor)
    interpreter.invoke()
    raw = np.array(interpreter.get_tensor(out["index"]))
    latency_ms = (time.perf_counter() - started) * 1000.0
    dequant = (raw.astype(np.float32) - runtime["output_zero_point"]) * runtime["output_scale"]
    class_index = int(np.argmax(dequant.reshape(-1)))
    labels = runtime["labels"]
    return {
        "raw_output": [int(v) for v in raw.reshape(-1).tolist()],
        "dequantized_output": [float(v) for v in dequant.reshape(-1).tolist()],
        "class_index": class_index,
        "class_name": labels.get(class_index, f"CLASS_{class_index}"),
        "latency_ms": float(latency_ms),
        "output_shape": list(raw.shape),
    }


def validate_npz_contract(payload: np.lib.npyio.NpzFile, path: Path) -> None:
    required = (
        "frames",
        "timestamps",
        "receive_monotonic",
        "frame_sequences",
        "source_uptime_ms",
        "minimum_raw",
        "maximum_raw",
        "analysis_json",
    )
    missing = [key for key in required if key not in payload.files]
    if missing:
        raise ValueError(f"{path.name} missing keys {missing}")
    frames = payload["frames"]
    if frames.dtype != np.uint16:
        raise ValueError(f"{path.name} dtype {frames.dtype} != uint16")
    if frames.ndim != 3 or frames.shape[1:] != (HEIGHT, WIDTH):
        raise ValueError(f"{path.name} shape {frames.shape} != (N,{HEIGHT},{WIDTH})")


def list_thermal_npz(snapshot: Path) -> list[Path]:
    directory = snapshot / "data" / "thermal"
    files = sorted(directory.glob("20260817_*.npz"))
    if not files:
        raise FileNotFoundError(f"no 20260817 thermal NPZ under {directory}")
    return files


def scan_npz_metadata(files: list[Path]) -> tuple[dict[str, Any], dict[Path, tuple[int, int, int, int]]]:
    """Scan NPZ min/max metadata only. Does not infer or rewrite pixels."""
    per_file: dict[Path, tuple[int, int, int, int]] = {}
    frames_below_raw_2300 = 0
    frames_total = 0
    files_with_low = 0
    file_mins: list[int] = []
    file_maxs: list[int] = []
    unreadable: list[dict[str, str]] = []
    for path in files:
        try:
            with np.load(path, allow_pickle=False) as payload:
                minimum_raw = np.array(payload["minimum_raw"], copy=True)
                maximum_raw = np.array(payload["maximum_raw"], copy=True)
                min_index = int(np.argmin(minimum_raw))
                max_index = int(np.argmax(maximum_raw))
                stats = (
                    int(minimum_raw[min_index]),
                    min_index,
                    int(maximum_raw[max_index]),
                    max_index,
                )
        except (OSError, EOFError, ValueError) as exc:
            unreadable.append({"filename": path.name, "error": type(exc).__name__})
            continue
        per_file[path] = stats
        file_mins.append(stats[0])
        file_maxs.append(stats[2])
        frames_total += int(minimum_raw.size)
        low = minimum_raw < 2300
        frames_below_raw_2300 += int(low.sum())
        if bool(low.any()):
            files_with_low += 1
    if not per_file:
        raise FileNotFoundError("no readable thermal NPZ metadata")
    metadata = {
        "npz_file_count": len(files),
        "readable_npz_file_count": len(per_file),
        "unreadable_npz_file_count": len(unreadable),
        "unreadable_npz_files": unreadable[:20],
        "frame_count_in_metadata": frames_total,
        "metadata_raw_min": int(min(file_mins)) if file_mins else None,
        "metadata_raw_max": int(max(file_maxs)) if file_maxs else None,
        "files_with_any_frame_min_raw_lt_2300": files_with_low,
        "frames_with_min_raw_lt_2300": frames_below_raw_2300,
        "fraction_frames_min_raw_lt_2300": float(frames_below_raw_2300 / frames_total) if frames_total else 0.0,
        "celsius_for_raw_2300": float(2300 / 10.0 - 273.15),
    }
    return metadata, per_file


def select_replay_set(
    files: list[Path],
    per_file: dict[Path, tuple[int, int, int, int]],
) -> list[dict[str, Any]]:
    early = files[0]
    mid = files[len(files) // 2]
    late = files[-1]
    lowest = min(files, key=lambda path: per_file[path][0])
    highest = max(files, key=lambda path: per_file[path][2])
    return [
        {"role": "early_field_capture", "path": early, "frame_index": 0},
        {"role": "middle_field_capture", "path": mid, "frame_index": 0},
        {"role": "late_field_capture", "path": late, "frame_index": 0},
        {
            "role": "low_temperature_looking_frame",
            "path": lowest,
            "frame_index": per_file[lowest][1],
        },
        {
            "role": "high_temperature_looking_frame",
            "path": highest,
            "frame_index": per_file[highest][3],
        },
    ]


def int8_saturation_stats(unclipped: np.ndarray, clipped: np.ndarray) -> dict[str, Any]:
    flat_u = unclipped.reshape(-1)
    flat_c = clipped.reshape(-1)
    low = flat_u < -128
    high = flat_u > 127
    return {
        "int8_min": int(flat_c.min()),
        "int8_max": int(flat_c.max()),
        "unique_value_count": int(np.unique(flat_c).size),
        "low_saturation_count": int(low.sum()),
        "high_saturation_count": int(high.sum()),
        "low_saturation_fraction": float(low.mean()),
        "high_saturation_fraction": float(high.mean()),
        "any_saturation_fraction": float((low | high).mean()),
    }


def extreme_pixel_report(celsius: np.ndarray, raw: np.ndarray) -> dict[str, Any]:
    low_mask = celsius < EXTREME_CELSIUS_LT
    high_mask = celsius > EXTREME_CELSIUS_GT
    sentinel0 = raw == 0
    sentinel_max = raw == np.iinfo(np.uint16).max
    rows, cols = np.nonzero(low_mask)
    locations = [{"row": int(r), "col": int(c), "raw": int(raw[r, c]), "celsius": float(celsius[r, c])} for r, c in zip(rows.tolist(), cols.tolist())]
    return {
        "investigation_thresholds_celsius": {
            "lt": EXTREME_CELSIUS_LT,
            "gt": EXTREME_CELSIUS_GT,
        },
        "low_count": int(low_mask.sum()),
        "high_count": int(high_mask.sum()),
        "raw_zero_count": int(sentinel0.sum()),
        "raw_uint16_max_count": int(sentinel_max.sum()),
        "low_locations_sample": locations[:8],
        "low_location_count_truncated": len(locations) > 8,
    }


def replay_frame(
    runtime: dict[str, Any],
    raw_frame: np.ndarray,
    *,
    original_copy: np.ndarray,
) -> dict[str, Any]:
    if not np.array_equal(raw_frame, original_copy):
        raise ValueError("original frame changed before conversion")
    celsius = celsius_from_raw_uint16(raw_frame)
    if celsius.shape != (HEIGHT, WIDTH):
        raise ValueError(f"unexpected celsius shape {celsius.shape}")
    z = apply_p1(celsius)
    clipped, unclipped = quantize_int8(z, runtime["input_scale"], runtime["input_zero_point"])
    tensor = clipped.reshape(1, HEIGHT, WIDTH, 1)
    invoked = invoke_int8(runtime, tensor)
    if not np.array_equal(raw_frame, original_copy):
        raise ValueError("original frame changed after conversion")
    return {
        "npz_contract": {
            "dtype": str(raw_frame.dtype),
            "shape": list(raw_frame.shape),
            "unchanged": True,
        },
        "celsius_stats": percentile_stats(celsius),
        "p1_stats": percentile_stats(z),
        "p1_nan_inf": bool(percentile_stats(z)["nonfinite_count"]),
        "int8": int8_saturation_stats(unclipped, clipped),
        "extreme_pixels": extreme_pixel_report(celsius, raw_frame),
        "invoke": invoked,
    }


def spatial_repeat_check(path: Path, frame_index: int, celsius: np.ndarray) -> dict[str, Any]:
    mask = celsius < EXTREME_CELSIUS_LT
    if not mask.any():
        return {"extreme_low_present": False, "repeat_in_other_frames": None}
    with np.load(path, allow_pickle=False) as payload:
        frames = payload["frames"]
        repeats = []
        for index, frame in enumerate(frames):
            other = celsius_from_raw_uint16(frame)
            overlap = int((mask & (other < EXTREME_CELSIUS_LT)).sum())
            repeats.append({"frame_index": int(index), "overlap_count": overlap})
    selected_count = int(mask.sum())
    other = [row for row in repeats if row["frame_index"] != frame_index]
    always = bool(other) and all(row["overlap_count"] == selected_count for row in other)
    return {
        "extreme_low_present": True,
        "selected_low_count": selected_count,
        "fixed_spatial_locations_in_npz": always,
        "other_frames_checked": len(other),
    }


def run_replay(snapshot: Path, artifact: Path = THERMAL_ARTIFACT) -> dict[str, Any]:
    p1 = json.loads(P1_LOCK_PATH.read_text(encoding="utf-8"))
    if p1["mean"] != P1_MEAN or p1["std"] != P1_STD:
        raise ValueError("p1_lock.json does not match frozen O2 constants")
    runtime = load_locked_t_b5(artifact)
    files = list_thermal_npz(snapshot)
    metadata, per_file = scan_npz_metadata(files)
    readable = [path for path in files if path in per_file]
    selected = select_replay_set(readable, per_file)
    frames_out = []
    for item in selected:
        path: Path = item["path"]
        frame_index = int(item["frame_index"])
        with np.load(path, allow_pickle=False) as payload:
            validate_npz_contract(payload, path)
            frames = payload["frames"]
            timestamps = payload["timestamps"]
            sequences = payload["frame_sequences"]
            raw = np.array(frames[frame_index], copy=True)
            original = np.array(raw, copy=True)
            batch_shape = [int(v) for v in frames.shape]
            frame_sequence = int(sequences[frame_index])
            timestamp = float(timestamps[frame_index])
        result = replay_frame(runtime, raw, original_copy=original)
        spatial = spatial_repeat_check(path, frame_index, celsius_from_raw_uint16(raw))
        frames_out.append(
            {
                "role": item["role"],
                "filename": path.name,
                "sha256": sha256_file(path),
                "frame_index": frame_index,
                "frame_sequence": frame_sequence,
                "timestamp": timestamp,
                "batch_shape": batch_shape,
                **result,
                "spatial_repeat": spatial,
            }
        )
    low_sat = [row["int8"]["low_saturation_fraction"] for row in frames_out]
    high_sat = [row["int8"]["high_saturation_fraction"] for row in frames_out]
    classes = [row["invoke"]["class_name"] for row in frames_out]
    return {
        "document_id": "RP-X0-O2-THERMAL-REAL-SNAPSHOT-TB5-01",
        "classification": "PIPELINE_COMPATIBILITY_ONLY",
        "physical_conversion": {
            "formula": "physical_C = raw_uint16 / 10.0 - 273.15",
            "source": "RP-X0 O1 MI48 SPI 0.1 K contract",
        },
        "p1": {
            "profile_id": "P1_TRAIN_FITTED_GLOBAL_ZSCORE",
            "mean": P1_MEAN,
            "std": P1_STD,
            "epsilon_in_lock_unused": p1.get("epsilon"),
            "reproduced_exactly": True,
        },
        "t_b5": {
            "artifact": THERMAL_ARTIFACT.name,
            "sha256": runtime["sha256"],
            "input_shape": [1, HEIGHT, WIDTH, 1],
            "output_shape": list(runtime["output"]["shape"]),
            "input_scale": runtime["input_scale"],
            "input_zero_point": runtime["input_zero_point"],
            "output_scale": runtime["output_scale"],
            "output_zero_point": runtime["output_zero_point"],
            "load_success": True,
        },
        "int8_representable_celsius": representable_celsius_range(),
        "snapshot": {
            "used_read_only": True,
            "modified": False,
            "pi_venv_executed": False,
            "npz_count_listed": len(files),
        },
        "metadata_extremes": metadata,
        "selected_frames": frames_out,
        "aggregate": {
            "low_saturation_fraction_min": float(min(low_sat)),
            "low_saturation_fraction_max": float(max(low_sat)),
            "high_saturation_fraction_min": float(min(high_sat)),
            "high_saturation_fraction_max": float(max(high_sat)),
            "interpreted_classes": classes,
            "independent_ground_truth": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP-X0 O2 Thermal real-snapshot T-B5 replay")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_replay(args.snapshot.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
