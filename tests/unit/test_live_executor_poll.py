"""Unit tests for _poll_until_settled in live_executor.

Validates the cancel-window race fix: the CLOB can transiently return
size_matched=0 even when an order is MATCHED. _poll_until_settled must
retry until it sees a non-zero size_matched or a terminal state.

These tests monkeypatch _safe_get_order so no network calls are made.
"""

import types
import unittest
from unittest.mock import patch, call

import execution.live_executor as executor


def _make_status(status: str, size_matched: float = 0.0) -> dict:
    return {"status": status, "size_matched": str(size_matched), "size": "125"}


class TestPollUntilSettled(unittest.TestCase):

    def test_fill_confirmed_on_first_call(self):
        """size_matched > 0 on the very first call — no retries, no sleep."""
        responses = [_make_status("MATCHED", 125.0)]
        with patch.object(executor, "_safe_get_order", side_effect=responses) as mock_get, \
             patch("time.sleep") as mock_sleep:
            result = executor._poll_until_settled("oid-1")
        self.assertEqual(executor._matched_shares_of(result), 125.0)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    def test_stale_then_fill_confirmed_on_retry(self):
        """MATCHED with size_matched=0 twice, then 125 on third call.
        This is the exact CLE trade scenario: retries must catch the late fill.
        """
        responses = [
            _make_status("MATCHED", 0.0),   # stale call 1
            _make_status("MATCHED", 0.0),   # stale retry 1
            _make_status("MATCHED", 125.0), # retry 2 — fill confirmed
        ]
        with patch.object(executor, "_safe_get_order", side_effect=responses) as mock_get, \
             patch("time.sleep") as mock_sleep:
            result = executor._poll_until_settled("oid-2", label="test")
        self.assertEqual(executor._matched_shares_of(result), 125.0)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # slept before retry 1 and 2

    def test_cancelled_exits_immediately(self):
        """CANCELED status → stop retrying, no fill to record."""
        responses = [_make_status("CANCELED", 0.0)]
        with patch.object(executor, "_safe_get_order", side_effect=responses) as mock_get, \
             patch("time.sleep") as mock_sleep:
            result = executor._poll_until_settled("oid-3")
        self.assertEqual(executor._matched_shares_of(result), 0.0)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    def test_exhausts_retries_still_returns_last_status(self):
        """If all retries are exhausted without a fill, return last status (not raise)."""
        n = executor._FILL_CONFIRM_RETRIES
        responses = [_make_status("MATCHED", 0.0)] * (n + 1)  # initial + n retries
        with patch.object(executor, "_safe_get_order", side_effect=responses) as mock_get, \
             patch("time.sleep"):
            result = executor._poll_until_settled("oid-4")
        self.assertIsNotNone(result)
        self.assertEqual(executor._matched_shares_of(result), 0.0)
        # initial call + _FILL_CONFIRM_RETRIES retries
        self.assertEqual(mock_get.call_count, n + 1)

    def test_none_response_exits_early(self):
        """_safe_get_order returns None (network error) — stop retrying."""
        with patch.object(executor, "_safe_get_order", return_value=None) as mock_get, \
             patch("time.sleep") as mock_sleep:
            result = executor._poll_until_settled("oid-5")
        self.assertIsNone(result)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    def test_expired_exits_immediately(self):
        """EXPIRED is also a terminal non-fill state."""
        responses = [_make_status("EXPIRED", 0.0)]
        with patch.object(executor, "_safe_get_order", side_effect=responses) as mock_get, \
             patch("time.sleep") as mock_sleep:
            result = executor._poll_until_settled("oid-6")
        mock_sleep.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    def test_live_with_zero_matched_retries(self):
        """LIVE status with size_matched=0 → retries (order still resting)."""
        responses = [
            _make_status("LIVE", 0.0),
            _make_status("LIVE", 0.0),
            _make_status("MATCHED", 125.0),
        ]
        with patch.object(executor, "_safe_get_order", side_effect=responses), \
             patch("time.sleep"):
            result = executor._poll_until_settled("oid-7")
        self.assertEqual(executor._matched_shares_of(result), 125.0)

    def test_sleep_duration(self):
        """Sleeps for exactly _FILL_CONFIRM_SLEEP seconds between retries."""
        responses = [
            _make_status("MATCHED", 0.0),
            _make_status("MATCHED", 125.0),
        ]
        with patch.object(executor, "_safe_get_order", side_effect=responses), \
             patch("time.sleep") as mock_sleep:
            executor._poll_until_settled("oid-8")
        mock_sleep.assert_called_once_with(executor._FILL_CONFIRM_SLEEP)


class TestMatchedSharesOf(unittest.TestCase):

    def test_snake_case(self):
        self.assertEqual(executor._matched_shares_of({"size_matched": "42.5"}), 42.5)

    def test_camel_case(self):
        self.assertEqual(executor._matched_shares_of({"sizeMatched": "10"}), 10.0)

    def test_none_status(self):
        self.assertEqual(executor._matched_shares_of(None), 0.0)

    def test_missing_key(self):
        self.assertEqual(executor._matched_shares_of({"status": "MATCHED"}), 0.0)


if __name__ == "__main__":
    unittest.main()
