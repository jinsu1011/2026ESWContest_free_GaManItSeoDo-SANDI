"""Stage 9 minimal live-smoke tooling.

Default invocation is plan-only and does not contact hardware.
`--live` is the explicit future Pi/sensor path and is not executed by tests.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hil.stage9_evaluate import evaluate_observation


ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "safenest.stage9.smoke.v1"
DEFAULT_WINDOW_SECONDS = 20.0
DEFAULT_HTTP_PORT = 8000
LOCAL_HTTP_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
PROBE_NAMES = (
    "backend_health",
    "tcp_9000",
    "udp_5005",
    "esp_connection",
    "co2_progress",
    "thermal_progress",
    "mmwave_progress",
    "pir_progress",
    "runtime_status",
    "logger_drops",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage 9 minimal live-smoke tooling. Default is plan/help only. "
            "Does not probe hardware unless --live is given."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Print the smoke plan without probing (default).")
    mode.add_argument(
        "--evaluate-fixture",
        metavar="PATH",
        help="Evaluate a recorded observation fixture. Never claims live smoke.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="FUTURE HARDWARE ONLY. Read-only probes against a running Pi runtime.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Live HTTP host. Default 127.0.0.1.")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help="Operational observation window for --live. Not a model/data contract.",
    )
    parser.add_argument("--output", help="Optional JSON output path. stdout is always used.")
    args = parser.parse_args(argv)

    if args.evaluate_fixture:
        document = fixture_document(Path(args.evaluate_fixture))
    elif args.live:
        document = live_document(
            host=args.host,
            http_port=args.http_port,
            window_seconds=args.window_seconds,
        )
    else:
        document = plan_document()

    rendered = json.dumps(document, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return exit_code(document)


def plan_document() -> dict[str, Any]:
    probes = {
        name: {
            "name": name,
            "status": "NOT_RUN",
            "observed": None,
            "expected": "future live observation",
            "reason": "plan mode does not probe hardware",
        }
        for name in PROBE_NAMES
    }
    return assemble_report(
        mode="PLAN",
        result="NOT_RUN",
        probes=probes,
        extra={
            "checks": [
                "backend /health and /api/status",
                "TCP :9000 listener",
                "UDP :5005 listener",
                "ESP TCP session",
                "CO2/Thermal/mmWave/PIR identity progress",
                "runtime-status partial availability",
                "no new logger drops",
            ],
            "does_not_check": [
                "30-minute soak",
                "model accuracy",
                "Thermal T-B5 activation",
                "mmWave historical B live gate",
                "risk thresholds",
                "Capture",
            ],
            "future_live_command": "python3 -m hil.stage9_smoke --live",
            "window_seconds_default": DEFAULT_WINDOW_SECONDS,
            "window_note": "Operational smoke timing, not a model or sampling contract.",
        },
    )


def fixture_document(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture must be a JSON object")
    evaluated = evaluate_observation(payload, mode="OFFLINE_FIXTURE")
    return assemble_report(
        mode="OFFLINE_FIXTURE",
        result=evaluated["result"],
        probes=evaluated["probes"],
        extra={
            "fixture": str(path),
            "window_seconds": payload.get("window_seconds"),
        },
    )


def live_document(
    *,
    host: str,
    http_port: int,
    window_seconds: float,
    http_get=None,
    collect_sockets=None,
    sleep=None,
    clock=None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    current_platform = platform_name or sys.platform
    if not current_platform.startswith("linux"):
        probes = {
            name: {
                "name": name,
                "status": "NOT_RUN",
                "observed": current_platform,
                "expected": "linux",
                "reason": "LIVE mode requires Linux/Pi listener inspection",
            }
            for name in PROBE_NAMES
        }
        return assemble_report(
            mode="LIVE",
            result="FAIL",
            probes=probes,
            extra={
                "live_unsupported_platform": True,
                "platform": current_platform,
                "window_seconds": window_seconds,
                "host": host,
                "http_port": http_port,
            },
        )
    if not is_local_http_host(host):
        probes = {
            name: {
                "name": name,
                "status": "NOT_RUN",
                "observed": host,
                "expected": "127.0.0.1/localhost",
                "reason": "LIVE ss probing is local; remote --host would mix socket and HTTP provenance",
            }
            for name in PROBE_NAMES
        }
        return assemble_report(
            mode="LIVE",
            result="FAIL",
            probes=probes,
            extra={
                "live_unsupported_remote_host": True,
                "host": host,
                "http_port": http_port,
                "window_seconds": window_seconds,
            },
        )

    getter = http_get or (lambda path: fetch_json(f"http://{host}:{http_port}{path}"))
    sockets = collect_sockets or collect_ss_listen
    sleeper = sleep or time.sleep
    now = clock or time.time
    started = now()
    health_before, health_error_before = getter("/health")
    status_before, status_error_before = getter("/api/status")
    socket_table, socket_error = sockets()
    sleeper(window_seconds)
    health_after, health_error_after = getter("/health")
    status_after, status_error_after = getter("/api/status")
    observation = {
        "health_before": health_before,
        "health_after": health_after,
        "health_error_before": health_error_before,
        "health_error_after": health_error_after,
        "status_before": status_before,
        "status_after": status_after,
        "status_error_before": status_error_before,
        "status_error_after": status_error_after,
        "socket_table": socket_table,
        "socket_error": socket_error,
        "window_seconds": window_seconds,
        "elapsed_seconds": now() - started,
    }
    evaluated = evaluate_observation(observation, mode="LIVE")
    return assemble_report(
        mode="LIVE",
        result=evaluated["result"],
        probes=evaluated["probes"],
        extra={
            "host": host,
            "http_port": http_port,
            "window_seconds": window_seconds,
            "elapsed_seconds": observation["elapsed_seconds"],
            "read_only": True,
        },
    )


def assemble_report(
    *,
    mode: str,
    result: str,
    probes: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = {
        "schema": SCHEMA,
        "mode": mode,
        "result": result,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "repository": {"commit": git_sha()},
        "host": {"platform": sys.platform, "python": platform.python_version()},
        "stage_9_live_smoke": result if mode == "LIVE" else "NOT_RUN",
        "mac_tooling_does_not_imply_live_smoke": True,
        "probes": dict(probes),
        "exit": {
            "PASS": 0,
            "PASS_WITH_LIMITATIONS": 0,
            "NOT_RUN": 0,
            "FAIL": 1,
        }.get(result, 1),
    }
    if extra:
        document.update(dict(extra))
    return document


def exit_code(document: Mapping[str, Any]) -> int:
    return 0 if document.get("result") in {"PASS", "PASS_WITH_LIMITATIONS", "NOT_RUN"} else 1


def is_local_http_host(host: str) -> bool:
    return str(host or "").strip().lower().split("%", 1)[0] in LOCAL_HTTP_HOSTS


def fetch_json(url: str, timeout: float = 2.0) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        return None, f"unreachable: {error}"
    except (OSError, ValueError, UnicodeError) as error:
        return None, f"invalid_response: {type(error).__name__}: {error}"
    if not isinstance(payload, dict):
        return None, "invalid_response: JSON object required"
    return payload, None


def collect_ss_listen() -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run(
            ["ss", "-H", "-l", "-t", "-u", "-n"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"{type(error).__name__}: {error}"
    if completed.returncode != 0:
        return None, completed.stderr.strip() or f"ss exited {completed.returncode}"
    return completed.stdout, None


def git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip() or "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
