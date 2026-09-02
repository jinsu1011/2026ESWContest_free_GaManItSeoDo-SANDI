from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.store import RuntimeStore
from database.store import PersistentRuntimeStore
from services.buzzer import MockBuzzer
from services.emergency import EmergencyActionError, EmergencyActionService
from services.sms_service import NaverSensSMSProvider, SMSDelivery


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    def wall(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value


class FakeSMSProvider:
    name = "fake_sms"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def is_configured(self) -> bool:
        return True

    def send(self, *, to: str, message: str) -> SMSDelivery:
        self.calls.append({"to": to, "message": message})
        return SMSDelivery(self.name, f"request-{len(self.calls)}", 1_000.0)


def publication(timestamp: float, risk_level: str | None, *, system: str = "ONLINE") -> tuple[dict, dict, dict]:
    state = {
        "timestamp": timestamp,
        "revision": int(timestamp),
        "system": system,
        "sensors": {
            "mmwave": {
                "status": "LIVE",
                "values": {"respiration_rate_bpm": 7.0, "presence_available": True, "presence": True},
            },
            "thermal": {"status": "LIVE", "values": {"maximum_raw": 1200}},
            "co2": {"status": "LIVE", "values": {"ppm": 2_400.0}},
            "pir": {"status": "LIVE", "values": {"motion": False}},
        },
    }
    ai = {"timestamp": timestamp, "state_revision": int(timestamp), "ai": {}}
    risk = {
        "timestamp": timestamp,
        "risk_score": 92.0 if risk_level == "DANGER" else 30.0 if risk_level == "WARNING" else None,
        "risk_level": risk_level,
        "system_health": "FAILED" if system != "ONLINE" else "HEALTHY",
        "degraded_mode": system != "ONLINE",
        "is_emergency": risk_level == "DANGER",
        "reasons": ["RESPIRATION_LOW", "CO2_CRITICAL", "NO_MOTION"],
        "components": {"pir": {"metadata": {"no_motion_seconds": 45.0}}},
    }
    return state, ai, risk


class EmergencyLatchTests(unittest.TestCase):
    def test_danger_is_latched_until_live_recovery_and_offline_does_not_clear_it(self):
        buzzer = MockBuzzer("mock")
        store = RuntimeStore(buzzer=buzzer)
        store.publish(*publication(100.0, "NORMAL"))
        danger = store.publish(*publication(101.0, "DANGER"))
        repeated = store.publish(*publication(102.0, "DANGER"))
        offline = store.publish(*publication(103.0, None, system="OFFLINE"))

        self.assertTrue(danger["emergency"]["active"])
        self.assertTrue(danger["emergency"]["buzzer_active"])
        self.assertEqual(danger["emergency"]["transition_id"], repeated["emergency"]["transition_id"])
        self.assertTrue(offline["emergency"]["active"])
        self.assertTrue(offline["emergency"]["latched_while_offline"])
        self.assertEqual(
            [event["event_type"] for event in store.events(200)].count("DANGER_ENTERED"),
            1,
        )

        acknowledged = store.acknowledge_alarm()
        self.assertTrue(acknowledged["acknowledged"])
        self.assertFalse(acknowledged["buzzer_active"])
        self.assertTrue(store.latest()["emergency"]["active"])
        recovered = store.publish(*publication(104.0, "WARNING"))
        self.assertFalse(recovered["emergency"]["active"])


class EmergencyActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = RuntimeStore(buzzer=MockBuzzer("mock"))
        self.store.publish(*publication(100.0, "NORMAL"))
        self.store.publish(*publication(101.0, "DANGER"))

    def test_119_is_explicit_simulation_and_is_single_flight(self):
        service = EmergencyActionService(self.store)
        started = service.start_119_simulation()
        self.assertIn("실제 119", started["disclaimer"])
        self.assertEqual(started["status"], "COUNTDOWN")
        with self.assertRaisesRegex(EmergencyActionError, "already in progress"):
            service.start_119_simulation()
        completed = service.complete_119_simulation(started["simulation_id"])
        self.assertEqual(completed["status"], "COMPLETED")
        events = [event["event_type"] for event in self.store.events(100)]
        self.assertIn("EMERGENCY_SIMULATION_STARTED", events)
        self.assertIn("EMERGENCY_SIMULATION_COMPLETED", events)

    def test_manager_sms_is_backend_only_idempotent_and_rate_limited(self):
        provider = FakeSMSProvider()
        service = EmergencyActionService(
            self.store,
            sms_provider=provider,
            manager_phone="010-1234-5678",
            manager_name="현장 담당자",
            room="A-01",
            sms_cooldown_seconds=60.0,
            clock=self.clock.wall,
            monotonic=self.clock.monotonic,
        )

        first = service.send_manager_sms(idempotency_key="attempt-1")
        duplicate = service.send_manager_sms(idempotency_key="attempt-1")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(first["manager"]["phone_masked"], "010-****-5678")
        self.assertTrue(duplicate["deduplicated"])
        self.assertNotIn("010-1234-5678", provider.calls[0]["message"])

        with self.assertRaisesRegex(EmergencyActionError, "cooling down") as context:
            service.send_manager_sms(idempotency_key="attempt-2")
        self.assertEqual(context.exception.code, "SMS_COOLDOWN")
        self.clock.advance(61.0)
        service.send_manager_sms(idempotency_key="attempt-2")
        self.assertEqual(len(provider.calls), 2)

    def test_unconfigured_sms_returns_safe_structured_error(self):
        service = EmergencyActionService(
            self.store,
            sms_provider=NaverSensSMSProvider(),
            manager_phone="010-1234-5678",
        )
        with self.assertRaises(EmergencyActionError) as context:
            service.send_manager_sms(idempotency_key="attempt-1")
        self.assertEqual(context.exception.code, "SMS_NOT_CONFIGURED")
        self.assertNotIn("010-1234-5678", str(context.exception))


class SMSProviderTests(unittest.TestCase):
    def test_naver_sens_request_is_signed_server_side_without_external_call(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"requestId":"request-123"}'

        provider = NaverSensSMSProvider(
            access_key="access-test",
            secret_key="secret-test",
            service_id="service-test",
            from_number="0212345678",
            timeout_seconds=3.0,
        )
        with patch("services.sms_service.urlopen", return_value=FakeResponse()) as mocked:
            delivery = provider.send(to="01012345678", message="SafeNest test")

        request = mocked.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        timestamp = headers["x-ncp-apigw-timestamp"]
        uri = "/sms/v2/services/service-test/messages"
        expected_signature = base64.b64encode(
            hmac.new(
                b"secret-test",
                f"POST {uri}\n{timestamp}\naccess-test".encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        body = json.loads(request.data.decode("utf-8"))

        self.assertEqual(delivery.request_id, "request-123")
        self.assertEqual(request.method, "POST")
        self.assertEqual(headers["x-ncp-iam-access-key"], "access-test")
        self.assertEqual(headers["x-ncp-apigw-signature-v2"], expected_signature)
        self.assertEqual(body["from"], "0212345678")
        self.assertEqual(body["messages"], [{"to": "01012345678"}])
        self.assertEqual(mocked.call_args.kwargs["timeout"], 3.0)


class PersistentEmergencyStateTests(unittest.TestCase):
    def test_emergency_latch_and_acknowledgement_survive_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "safenest.db"
            first = PersistentRuntimeStore(path, buzzer=MockBuzzer("mock"))
            first.publish(*publication(100.0, "NORMAL"))
            danger = first.publish(*publication(101.0, "DANGER"))
            first.acknowledge_alarm()
            history = first.history(1)[0]
            self.assertTrue(history["emergency_active"])
            self.assertTrue(history["alarm_acknowledged"])
            self.assertEqual(history["danger_transition_id"], danger["emergency"]["transition_id"])
            self.assertIn("ALARM_ACKNOWLEDGED", [event["event_type"] for event in first.events(100)])
            first.close()

            second = PersistentRuntimeStore(path, buzzer=MockBuzzer("mock"))
            restored = second.emergency_snapshot()
            self.assertTrue(restored["active"])
            self.assertTrue(restored["acknowledged"])
            self.assertEqual(restored["transition_id"], danger["emergency"]["transition_id"])
            second.close()


if __name__ == "__main__":
    unittest.main()
