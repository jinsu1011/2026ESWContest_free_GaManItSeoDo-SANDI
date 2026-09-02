from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from database.repository import SQLiteRepository
from database.store import PersistentRuntimeStore


def documents(timestamp=100.0, risk_level="NORMAL", risk_score=10.0, emergency=False):
    state = {
        "timestamp": timestamp,
        "revision": int(timestamp),
        "system": "ONLINE",
        "sensors": {
            "mmwave": {"status": "LIVE", "values": {
                "presence_available": True,
                "presence": True,
                "respiration_rate_bpm": 15.0,
                "heart_rate_bpm": 70.0,
            }},
            "thermal": {"status": "LIVE", "values": {
                "maximum_raw": 1234,
                "pixel_bytes": "must-not-be-persisted",
            }},
            "co2": {"status": "LIVE", "values": {"ppm": 820.0}},
            "pir": {"status": "LIVE", "values": {"motion": False}},
        },
    }
    ai = {
        "timestamp": timestamp,
        "state_revision": int(timestamp),
        "ai": {
            "thermal": {
                "available": True,
                "state": "HUMAN_NORMAL",
                "metadata": {"probabilities": [0.05, 0.80, 0.15]},
            }
        },
    }
    risk = {
        "timestamp": timestamp,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "system_health": "HEALTHY",
        "is_emergency": emergency,
        "reasons": ["TEST_REASON"],
        "components": {},
    }
    return state, ai, risk


def publication(revision=1, **kwargs):
    state, ai, risk = documents(**kwargs)
    return {
        "timestamp": state["timestamp"],
        "state": state,
        "ai": ai,
        "risk": risk,
        "publication_revision": revision,
    }


def event(sequence=1, timestamp=100.0):
    return {
        "event_id": f"event-{sequence}",
        "sequence": sequence,
        "timestamp": timestamp,
        "event_type": "RISK_LEVEL_CHANGED",
        "details": {"from": "NORMAL", "to": "WARNING"},
    }


class SQLiteRepositoryTests(unittest.TestCase):
    def test_snapshot_fields_and_event_round_trip(self):
        repository = SQLiteRepository(":memory:")
        repository.persist(publication(), [event()])
        history = repository.fetch_history()
        events = repository.fetch_events()

        self.assertEqual(repository.counts(), {"snapshots": 1, "events": 1})
        self.assertTrue(history[0]["mmwave_presence"])
        self.assertEqual(history[0]["respiration_rate_bpm"], 15.0)
        self.assertEqual(history[0]["thermal_max_raw"], 1234)
        self.assertIsNone(history[0]["thermal_max_temp_c"])
        self.assertAlmostEqual(history[0]["thermal_human_probability"], 0.95)
        self.assertEqual(history[0]["co2_ppm"], 820.0)
        self.assertFalse(history[0]["pir_motion"])
        self.assertEqual(history[0]["risk_level"], "NORMAL")
        self.assertEqual(history[0]["event_type"], "SNAPSHOT")
        self.assertNotIn("pixel_bytes", history[0])
        self.assertEqual(events[0]["details"]["to"], "WARNING")
        json.dumps(history, allow_nan=False)
        repository.close()

    def test_schema_has_no_raw_frame_blob_column(self):
        schema = (Path(__file__).resolve().parent.parent / "database" / "schema.sql").read_text(
            encoding="utf-8"
        ).upper()
        self.assertNotIn(" BLOB", schema)
        self.assertNotIn("PIXEL_BYTES", schema)
        self.assertIn("THERMAL_MAX_RAW", schema)
        self.assertIn("THERMAL_MAX_TEMP_C", schema)

    def test_transaction_rolls_back_on_constraint_failure(self):
        repository = SQLiteRepository(":memory:")
        broken = publication()
        broken["risk"]["risk_level"] = "UNSUPPORTED"
        with self.assertRaises(sqlite3.IntegrityError):
            repository.persist(broken, [event()])
        self.assertEqual(repository.counts(), {"snapshots": 0, "events": 0})
        repository.close()

    def test_nan_is_rejected_before_sql(self):
        repository = SQLiteRepository(":memory:")
        broken = publication()
        broken["risk"]["risk_score"] = float("nan")
        with self.assertRaises(ValueError):
            repository.persist(broken, [])
        self.assertEqual(repository.counts()["snapshots"], 0)
        repository.close()

    def test_query_limits_and_close_are_enforced(self):
        repository = SQLiteRepository(":memory:")
        for invalid in (0, 201, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                repository.fetch_history(invalid)
        repository.close()
        repository.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            repository.counts()

    def test_unknown_schema_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "old.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '999')")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "expected=2, found=999"):
                SQLiteRepository(path)


class PersistentStoreTests(unittest.TestCase):
    def test_restart_continues_revision_and_event_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "safenest.db"
            first = PersistentRuntimeStore(path)
            one = first.publish(*documents(timestamp=100.0))
            first.close()

            second = PersistentRuntimeStore(path)
            two = second.publish(*documents(timestamp=101.0, risk_level="WARNING", risk_score=40.0))
            events = second.events(20)
            self.assertEqual(one["publication_revision"], 1)
            self.assertEqual(two["publication_revision"], 2)
            self.assertEqual(second.repository.counts()["snapshots"], 2)
            self.assertGreater(max(item["sequence"] for item in events), 1)
            self.assertIn("RISK_LEVEL_CHANGED", [item["event_type"] for item in events])
            second.close()

    def test_concurrent_publications_are_all_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PersistentRuntimeStore(Path(temp_dir) / "concurrent.db")
            errors = []

            def write(index):
                try:
                    store.publish(*documents(timestamp=float(index + 1)))
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(store.repository.counts()["snapshots"], 20)
            self.assertEqual(len(store.history(200)), 20)
            store.close()

    def test_database_failure_keeps_memory_publication_alive(self):
        class BrokenRepository:
            database_path = "broken.db"

            def last_publication_revision(self):
                return 0

            def last_event_sequence(self):
                return 0

            def persist(self, *_args):
                raise sqlite3.OperationalError("disk unavailable")

            fetch_events = fetch_history = counts = persist

            def close(self):
                pass

        store = PersistentRuntimeStore("ignored.db", repository=BrokenRepository())
        result = store.publish(*documents())
        self.assertEqual(result["risk"]["risk_level"], "NORMAL")
        self.assertIsNotNone(store.latest())
        self.assertFalse(store.diagnostics()["database"]["available"])
        self.assertIn("disk unavailable", store.diagnostics()["database"]["error"])
        self.assertIn("RUNTIME_ERROR", [item["event_type"] for item in store.events(20)])


if __name__ == "__main__":
    unittest.main()
