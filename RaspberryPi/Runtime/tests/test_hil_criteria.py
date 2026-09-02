from __future__ import annotations

import copy
import unittest

from hil.criteria import SCENARIOS, evaluate
from hil.preflight import _model_hash_checks


def sample() -> dict:
    return {
        "captured_at": 100.0,
        "status": {
            "system": "ONLINE",
            "ready": True,
            "risk": {
                "risk_level": "NORMAL",
                "risk_score": 0.0,
                "presence_detected": False,
                "presence_source": "THERMAL",
                "is_emergency": False,
                "reasons": [],
            },
            "mmwave": {
                "state": {"status": "LIVE", "sequence": 1, "values": {
                    "respiration_rate_bpm": 15.0,
                    "presence_available": False,
                    "presence": None,
                }},
                "ai": {"available": False, "state": "INPUT_UNAVAILABLE"},
                "risk_component": {"available": True, "score": 0.0},
            },
            "thermal": {
                "state": {"status": "LIVE", "sequence": 1, "values": {
                    "minimum_raw": 1000, "maximum_raw": 2000,
                }},
                "ai": {"available": True, "state": "NO_HUMAN", "metadata": {
                    "probabilities": [0.99, 0.005, 0.005],
                }},
                "risk_component": {"available": True, "score": 0.0},
            },
            "co2": {
                "state": {"status": "LIVE", "sequence": 1, "values": {"ppm": 700.0}},
                "ai": {"available": False},
                "risk_component": {"available": True, "score": 0.1},
            },
            "pir": {
                "state": {"status": "LIVE", "sequence": 1, "values": {"motion": False}},
                "ai": {"available": True},
                "risk_component": {"available": True, "score": 0.0},
            },
        },
        "health": {
            "ok": True,
            "database": {"available": True},
            "receiver": {"connections": 1},
        },
    }


class HILCriteriaTests(unittest.TestCase):
    def test_all_scenario_names_are_stable(self):
        self.assertEqual(len(SCENARIOS), 10)
        self.assertEqual(len(set(SCENARIOS)), 10)

    def test_01_no_person_passes(self):
        self.assertEqual(evaluate(SCENARIOS[0], [sample()])["outcome"], "PASS")

    def test_02_person_normal_passes(self):
        current = sample()
        current["status"]["risk"]["presence_detected"] = True
        current["status"]["thermal"]["ai"].update({
            "state": "HUMAN_NORMAL", "metadata": {"probabilities": [0.01, 0.98, 0.01]},
        })
        self.assertEqual(evaluate(SCENARIOS[1], [current])["outcome"], "PASS")

    def test_03_stationary_person_passes(self):
        current = sample()
        current["status"]["risk"]["presence_detected"] = True
        self.assertEqual(evaluate(SCENARIOS[2], [current])["outcome"], "PASS")

    def test_04_abnormal_breathing_fails_when_risk_remains_normal(self):
        current = sample()
        current["status"]["mmwave"]["state"]["values"]["respiration_rate_bpm"] = 5.0
        current["status"]["risk"]["reasons"] = ["ABNORMAL_RESPIRATION_RPM"]
        result = evaluate(SCENARIOS[3], [current])
        self.assertEqual(result["outcome"], "FAIL")
        self.assertFalse(result["checks"][1]["passed"])

    def test_05_co2_rise_passes(self):
        first = sample()
        last = copy.deepcopy(first)
        last["status"]["co2"]["state"]["values"]["ppm"] = 2600.0
        last["status"]["co2"]["risk_component"]["score"] = 1.0
        last["status"]["risk"]["reasons"] = ["CO2_IMMEDIATE_DANGER"]
        self.assertEqual(evaluate(SCENARIOS[4], [first, last])["outcome"], "PASS")

    def test_06_missing_mmwave_presence_is_inconclusive(self):
        self.assertEqual(evaluate(SCENARIOS[5], [sample()])["outcome"], "INCONCLUSIVE")

    def test_07_nonhuman_passes(self):
        self.assertEqual(evaluate(SCENARIOS[6], [sample()])["outcome"], "PASS")

    def test_08_disconnect_then_live_passes(self):
        live_before = sample()
        disconnected = copy.deepcopy(live_before)
        disconnected["status"]["thermal"]["state"]["status"] = "DISCONNECTED"
        recovered = copy.deepcopy(live_before)
        recovered["health"]["receiver"]["connections"] = 2
        self.assertEqual(
            evaluate(SCENARIOS[7], [live_before, disconnected, recovered])["outcome"],
            "PASS",
        )

    def test_09_sequence_reset_and_reconnect_passes(self):
        before = sample()
        before["status"]["thermal"]["state"]["sequence"] = 100
        after = copy.deepcopy(before)
        after["status"]["thermal"]["state"]["sequence"] = 1
        after["health"]["receiver"]["connections"] = 2
        self.assertEqual(evaluate(SCENARIOS[8], [before, after])["outcome"], "PASS")

    def test_10_ai_failure_with_live_service_passes(self):
        current = sample()
        current["status"]["thermal"]["ai"] = {
            "available": False, "state": "INPUT_UNAVAILABLE", "error": "MODEL_RUNTIME_UNAVAILABLE",
        }
        self.assertEqual(evaluate(SCENARIOS[9], [current])["outcome"], "PASS")

    def test_no_status_samples_are_inconclusive(self):
        self.assertEqual(evaluate(SCENARIOS[0], [{"status_error": "offline"}])["outcome"], "INCONCLUSIVE")

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate("unknown", [sample()])

    def test_preflight_model_hashes_match_manifest(self):
        checks = _model_hash_checks()
        self.assertEqual(len(checks), 6)
        self.assertTrue(all(check["passed"] for check in checks), checks)



if __name__ == "__main__":
    unittest.main()
