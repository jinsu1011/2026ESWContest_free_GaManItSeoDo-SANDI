from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import unittest

from backend.views import ROUTE_CONTRACTS


from paths import RUNTIME_ROOT, WEB_GUEST, WEB_PORTAL, WEB_ROOT

DASHBOARD = WEB_ROOT


class DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"] or "")
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"] or "")
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href") or "")


class DashboardStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        cls.css = (DASHBOARD / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (DASHBOARD / "app.js").read_text(encoding="utf-8")

    def test_assets_exist_and_html_references_same_origin_routes(self):
        parser = DashboardParser()
        parser.feed(self.html)
        self.assertEqual(parser.scripts, ["/dashboard/assets/app.js"])
        self.assertEqual(parser.stylesheets, ["/dashboard/assets/styles.css"])
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertIn("GET /dashboard", ROUTE_CONTRACTS)

    def test_required_live_panels_are_present(self):
        for element_id in (
            "riskLevel", "riskScore", "reasonList", "mmwaveCard", "thermalCard",
            "co2Card", "pirCard", "thermalCanvas", "trendCanvas", "eventList",
            "emergencyOverlay", "report119Button", "contactManagerButton",
            "acknowledgeButton", "voiceToggleButton", "simulationModal",
            "runtimeBadge", "thermalSensor", "thermalAiStatus", "co2Ai", "pirAi",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_websocket_has_status_polling_and_auxiliary_api_fallbacks(self):
        self.assertIn("new WebSocket", self.javascript)
        for endpoint in ("/ws", "/api/status", "/api/events", "/api/history"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.javascript)
        self.assertIn("startPolling()", self.javascript)

    def test_dashboard_never_posts_or_injects_api_html(self):
        self.assertNotIn("innerHTML", self.javascript)
        self.assertIn('method: "POST"', self.javascript)
        self.assertIn("textContent", self.javascript)
        for endpoint in (
            "/api/emergency/119/simulation/start",
            "/api/emergency/119/simulation/complete",
            "/api/emergency/contact",
            "/api/emergency/acknowledge",
            "/api/emergency/voice",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.javascript)
        self.assertIn("simulationRunning", self.javascript)
        self.assertIn("transitionId", self.javascript)

    def test_thermal_values_are_explicitly_raw_and_uncalibrated(self):
        self.assertIn("온도 보정", self.html)
        self.assertIn("미적용", self.html)
        self.assertIn("NORMALIZED PREVIEW", self.javascript)
        self.assertNotIn("°C", self.javascript)

    def test_no_legacy_demo_measurements_are_embedded(self):
        combined = self.html + self.javascript
        for demo_value in ("36.7", "39.1", "620 ppm"):
            with self.subTest(demo_value=demo_value):
                self.assertNotIn(demo_value, combined)

    def test_layout_has_small_screen_and_reduced_motion_support(self):
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_portal_and_guest_thermal_assets_exist_in_canonical_web_root(self):
        portal = (WEB_PORTAL / "preview.html").read_text(encoding="utf-8")
        thermal_client = (WEB_PORTAL / "thermal-client.js").read_text(encoding="utf-8")
        guest = (WEB_GUEST / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="thermalCanvas"', portal)
        self.assertIn('id="thermalCanvas"', guest)
        self.assertIn("/api/thermal/", thermal_client)
        self.assertIn("const WIDTH = 80, HEIGHT = 62", thermal_client)

    def test_backend_serves_assets_from_canonical_web_root(self):
        backend = (RUNTIME_ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn("WEB_ROOT", backend)
        self.assertIn("WEB_PORTAL", backend)
        self.assertIn("WEB_GUEST", backend)
        self.assertNotIn('repository_root / "web"', backend)


class ProductionDashboardStaticTests(unittest.TestCase):
    def test_final_assets_are_same_origin_and_exclude_demo_controls(self):
        html = (DASHBOARD / "index_final.html").read_text(encoding="utf-8")
        javascript = (DASHBOARD / "app_final.js").read_text(encoding="utf-8")
        parser = DashboardParser()
        parser.feed(html)

        self.assertEqual(parser.scripts, ["/dashboard/assets/app_final.js"])
        self.assertEqual(parser.stylesheets, ["/dashboard/assets/styles_final.css"])
        self.assertNotIn("COMPETITION DEMO ONLY", html)
        self.assertNotIn("report119Button", html)
        self.assertNotIn("/api/emergency/119/simulation", javascript)
        self.assertNotIn("simulationRunning", javascript)
        for endpoint in ("/ws", "/api/status", "/api/events", "/api/history"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, javascript)


if __name__ == "__main__":
    unittest.main()
