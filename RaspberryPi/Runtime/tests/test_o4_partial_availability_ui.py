"""O4 LCD/Web presentation of the existing PR #17 runtime-status contract."""

from __future__ import annotations

from pathlib import Path
import unittest

from backend.runtime_status import runtime_status_document
from paths import LCD_STATIC, WEB_ROOT
from backend.views import legacy_state_document, status_document
from tests.test_runtime_status import ai_result, sensor, state


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_JS = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
DASHBOARD_HTML = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
LCD_HTML = (LCD_STATIC / "display.html").read_text(encoding="utf-8")


RUNTIME_STATUS_LABELS = {
    "READY": "Ready",
    "READY_WITH_LIMITATIONS": "Limited",
    "DEGRADED": "Degraded",
    "NOT_READY": "Not ready",
}
SENSOR_STATUS_LABELS = {
    "AVAILABLE": "Available",
    "STALE": "Stale",
    "UNAVAILABLE": "Unavailable",
    "INVALID": "Invalid",
}
AI_STATUS_LABELS = {
    "ACTIVE": "Active",
    "BLOCKED": "Blocked",
    "MODEL_PENDING": "Pending",
    "NOT_APPLICABLE": "N/A",
    "UNAVAILABLE": "Unavailable",
    "NOT_EVALUATED": "Unknown",
}


def label_runtime_status(status: str | None) -> str:
    return RUNTIME_STATUS_LABELS.get(status or "", "Unknown")


def label_sensor_status(status: str | None) -> str:
    return SENSOR_STATUS_LABELS.get(status or "", "Unknown")


def label_ai_status(status: str | None, blocked_reason: str | None = None) -> str:
    if status == "ACTIVE":
        return AI_STATUS_LABELS["ACTIVE"]
    if status == "BLOCKED" and blocked_reason == "THERMAL_MODEL_RUNTIME_UNAVAILABLE":
        return "Validation pending"
    if status in AI_STATUS_LABELS:
        return AI_STATUS_LABELS[status]
    return "Unknown"


def publication(*, co2_ai: bool = True, pir_motion: bool = False, co2_status: str = "LIVE") -> dict:
    return {
        "timestamp": 100.0,
        "publication_revision": 1,
        "state": state(
            co2=sensor(co2_status),
            pir=sensor(motion=pir_motion),
        ),
        "ai": {"ai": {"co2": ai_result(available=co2_ai)}},
        "risk": {"system_health": "HEALTHY", "components": {}},
        "emergency": {},
    }


class O4LabelContractTests(unittest.TestCase):
    def test_unknown_ai_state_is_never_rendered_as_active(self) -> None:
        self.assertEqual(label_ai_status("FUTURE_ENUM"), "Unknown")
        self.assertEqual(label_ai_status(None), "Unknown")
        self.assertEqual(label_ai_status(""), "Unknown")
        self.assertNotEqual(label_ai_status("WEIRD"), "Active")
        self.assertIn('if (status === "ACTIVE") return AI_STATUS_LABELS.ACTIVE;', DASHBOARD_JS)
        self.assertIn('if (status === "ACTIVE") return LCD_AI_SHORT.ACTIVE;', LCD_HTML)
        self.assertIn('return "Unknown";', DASHBOARD_JS)
        self.assertIn("|| \"?\"", LCD_HTML.replace("'", '"'))

    def test_dashboard_and_lcd_do_not_recompute_global_runtime_status(self) -> None:
        self.assertIn("payload.runtime_status?.status", DASHBOARD_JS)
        self.assertIn("runtimeDocument(payload).status", LCD_HTML)
        self.assertNotIn("READY_WITH_LIMITATIONS =", DASHBOARD_JS)
        self.assertNotIn("limitations.length", DASHBOARD_JS)


