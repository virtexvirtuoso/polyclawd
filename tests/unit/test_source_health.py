"""Tests for source health registry."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from importlib.util import spec_from_file_location, module_from_spec

# Direct import to avoid api.services.__init__ pulling in aiofiles
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = spec_from_file_location("source_health", PROJECT_ROOT / "api" / "services" / "source_health.py")
source_health = module_from_spec(_spec)
_spec.loader.exec_module(source_health)


class TestSourceHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self._orig = source_health.DB_PATH
        source_health.DB_PATH = self.db_path

    def tearDown(self):
        source_health.DB_PATH = self._orig
        if self.db_path.exists():
            os.unlink(self.db_path)

    def test_record_success(self):
        source_health.record_success("kalshi", 150.0)
        h = source_health.get_source_health("kalshi")
        self.assertIsNotNone(h)
        self.assertEqual(h["total_successes"], 1)
        self.assertEqual(h["consecutive_failures"], 0)
        self.assertAlmostEqual(h["last_latency_ms"], 150.0, places=0)

    def test_record_failure(self):
        source_health.record_failure("kalshi", "timeout")
        source_health.record_failure("kalshi", "timeout")
        h = source_health.get_source_health("kalshi")
        self.assertEqual(h["consecutive_failures"], 2)
        self.assertEqual(h["total_failures"], 2)

    def test_success_resets_failures(self):
        source_health.record_failure("kalshi", "err")
        source_health.record_failure("kalshi", "err")
        source_health.record_success("kalshi", 100.0)
        h = source_health.get_source_health("kalshi")
        self.assertEqual(h["consecutive_failures"], 0)

    def test_circuit_breaker(self):
        from datetime import datetime, timedelta, timezone
        source_health.record_success("test_src", 10)
        future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        source_health.set_circuit_open("test_src", future)
        self.assertTrue(source_health.is_circuit_open("test_src"))

    def test_circuit_not_open_after_expiry(self):
        from datetime import datetime, timedelta, timezone
        source_health.record_success("test_src", 10)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        source_health.set_circuit_open("test_src", past)
        self.assertFalse(source_health.is_circuit_open("test_src"))

    def test_get_all_source_health(self):
        source_health.record_success("kalshi", 100)
        all_h = source_health.get_all_source_health()
        self.assertTrue(len(all_h) >= 7)
        kalshi = next(s for s in all_h if s["source"] == "kalshi")
        self.assertEqual(kalshi["status"], "healthy")

    def test_get_last_success_timestamp(self):
        source_health.record_success("manifold", 200)
        ts = source_health.get_last_success_timestamp("manifold")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time(), delta=5)

    def test_avg_latency_ema(self):
        source_health.record_success("kalshi", 100.0)
        source_health.record_success("kalshi", 200.0)
        h = source_health.get_source_health("kalshi")
        self.assertAlmostEqual(h["avg_latency_ms"], 120.0, places=0)


if __name__ == "__main__":
    unittest.main()
