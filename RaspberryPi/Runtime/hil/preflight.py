#!/usr/bin/env python3
"""Deployment readiness checks.

Default mode is the existing Pi-start preflight used by deployment/run_pi.sh.
`--offline-preflight` is the Stage 7 Mac-offline structural validator. It does
not bind ports, does not require Raspberry Pi, and does not treat a Mac PASS as
a Pi PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import re
import socket
import sys

from paths import (
    LCD_STATIC,
    MODEL_MANIFEST,
    ONDEVICE_AI_ROOT,
    RUNTIME_ROOT,
    WEB_GUEST,
    WEB_PORTAL,
    WEB_ROOT,
)

ROOT = RUNTIME_ROOT

REQUIRED_RUNTIME_FILES = (
    "backend/run_backend.py",
    "backend/app.py",
    "backend/runtime.py",
    "backend/runtime_status.py",
    "backend/views.py",
    "deployment/run_pi.sh",
    "database/schema.sql",
    "gateway/receiver.py",
    "gateway/thermal_udp.py",
    "state/manager.py",
    "ai/pipeline.py",
    "ai/runtime.py",
    "risk/engine.py",
    ".env.example",
)

CANONICAL_EXTERNAL_FILES = (
    ("web/dashboard/index.html", WEB_ROOT / "index.html"),
    ("web/dashboard/app.js", WEB_ROOT / "app.js"),
    ("web/dashboard/styles.css", WEB_ROOT / "styles.css"),
    ("web/portal/preview.html", WEB_PORTAL / "preview.html"),
    ("web/portal/admin-api.js", WEB_PORTAL / "admin-api.js"),
    ("web/portal/thermal-client.js", WEB_PORTAL / "thermal-client.js"),
    ("web/guest/index.html", WEB_GUEST / "index.html"),
    ("sources/display-test2/raspberry_pi_lcd/static/display.html", LCD_STATIC / "display.html"),
    ("sources/display-test2/raspberry_pi_lcd/static/control.html", LCD_STATIC / "control.html"),
    ("sources/display-test2/raspberry_pi_lcd/static/common.css", LCD_STATIC / "common.css"),
    ("sources/ondevice_ai/models/model_manifest.json", MODEL_MANIFEST),
)

EXPECTED_ENV_KEYS = (
    "SAFENEST_ADMIN_ID",
    "SAFENEST_ADMIN_PASSWORD",
    "SMS_ACCESS_KEY",
    "SMS_SECRET_KEY",
    "SMS_SERVICE_ID",
    "SMS_FROM_NUMBER",
    "SMS_API_BASE_URL",
    "SMS_TIMEOUT_SECONDS",
    "MANAGER_PHONE_NUMBER",
    "MANAGER_NAME",
    "SAFENEST_SMS_COOLDOWN_SECONDS",
    "SAFENEST_GPIO_MODE",
    "SAFENEST_BUZZER_GPIO_PIN",
    "SAFENEST_BUZZER_FREQUENCY_HZ",
    "SAFENEST_CO2_UPDATE_INTERVAL_SECONDS",
    "SAFENEST_SENSOR_DATA_ENABLED",
    "SAFENEST_SENSOR_DATA_ROOT",
    "SAFENEST_SENSOR_DATA_MAX_GB",
    "SAFENEST_MIN_FREE_DISK_GB",
    "SAFENEST_MMWAVE_DATA_MAX_GB",
    "SAFENEST_CO2_DATA_MAX_GB",
    "SAFENEST_THERMAL_DATA_MAX_GB",
    "SAFENEST_SENSOR_DATA_QUEUE_CAPACITY",
    "SAFENEST_THERMAL_BATCH_FRAMES",
    "SAFENEST_THERMAL_FLUSH_SECONDS",
    "SAFENEST_SENSOR_CLEANUP_INTERVAL_SECONDS",
    "SAFENEST_THERMAL_UDP_HOST",
    "SAFENEST_THERMAL_UDP_PORT",
    "SAFENEST_THERMAL_UDP_FRAME_TIMEOUT_SECONDS",
    "SAFENEST_THERMAL_UDP_MAX_PENDING_FRAMES",
    "SAFENEST_VENV_PATH",
)

FORBIDDEN_THERMAL_SELECTORS = (
    "SMALL_CNN_BASELINE",
    "T-B5",
    "T_B5",
    "full_int8.tflite",
)
THERMAL_ACTIVE_KEY = "thermal_public_sdt_fp32_active"
THERMAL_MODEL_ID = "thermal_public_sdt_pooled_mlp_fp32_tflite_v1"
THERMAL_RUNTIME_ROLE = "ACTIVE_THERMAL_PUBLIC_SDT_SOFTWARE_ONLY"
THERMAL_RELATIVE_PATH = (
    "models/thermal/public_sdt/public_sdt_pooled_mlp_fp32_tflite_v1.tflite"
)

MN9_MODEL_ID = "MMWAVE_M_N9_FULL_INT8_V1"
MN9_RUNTIME_ROLE = "ACTIVE_M_N9"
MN9_RELATIVE_PATH = "models/mmwave/m_n9/MMWAVE_M_N9_FULL_INT8_V1.tflite"
B23_MODEL_ID = "M-PV2_FAMILY_B_TRACE_TCN_BREATHING_RR_QUALITY"
B23_RUNTIME_ROLE = "ACTIVE_B23_PROTOTYPE"
B23_RELATIVE_PATH = "models/mmwave/m_prot_b23/candidate_seed_23.pt"
HISTORICAL_V010_FILENAME = "mmwave_resp_int8_v0.1.0.tflite"
HISTORICAL_B_PATH_MARKERS = ("rp_x0_b_complete", "M-B3_")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SafeNest deployment readiness checks")
    parser.add_argument(
        "--offline-preflight",
        action="store_true",
        help="Stage 7 Mac-offline structural checks. Does not validate Pi or sensors.",
    )
    args = parser.parse_args(argv)
    document = offline_preflight_document() if args.offline_preflight else pi_start_document()
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if document["ok"] else 1


def pi_start_document(root: Path = ROOT) -> dict[str, object]:
    checks = [
        _check("python_3_10", sys.version_info >= (3, 10), platform.python_version()),
        _check("linux", sys.platform.startswith("linux"), sys.platform, required=False),
        _check("raspberry_pi", "raspberry pi" in _device_model().lower(), _device_model(), required=False),
    ]
    for module in ("fastapi", "uvicorn", "numpy"):
        checks.append(_check(f"module_{module}", importlib.util.find_spec(module) is not None, module))
    runtime_modules = ("ai_edge_litert", "tflite_runtime", "tensorflow")
    available_runtimes = [name for name in runtime_modules if importlib.util.find_spec(name) is not None]
    checks.append(_check("tflite_runtime", bool(available_runtimes), available_runtimes))
    for name, path in (
        ("file_backend_entrypoint", root / "backend" / "run_backend.py"),
        ("file_database_schema", root / "database" / "schema.sql"),
        ("file_dashboard_html", WEB_ROOT / "index.html"),
        ("file_admin_portal_html", WEB_PORTAL / "preview.html"),
        ("file_thermal_client_js", WEB_PORTAL / "thermal-client.js"),
        ("file_guest_thermal_html", WEB_GUEST / "index.html"),
        ("file_lcd_display_html", LCD_STATIC / "display.html"),
        ("file_lcd_control_html", LCD_STATIC / "control.html"),
        ("file_lcd_common_css", LCD_STATIC / "common.css"),
        ("file_model_manifest", MODEL_MANIFEST),
    ):
        checks.append(_check(name, path.is_file(), str(path)))
    for name, port, socket_type in (
        ("port_8000_tcp_available", 8000, socket.SOCK_STREAM),
        ("port_9000_tcp_available", 9000, socket.SOCK_STREAM),
        ("port_5005_udp_available", 5005, socket.SOCK_DGRAM),
    ):
        available, detail = _port_available(port, socket_type)
        checks.append(_check(name, available, detail))
    checks.extend(_model_hash_checks(root))
    failed = [check for check in checks if check["required"] and not check["passed"]]
    return {"ok": not failed, "mode": "PI_START_PREFLIGHT", "checks": checks}


def offline_preflight_document(root: Path = ROOT) -> dict[str, object]:
    checks = [
        _check("python_version_reported", True, platform.python_version()),
        _check(
            "python_3_10_for_runtime_import",
            sys.version_info >= (3, 10),
            platform.python_version(),
            required=False,
        ),
    ]
    checks.extend(_structure_checks(root))
    checks.extend(_config_checks(root))
    checks.extend(_listener_contract_checks(root))
    checks.extend(_path_safety_checks(root))
    checks.extend(_model_hash_checks(root))
    checks.extend(_artifact_selection_checks(root))
    checks.extend(_status_contract_checks(root))
    checks.extend(_o4_asset_checks(root))
    checks.extend(_writable_directory_checks(root))
    checks.append(_runtime_import_check())
    checks.append(_check("pi_checks", True, "NOT_RUN", required=False))
    checks.append(_check("sensor_checks", True, "NOT_RUN", required=False))
    failed = [check for check in checks if check["required"] and not check["passed"]]
    return {
        "schema": "safenest.stage7.offline_preflight.v1",
        "ok": not failed,
        "mode": "MAC_OFFLINE_PREFLIGHT",
        "python": platform.python_version(),
        "pi_checks": "NOT_RUN",
        "sensor_checks": "NOT_RUN",
        "mac_pass_does_not_imply_pi_pass": True,
        "checks": checks,
    }


def _structure_checks(root: Path) -> list[dict[str, object]]:
    checks = []
    for relative in REQUIRED_RUNTIME_FILES:
        path = root / relative
        checks.append(_check(f"file_{relative.replace('/', '_')}", path.is_file(), str(path)))
    for relative, path in CANONICAL_EXTERNAL_FILES:
        checks.append(_check(f"file_{relative.replace('/', '_')}", path.is_file(), str(path)))
    return checks


def _config_checks(root: Path) -> list[dict[str, object]]:
    example = root / ".env.example"
    text = example.read_text(encoding="utf-8") if example.is_file() else ""
    present = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))
    missing = [key for key in EXPECTED_ENV_KEYS if key not in present]
    return [
        _check(
            "env_example_documents_runtime_keys",
            not missing,
            {"missing": missing, "documented": sorted(present)},
        )
    ]


def _listener_contract_checks(root: Path) -> list[dict[str, object]]:
    backend = (root / "backend" / "run_backend.py").read_text(encoding="utf-8")
    runtime = (root / "backend" / "runtime.py").read_text(encoding="utf-8")
    return [
        _check("http_default_port_8000", 'default=8000' in backend or "api-port" in backend and "8000" in backend, "8000"),
        _check("tcp_default_port_9000", "sensor_port: int = 9000" in runtime and "default=9000" in backend, "9000"),
        _check(
            "udp_default_port_5005",
            'SAFENEST_THERMAL_UDP_PORT", "5005"' in backend and "thermal_udp_port: int = 5005" in runtime,
            "5005",
        ),
        _check("tcp_default_bind_all", 'default="0.0.0.0"' in backend and 'sensor_host: str = "0.0.0.0"' in runtime, "0.0.0.0"),
        _check("udp_default_bind_all", 'SAFENEST_THERMAL_UDP_HOST", "0.0.0.0"' in backend, "0.0.0.0"),
    ]


def _path_safety_checks(root: Path) -> list[dict[str, object]]:
    scanned = (
        "backend/run_backend.py",
        "backend/runtime.py",
        "backend/app.py",
        "deployment/run_pi.sh",
    )
    hits = []
    for relative in scanned:
        text = (root / relative).read_text(encoding="utf-8")
        for match in re.finditer(r"/Users/[^\s\"']+", text):
            hits.append(f"{relative}:{match.group(0)}")
    db_default = 'parent.parent / "data" / "safenest.db"' in (root / "backend" / "run_backend.py").read_text(encoding="utf-8")
    venv_default = "${REPOSITORY_ROOT}/.venv" in (root / "deployment" / "run_pi.sh").read_text(encoding="utf-8")
    return [
        _check("no_developer_absolute_runtime_paths", not hits, hits),
        _check("db_path_repository_relative", db_default, "data/safenest.db"),
        _check("venv_path_repository_relative", venv_default, "<repo>/.venv"),
    ]


def _artifact_selection_checks(root: Path) -> list[dict[str, object]]:
    manifest_path = MODEL_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [_check("artifact_manifest_readable", False, f"{type(error).__name__}: {error}")]
    models = manifest.get("models") if isinstance(manifest.get("models"), dict) else {}
    selectors = (
        manifest.get("active_runtime_selectors")
        if isinstance(manifest.get("active_runtime_selectors"), dict)
        else {}
    )
    thermal_key = str(selectors.get("thermal") or "")
    thermal = models.get(thermal_key) if isinstance(models.get(thermal_key), dict) else {}
    legacy_thermal = models.get("thermal") if isinstance(models.get("thermal"), dict) else {}
    mmwave = models.get("mmwave") if isinstance(models.get("mmwave"), dict) else {}
    thermal_path = str(thermal.get("path") or "")
    forbidden = [token for token in FORBIDDEN_THERMAL_SELECTORS if token.lower() in thermal_path.lower()]
    thermal_observed = {
        "selector_key": thermal_key,
        "model_id": thermal.get("model_id"),
        "runtime_role": thermal.get("runtime_role"),
        "active_runtime_selector": thermal.get("active_runtime_selector"),
        "deployment_allowed": thermal.get("deployment_allowed"),
        "artifact_release": thermal.get("artifact_release"),
        "safety_authority": thermal.get("safety_authority"),
        "risk_authority": thermal.get("risk_authority"),
        "proxy_risk_score": thermal.get("proxy_risk_score"),
        "class_2": (thermal.get("class_map") or {}).get("2"),
        "path": thermal_path,
        "forbidden_hits": forbidden,
    }
    checks = [
        _check(
            "thermal_active_selector_is_public_sdt_fp32",
            thermal_key == THERMAL_ACTIVE_KEY
            and thermal.get("model_id") == THERMAL_MODEL_ID
            and thermal.get("runtime_role") == THERMAL_RUNTIME_ROLE
            and thermal.get("active_runtime_selector") is True
            and thermal.get("deployment_allowed") is True
            and thermal.get("artifact_release") == "FINAL_RUNTIME_MODEL"
            and thermal_path.endswith(THERMAL_RELATIVE_PATH)
            and not forbidden,
            thermal_observed,
        ),
        _check(
            "thermal_proxy_risk_is_bounded_without_emergency_authority",
            thermal.get("safety_authority") is False
            and thermal.get("risk_authority") == "LIMITED_POSTURE_PROXY"
            and thermal.get("proxy_risk_score") == 0.4
            and (thermal.get("class_map") or {}).get("2") == "HUMAN_FALL_PROXY",
            thermal_observed,
        ),
        _check(
            "thermal_legacy_int8_is_nonactive",
            legacy_thermal.get("active_runtime_selector") is False
            and legacy_thermal.get("deployment_allowed") is False
            and legacy_thermal.get("runtime_role") == "LEGACY_THERMAL_INT8_NONACTIVE",
            {
                "runtime_role": legacy_thermal.get("runtime_role"),
                "active_runtime_selector": legacy_thermal.get("active_runtime_selector"),
                "deployment_allowed": legacy_thermal.get("deployment_allowed"),
                "path": legacy_thermal.get("path"),
            },
        ),
    ]
    checks.extend(_mmwave_selector_contract_checks(mmwave if isinstance(mmwave, dict) else {}))
    checks.append(
        _check(
            "runtime_loads_manifest_sensor_keys_only",
            'self._assert_deployment_allowed()' in (root / "ai" / "runtime.py").read_text(encoding="utf-8"),
            "ai/runtime.py LazyModel",
        )
    )
    return checks


def _mmwave_selector_contract_checks(mmwave: dict[str, object]) -> list[dict[str, object]]:
    path = str(mmwave.get("path") or "")
    path_is_b23 = path.endswith(B23_RELATIVE_PATH)
    path_is_mn9 = path.endswith(MN9_RELATIVE_PATH)
    path_is_historical_v010 = HISTORICAL_V010_FILENAME in path
    path_is_historical_b = any(marker in path for marker in HISTORICAL_B_PATH_MARKERS)
    observed = {
        "model_id": mmwave.get("model_id"),
        "runtime_role": mmwave.get("runtime_role"),
        "active_runtime_selector": mmwave.get("active_runtime_selector"),
        "deployment_allowed": mmwave.get("deployment_allowed"),
        "HISTORICAL_B_NOT_ACTIVE": mmwave.get("HISTORICAL_B_NOT_ACTIVE"),
        "path": path,
        "DEVICE_VALIDATED": mmwave.get("DEVICE_VALIDATED"),
        "hardware_validation": mmwave.get("hardware_validation"),
        "PI_SMOKE": mmwave.get("PI_SMOKE"),
        "PRESENCE_GATE_REQUIRED": mmwave.get("PRESENCE_GATE_REQUIRED"),
        "representation": mmwave.get("representation"),
    }
    selector_is_b23 = (
        mmwave.get("model_id") == B23_MODEL_ID
        and mmwave.get("runtime_role") == B23_RUNTIME_ROLE
        and mmwave.get("active_runtime_selector") is True
        and mmwave.get("deployment_allowed") is True
        and mmwave.get("HISTORICAL_B_NOT_ACTIVE") is True
        and path_is_b23
        and not path_is_mn9
        and not path_is_historical_v010
        and not path_is_historical_b
    )
    historical_b_inactive = (
        mmwave.get("HISTORICAL_B_NOT_ACTIVE") is True
        and not path_is_historical_b
        and mmwave.get("runtime_role") != "HISTORICAL_B_STAGE"
    )
    device_not_overclaimed = (
        mmwave.get("DEVICE_VALIDATED") is False
        and mmwave.get("hardware_validation") == "NOT_PERFORMED"
        and mmwave.get("PI_SMOKE") == "NOT_PERFORMED"
        and mmwave.get("PRESENCE_GATE_REQUIRED") is True
    )
    return [
        _check("mmwave_primary_selector_is_b23", selector_is_b23, observed),
        _check("mmwave_historical_b_not_active", historical_b_inactive, observed),
        _check("mmwave_device_validation_not_overclaimed", device_not_overclaimed, observed),
    ]


def _status_contract_checks(root: Path) -> list[dict[str, object]]:
    status = (root / "backend" / "runtime_status.py").read_text(encoding="utf-8")
    views = (root / "backend" / "views.py").read_text(encoding="utf-8")
    return [
        _check("runtime_status_module_present", "def runtime_status_document" in status, "runtime_status_document"),
        _check("ready_with_limitations_preserved", "READY_WITH_LIMITATIONS" in status, "READY_WITH_LIMITATIONS"),
        _check(
            "thermal_active_limited_risk_status_preserved",
            "LIMITED_PROXY_NO_EMERGENCY" in status
            and "THERMAL_MODEL_RUNTIME_UNAVAILABLE" in status,
            "ACTIVE_WITH_LIMITED_PROXY_RISK",
        ),
        _check("pir_not_applicable_preserved", 'ai_status": "NOT_APPLICABLE"' in status, "NOT_APPLICABLE"),
        _check("status_api_exposes_runtime_status", '"runtime_status": copy.deepcopy(runtime_status)' in views, "/api/status"),
        _check("lcd_state_reuses_runtime_status", '"runtime_status": copy.deepcopy(status["runtime_status"])' in views, "/api/state"),
    ]


def _o4_asset_checks(root: Path) -> list[dict[str, object]]:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    lcd = (LCD_STATIC / "display.html").read_text(encoding="utf-8")
    return [
        _check("dashboard_runtime_badge", 'id="runtimeBadge"' in html, "runtimeBadge"),
        _check("dashboard_thermal_sensor_ai_split", 'id="thermalSensor"' in html and 'id="thermalAiStatus"' in html, "thermal split"),
        _check("dashboard_consumes_backend_runtime_status", "payload.runtime_status?.status" in js, "backend authority"),
        _check("lcd_consumes_backend_runtime_status", "runtimeDocument(payload).status" in lcd, "LCD /api/state"),
    ]


def _writable_directory_checks(root: Path) -> list[dict[str, object]]:
    checks = []
    for relative in ("data", "data/co2", "data/mmwave", "data/thermal"):
        path = root / relative
        checks.append(_check(f"runtime_dir_{relative.replace('/', '_')}", path.is_dir(), str(path)))
    return checks


def _runtime_import_check() -> dict[str, object]:
    if sys.version_info < (3, 10):
        return _check(
            "runtime_import_construct",
            True,
            f"SKIPPED_PYTHON_LT_3_10:{platform.python_version()}",
            required=False,
        )
    try:
        observed, passed = _construct_offline_runtime()
        return _check("runtime_import_construct", passed, observed)
    except Exception as error:
        return _check(
            "runtime_import_construct",
            False,
            f"{type(error).__name__}: {error}",
        )


def _construct_offline_runtime() -> tuple[dict[str, object], bool]:
    from backend.runtime import SafeNestRuntime
    from backend.store import RuntimeStore
    from backend.views import status_document

    store = RuntimeStore()
    runtime = SafeNestRuntime(store=store, sensor_port=0, thermal_udp_port=0)
    publication = runtime.evaluate_once()
    document = status_document(publication)
    observed = {
        "runtime_status": document.get("runtime_status", {}).get("status"),
        "thermal_ai": document.get("thermal", {}).get("runtime_status", {}).get("ai_status"),
    }
    passed = observed["thermal_ai"] == "BLOCKED" and observed["runtime_status"] in {
        "NOT_READY",
        "DEGRADED",
        "READY_WITH_LIMITATIONS",
    }
    return observed, passed


def _check(name: str, passed: bool, observed: object, *, required: bool = True) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "required": required, "observed": observed}


def _device_model() -> str:
    path = Path("/proc/device-tree/model")
    try:
        return path.read_text(encoding="utf-8").rstrip("\x00")
    except OSError:
        return "unavailable"


def _port_available(port: int, socket_type: int = socket.SOCK_STREAM) -> tuple[bool, str]:
    protocol = "udp" if socket_type == socket.SOCK_DGRAM else "tcp"
    try:
        with socket.socket(socket.AF_INET, socket_type) as listener:
            listener.bind(("0.0.0.0", port))
        return True, f"{protocol}:{port} available"
    except OSError as error:
        return False, f"{protocol}:{port} {type(error).__name__}: {error}"


def _model_hash_checks(root: Path = ROOT) -> list[dict[str, object]]:
    model_root = ONDEVICE_AI_ROOT
    manifest_path = model_root / "models" / "model_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [_check("model_manifest_readable", False, f"{type(error).__name__}: {error}")]
    checks = []
    for sensor_id, model in manifest.get("models", {}).items():
        path = model_root / str(model.get("path", ""))
        expected = str(model.get("sha256", ""))
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            checks.append(_check(f"model_{sensor_id}_sha256", False, f"{type(error).__name__}: {error}"))
            continue
        checks.append(_check(
            f"model_{sensor_id}_sha256",
            bool(expected) and actual == expected,
            {"expected": expected, "actual": actual},
        ))
    return checks


if __name__ == "__main__":
    raise SystemExit(main())
