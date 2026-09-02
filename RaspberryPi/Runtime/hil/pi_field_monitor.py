#!/usr/bin/env python3
"""SafeNest Pi field monitor — storage / AI input / link / risk / LCD (table view).

Read-only. Polls GET /health, /api/status, /api/state.

The header Thermal line is inferred from GET /api/status runtime_status
(model_selector / preprocessing_id), not from launch flags or logs.

Examples (on Pi):
  python3 hil/pi_field_monitor.py
  python3 hil/pi_field_monitor.py --once
  python3 hil/pi_field_monitor.py --interval 3
  python3 hil/pi_field_monitor.py --raw-labels

Examples (from Mac):
  python3 hil/pi_field_monitor.py --base http://<PI_IP>:8000
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from typing import Any


SENSORS = ("mmwave", "thermal", "co2", "pir")

# Short labels for the table. Full names are spelled out in the legend below.
AI_STATE_SHORT = {
    "PHYSIOLOGY_ELIGIBLE": "PHYS_OK",
    "ABSENT": "ABSENT",
    "QUALITY_SUPPRESSED": "Q_LOW",
    "RR_UNAVAILABLE": "NO_RR",
    "WINDOW_NOT_READY": "WARMUP",
    "PRESENCE_UNAVAILABLE": "NO_OCC",
    "PRESENCE_FALSE": "EMPTY",
    "WINDOW_UNAVAILABLE": "NO_WIN",
    "INPUT_UNAVAILABLE": "NO_IN",
    "NORMAL": "NORMAL",
    "RAPID_OR_ABNORMAL": "RAPID",
    "APNEA": "APNEA",
    "NOT_HUMAN": "NO_HUM",
    "HUMAN_NORMAL": "HUMAN",
    "HUMAN_FALL": "FALL",
    "HUMAN_FALL_PROXY": "FALL_PX",
    "VACANT": "VACANT",
    "OCCUPIED": "OCC",
    "MOTION": "MOVE",
    "NO_MOTION": "STILL",
}

ERR_SHORT = {
    "WINDOW_NOT_READY": "WARMUP",
    "PRESENCE_UNAVAILABLE": "NO_OCC",
    "PRESENCE_FALSE": "EMPTY",
    "R1_TIMESTAMP_GRID_INCONSISTENT": "R1_TIME",
    "WINDOW_CONTAINS_LARGE_GAP": "GAP",
    "SENSOR_NO_DATA": "NO_DATA",
    "SENSOR_INVALID": "BAD",
    "SENSOR_STALE": "STALE",
    "SENSOR_DISCONNECTED": "DISC",
    "QUALITY_SUPPRESSED": "Q_LOW",
    "QUALITY_FAIL": "Q_LOW",
    "UNAVAILABLE_INVALID_DECODE": "NO_RR",
    "PHASE_MISSING": "NO_PH",
    "PHASE_STALE": "OLD_PH",
    "BOOT_BOUNDARY": "BOOT",
    "TIMESTAMP_INVALID": "BAD_TS",
    "PHASE_SEQUENCE_MISSING": "NO_SEQ",
    "SOURCE_RATE_BELOW_TARGET": "SLOW",
    "R1_SAMPLE_COUNT_MISMATCH": "R1_N",
    "INT8_QUANTIZATION_REVIEW_REQUIRED": "INT8",
    "CANONICAL_FRESHNESS_METADATA_MISSING": "NO_META",
    "PRESENCE_STATE_UNAVAILABLE": "NO_OCC",
    "THERMAL_FRAME_MISSING": "NO_FRM",
    "INSUFFICIENT_CONTINUOUS_DURATION": "WARMUP",
    "INPUT_WARMUP": "WARMUP",
    "MODEL_RUNTIME_UNAVAILABLE": "NO_MDL",
}

RISK_SHORT = {
    "RESPIRATION_NORMAL": "RR_OK",
    "RESPIRATION_ABNORMAL": "RR_ABN",
    "RESPIRATION_INPUT_UNAVAILABLE": "RR_NA",
    "NOT_HUMAN": "NO_HUM",
    "HUMAN_NORMAL": "HUMAN",
    "HUMAN_FALL": "FALL",
    "HUMAN_FALL_PROXY": "FALL_PX",
    "CO2_NORMAL": "CO2_OK",
    "CO2_WARNING": "CO2_WN",
    "CO2_DANGER": "CO2_DG",
    "CO2_IMMEDIATE_DANGER": "CO2_NOW",
    "MOTION": "MOVE",
    "NO_MOTION": "STILL",
    "NO_MOTION_RISING": "STILL+",
    "LONG_NO_MOTION": "STILL++",
    "UNAVAILABLE": "NA",
    "RULE_FALLBACK": "RULE",
    "NORMAL": "OK",
    "WARNING": "WARN",
    "DANGER": "DANGER",
}

STATUS_SHORT = {
    "LIVE": "LIVE",
    "NO_DATA": "NONE",
    "STALE": "STALE",
    "DISCONNECTED": "DISC",
    "INVALID": "BAD",
    "DEGRADED": "DEG",
}

# Process-selected Thermal identity. Unknown selectors stay UNKNOWN + raw id.
THERMAL_SELECTOR_LABELS = {
    "thermal_public_sdt_fp32_active": "BASELINE",
    "thermal_tv2_candidate_a_a0_fp32_v1": "A",
    "thermal_tv2_candidate_b_seed42_fp32_test_v1": "B",
}


def short_label(value: Any, mapping: dict[str, str], *, raw: bool) -> Any:
    if value is None:
        return None
    text = str(value)
    if raw:
        return text
    return mapping.get(text, text)


def get_json(base: str, path: str, timeout: float) -> dict[str, Any]:
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def cell(value: Any, width: int) -> str:
    text = "-" if value is None else str(value)
    if len(text) > width:
        text = text[: max(0, width - 1)] + "…"
    return text.ljust(width)


def table(headers: list[str], rows: list[list[Any]], widths: list[int] | None = None) -> str:
    if widths is None:
        widths = []
        for i, h in enumerate(headers):
            col = [str(h)] + [("-" if r[i] is None else str(r[i])) for r in rows]
            widths.append(min(28, max(len(c) for c in col)))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep]
    lines.append("| " + " | ".join(cell(h, w) for h, w in zip(headers, widths)) + " |")
    lines.append(sep)
    for row in rows:
        lines.append("| " + " | ".join(cell(v, w) for v, w in zip(row, widths)) + " |")
    lines.append(sep)
    return "\n".join(lines)


def fmt_num(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:.0f}"
        return f"{v:.2f}"
    return str(v)


def delta(curr: Any, prev: Any) -> str:
    if curr is None or prev is None:
        return "-"
    try:
        d = float(curr) - float(prev)
    except (TypeError, ValueError):
        return "-"
    if abs(d) < 1e-9:
        return "0"
    if d > 0:
        return f"+{fmt_num(d)}"
    return fmt_num(d)


def judge_flow(d: float | None, *, need_positive: bool = True) -> str:
    if d is None:
        return "?"
    if need_positive:
        return "YES" if d > 0 else "NO"
    return "OK" if d >= 0 else "DROP"


def sensor_block(status: dict[str, Any], name: str) -> dict[str, Any]:
    block = status.get(name)
    return block if isinstance(block, dict) else {}


def dig(mapping: Any, *keys: str, default: Any = None) -> Any:
    cur = mapping
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
    return cur


def thermal_runtime_status(status: Any) -> dict[str, Any]:
    """Read Thermal identity from GET /api/status. Never from env or logs."""

    if not isinstance(status, dict):
        return {}
    thermal = status.get("thermal")
    if isinstance(thermal, dict):
        runtime = thermal.get("runtime_status")
        if isinstance(runtime, dict):
            return runtime
    nested = dig(status, "runtime_status", "sensors", "thermal", default={})
    return nested if isinstance(nested, dict) else {}


def friendly_thermal_choice(selector: Any) -> str:
    text = str(selector).strip() if selector is not None else ""
    if not text:
        return "UNAVAILABLE"
    return THERMAL_SELECTOR_LABELS.get(text, "UNKNOWN")


def format_thermal_model_line(status: Any) -> str:
    runtime = thermal_runtime_status(status)
    selector = runtime.get("model_selector")
    text = str(selector).strip() if selector is not None else ""
    if not text:
        return "Thermal: UNAVAILABLE | selector=-"
    parts = [f"Thermal: {friendly_thermal_choice(text)} | {text}"]
    preprocessing = runtime.get("preprocessing_id")
    if preprocessing:
        parts.append(str(preprocessing))
    sha = runtime.get("model_sha256")
    if sha:
        sha_text = str(sha)
        parts.append(sha_text[:12] if len(sha_text) > 12 else sha_text)
    return " | ".join(parts)


def snapshot(base: str, timeout: float) -> dict[str, Any]:
    health = get_json(base, "/health", timeout)
    status = get_json(base, "/api/status", timeout)
    try:
        state = get_json(base, "/api/state", timeout)
    except Exception:
        state = {}
    return {"t": time.time(), "health": health, "status": status, "state": state}


def _tri(value: Any) -> str:
    if value is True:
        return "Y"
    if value is False:
        return "N"
    return "?"


def _mmwave_hint(vals: dict[str, Any], meta: dict[str, Any]) -> str:
    bits: list[str] = []
    bits.append(f"occ={_tri(vals.get('human_detected_raw'))}")
    if "occupancy_latch" in meta:
        bits.append(f"latch={_tri(meta.get('occupancy_latch'))}")
    br = meta.get("breathing_probability")
    if isinstance(br, (int, float)):
        bits.append(f"br={br:.2f}")
    rr = meta.get("rr_bpm")
    if isinstance(rr, (int, float)):
        bits.append(f"rr={rr:.0f}")
    q = meta.get("quality_probability")
    if isinstance(q, (int, float)):
        bits.append(f"q={q:.2f}")
    vrr = vals.get("breath_rate_raw")
    if isinstance(vrr, (int, float)):
        bits.append(f"vRR={vrr:.0f}")
    r1 = meta.get("r1_sample_count")
    if r1 is not None:
        bits.append(f"r1={r1}")
    src = meta.get("runtime") or ""
    if src:
        bits.append("B23" if "B23" in str(src) else str(src)[:6])
    return ",".join(bits) if bits else "-"


def render(
    curr: dict[str, Any],
    prev: dict[str, Any] | None,
    dt: float | None,
    *,
    raw_labels: bool = False,
) -> str:
    h = curr["health"]
    s = curr["status"]
    lcd = curr.get("state") or {}
    rx = dig(h, "receiver", default={}) or {}
    th = dig(rx, "thermal_udp", default={}) or {}
    log = dig(rx, "sensor_logging", default={}) or {}
    db = dig(h, "database", default={}) or {}
    risk = dig(s, "risk", default={}) or {}

    prev_h = dig(prev or {}, "health", default={}) or {}
    prev_rx = dig(prev_h, "receiver", default={}) or {}
    prev_th = dig(prev_rx, "thermal_udp", default={}) or {}
    prev_log = dig(prev_rx, "sensor_logging", default={}) or {}
    prev_db = dig(prev_h, "database", default={}) or {}

    telem = rx.get("telemetry_packets")
    frames = th.get("completed_frames")
    written = dig(log, "written", default={}) or {}
    prev_written = dig(prev_log, "written", default={}) or {}
    snap_n = dig(db, "counts", "snapshots")
    event_n = dig(db, "counts", "events")

    d_telem = None if prev is None else (telem or 0) - (prev_rx.get("telemetry_packets") or 0)
    d_frames = None if prev is None else (frames or 0) - (prev_th.get("completed_frames") or 0)
    d_mm = None if prev is None else (written.get("mmwave") or 0) - (prev_written.get("mmwave") or 0)
    d_co2 = None if prev is None else (written.get("co2") or 0) - (prev_written.get("co2") or 0)
    d_thm = None if prev is None else (written.get("thermal") or 0) - (prev_written.get("thermal") or 0)
    d_snap = None if prev is None else (snap_n or 0) - (dig(prev_db, "counts", "snapshots") or 0)

    cols = shutil.get_terminal_size((100, 24)).columns
    title = "SafeNest field monitor"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    dt_s = f"{dt:.1f}s" if dt is not None else "-"

    lines: list[str] = []
    lines.append(f"{title}  |  {stamp}  |  Δ window {dt_s}  |  cols≈{cols}")
    lines.append(format_thermal_model_line(s))
    lines.append("")

    # --- overview judgments ---
    lines.append("## Verdict")
    verdict_rows = [
        ["TCP flow", judge_flow(d_telem), f"conn={rx.get('connections')} telem={telem} Δ={delta(telem, prev_rx.get('telemetry_packets'))}"],
        ["UDP flow", judge_flow(d_frames), f"frames={frames} Δ={delta(frames, prev_th.get('completed_frames'))}"],
        ["Save mmW", judge_flow(d_mm), f"n={written.get('mmwave')} Δ={delta(written.get('mmwave'), prev_written.get('mmwave'))}"],
        ["Save CO2", judge_flow(d_co2), f"n={written.get('co2')} Δ={delta(written.get('co2'), prev_written.get('co2'))}"],
        ["Save thm", judge_flow(d_thm), f"n={written.get('thermal')} Δ={delta(written.get('thermal'), prev_written.get('thermal'))}"],
        ["DB grow", judge_flow(d_snap), f"snap={snap_n} ev={event_n} Δ={delta(snap_n, dig(prev_db, 'counts', 'snapshots'))}"],
        ["Log worker", "YES" if log.get("running") else "NO", f"on={log.get('enabled')} q={log.get('queue_size')}/{log.get('queue_capacity')} err={log.get('errors')}"],
    ]
    # AI input: any sensor not INPUT_UNAVAILABLE / BLOCKED with LIVE-ish status
    ai_ok = []
    ai_bad = []
    for name in SENSORS:
        ai = dig(sensor_block(s, name), "ai", default={}) or {}
        st = dig(sensor_block(s, name), "state", default={}) or {}
        ai_state = ai.get("state")
        sens = st.get("status")
        if sens in {"LIVE", "DEGRADED"} and ai_state not in {None, "INPUT_UNAVAILABLE"}:
            ai_ok.append(name)
        else:
            ai_bad.append(f"{name}:{sens}/{ai_state}")
    verdict_rows.append(
        [
            "AI input",
            "YES" if ai_ok else "NO",
            ("ok=" + ",".join(ai_ok)) if ai_ok else ("fail=" + ",".join(ai_bad)),
        ]
    )
    verdict_rows.append(
        [
            "Risk",
            "YES" if risk.get("formula_id") == "SAFENEST_RISK_V1" else "NO",
            f"{risk.get('formula_id')} score={risk.get('risk_score')} level={risk.get('risk_level')} evid={risk.get('evidence_sufficient')}",
        ]
    )
    verdict_rows.append(
        [
            "LCD state",
            str(lcd.get("state") or "-").upper(),
            f"room={lcd.get('room')} rev={lcd.get('revision')}",
        ]
    )
    lines.append(table(["check", "ok?", "detail"], verdict_rows, [10, 12, min(70, max(36, cols - 32))]))
    lines.append("")

    # --- link / storage ---
    lines.append("## Link & storage")
    lines.append(
        table(
            ["metric", "now", "Δ", "note"],
            [
                ["system", f"{s.get('system')}/{s.get('system_health')}", "-", f"ready={h.get('ready')} offline={s.get('offline')}"],
                ["tcp:9000 conn", rx.get("connections"), delta(rx.get("connections"), prev_rx.get("connections")), f"disc={rx.get('disconnects')} gaps={rx.get('sequence_gaps')} proto_err={rx.get('protocol_errors')}"],
                ["telemetry pkts", telem, delta(telem, prev_rx.get("telemetry_packets")), f"thermal_tcp_unexpected={rx.get('unexpected_tcp_thermal_packets')}"],
                ["udp:5005 frames", frames, delta(frames, prev_th.get("completed_frames")), f"dgram={th.get('received_datagrams')} incomplete={th.get('incomplete_frames')} fps={fmt_num(th.get('effective_fps'))}"],
                ["log written mm", written.get("mmwave"), delta(written.get("mmwave"), prev_written.get("mmwave")), f"accepted={dig(log,'accepted','mmwave')} dropped={dig(log,'dropped','mmwave')}"],
                ["log written co2", written.get("co2"), delta(written.get("co2"), prev_written.get("co2")), f"accepted={dig(log,'accepted','co2')} dropped={dig(log,'dropped','co2')}"],
                ["log written thm", written.get("thermal"), delta(written.get("thermal"), prev_written.get("thermal")), f"accepted={dig(log,'accepted','thermal')} dropped={dig(log,'dropped','thermal')}"],
                ["db snapshots", snap_n, delta(snap_n, dig(prev_db, "counts", "snapshots")), f"path={db.get('path')}"],
                ["db events", event_n, delta(event_n, dig(prev_db, "counts", "events")), f"schema={db.get('schema_version')} avail={db.get('available')}"],
            ],
            [16, 14, 10, min(55, max(28, cols - 50))],
        )
    )
    lines.append("")

    # --- per-sensor / AI / risk / LCD ---
    lines.append("## Sensors / AI / risk component")
    sens_rows: list[list[Any]] = []
    for name in SENSORS:
        block = sensor_block(s, name)
        st = dig(block, "state", default={}) or {}
        ai = dig(block, "ai", default={}) or {}
        rc = dig(block, "risk_component", default={}) or {}
        rt = dig(block, "runtime_status", default={}) or dig(s, "runtime_status", "sensors", name, default={}) or {}
        vals = dig(st, "values", default={}) or {}
        meta = dig(ai, "metadata", default={}) or {}
        # compact value hint
        if name == "mmwave":
            hint = _mmwave_hint(vals, meta)
        else:
            hint_bits = []
            if vals.get("co2_ppm") is not None:
                hint_bits.append(f"ppm={vals.get('co2_ppm')}")
            if "motion" in vals and vals.get("motion") is not None:
                hint_bits.append(f"mov={_tri(vals.get('motion'))}")
            if vals.get("max_c") is not None:
                hint_bits.append(f"maxC={vals.get('max_c')}")
            if meta.get("canonical_window_status"):
                hint_bits.append(str(meta.get("canonical_window_status")))
            hint = ",".join(hint_bits) if hint_bits else "-"
        err = ai.get("error") or rt.get("blocked_reason")
        risk_state = rc.get("state") or dig(risk, "component_status", name)
        sens_rows.append(
            [
                name,
                short_label(st.get("status"), STATUS_SHORT, raw=raw_labels),
                fmt_num(st.get("age_seconds") if st.get("age_seconds") is not None else st.get("age_s")),
                short_label(ai.get("state"), AI_STATE_SHORT, raw=raw_labels),
                short_label(err, ERR_SHORT, raw=raw_labels) if err else "-",
                fmt_num(ai.get("confidence") if name == "mmwave" and ai.get("confidence") is not None else ai.get("score")),
                fmt_num(ai.get("latency_ms")),
                short_label(risk_state, RISK_SHORT, raw=raw_labels),
                fmt_num(rc.get("score") if rc.get("score") is not None else dig(risk, "component_scores", name)),
                hint,
            ]
        )
    lines.append(
        table(
            ["sensor", "status", "age_s", "ai", "err", "score", "ms", "risk", "rsc", "values"],
            sens_rows,
            [7, 6, 6, 8, 8, 6, 5, 8, 6, min(72, max(56, cols - 64))],
        )
    )
    lines.append("shorts: PHYS_OK=B23추론가능  WARMUP=창모으는중  NO_OCC=재실모름  EMPTY=빈방  Q_LOW=품질낮음")
    lines.append("mmwave values: occ=ESP재실 latch=유지 br=숨확률 rr=B23호흡수 q=품질 vRR=벤더BPM")
    lines.append("")

    lines.append("## Risk / LCD (display)")
    lines.append(
        table(
            ["field", "value"],
            [
                ["formula_id", risk.get("formula_id")],
                ["formula_version", risk.get("formula_version")],
                ["risk_score / level", f"{risk.get('risk_score')} / {risk.get('risk_level')}"],
                ["effective_weight", risk.get("effective_weight")],
                ["evidence_sufficient", risk.get("evidence_sufficient")],
                ["presence", f"{risk.get('presence_detected')} ({risk.get('presence_source')})"],
                ["degraded_mode", risk.get("degraded_mode")],
                ["reasons", ",".join(risk.get("reasons") or []) or "-"],
                ["LCD state", lcd.get("state")],
                ["LCD room", lcd.get("room")],
                ["LCD revision", lcd.get("revision")],
                ["pub_revision", s.get("publication_revision") or h.get("publication_revision")],
            ],
            [20, min(70, max(40, cols - 28))],
        )
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="SafeNest Pi field monitor (tables)")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--interval", type=float, default=4.0, help="seconds between samples")
    parser.add_argument("--once", action="store_true", help="two samples then exit (still shows Δ)")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-clear", action="store_true")
    parser.add_argument(
        "--raw-labels",
        action="store_true",
        help="show full AI/error/risk strings instead of short labels",
    )
    args = parser.parse_args()

    prev: dict[str, Any] | None = None
    try:
        while True:
            try:
                curr = snapshot(args.base, args.timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"[error] cannot fetch {args.base}: {exc}", file=sys.stderr)
                if args.once:
                    return 2
                time.sleep(args.interval)
                continue

            dt = None if prev is None else (curr["t"] - prev["t"])
            # need a previous sample for meaningful Δ; take one quiet sample first
            if prev is None:
                prev = curr
                if not args.no_clear:
                    print("\033[2J\033[H", end="")
                print(f"warming Δ sample against {args.base} …")
                time.sleep(args.interval)
                continue

            body = render(curr, prev, dt, raw_labels=args.raw_labels)
            if not args.no_clear:
                print("\033[2J\033[H", end="")
            print(body)
            prev = curr

            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
