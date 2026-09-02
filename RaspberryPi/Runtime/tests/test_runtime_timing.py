from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from backend.runtime import SafeNestRuntime
from backend.store import RuntimeStore
from storage.sensor_logger import SensorStorageConfig


SENSORS = ("mmwave", "thermal", "co2", "pir")


class FakeManager:
    def __init__(self) -> None:
        self.level = "WARNING"
        self.revision = 0

    def ingest(self, _packet, _peer, **_times) -> int:
        self.level = "DANGER"
        self.revision += 1
        return self.revision

    def snapshot(self):
        return {
            "timestamp": 100.0 + self.revision,
            "revision": self.revision,
            "system": "ONLINE",
            "sensors": {
                name: {"sensor_id": name, "status": "LIVE", "values": {}}
                for name in SENSORS
            },
        }

    @staticmethod
    def latest_thermal_frame():
        return None


class FakeAI:
    @staticmethod
    def evaluate(state, _frame):
        return {
            "timestamp": state["timestamp"],
            "state_revision": state["revision"],
            "ai": {},
        }


class FakeRiskResult:
    def __init__(self, level: str, timestamp: float) -> None:
        self.level = level
        self.timestamp = timestamp

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "risk_score": 40.0 if self.level == "WARNING" else 100.0,
            "risk_level": self.level,
            "system_health": "HEALTHY",
            "degraded_mode": False,
            "is_emergency": self.level == "DANGER",
            "components": {},
        }


class FakeRiskEngine:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, state, _ai):
        self.calls += 1
        return FakeRiskResult("DANGER" if state["revision"] else "WARNING", state["timestamp"])


class FakeStopEvent:
    def __init__(self) -> None:
        self.waits = []

    def wait(self, interval: float) -> bool:
        self.waits.append(interval)
        return len(self.waits) >= 2


class RuntimeTimingTests(unittest.TestCase):
    def runtime(self, root: Path, **kwargs) -> SafeNestRuntime:
        return SafeNestRuntime(
            sensor_port=0,
            manager=kwargs.get("manager", FakeManager()),
            ai_pipeline=kwargs.get("ai_pipeline", FakeAI()),
            risk_engine=kwargs.get("risk_engine", FakeRiskEngine()),
            store=kwargs.get("store", RuntimeStore()),
            storage_config=SensorStorageConfig(root=root, enabled=False),
            **({"evaluation_interval_seconds": kwargs["evaluation_interval_seconds"]}
               if "evaluation_interval_seconds" in kwargs else {}),
        )

    def test_default_risk_evaluation_interval_is_fifteen_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.runtime(Path(temporary))
            self.assertEqual(runtime.evaluation_interval_seconds, 15.0)

    def test_evaluation_loop_uses_the_configured_fifteen_second_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = FakeRiskEngine()
            runtime = self.runtime(Path(temporary), risk_engine=engine)
            stop = FakeStopEvent()
            runtime._stop_event = stop
            runtime._evaluation_loop()

            self.assertEqual(stop.waits, [15.0, 15.0])
            self.assertEqual(engine.calls, 1)

    def test_sensor_changes_do_not_replace_published_risk_before_next_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = FakeManager()
            store = RuntimeStore()
            runtime = self.runtime(Path(temporary), manager=manager, store=store)
            first = runtime.evaluate_once()
            self.assertEqual(first["risk"]["risk_level"], "WARNING")

            runtime._on_packet(object(), ("192.168.1.20", 40_000))
            held = store.latest()
            self.assertEqual(held["risk"]["risk_level"], "WARNING")
            self.assertEqual(held["state"]["revision"], 0)

            next_cycle = runtime.evaluate_once()
            self.assertEqual(next_cycle["risk"]["risk_level"], "DANGER")
            self.assertEqual(next_cycle["state"]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
