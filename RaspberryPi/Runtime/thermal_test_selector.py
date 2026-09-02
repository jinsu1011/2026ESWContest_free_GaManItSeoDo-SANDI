"""Fail-closed Thermal controlled-test selector.

Ordinary SafeNest startup (no test env) always uses
``active_runtime_selectors.thermal``. Test switching is opt-in only through
``SAFENEST_THERMAL_TEST_MODE=1`` plus an allowlisted
``SAFENEST_THERMAL_MODEL_SELECTOR``. The active/default selector field is not
repurposed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

TEST_MODE_ENV = "SAFENEST_THERMAL_TEST_MODE"
SELECTOR_ENV = "SAFENEST_THERMAL_MODEL_SELECTOR"
DEFAULT_THERMAL_SELECTOR = "thermal_public_sdt_fp32_active"


class ThermalSelectorError(ValueError):
    """Unknown, incomplete, or disallowed Thermal test selector."""


def thermal_test_mode_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(TEST_MODE_ENV, "")).strip()
    if raw == "":
        return False
    if raw == "1":
        return True
    raise ThermalSelectorError(
        f"THERMAL_TEST_MODE_INVALID: {TEST_MODE_ENV}={raw!r}; only unset or '1' is accepted"
    )


def load_model_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    if not path.is_file():
        raise ThermalSelectorError(f"THERMAL_MANIFEST_MISSING: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ThermalSelectorError(
            f"THERMAL_MANIFEST_UNREADABLE: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ThermalSelectorError("THERMAL_MANIFEST_INVALID: root must be an object")
    return payload


def peek_configured_thermal_selector(environ: Mapping[str, str] | None = None) -> str:
    """Best-effort identity for status/logs. Does not validate allowlist."""

    env = os.environ if environ is None else environ
    if str(env.get(TEST_MODE_ENV, "")).strip() == "1":
        requested = str(env.get(SELECTOR_ENV, "")).strip()
        if requested:
            return requested
    return DEFAULT_THERMAL_SELECTOR


def resolve_thermal_runtime_selector(
    manifest: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the Thermal selector for this process. Fail closed."""

    env = os.environ if environ is None else environ
    models = manifest.get("models")
    if not isinstance(models, dict):
        raise ThermalSelectorError("THERMAL_MANIFEST_INVALID: models missing")

    active_selectors = manifest.get("active_runtime_selectors")
    if not isinstance(active_selectors, dict):
        raise ThermalSelectorError("THERMAL_MANIFEST_INVALID: active_runtime_selectors missing")
    active = str(active_selectors.get("thermal") or "").strip()
    if not active:
        raise ThermalSelectorError("THERMAL_ACTIVE_SELECTOR_MISSING")
    if active != DEFAULT_THERMAL_SELECTOR:
        raise ThermalSelectorError(
            "THERMAL_ACTIVE_SELECTOR_DRIFT: "
            f"expected={DEFAULT_THERMAL_SELECTOR}, manifest={active}"
        )

    requested = str(env.get(SELECTOR_ENV, "")).strip()
    test_mode = thermal_test_mode_enabled(env)

    if not test_mode:
        if requested:
            raise ThermalSelectorError(
                "THERMAL_TEST_SELECTOR_WITHOUT_TEST_MODE: "
                f"{SELECTOR_ENV}={requested!r} requires {TEST_MODE_ENV}=1"
            )
        return active

    if not requested:
        raise ThermalSelectorError(
            f"THERMAL_TEST_MODE_REQUIRES_SELECTOR: {TEST_MODE_ENV}=1 needs {SELECTOR_ENV}"
        )

    allowlist_root = manifest.get("controlled_test_runtime_selectors")
    if not isinstance(allowlist_root, dict):
        raise ThermalSelectorError("THERMAL_TEST_ALLOWLIST_MISSING")
    allowlist = allowlist_root.get("thermal")
    if not isinstance(allowlist, list):
        raise ThermalSelectorError("THERMAL_TEST_ALLOWLIST_INVALID")
    allowed = {str(item) for item in allowlist}
    if requested not in allowed:
        raise ThermalSelectorError(
            f"THERMAL_TEST_SELECTOR_NOT_ALLOWLISTED: {requested!r}"
        )
    if requested not in models:
        raise ThermalSelectorError(
            f"THERMAL_TEST_SELECTOR_UNKNOWN: {requested!r} is not a manifest key"
        )
    metadata = models[requested]
    if not isinstance(metadata, dict):
        raise ThermalSelectorError(f"THERMAL_TEST_SELECTOR_INVALID_ENTRY: {requested!r}")
    if metadata.get("controlled_test_allowed") is not True:
        raise ThermalSelectorError(
            f"THERMAL_TEST_SELECTOR_NOT_ALLOWED: {requested!r} controlled_test_allowed is not true"
        )
    return requested


def describe_thermal_selection(selector: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    models = manifest.get("models") if isinstance(manifest.get("models"), dict) else {}
    metadata = models.get(selector) if isinstance(models.get(selector), dict) else {}
    return {
        "selector": selector,
        "model_id": metadata.get("model_id"),
        "model_version": metadata.get("version"),
        "sha256": metadata.get("sha256"),
        "preprocessing_id": metadata.get("preprocessing_id"),
        "runtime_role": metadata.get("runtime_role"),
        "controlled_test_mode": thermal_test_mode_enabled(),
    }
