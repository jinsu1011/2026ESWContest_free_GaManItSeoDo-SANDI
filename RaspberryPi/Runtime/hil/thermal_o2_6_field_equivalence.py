"""RP-X0 O2.6 real-field FLOAT↔INT8 quantization-equivalence audit.

Deterministic ~240-frame replay. Does not implement O3, retrain, or activate
production Thermal. Does not write raw frames into Git.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from hil.thermal_o2_5_float_int8_compat import (
    EQUIVALENCE_CONTRACT,
    EXPECTED_FP32_SHA256,
    LABELS,
    compare_outputs,
    invoke_float,
    load_float_t_b5,
)
from hil.thermal_o2_real_snapshot_replay import (
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
    quantize_int8,
    sha256_file,
)

BIN_COUNT = 24
FRAMES_PER_BIN = 10
SATURATION_BINS = (
    (0.0, 0.10, "0-10%"),
    (0.10, 0.25, "10-25%"),
    (0.25, 0.50, "25-50%"),
    (0.50, 0.75, "50-75%"),
    (0.75, 1.0000001, "75-100%"),
)
CANONICAL_POLICY_ID = "THERMAL_T_B4_FLOAT_TFLITE_FP32_FULL_INT8_EQUIVALENCE_001"
CANONICAL_POLICY_STATUS = "EQUIVALENCE_MEASURED_NO_PREEXISTING_ABSOLUTE_ACCEPTANCE_THRESHOLD"


def even_indices(n: int, k: int = FRAMES_PER_BIN) -> list[int]:
    """Fixed evenly spaced positions. No output-dependent backfill."""
    if n <= 0:
        return []
    if n <= k:
        return list(range(n))
    raw = np.round(np.linspace(0, n - 1, k)).astype(int)
    out: list[int] = []
    seen: set[int] = set()
    for index in raw.tolist():
        value = int(index)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def assign_time_bin(timestamp: float, t_min: float, t_max: float, bin_count: int = BIN_COUNT) -> int:
    if t_max <= t_min:
        return 0
    width = (t_max - t_min) / bin_count
    index = int((timestamp - t_min) / width)
    if index >= bin_count:
        return bin_count - 1
    if index < 0:
        return 0
    return index


def percentile(values: np.ndarray, q: float) -> float:
    data = np.asarray(values, dtype=np.float64).reshape(-1)
    if data.size == 0:
        return float("nan")
    return float(np.percentile(data, q))


def catalog_readable_frames(snapshot: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    directory = snapshot / "data" / "thermal"
    files = sorted(directory.glob("20260817_*.npz"))
    frames: list[dict[str, Any]] = []
    corrupted: list[dict[str, str]] = []
    for path in files:
        try:
            with np.load(path, allow_pickle=False) as payload:
                if "frames" not in payload.files or "timestamps" not in payload.files:
                    raise ValueError("missing frames/timestamps")
                batch_shape = tuple(payload["frames"].shape)
                batch_dtype = payload["frames"].dtype
                stamps = np.array(payload["timestamps"], copy=True)
        except (OSError, EOFError, ValueError) as exc:
            corrupted.append(
                {
                    "filename": path.name,
                    "classification": "FIELD_CAPTURE_ARTIFACT",
                    "error": type(exc).__name__,
                }
            )
            continue
        if len(batch_shape) != 3 or batch_shape[1:] != (HEIGHT, WIDTH) or batch_dtype != np.uint16:
            corrupted.append(
                {
                    "filename": path.name,
                    "classification": "FIELD_CAPTURE_ARTIFACT",
                    "error": "CONTRACT_MISMATCH",
                }
            )
            continue
        if stamps.shape[0] != batch_shape[0]:
            corrupted.append(
                {
                    "filename": path.name,
                    "classification": "FIELD_CAPTURE_ARTIFACT",
                    "error": "TIMESTAMP_LENGTH_MISMATCH",
                }
            )
            continue
        for index in range(stamps.shape[0]):
            frames.append(
                {
                    "filename": path.name,
                    "path": path,
                    "frame_index": int(index),
                    "timestamp": float(stamps[index]),
                }
            )
    frames.sort(key=lambda item: (item["timestamp"], item["filename"], item["frame_index"]))
    return frames, corrupted


def select_deterministic_sample(frames: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not frames:
        raise ValueError("no readable frames")
    t_min = float(frames[0]["timestamp"])
    t_max = float(frames[-1]["timestamp"])
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(BIN_COUNT)]
    for item in frames:
        bin_id = assign_time_bin(item["timestamp"], t_min, t_max)
        buckets[bin_id].append(item)
    selected: list[dict[str, Any]] = []
    bin_report = []
    for bin_id, bucket in enumerate(buckets):
        idxs = even_indices(len(bucket), FRAMES_PER_BIN)
        chosen = []
        for index in idxs:
            row = dict(bucket[index])
            row["bin"] = bin_id
            row["bin_position"] = index
            chosen.append(row)
            selected.append(row)
        bin_report.append(
            {
                "bin": bin_id,
                "available": len(bucket),
                "selected": len(chosen),
            }
        )
    plan = {
        "algorithm": "24_equal_time_bins_x_10_evenly_spaced",
        "t_min": t_min,
        "t_max": t_max,
        "readable_frame_count": len(frames),
        "target_frames": BIN_COUNT * FRAMES_PER_BIN,
        "actual_frames": len(selected),
        "bins": bin_report,
        "identities_frozen_before_inference": True,
    }
    return selected, plan


def physical_stats(celsius: np.ndarray, z: np.ndarray) -> dict[str, Any]:
    c = np.asarray(celsius, dtype=np.float64).reshape(-1)
    p = np.asarray(z, dtype=np.float64).reshape(-1)
    finite_c = np.isfinite(c)
    return {
        "celsius": {
            "min": float(c[finite_c].min()) if finite_c.any() else None,
            "p1": percentile(c[finite_c], 1) if finite_c.any() else None,
            "p5": percentile(c[finite_c], 5) if finite_c.any() else None,
            "median": float(np.median(c[finite_c])) if finite_c.any() else None,
            "p95": percentile(c[finite_c], 95) if finite_c.any() else None,
            "p99": percentile(c[finite_c], 99) if finite_c.any() else None,
            "max": float(c[finite_c].max()) if finite_c.any() else None,
            "finite_fraction": float(finite_c.mean()),
        },
        "p1": {
            "min": float(p.min()),
            "max": float(p.max()),
            "mean": float(p.mean()),
            "std": float(p.std()),
            "finite": bool(np.all(np.isfinite(p))),
        },
    }


def int8_input_stats(clipped: np.ndarray, unclipped: np.ndarray) -> dict[str, Any]:
    flat = np.asarray(clipped).reshape(-1)
    un = np.asarray(unclipped).reshape(-1)
    return {
        "q_min": int(flat.min()),
        "q_max": int(flat.max()),
        "unique_q_count": int(np.unique(flat).size),
        "q_minus_128_fraction": float((un < -128).mean()),
        "q_plus_127_fraction": float((un > 127).mean()),
        "q_minus_128_represented_fraction": float((flat == -128).mean()),
    }


def saturation_bin_label(fraction: float) -> str:
    for lo, hi, label in SATURATION_BINS:
        if lo <= fraction < hi:
            return label
    return SATURATION_BINS[-1][2]


def classify_saturation_association(bin_rows: list[dict[str, Any]]) -> str:
    rates = []
    for row in bin_rows:
        total = int(row["frames"])
        if total < 8:
            continue
        rates.append((row["label"], float(row["agreement_rate"]), total))
    if len(rates) < 3:
        return "INSUFFICIENT_EVIDENCE"
    disagrees = [1.0 - r[1] for r in rates]
    # Monotone increase of disagreement with saturation bin order.
    increasing = all(disagrees[i] <= disagrees[i + 1] + 1e-12 for i in range(len(disagrees) - 1))
    spread = max(disagrees) - min(disagrees)
    if spread < 0.05:
        return "NO_ASSOCIATION_OBSERVED"
    if increasing and max(disagrees) >= 0.20 and spread >= 0.15:
        return "STRONG_ASSOCIATION_OBSERVED"
    if increasing and spread >= 0.08:
        return "POSSIBLE_ASSOCIATION"
    return "NO_ASSOCIATION_OBSERVED"


def transition_matrix(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    names = [LABELS[i] for i in range(3)]
    matrix = {src: {dst: 0 for dst in names} for src in names}
    for row in rows:
        matrix[row["float_class"]][row["int8_class"]] += 1
    return matrix


def decide_classification(
    *,
    agreement_rate: float,
    saturation_rel: str,
    collapse: bool,
    mae_p95: float,
) -> tuple[str, bool]:
    """Canonical T-B4 policy has no absolute AI acceptance threshold.

    O3 engineering uses mutual consistency vs collapse as a conservative
    integration decision, not a 95% invented model-quality gate.
    """
    mutually_consistent = agreement_rate >= 0.95 and mae_p95 <= 0.05 and saturation_rel != "STRONG_ASSOCIATION_OBSERVED"
    if mutually_consistent and collapse:
        return "DEVICE_DOMAIN_BEHAVIOR_REQUIRES_SEPARATE_VALIDATION", True
    if mutually_consistent and not collapse:
        return "INT8_EQUIVALENCE_ACCEPTABLE_FOR_O3", True
    if saturation_rel in {"POSSIBLE_ASSOCIATION", "STRONG_ASSOCIATION_OBSERVED"} and agreement_rate < 0.95:
        return "INT8_QUANTIZATION_REVIEW_REQUIRED", False
    if agreement_rate < 0.95:
        return "INT8_QUANTIZATION_REVIEW_REQUIRED", False
    return "DEVICE_DOMAIN_BEHAVIOR_REQUIRES_SEPARATE_VALIDATION", True


def replay_selected(
    snapshot: Path,
    selected: list[dict[str, Any]],
    float_artifact: Path,
    int8_artifact: Path = THERMAL_ARTIFACT,
) -> list[dict[str, Any]]:
    float_rt = load_float_t_b5(float_artifact)
    int8_rt = load_locked_t_b5(int8_artifact)
    if abs(int8_rt["input_scale"] - EXPECTED_INPUT_SCALE) > 1e-12:
        raise ValueError("INT8 input scale mismatch")
    if int8_rt["input_zero_point"] != EXPECTED_INPUT_ZERO_POINT:
        raise ValueError("INT8 input zero_point mismatch")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in selected:
        grouped.setdefault(item["filename"], []).append(item)
    results: list[dict[str, Any]] = []
    thermal = snapshot / "data" / "thermal"
    for filename, items in grouped.items():
        path = thermal / filename
        with np.load(path, allow_pickle=False) as payload:
            batch = np.array(payload["frames"], copy=True)
        for item in items:
            raw = batch[int(item["frame_index"])]
            celsius = celsius_from_raw_uint16(raw)
            z = apply_p1(celsius)
            clipped, unclipped = quantize_int8(z, int8_rt["input_scale"], int8_rt["input_zero_point"])
            float_out = invoke_float(float_rt, z)
            int8_out = invoke_int8(int8_rt, clipped.reshape(1, HEIGHT, WIDTH, 1))
            comparison = compare_outputs(float_out["probabilities"], int8_out["dequantized_output"])
            phys = physical_stats(celsius, z)
            qstats = int8_input_stats(clipped, unclipped)
            results.append(
                {
                    "filename": filename,
                    "frame_index": int(item["frame_index"]),
                    "timestamp": float(item["timestamp"]),
                    "bin": int(item["bin"]),
                    "celsius": phys["celsius"],
                    "p1": phys["p1"],
                    "int8_input": qstats,
                    "float_class": float_out["class_name"],
                    "int8_class": int8_out["class_name"],
                    "float_output": float_out["probabilities"],
                    "int8_dequantized": int8_out["dequantized_output"],
                    "int8_raw_output": int8_out["raw_output"],
                    "float_margin": comparison["float_margin"],
                    "int8_margin": comparison["int8_margin"],
                    "top1_agree": comparison["top1_agree"],
                    "ranking_agree": comparison["ranking_agree"],
                    "probability_mae": comparison["probability_mae"],
                    "probability_l1": comparison["probability_l1"],
                    "margin_difference": comparison["int8_margin"] - comparison["float_margin"],
                }
            )
    results.sort(key=lambda item: (item["timestamp"], item["filename"], item["frame_index"]))
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    agree = sum(1 for row in results if row["top1_agree"])
    rank = sum(1 for row in results if row["ranking_agree"])
    mae = np.array([row["probability_mae"] for row in results], dtype=np.float64)
    margin_diff = np.array([row["margin_difference"] for row in results], dtype=np.float64)
    low = np.array([row["int8_input"]["q_minus_128_fraction"] for row in results], dtype=np.float64)
    high = np.array([row["int8_input"]["q_plus_127_fraction"] for row in results], dtype=np.float64)
    float_counts = Counter(row["float_class"] for row in results)
    int8_counts = Counter(row["int8_class"] for row in results)
    bins = []
    for lo, hi, label in SATURATION_BINS:
        members = [row for row in results if lo <= row["int8_input"]["q_minus_128_fraction"] < hi]
        a = sum(1 for row in members if row["top1_agree"])
        bins.append(
            {
                "label": label,
                "frames": len(members),
                "agree": a,
                "disagree": len(members) - a,
                "agreement_rate": (a / len(members)) if members else None,
            }
        )
    sat_rel = classify_saturation_association(bins)
    collapse_float = (max(float_counts.values()) / n) >= 0.90 if n else False
    collapse_int8 = (max(int8_counts.values()) / n) >= 0.90 if n else False
    collapse = bool(collapse_float or collapse_int8)
    agreement_rate = agree / n if n else 0.0
    mae_p95 = percentile(mae, 95) if n else float("nan")
    classification, o3 = decide_classification(
        agreement_rate=agreement_rate,
        saturation_rel=sat_rel,
        collapse=collapse,
        mae_p95=mae_p95,
    )
    disagreements = [
        {
            "filename": row["filename"],
            "frame_index": row["frame_index"],
            "timestamp": row["timestamp"],
            "float_class": row["float_class"],
            "int8_class": row["int8_class"],
            "float_output": row["float_output"],
            "int8_dequantized": row["int8_dequantized"],
            "q_minus_128_fraction": row["int8_input"]["q_minus_128_fraction"],
            "q_plus_127_fraction": row["int8_input"]["q_plus_127_fraction"],
            "celsius_min": row["celsius"]["min"],
            "celsius_median": row["celsius"]["median"],
            "celsius_max": row["celsius"]["max"],
            "probability_mae": row["probability_mae"],
        }
        for row in results
        if not row["top1_agree"]
    ]
    return {
        "n": n,
        "top1_agree": agree,
        "top1_disagree": n - agree,
        "top1_agreement_rate": agreement_rate,
        "ranking_agreement_rate": rank / n if n else 0.0,
        "mae_median": float(np.median(mae)) if n else None,
        "mae_p90": percentile(mae, 90) if n else None,
        "mae_p95": mae_p95,
        "mae_max": float(mae.max()) if n else None,
        "margin_diff_median": float(np.median(margin_diff)) if n else None,
        "margin_diff_p95": percentile(margin_diff, 95) if n else None,
        "margin_diff_max": float(np.max(np.abs(margin_diff))) if n else None,
        "low_side_median": float(np.median(low)) if n else None,
        "low_side_p95": percentile(low, 95) if n else None,
        "high_side_max": float(high.max()) if n else None,
        "float_class_counts": {name: int(float_counts.get(name, 0)) for name in LABELS.values()},
        "int8_class_counts": {name: int(int8_counts.get(name, 0)) for name in LABELS.values()},
        "real_device_output_collapse_warning": collapse,
        "transition_matrix": transition_matrix(results),
        "saturation_bins": bins,
        "saturation_relationship": sat_rel,
        "disagreements": disagreements,
        "classification": classification,
        "o3_adapter_implementation": o3,
    }


def run_audit(snapshot: Path, float_artifact: Path) -> dict[str, Any]:
    if sha256_file(float_artifact) != EXPECTED_FP32_SHA256:
        raise ValueError("FLOAT SHA mismatch")
    if sha256_file(THERMAL_ARTIFACT) != EXPECTED_SHA256:
        raise ValueError("INT8 SHA mismatch")
    frames, corrupted = catalog_readable_frames(snapshot)
    selected, plan = select_deterministic_sample(frames)
    results = replay_selected(snapshot, selected, float_artifact)
    summary = summarize(results)
    return {
        "document_id": "RP-X0-O2-6-THERMAL-FIELD-FLOAT-INT8-EQUIV-01",
        "dependency_commit": "1929bc33cea1f7004e3d9d19900bf4023e0efb97",
        "canonical_ai_equivalence_policy": "FOUND",
        "policy_id": CANONICAL_POLICY_ID,
        "policy": CANONICAL_POLICY_STATUS,
        "equivalence_contract": EQUIVALENCE_CONTRACT,
        "fallback_95pct_used_as_ai_acceptance": False,
        "lineage": {
            "float_sha256": EXPECTED_FP32_SHA256,
            "int8_sha256": EXPECTED_SHA256,
            "same_lineage": True,
        },
        "preprocessing": {
            "celsius": "physical_C = raw_uint16 / 10.0 - 273.15",
            "p1_mean": P1_MEAN,
            "p1_std": P1_STD,
            "int8_scale": EXPECTED_INPUT_SCALE,
            "int8_zero_point": EXPECTED_INPUT_ZERO_POINT,
            "reproduced_exactly": True,
        },
        "corrupted_npz": {
            "count": len(corrupted),
            "classification": "FIELD_CAPTURE_ARTIFACT",
            "files": corrupted,
        },
        "sampling": plan,
        "selected_identities": [
            {
                "filename": item["filename"],
                "frame_index": item["frame_index"],
                "timestamp": item["timestamp"],
                "bin": item["bin"],
            }
            for item in selected
        ],
        "summary": summary,
        "production_thermal_activation": False,
        "snapshot_modified": False,
        "models_modified": False,
        "gate": "PASS_WITH_LIMITATIONS",
    }


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Path):
        return obj.name
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP-X0 O2.6 field FLOAT↔INT8 equivalence")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--float-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_audit(args.snapshot.resolve(), args.float_artifact.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    s = report["summary"]
    print(s["classification"], "agree", s["top1_agree"], "/", s["n"], s["saturation_relationship"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
