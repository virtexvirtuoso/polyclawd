"""Tests for resilient fetch wrapper."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Direct imports to avoid aiofiles dependency
_spec_sh = spec_from_file_location("source_health", PROJECT_ROOT / "api" / "services" / "source_health.py")
source_health = module_from_spec(_spec_sh)
_spec_sh.loader.exec_module(source_health)

# Register in sys.modules so resilient_fetch can import it
sys.modules["api.services.source_health"] = source_health

_spec_rf = spec_from_file_location("resilient_fetch", PROJECT_ROOT / "api" / "services" / "resilient_fetch.py")
resilient_fetch = module_from_spec(_spec_rf)
_spec_rf.loader.exec_module(resilient_fetch)


class TestResilientFetch(unittest.TestCase):
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

    def test_success_first_try(self):
        result = resilient_fetch.resilient_call("kalshi", lambda: {"ok": True}, retries=2)
        self.assertEqual(result, {"ok": True})

    def test_retry_on_failure(self):
        calls = {"count": 0}
        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("fail")
            return "success"
        result = resilient_fetch.resilient_call("kalshi", flaky, retries=2, backoff_base=0.01)
        self.assertEqual(result, "success")
        self.assertEqual(calls["count"], 3)

    def test_returns_default_on_total_failure(self):
        def always_fail():
            raise RuntimeError("fail")
        result = resilient_fetch.resilient_call("kalshi", always_fail, retries=1, backoff_base=0.01, default=[])
        self.assertEqual(result, [])

    def test_circuit_breaker_skips(self):
        from datetime import datetime, timedelta, timezone
        source_health.record_success("test_src", 10)
        future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        source_health.set_circuit_open("test_src", future)
        calls = {"count": 0}
        def should_not_call():
            calls["count"] += 1
            return "data"
        result = resilient_fetch.resilient_call("test_src", should_not_call, retries=2, default="skipped")
        self.assertEqual(result, "skipped")
        self.assertEqual(calls["count"], 0)

    def test_decorator(self):
        @resilient_fetch.resilient("kalshi", retries=0)
        def fetch_data():
            return [1, 2, 3]
        result = fetch_data()
        self.assertEqual(result, [1, 2, 3])

    def test_health_updated_on_success(self):
        resilient_fetch.resilient_call("kalshi", lambda: "ok", retries=0)
        h = source_health.get_source_health("kalshi")
        self.assertIsNotNone(h)
        self.assertEqual(h["total_successes"], 1)

    def test_health_updated_on_failure(self):
        def always_fail():
            raise RuntimeError("x")
        resilient_fetch.resilient_call("kalshi", always_fail, retries=0, backoff_base=0.01)
        h = source_health.get_source_health("kalshi")
        self.assertEqual(h["consecutive_failures"], 1)

    def test_circuit_trips_after_threshold(self):
        def always_fail():
            raise RuntimeError("x")
        for _ in range(3):
            resilient_fetch.resilient_call("trippable", always_fail, retries=1, backoff_base=0.01)
        self.assertTrue(source_health.is_circuit_open("trippable"))


if __name__ == "__main__":
    unittest.main()
