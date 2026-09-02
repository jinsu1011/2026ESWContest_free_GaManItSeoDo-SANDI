from __future__ import annotations

from dataclasses import fields
import unittest

from e2e.harness import EndToEndHarness
from gateway.protocol import TelemetryPayload


class Phase10EndToEndTests(unittest.TestCase):
    def test_01_no_person_is_normal(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("NO_HUMAN")
            harness.connect_and_send(sequence=1, motion=False)
            publication = harness.evaluate()
            self.assertEqual(publication["risk"]["risk_level"], "NORMAL")
            self.assertFalse(publication["risk"]["presence_detected"])
            self.assertEqual(publication["risk"]["presence_source"], "THERMAL")
            self.assertTrue(all(
                sensor["status"] == "LIVE"
                for sensor in publication["state"]["sensors"].values()
            ))

    def test_02_person_and_normal_breathing_is_normal(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("HUMAN_NORMAL")
            harness.connect_and_send(sequence=1, respiration=15.0, motion=True)
            publication = harness.evaluate()
            self.assertEqual(publication["risk"]["risk_level"], "NORMAL")
            self.assertTrue(publication["risk"]["presence_detected"])
            self.assertEqual(publication["risk"]["presence_source"], "THERMAL")
            self.assertEqual(publication["ai"]["ai"]["thermal"]["state"], "HUMAN_NORMAL")

    def test_03_person_without_motion_is_not_immediate_emergency(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("HUMAN_NORMAL")
            harness.connect_and_send(sequence=1, respiration=15.0, motion=False)
            publication = harness.evaluate()
            pir = publication["risk"]["components"]["pir"]
            self.assertEqual(pir["state"], "NO_MOTION")
            self.assertEqual(pir["score"], 0.5)
            self.assertFalse(publication["risk"]["is_emergency"])
            self.assertNotEqual(publication["risk"]["risk_level"], "DANGER")
            self.assertNotIn("LONG_NO_MOTION", publication["risk"]["reasons"])

    def test_04_abnormal_breathing_exposes_locked_v4_acceptance_gap(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("NO_HUMAN")
            harness.connect_and_send(sequence=1, respiration=5.0, motion=True)
            publication = harness.evaluate()
            self.assertIn("ABNORMAL_RESPIRATION_RPM", publication["risk"]["reasons"])
            self.assertEqual(publication["risk"]["components"]["mmwave"]["score"], 0.75)
            self.assertAlmostEqual(publication["risk"]["risk_score"], 29.75)
            self.assertEqual(publication["risk"]["risk_level"], "NORMAL")

    def test_05_high_co2_increases_environmental_risk(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("NO_HUMAN")
            harness.connect_and_send(sequence=1, co2=2_600.0, motion=True)
            publication = harness.evaluate()
            self.assertEqual(publication["risk"]["components"]["co2"]["score"], 1.0)
            self.assertIn("HIGH_CO2_DANGER", publication["risk"]["reasons"])
            self.assertEqual(publication["risk"]["risk_level"], "WARNING")

    def test_06_mmwave_false_positive_is_blocked_at_protocol_but_risk_rule_exists(self):
        protocol_fields = {field.name for field in fields(TelemetryPayload)}
        self.assertNotIn("presence", protocol_fields)
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("NO_HUMAN")
            harness.connect_and_send(sequence=1)
            snapshot = harness.manager.snapshot()
            values = snapshot["sensors"]["mmwave"]["values"]
            values["presence_available"] = True
            values["presence"] = True
            ai = harness.pipeline.evaluate(snapshot, harness.manager.latest_thermal_frame())
            risk = harness.runtime.risk_engine.evaluate(snapshot, ai)
            self.assertIn("MMWAVE_THERMAL_MISMATCH", risk.reasons)

    def test_07_nonbiological_thermal_source_is_not_reported_as_human(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.set_state("NO_HUMAN")
            harness.connect_and_send(sequence=1)
            publication = harness.evaluate()
            thermal = publication["ai"]["ai"]["thermal"]
            human_probability = sum(thermal["metadata"]["probabilities"][1:])
            self.assertEqual(thermal["state"], "NO_HUMAN")
            self.assertLess(human_probability, 0.05)
            self.assertFalse(publication["risk"]["presence_detected"])

    def test_08_tcp_disconnect_is_recoverable(self):
        with EndToEndHarness() as harness:
            first = harness.connect_and_send(sequence=1, fragment_size=37)
            harness.evaluate()
            harness.close_client(first)
            harness.wait_for_sensor_status("mmwave", "DISCONNECTED")
            self.assertEqual(harness.manager.snapshot()["system"], "DEGRADED")
            second = harness.connect_and_send(sequence=2, fragment_size=53)
            publication = harness.evaluate()
            self.assertEqual(publication["state"]["system"], "ONLINE")
            self.assertGreaterEqual(harness.runtime.receiver_stats()["connections"], 2)
            harness.close_client(second)

    def test_09_esp32_reboot_sequence_reset_is_accepted_on_new_connection(self):
        with EndToEndHarness() as harness:
            first = harness.connect_and_send(sequence=100)
            harness.close_client(first)
            harness.wait_for_sensor_status("mmwave", "DISCONNECTED")
            second = harness.connect_and_send(sequence=1)
            publication = harness.evaluate()
            self.assertEqual(publication["state"]["sensors"]["thermal"]["sequence"], 1)
            self.assertEqual(harness.runtime.receiver_stats()["protocol_errors"], 0)
            harness.close_client(second)

    def test_10_ai_failure_keeps_rules_database_and_api_alive(self):
        with EndToEndHarness() as harness:
            harness.thermal_model.fail = True
            harness.connect_and_send(sequence=1, respiration=15.0, co2=700.0)
            publication = harness.evaluate()
            self.assertFalse(publication["ai"]["ai"]["thermal"]["available"])
            self.assertEqual(publication["risk"]["components"]["mmwave"]["source"], "rule_fallback")
            self.assertEqual(publication["risk"]["components"]["co2"]["source"], "rule_fallback")
            self.assertEqual(publication["risk"]["risk_level"], "NORMAL")
            self.assertTrue(harness.status()["ready"])
            self.assertGreaterEqual(harness.store.diagnostics()["database"]["counts"]["snapshots"], 2)


if __name__ == "__main__":
    unittest.main()