class O4PresentationScenarioTests(unittest.TestCase):
    def test_thermal_sensor_available_ai_blocked_is_not_a_sensor_error(self) -> None:
        document = status_document(publication())
        thermal = document["thermal"]["runtime_status"]
        self.assertEqual(thermal["sensor_status"], "AVAILABLE")
        self.assertEqual(thermal["data_freshness"], "CURRENT")
        self.assertEqual(thermal["artifact_status"], "PRESENT")
        self.assertEqual(thermal["ai_status"], "BLOCKED")
        self.assertEqual(label_sensor_status(thermal["sensor_status"]), "Available")
        self.assertEqual(
            label_ai_status(thermal["ai_status"], thermal["blocked_reason"]),
            "Validation pending",
        )
        self.assertNotEqual(label_sensor_status(thermal["sensor_status"]), "Unavailable")
        self.assertEqual(document["runtime_status"]["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(label_runtime_status(document["runtime_status"]["status"]), "Limited")
        self.assertIn("thermalSensor", DASHBOARD_HTML)
        self.assertIn("thermalAiStatus", DASHBOARD_HTML)
        self.assertIn("AI_STATUS_LABELS", DASHBOARD_JS)
        self.assertNotIn("Fall detector active", DASHBOARD_JS)
        self.assertNotIn("Sensor ERROR", DASHBOARD_HTML)

    def test_pir_no_motion_is_valid_and_ai_is_not_applicable(self) -> None:
        document = status_document(publication(pir_motion=False))
        pir = document["pir"]["runtime_status"]
        self.assertEqual(pir["sensor_status"], "AVAILABLE")
        self.assertEqual(pir["sensor_value_status"], "NO_MOTION")
        self.assertEqual(pir["ai_status"], "NOT_APPLICABLE")
        self.assertEqual(label_ai_status(pir["ai_status"]), "N/A")
        motion_false = status_document(publication(pir_motion=True))["pir"]["runtime_status"]
        self.assertEqual(motion_false["sensor_value_status"], "MOTION")
        self.assertEqual(label_ai_status(motion_false["ai_status"]), "N/A")
        self.assertIn("pirAi", DASHBOARD_HTML)
        self.assertIn("움직임 없음", DASHBOARD_JS)

    def test_co2_healthy_ai_keeps_sensor_and_ai_separate(self) -> None:
        document = status_document(publication(co2_ai=True))
        co2 = document["co2"]["runtime_status"]
        self.assertEqual(co2["sensor_status"], "AVAILABLE")
        self.assertEqual(co2["data_freshness"], "CURRENT")
        self.assertEqual(co2["ai_status"], "ACTIVE")
        self.assertEqual(co2["output_status"], "AVAILABLE")
        self.assertEqual(label_sensor_status(co2["sensor_status"]), "Available")
        self.assertEqual(label_ai_status(co2["ai_status"]), "Active")
        self.assertIn("co2Ai", DASHBOARD_HTML)
        self.assertIn("co2Sensor", DASHBOARD_HTML)

    def test_ready_with_limitations_is_not_a_fatal_error(self) -> None:
        document = status_document(publication())
        self.assertEqual(document["runtime_status"]["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(document["system_health"], "HEALTHY")
        self.assertEqual(label_runtime_status(document["runtime_status"]["status"]), "Limited")
        self.assertNotEqual(label_runtime_status(document["runtime_status"]["status"]), "Not ready")
        self.assertIn("runtimeBadge", DASHBOARD_HTML)
        self.assertIn("READY_WITH_LIMITATIONS", DASHBOARD_JS)
        self.assertIn("제한 운영", LCD_HTML)
        self.assertIn("LIMITED", LCD_HTML)
        self.assertIn('data-runtime="READY_WITH_LIMITATIONS"', (WEB_ROOT / "styles.css").read_text(encoding="utf-8"))

    def test_stale_is_distinguished_from_disconnected(self) -> None:
        document = status_document(publication(co2_status="STALE"))
        co2 = document["co2"]["runtime_status"]
        self.assertEqual(co2["sensor_status"], "STALE")
        self.assertEqual(co2["sensor_connectivity"], "CONNECTED")
        self.assertEqual(co2["data_freshness"], "STALE")
        self.assertEqual(label_sensor_status(co2["sensor_status"]), "Stale")
        self.assertNotEqual(label_sensor_status(co2["sensor_status"]), "Unavailable")
        self.assertIn('if (currentState.status === "DISCONNECTED") return "연결 끊김";', LCD_HTML)
        self.assertIn('if (currentState.status === "STALE") return "지연";', LCD_HTML)
        self.assertNotIn('STALE" || currentState.status === "DISCONNECTED"', LCD_HTML)


class O4ApiAndLcdSurfaceTests(unittest.TestCase):
    def test_lcd_state_reuses_existing_runtime_status_projection(self) -> None:
        legacy = legacy_state_document(publication(), room="A-01")
        self.assertEqual(legacy["runtime_status"]["status"], "READY_WITH_LIMITATIONS")
        self.assertEqual(legacy["sensors"]["thermal"]["runtime_status"]["ai_status"], "BLOCKED")
        self.assertEqual(legacy["sensors"]["pir"]["runtime_status"]["ai_status"], "NOT_APPLICABLE")
        self.assertIn("/api/state", LCD_HTML)
        self.assertIn("runtime.sensors", LCD_HTML)
        self.assertIn("THM", LCD_HTML)
        self.assertIn("lcdAiShort", LCD_HTML)

    def test_existing_status_endpoints_remain_the_authority(self) -> None:
        document = status_document(publication())
        self.assertIn("runtime_status", document)
        self.assertEqual(
            runtime_status_document(publication()["state"], publication()["ai"]["ai"])["status"],
            document["runtime_status"]["status"],
        )
        self.assertIn("/api/status", DASHBOARD_JS)
        self.assertNotIn("/api/status/v2", DASHBOARD_JS)
        self.assertNotIn("/api/status/v2", LCD_HTML)

    def test_dashboard_keeps_machine_readable_runtime_values(self) -> None:
        self.assertIn('setCapability("runtimeBadge"', DASHBOARD_JS)
        self.assertIn("element.dataset[datasetKey]", DASHBOARD_JS)
        self.assertIn("AI_STATUS_LABELS", DASHBOARD_JS)
        self.assertIn("RUNTIME_STATUS_LABELS", DASHBOARD_JS)


if __name__ == "__main__":
    unittest.main()
