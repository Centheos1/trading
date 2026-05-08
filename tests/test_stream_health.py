"""
Phase 10 — Track A: FeedStreamHealth state-machine unit tests.

Pure-Python coverage for `data_feed/stream_health.py`. No asyncio, no network,
no C++ engine.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_feed import stream_health as sh
from data_feed.stream_health import (
    FEED_MAX_CONSECUTIVE_FAILURES,
    FeedState,
    FeedStreamHealth,
)


class TestFeedStreamHealthDefaults(unittest.TestCase):

    def test_default_state_disconnected(self):
        h = FeedStreamHealth()
        self.assertEqual(h.state, FeedState.DISCONNECTED)
        self.assertEqual(h.symbol, "")
        self.assertEqual(h.messages_received, 0)
        self.assertEqual(h.reconnect_count, 0)
        self.assertEqual(h.consecutive_failures, 0)
        self.assertEqual(h.last_error, "")
        self.assertEqual(h.last_msg_wall_clock, 0.0)
        self.assertEqual(h.last_msg_exchange_ts, 0)

    def test_seconds_since_last_msg_when_never_seen(self):
        h = FeedStreamHealth()
        self.assertEqual(h.seconds_since_last_msg, 999.0)

    def test_is_healthy_only_when_live(self):
        h = FeedStreamHealth()
        self.assertFalse(h.is_healthy)
        h.state = FeedState.LIVE
        self.assertTrue(h.is_healthy)
        h.state = FeedState.STALE
        self.assertFalse(h.is_healthy)


class TestOnMessage(unittest.TestCase):

    def test_first_message_transitions_to_live(self):
        h = FeedStreamHealth()
        h.consecutive_failures = 3   # simulate prior failures
        h.on_message(exchange_ts=1234)
        self.assertEqual(h.state, FeedState.LIVE)
        self.assertEqual(h.messages_received, 1)
        self.assertEqual(h.last_msg_exchange_ts, 1234)
        self.assertGreater(h.last_msg_wall_clock, 0.0)
        # consecutive_failures should reset on first transition to LIVE
        self.assertEqual(h.consecutive_failures, 0)

    def test_repeated_messages_preserve_live(self):
        h = FeedStreamHealth()
        h.on_message(exchange_ts=1)
        h.on_message(exchange_ts=2)
        h.on_message(exchange_ts=3)
        self.assertEqual(h.state, FeedState.LIVE)
        self.assertEqual(h.messages_received, 3)
        self.assertEqual(h.last_msg_exchange_ts, 3)

    def test_on_message_without_exchange_ts_keeps_prior(self):
        h = FeedStreamHealth()
        h.on_message(exchange_ts=99)
        h.on_message(exchange_ts=0)   # zero must NOT overwrite prior ts
        self.assertEqual(h.last_msg_exchange_ts, 99)
        self.assertEqual(h.messages_received, 2)


class TestOnDisconnect(unittest.TestCase):

    def test_disconnect_with_error_records_it(self):
        h = FeedStreamHealth()
        h.state = FeedState.LIVE
        h.on_disconnect("eof")
        self.assertEqual(h.state, FeedState.DISCONNECTED)
        self.assertEqual(h.last_error, "eof")

    def test_disconnect_without_error_keeps_prior_error(self):
        h = FeedStreamHealth()
        h.last_error = "prev"
        h.on_disconnect("")
        self.assertEqual(h.state, FeedState.DISCONNECTED)
        self.assertEqual(h.last_error, "prev")


class TestReconnect(unittest.TestCase):

    def test_reconnect_start_increments_count_and_state(self):
        h = FeedStreamHealth()
        self.assertEqual(h.reconnect_count, 0)
        h.on_reconnect_start()
        self.assertEqual(h.state, FeedState.RECONNECTING)
        self.assertEqual(h.reconnect_count, 1)
        self.assertGreater(h.last_reconnect_wall_clock, 0.0)

    def test_reconnect_fail_below_threshold_returns_disconnected(self):
        h = FeedStreamHealth()
        h.on_reconnect_fail("nope")
        self.assertEqual(h.state, FeedState.DISCONNECTED)
        self.assertEqual(h.consecutive_failures, 1)
        self.assertEqual(h.last_error, "nope")

    def test_reconnect_fail_at_threshold_promotes_to_failed(self):
        h = FeedStreamHealth()
        for _ in range(FEED_MAX_CONSECUTIVE_FAILURES):
            h.on_reconnect_fail("x")
        self.assertEqual(h.state, FeedState.FAILED)
        self.assertEqual(h.consecutive_failures, FEED_MAX_CONSECUTIVE_FAILURES)

    def test_failed_then_message_recovers_to_live(self):
        h = FeedStreamHealth()
        for _ in range(FEED_MAX_CONSECUTIVE_FAILURES):
            h.on_reconnect_fail("x")
        self.assertEqual(h.state, FeedState.FAILED)
        h.on_message(exchange_ts=42)
        self.assertEqual(h.state, FeedState.LIVE)
        self.assertEqual(h.consecutive_failures, 0)


class TestOnStale(unittest.TestCase):

    def test_stale_only_from_live(self):
        h = FeedStreamHealth()
        # DISCONNECTED -> stale is no-op
        h.on_stale()
        self.assertEqual(h.state, FeedState.DISCONNECTED)
        # RECONNECTING -> stale is no-op
        h.state = FeedState.RECONNECTING
        h.on_stale()
        self.assertEqual(h.state, FeedState.RECONNECTING)
        # LIVE -> STALE
        h.state = FeedState.LIVE
        h.on_stale()
        self.assertEqual(h.state, FeedState.STALE)


class TestSecondsSinceLastMsg(unittest.TestCase):

    def test_delta_after_message(self):
        h = FeedStreamHealth()
        with patch.object(sh.time, "monotonic", return_value=100.0):
            h.on_message(exchange_ts=1)
        with patch.object(sh.time, "monotonic", return_value=103.5):
            self.assertAlmostEqual(h.seconds_since_last_msg, 3.5, places=3)


class TestShortStatus(unittest.TestCase):

    def test_live_short_status(self):
        h = FeedStreamHealth()
        h.state = FeedState.LIVE
        self.assertEqual(h.short_status, "LIVE")

    def test_stale_short_status_includes_age(self):
        h = FeedStreamHealth()
        with patch.object(sh.time, "monotonic", return_value=100.0):
            h.on_message(exchange_ts=1)
        h.state = FeedState.STALE
        with patch.object(sh.time, "monotonic", return_value=112.0):
            s = h.short_status
        self.assertTrue(s.startswith("STALE"))
        self.assertIn("12", s)

    def test_reconnecting_short_status_includes_count(self):
        h = FeedStreamHealth()
        h.on_reconnect_start()
        h.on_reconnect_start()
        s = h.short_status
        self.assertTrue(s.startswith("RECONNECTING"))
        self.assertIn("#2", s)

    def test_failed_short_status_includes_failure_count(self):
        h = FeedStreamHealth()
        for _ in range(FEED_MAX_CONSECUTIVE_FAILURES):
            h.on_reconnect_fail("x")
        s = h.short_status
        self.assertTrue(s.startswith("FAILED"))
        self.assertIn(str(FEED_MAX_CONSECUTIVE_FAILURES) + "x", s)

    def test_default_short_status_is_state_uppercase(self):
        h = FeedStreamHealth()
        # DISCONNECTED branch hits the fall-through `return s`
        self.assertEqual(h.short_status, "DISCONNECTED")
        h.state = FeedState.CONNECTING
        self.assertEqual(h.short_status, "CONNECTING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
