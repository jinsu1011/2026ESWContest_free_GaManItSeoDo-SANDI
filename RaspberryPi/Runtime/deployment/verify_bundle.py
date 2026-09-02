#!/usr/bin/env python3
"""Verify that a SafeNest folder is complete and safe to distribute."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


# Verification runs against the repository root so every canonical component
# (ESP32 firmware, Ondevice_AI, Web, Runtime) is expressed with one path base.
ROOT = Path(__file__).resolve().parents[3]
RUNTIME = "RaspberryPi/Runtime"
REQUIRED_FILES = (
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "COMPONENT_SOURCES.json",
    "run_safenest.sh",
    f"{RUNTIME}/requirements-backend.txt",
    f"{RUNTIME}/.env.example",
    f"{RUNTIME}/paths.py",
    f"{RUNTIME}/thermal_test_selector.py",
    f"{RUNTIME}/backend/run_backend.py",
    f"{RUNTIME}/backend/app.py",
    f"{RUNTIME}/backend/runtime.py",
    f"{RUNTIME}/backend/runtime_status.py",
    f"{RUNTIME}/backend/portal.py",
    f"{RUNTIME}/backend/views.py",
    f"{RUNTIME}/backend/store.py",
    f"{RUNTIME}/hil/preflight.py",
    f"{RUNTIME}/gateway/protocol.py",
    f"{RUNTIME}/gateway/receiver.py",
    f"{RUNTIME}/gateway/thermal_udp.py",
    f"{RUNTIME}/state/manager.py",
    f"{RUNTIME}/ai/pipeline.py",
    f"{RUNTIME}/ai/runtime.py",
    f"{RUNTIME}/ai/mmwave_b23_runtime.py",
    f"{RUNTIME}/ai/co2_canonical_runtime.py",
    f"{RUNTIME}/risk/engine.py",
    f"{RUNTIME}/risk/formula_v1.py",
    f"{RUNTIME}/risk/risk_formula_v1.json",
    f"{RUNTIME}/storage/sensor_logger.py",
    f"{RUNTIME}/services/tts.py",
    f"{RUNTIME}/services/emergency.py",
    f"{RUNTIME}/database/schema.sql",
    f"{RUNTIME}/database/repository.py",
    f"{RUNTIME}/database/store.py",
    f"{RUNTIME}/deployment/run_pi.sh",
    # Web: every file the backend serves or the preflight requires.
    "RaspberryPi/Web/index.html",
    "RaspberryPi/Web/styles.css",
    "RaspberryPi/Web/app.js",
    "RaspberryPi/Web/index_final.html",
    "RaspberryPi/Web/styles_final.css",
    "RaspberryPi/Web/app_final.js",
    "RaspberryPi/Web/portal/preview.html",
    "RaspberryPi/Web/portal/admin-api.js",
    "RaspberryPi/Web/portal/thermal-client.js",
    "RaspberryPi/Web/guest/index.html",
    "RaspberryPi/Web/vendor/chart.js/chart.umd.min.js",
    "RaspberryPi/Web/vendor/chart.js/LICENSE.md",
    # LCD canonical panel.
    "RaspberryPi/LCD/static/display.html",
    "RaspberryPi/LCD/static/common.css",
    "RaspberryPi/LCD/static/control.html",
    # On-device AI: adapters plus the frozen manifest they resolve against.
    "RaspberryPi/Ondevice_AI/requirements-pi.txt",
    "RaspberryPi/Ondevice_AI/models/model_manifest.json",
    "RaspberryPi/Ondevice_AI/inference/thermal_interpreter.py",
    "RaspberryPi/Ondevice_AI/inference/co2_c_b6_interpreter.py",
    "RaspberryPi/Ondevice_AI/inference/mmwave_m_n9_interpreter.py",
    "RaspberryPi/Ondevice_AI/risk/risk_config.json",
    # ESP32 canonical firmware.
    "ESP32/Arduino/esp32_sensor_node/esp32_sensor_node.ino",
    "ESP32/Arduino/esp32_sensor_node/secrets.example.h",
    "ESP32/docs/COMMUNICATION_PROTOCOL.md",
)
FORBIDDEN_NAMES = {"secrets.h"}
FORBIDDEN_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc"}


def verify(root: Path = ROOT) -> dict[str, object]:
    root = root.resolve()
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    forbidden = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            forbidden.append(str(path.relative_to(root)))
    caches = [str(path.relative_to(root)) for path in root.rglob("__pycache__") if path.is_dir()]
    model_checks = _model_checks(root)
    checks = {
        "required_files_present": not missing,
        "model_hashes_match": bool(model_checks) and all(item["match"] for item in model_checks),
        "no_secrets_or_databases": not forbidden,
        "no_python_caches": not caches,
    }
    return {
        "schema": "safenest.bundle.verification.v1",
        "ok": all(checks.values()),
        "root": str(root),
        "checks": checks,
        "missing": missing,
        "forbidden": forbidden,
        "caches": caches,
        "models": model_checks,
        "file_count": sum(1 for path in root.rglob("*") if path.is_file()),
    }


def _model_checks(root: Path) -> list[dict[str, object]]:
    model_root = root / "RaspberryPi" / "Ondevice_AI"
    manifest_path = model_root / "models" / "model_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    results = []
    for sensor_id, entry in manifest.get("models", {}).items():
        path = model_root / str(entry.get("path", ""))
        expected = str(entry.get("sha256", ""))
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual = None
        results.append({
            "sensor_id": sensor_id,
            "path": str(path.relative_to(root)),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "match": bool(expected) and actual == expected,
        })
    return results


def main() -> int:
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
