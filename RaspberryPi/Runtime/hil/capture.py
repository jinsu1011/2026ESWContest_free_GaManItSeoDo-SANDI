#!/usr/bin/env python3
"""Capture live SafeNest API evidence for one physical HIL scenario."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hil.criteria import SCENARIOS, evaluate


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one SafeNest physical HIL scenario")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration and interval must be positive")

    started = time.time()
    samples: list[dict[str, object]] = []
    deadline = time.monotonic() + args.duration
    while True:
        sample: dict[str, object] = {"captured_at": time.time()}
        for name, endpoint in (("status", "/api/status"), ("health", "/health")):
            try:
                sample[name] = _fetch_json(args.base_url.rstrip("/") + endpoint)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
                sample[f"{name}_error"] = f"{type(error).__name__}: {error}"
        samples.append(sample)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))

    result = evaluate(args.scenario, samples)
    document = {
        "schema": "safenest.hil.report.v1",
        "scenario": args.scenario,
        "base_url": args.base_url,
        "started_at": started,
        "finished_at": time.time(),
        "sample_count": len(samples),
        "result": result,
        "samples": samples,
    }
    output = args.output or _default_output(args.scenario)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"{result['outcome']}: {result['summary']}")
    print(f"evidence: {output}")
    for check in result["checks"]:
        print(f"  {'PASS' if check['passed'] else 'FAIL'} {check['name']}: {check['observed']}")
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[str(result["outcome"])]


def _fetch_json(url: str) -> dict[str, object]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "SafeNest-HIL/1"})
    with urlopen(request, timeout=3.0) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP status {response.status}")
        document = json.loads(response.read().decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("API response root is not an object")
    return document


def _default_output(scenario: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(__file__).resolve().parent / "reports" / f"{scenario}-{stamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
