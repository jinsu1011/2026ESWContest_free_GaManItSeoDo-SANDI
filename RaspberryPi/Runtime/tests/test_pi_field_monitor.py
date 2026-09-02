"""Field-monitor Thermal identity rendering. No live HTTP."""

from __future__ import annotations

import unittest

from hil.pi_field_monitor import (
    format_thermal_model_line,
    friendly_thermal_choice,
    render,
    short_label,
    AI_STATE_SHORT,
    RISK_SHORT,
)


BASELINE = "thermal_public_sdt_fp32_active"
CANDIDATE_A = "thermal_tv2_candidate_a_a0_fp32_v1"
CANDIDATE_B = "thermal_tv2_candidate_b_seed42_fp32_test_v1"
BASELINE_PRE = "PUBLIC_SDT_BILINEAR_62X80_FRAME_MINMAX_V1"
ROBUST_PRE = "FRAME_ROBUST_P2_P98_V1"
BASELINE_SHA = "f88d65d76dbb21862e1f3cdff17cefb038a432047ded6ac8d5563bc8bc8c52ff"


def _status(*, selector: str | None, preprocessing: str | None = None, sha: str | None = None) -> dict:
    runtime = {
        "ai_status": "BLOCKED",
        "blocked_reason": "SENSOR_STALE",
    }
    if selector is not None:
        runtime["model_selector"] = selector
    if preprocessing is not None:
        runtime["preprocessing_id"] = preprocessing
    if sha is not None:
        runtime["model_sha256"] = sha
    return {
        "system": "ONLINE",
        "system_health": "HEALTHY",
        "offline": False,
        "thermal": {
            "state": {"status": "STALE", "age_seconds": 12},
            "ai": {"state": "INPUT_UNAVAILABLE"},
            "risk_component": {},
            "runtime_status": runtime,
        },
        "mmwave": {"state": {}, "ai": {}, "risk_component": {}, "runtime_status": {}},
        "co2": {"state": {}, "ai": {}, "risk_component": {}, "runtime_status": {}},
        "pir": {"state": {}, "ai": {}, "risk_component": {}, "runtime_status": {}},
        "risk": {"formula_id": "SAFENEST_RISK_V1", "risk_score": 0, "risk_level": "NORMAL"},
        "runtime_status": {"sensors": {"thermal": runtime}},
    }


def _snapshot(status: dict) -> dict:
    return {
        "t": 1.0,
        "health": {
            "ready": True,
            "receiver": {
                "connections": 1,
                "telemetry_packets": 10,
                "thermal_udp": {"completed_frames": 3},
                "sensor_logging": {"running": True, "enabled": True, "written": {}},
            },
            "database": {"counts": {"snapshots": 1, "events": 0}},
        },
        "status": status,
        "state": {"state": "normal-empty", "room": "lab", "revision": 1},
    }


class ThermalModelIdentityTests(unittest.TestCase):
    def test_friendly_labels(self) -> None:
        self.assertEqual(friendly_thermal_choice(BASELINE), "BASELINE")
        self.assertEqual(friendly_thermal_choice(CANDIDATE_A), "A")
        self.assertEqual(friendly_thermal_choice(CANDIDATE_B), "B")
        self.assertEqual(friendly_thermal_choice("future_selector"), "UNKNOWN")
        self.assertEqual(friendly_thermal_choice(""), "UNAVAILABLE")
        self.assertEqual(friendly_thermal_choice(None), "UNAVAILABLE")

    def test_baseline_line(self) -> None:
        line = format_thermal_model_line(
            _status(selector=BASELINE, preprocessing=BASELINE_PRE, sha=BASELINE_SHA)
        )
        self.assertEqual(
            line,
            f"Thermal: BASELINE | {BASELINE} | {BASELINE_PRE} | {BASELINE_SHA[:12]}",
        )

    def test_candidate_a_line(self) -> None:
        line = format_thermal_model_line(
            _status(selector=CANDIDATE_A, preprocessing=ROBUST_PRE)
        )
        self.assertIn("Thermal: A |", line)
        self.assertIn(CANDIDATE_A, line)
        self.assertIn(ROBUST_PRE, line)

    def test_candidate_b_line(self) -> None:
        line = format_thermal_model_line(
            _status(selector=CANDIDATE_B, preprocessing=ROBUST_PRE)
        )
        self.assertIn("Thermal: B |", line)
        self.assertIn(CANDIDATE_B, line)

    def test_unknown_keeps_raw_selector(self) -> None:
        line = format_thermal_model_line(_status(selector="thermal_future_v9"))
        self.assertTrue(line.startswith("Thermal: UNKNOWN | thermal_future_v9"))
        self.assertNotIn("BASELINE", line)

    def test_missing_selector_is_unavailable(self) -> None:
        line = format_thermal_model_line(_status(selector=None))
        self.assertEqual(line, "Thermal: UNAVAILABLE | selector=-")

    def test_empty_selector_is_unavailable(self) -> None:
        self.assertEqual(
            format_thermal_model_line(_status(selector="")),
            "Thermal: UNAVAILABLE | selector=-",
        )

    def test_fallback_when_thermal_block_is_null(self) -> None:
        status = {
            "thermal": None,
            "runtime_status": {
                "sensors": {
                    "thermal": {
                        "model_selector": CANDIDATE_A,
                        "preprocessing_id": ROBUST_PRE,
                    }
                }
            },
        }
        line = format_thermal_model_line(status)
        self.assertIn("Thermal: A |", line)
        self.assertIn(CANDIDATE_A, line)
        self.assertIn(ROBUST_PRE, line)

    def test_render_puts_identity_near_top_without_live_http(self) -> None:
        curr = _snapshot(_status(selector=CANDIDATE_A, preprocessing=ROBUST_PRE))
        prev = _snapshot(_status(selector=CANDIDATE_A, preprocessing=ROBUST_PRE))
        prev["t"] = 0.0
        body = render(curr, prev, 4.0)
        first_lines = body.splitlines()[:3]
        self.assertTrue(first_lines[0].startswith("SafeNest field monitor"))
        self.assertIn(f"Thermal: A | {CANDIDATE_A} | {ROBUST_PRE}", first_lines[1])
        self.assertIn("## Verdict", body)
        self.assertIn("## Link & storage", body)
        self.assertIn("## Sensors / AI / risk component", body)
        self.assertIn("## Risk / LCD (display)", body)

    def test_human_fall_proxy_has_compact_label(self) -> None:
        self.assertEqual(
            short_label("HUMAN_FALL_PROXY", AI_STATE_SHORT, raw=False),
            "FALL_PX",
        )
        self.assertEqual(
            short_label("HUMAN_FALL_PROXY", RISK_SHORT, raw=False),
            "FALL_PX",
        )
        self.assertEqual(
            short_label("HUMAN_FALL", AI_STATE_SHORT, raw=False),
            "FALL",
        )


if __name__ == "__main__":
    unittest.main()
