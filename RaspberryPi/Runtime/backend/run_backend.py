#!/usr/bin/env python3
"""Run the PHASE 7 FastAPI backend and the complete sensor gateway."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="SafeNest integrated FastAPI backend")
    parser.add_argument("--api-host", default="0.0.0.0")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--sensor-host", default="0.0.0.0")
    parser.add_argument("--sensor-port", type=int, default=9000)
    parser.add_argument(
        "--thermal-udp-host",
        default=os.getenv("SAFENEST_THERMAL_UDP_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--thermal-udp-port",
        type=int,
        default=int(os.getenv("SAFENEST_THERMAL_UDP_PORT", "5005")),
    )
    parser.add_argument(
        "--thermal-udp-frame-timeout",
        type=float,
        default=float(os.getenv("SAFENEST_THERMAL_UDP_FRAME_TIMEOUT_SECONDS", "0.5")),
    )
    parser.add_argument(
        "--thermal-udp-max-pending-frames",
        type=int,
        default=int(os.getenv("SAFENEST_THERMAL_UDP_MAX_PENDING_FRAMES", "8")),
    )
    parser.add_argument("--packet-deadline", type=float, default=5.0)
    parser.add_argument("--evaluation-interval", type=float, default=15.0)
    parser.add_argument("--room", default="밀폐공간 A-01")
    parser.add_argument(
        "--db-path",
        default=str(Path(__file__).resolve().parent.parent / "data" / "safenest.db"),
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    if (
        not 1 <= args.api_port <= 65535
        or not 1 <= args.sensor_port <= 65535
        or not 1 <= args.thermal_udp_port <= 65535
    ):
        parser.error("ports must be between 1 and 65535")
    if (
        args.evaluation_interval <= 0
        or args.packet_deadline <= 0
        or args.thermal_udp_frame_timeout <= 0
        or args.thermal_udp_max_pending_frames <= 0
    ):
        parser.error("intervals must be positive")

    try:
        import uvicorn
        from backend.app import create_app
        from backend.runtime import SafeNestRuntime
        from database.store import PersistentRuntimeStore
    except ImportError as error:
        print(
            "Backend dependency missing. Install requirements-backend.txt from the repository root",
            file=sys.stderr,
        )
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    store = PersistentRuntimeStore(args.db_path)
    runtime = SafeNestRuntime(
        sensor_host=args.sensor_host,
        sensor_port=args.sensor_port,
        thermal_udp_host=args.thermal_udp_host,
        thermal_udp_port=args.thermal_udp_port,
        thermal_udp_frame_timeout_seconds=args.thermal_udp_frame_timeout,
        thermal_udp_max_pending_frames=args.thermal_udp_max_pending_frames,
        packet_deadline_seconds=args.packet_deadline,
        evaluation_interval_seconds=args.evaluation_interval,
        store=store,
    )
    app = create_app(runtime, room=args.room)
    uvicorn.run(app, host=args.api_host, port=args.api_port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
