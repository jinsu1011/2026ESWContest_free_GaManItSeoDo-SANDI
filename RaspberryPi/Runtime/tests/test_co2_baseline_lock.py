"""Room-air CO2 baseline lock. Loads the module by path so ai/__init__ is not imported."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = RUNTIME_ROOT / "ai" / "co2_canonical_runtime.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("co2_canonical_runtime_isolated", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()
CO2BaselineLock = _MOD.CO2BaselineLock


def event(event_id: int, clock_ms: float, ppm: float, *, boot="boot-a", valid=True):
    return {
        "device_id": "esp32-01",
        "boot_id": boot,
        "values": {
            "measurement_event_valid": valid,
            "measurement_event_id": event_id,
            "measurement_monotonic_ms": clock_ms,
            "latest_measurement_ppm": ppm,
        },
    }


def feed(lock: CO2BaselineLock, samples, *, boot="boot-a", start_id=1):
    for offset, (clock_ms, ppm) in enumerate(samples):
        lock.observe(event(start_id + offset, clock_ms, ppm, boot=boot))
    return lock.latest()


class BaselineLockTests(unittest.TestCase):
    def test_lock_uses_median_of_the_warmup_window(self):
        lock = CO2BaselineLock()
        feed(lock, [(0, 390.0), (60_000, 410.0), (120_000, 400.0)])
        warming = lock.latest()
        self.assertFalse(warming.locked)
        self.assertEqual(warming.status, "CO2_BASELINE_UNLOCKED_WARMUP")
        locked = feed(lock, [(180_000, 400.0)], start_id=4)
        self.assertTrue(locked.locked)
        self.assertEqual(locked.baseline_ppm, 400.0)
        self.assertEqual(locked.delta_plus_ppm, 0.0)
        self.assertFalse(locked.relative_warning)

    def test_relative_warning_is_plus_only_with_hysteresis(self):
        lock = CO2BaselineLock()
        feed(lock, [(0, 400.0), (60_000, 400.0), (120_000, 400.0), (180_000, 400.0)])
        self.assertTrue(lock.latest().locked)
        drop = feed(lock, [(240_000, 200.0)], start_id=5)
        self.assertEqual(drop.delta_plus_ppm, 0.0)
        self.assertFalse(drop.relative_warning)
        rise = feed(lock, [(300_000, 900.0)], start_id=6)
        self.assertAlmostEqual(rise.delta_plus_ppm, 500.0)
        self.assertTrue(rise.relative_warning)
        hold = feed(lock, [(360_000, 780.0)], start_id=7)
        self.assertAlmostEqual(hold.delta_plus_ppm, 380.0)
        self.assertTrue(hold.relative_warning)
        clear = feed(lock, [(420_000, 740.0)], start_id=8)
        self.assertAlmostEqual(clear.delta_plus_ppm, 340.0)
        self.assertFalse(clear.relative_warning)

    def test_gap_unlocks_the_room_baseline(self):
        lock = CO2BaselineLock()
        feed(lock, [(0, 400.0), (60_000, 400.0), (120_000, 400.0), (180_000, 400.0)])
        self.assertTrue(lock.latest().locked)
        after = feed(lock, [(400_000, 900.0)], start_id=20)
        self.assertEqual(after.status, "CO2_BASELINE_UNLOCKED_GAP_RESTART")
        self.assertIsNone(after.baseline_ppm)
        self.assertFalse(after.relative_warning)

    def test_boot_boundary_unlocks_the_room_baseline(self):
        lock = CO2BaselineLock()
        feed(lock, [(0, 400.0), (60_000, 400.0), (120_000, 400.0), (180_000, 400.0)])
        self.assertTrue(lock.latest().locked)
        lock.observe(event(1, 0.0, 500.0, boot="boot-b"))
        self.assertFalse(lock.latest().locked)

    def test_from_risk_config_matches_json(self):
        lock = CO2BaselineLock.from_risk_config()
        self.assertEqual(lock.lock_seconds, 180.0)
        self.assertEqual(lock.delta_enter_ppm, 500.0)
        self.assertEqual(lock.delta_exit_ppm, 350.0)
        self.assertEqual(lock.minimum_samples, 3)
        self.assertEqual(lock.max_internal_gap_seconds, 90.0)


if __name__ == "__main__":
    unittest.main()
