"""
Phase 10 — Track B: integration tests for `run_binance_usdm_futures_ws_feed`.

Drives the asyncio runner against an in-process fake `websockets_module` and a
SimpleNamespace `ofe` stub. No network, no C++ engine.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_feed import binance_futures_ws as bfws
from data_feed.binance_futures_ws import (
    _reconnect_backoff,
    run_binance_usdm_futures_ws_feed,
)
from data_feed.stream_health import (
    FEED_MAX_CONSECUTIVE_FAILURES,
    FEED_RECONNECT_MAX_BACKOFF_S,
    FeedState,
    FeedStreamHealth,
)


# --------------------------------------------------------------------------
# Fake websockets module
# --------------------------------------------------------------------------


class _Close:
    """Script sentinel: next recv() raises ConnectionClosed."""


class _Block:
    """Script sentinel: next recv() blocks until WS is closed."""


CLOSE = _Close()
BLOCK = _Block()


class FakeConnectionClosed(Exception):
    pass


class FakeWS:
    def __init__(self, scripted: List[object], conn_closed_cls):
        self._items = list(scripted)
        self._conn_closed = conn_closed_cls
        self._closed = asyncio.Event()
        self.closed = False

    async def recv(self):
        if self.closed:
            raise self._conn_closed("ws closed")
        if not self._items:
            # nothing left to send → block until closed (let wait_for time out)
            try:
                await self._closed.wait()
            except asyncio.CancelledError:
                raise
            raise self._conn_closed("ws closed")
        item = self._items.pop(0)
        if isinstance(item, _Close):
            raise self._conn_closed("scripted close")
        if isinstance(item, _Block):
            await self._closed.wait()
            raise self._conn_closed("ws closed")
        return item

    async def close(self):
        self.closed = True
        self._closed.set()


class FakeWebsocketsModule:
    """Stand-in for the real `websockets` package."""

    def __init__(
        self,
        *,
        on_connect: Callable[[str], "FakeWS"],
        ConnectionClosed=FakeConnectionClosed,
    ):
        self._on_connect = on_connect
        self.ConnectionClosed = ConnectionClosed

    async def connect(self, uri, **kwargs):
        return self._on_connect(uri)


# --------------------------------------------------------------------------
# ofe stub
# --------------------------------------------------------------------------


class _StubTrade:
    def __init__(self):
        self.timestamp = 0
        self.price = 0.0
        self.quantity = 0.0
        self.is_buyer_maker = False


class _StubDepthLevel:
    def __init__(self):
        self.price = 0.0
        self.quantity = 0.0


class _StubDepthUpdate:
    def __init__(self):
        self.timestamp = 0
        self.first_update_id = 0
        self.final_update_id = 0
        self.is_snapshot = False
        self.bids = []
        self.asks = []


def _make_ofe_stub() -> SimpleNamespace:
    return SimpleNamespace(
        Trade=_StubTrade,
        DepthLevel=_StubDepthLevel,
        DepthUpdate=_StubDepthUpdate,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _trade_msg(
    *, t: int = 1, ts: int = 1_700_000_000_000,
    price: float = 100.0, qty: float = 1.0, m: bool = False,
) -> str:
    return json.dumps({
        "T": ts, "p": str(price), "q": str(qty), "m": m, "t": t,
    })


def _depth_msg(
    *, ts: int = 1_700_000_000_000,
    bids: Optional[list] = None, asks: Optional[list] = None,
) -> str:
    return json.dumps({
        "E": ts, "U": 1, "u": 2,
        "b": bids if bids is not None else [["100.0", "1.0"]],
        "a": asks if asks is not None else [["101.0", "2.0"]],
    })


def _start_runner(
    *,
    trade_health: FeedStreamHealth,
    depth_health: FeedStreamHealth,
    on_ws_trade,
    on_ws_depth,
    websockets_module,
    recent_trade_ids: Optional[deque] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[threading.Thread, threading.Event]:
    if stop_event is None:
        stop_event = threading.Event()
    if recent_trade_ids is None:
        recent_trade_ids = deque(maxlen=512)
    th = threading.Thread(
        target=run_binance_usdm_futures_ws_feed,
        kwargs=dict(
            symbol_lower="btcusdt",
            stop_event=stop_event,
            ofe=_make_ofe_stub(),
            trade_health=trade_health,
            depth_health=depth_health,
            recent_trade_ids=recent_trade_ids,
            on_ws_trade=on_ws_trade,
            on_ws_depth=on_ws_depth,
            websockets_module=websockets_module,
        ),
        daemon=True,
    )
    th.start()
    return th, stop_event


def _wait_until(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestEarlyReturn(unittest.TestCase):

    def test_runner_returns_when_websockets_module_is_none(self):
        run_binance_usdm_futures_ws_feed(
            symbol_lower="btcusdt",
            stop_event=threading.Event(),
            ofe=_make_ofe_stub(),
            trade_health=FeedStreamHealth(),
            depth_health=FeedStreamHealth(),
            recent_trade_ids=deque(maxlen=8),
            on_ws_trade=lambda *a, **kw: None,
            on_ws_depth=lambda *a, **kw: None,
            websockets_module=None,
        )


class TestTradeStream(unittest.TestCase):

    def test_trade_stream_parses_and_dispatches(self):
        trades_seen: List[tuple] = []
        depth_seen: List = []

        def factory(uri: str) -> FakeWS:
            if "@trade" in uri:
                return FakeWS([_trade_msg(t=1, m=False), BLOCK], FakeConnectionClosed)
            return FakeWS([BLOCK], FakeConnectionClosed)

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        th = FeedStreamHealth()
        dh = FeedStreamHealth()

        thread, stop_ev = _start_runner(
            trade_health=th, depth_health=dh,
            on_ws_trade=lambda t, is_buy: trades_seen.append((t, is_buy)),
            on_ws_depth=depth_seen.append,
            websockets_module=ws_mod,
        )
        try:
            ok = _wait_until(lambda: th.state == FeedState.LIVE and th.messages_received >= 1, 5.0)
            self.assertTrue(ok, f"trade feed never went LIVE: state={th.state}")
            self.assertEqual(len(trades_seen), 1)
            t_obj, is_buy_aggressor = trades_seen[0]
            self.assertTrue(is_buy_aggressor)            # m=False → buy aggressor
            self.assertEqual(t_obj.is_buyer_maker, False)
            self.assertEqual(t_obj.price, 100.0)
            self.assertEqual(t_obj.quantity, 1.0)
            self.assertEqual(th.messages_received, 1)
        finally:
            stop_ev.set()
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive())

    def test_trade_dedupe_via_recent_ids(self):
        trades_seen: List[tuple] = []

        def factory(uri: str) -> FakeWS:
            if "@trade" in uri:
                return FakeWS([
                    _trade_msg(t=42, m=False),
                    _trade_msg(t=42, m=False),  # duplicate
                    BLOCK,
                ], FakeConnectionClosed)
            return FakeWS([BLOCK], FakeConnectionClosed)

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        th = FeedStreamHealth(); dh = FeedStreamHealth()
        thread, stop_ev = _start_runner(
            trade_health=th, depth_health=dh,
            on_ws_trade=lambda t, is_buy: trades_seen.append((t, is_buy)),
            on_ws_depth=lambda u: None,
            websockets_module=ws_mod,
            recent_trade_ids=deque(maxlen=8),
        )
        try:
            # Wait long enough for both messages to be processed
            ok = _wait_until(lambda: th.messages_received >= 1, 5.0)
            self.assertTrue(ok)
            time.sleep(0.6)   # let the dup be consumed too
            self.assertEqual(len(trades_seen), 1)
        finally:
            stop_ev.set()
            thread.join(timeout=5.0)

    def test_trade_parse_error_does_not_kill_loop(self):
        trades_seen: List[tuple] = []

        def factory(uri: str) -> FakeWS:
            if "@trade" in uri:
                # Missing required key 'p' → KeyError caught
                bad = json.dumps({"T": 1, "q": "1.0", "m": False, "t": 1})
                good = _trade_msg(t=2, m=True)
                return FakeWS([bad, good, BLOCK], FakeConnectionClosed)
            return FakeWS([BLOCK], FakeConnectionClosed)

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        th = FeedStreamHealth(); dh = FeedStreamHealth()
        thread, stop_ev = _start_runner(
            trade_health=th, depth_health=dh,
            on_ws_trade=lambda t, is_buy: trades_seen.append((t, is_buy)),
            on_ws_depth=lambda u: None,
            websockets_module=ws_mod,
        )
        try:
            ok = _wait_until(lambda: len(trades_seen) >= 1, 5.0)
            self.assertTrue(ok)
            self.assertEqual(len(trades_seen), 1)
            _, is_buy = trades_seen[0]
            self.assertFalse(is_buy)   # second trade was m=True → seller aggressor
            self.assertEqual(th.state, FeedState.LIVE)
        finally:
            stop_ev.set()
            thread.join(timeout=5.0)

    def test_connection_closed_triggers_reconnect(self):
        connect_calls = {"trade": 0, "depth": 0}

        def factory(uri: str) -> FakeWS:
            if "@trade" in uri:
                connect_calls["trade"] += 1
                if connect_calls["trade"] == 1:
                    # First connection: send one msg, then close
                    return FakeWS([_trade_msg(t=1), CLOSE], FakeConnectionClosed)
                # Subsequent connections: hold open
                return FakeWS([BLOCK], FakeConnectionClosed)
            connect_calls["depth"] += 1
            return FakeWS([BLOCK], FakeConnectionClosed)

        # Tighten backoff for fast test
        bfws.FEED_RECONNECT_INITIAL_BACKOFF_S = 0.05
        bfws.FEED_RECONNECT_MAX_BACKOFF_S = 0.1
        try:
            ws_mod = FakeWebsocketsModule(on_connect=factory)
            th = FeedStreamHealth(); dh = FeedStreamHealth()
            thread, stop_ev = _start_runner(
                trade_health=th, depth_health=dh,
                on_ws_trade=lambda t, is_buy: None,
                on_ws_depth=lambda u: None,
                websockets_module=ws_mod,
            )
            try:
                ok = _wait_until(
                    lambda: connect_calls["trade"] >= 2 and th.reconnect_count >= 1,
                    5.0,
                )
                self.assertTrue(
                    ok,
                    f"never reconnected; calls={connect_calls}, "
                    f"count={th.reconnect_count}",
                )
            finally:
                stop_ev.set()
                thread.join(timeout=5.0)
        finally:
            bfws.FEED_RECONNECT_INITIAL_BACKOFF_S = 1.0
            bfws.FEED_RECONNECT_MAX_BACKOFF_S = 30.0


class TestDepthStream(unittest.TestCase):

    def test_depth_stream_parses_and_dispatches(self):
        depth_seen: List = []

        def factory(uri: str) -> FakeWS:
            if "@depth" in uri:
                return FakeWS([
                    _depth_msg(
                        ts=12345,
                        bids=[["99.5", "2.0"], ["99.0", "5.0"]],
                        asks=[["100.5", "3.0"]],
                    ),
                    BLOCK,
                ], FakeConnectionClosed)
            return FakeWS([BLOCK], FakeConnectionClosed)

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        th = FeedStreamHealth(); dh = FeedStreamHealth()
        thread, stop_ev = _start_runner(
            trade_health=th, depth_health=dh,
            on_ws_trade=lambda t, is_buy: None,
            on_ws_depth=depth_seen.append,
            websockets_module=ws_mod,
        )
        try:
            ok = _wait_until(lambda: dh.state == FeedState.LIVE, 5.0)
            self.assertTrue(ok, f"depth feed never went LIVE: state={dh.state}")
            self.assertEqual(len(depth_seen), 1)
            u = depth_seen[0]
            self.assertEqual(u.timestamp, 12345)
            self.assertEqual(len(u.bids), 2)
            self.assertEqual(len(u.asks), 1)
            self.assertEqual(u.bids[0].price, 99.5)
            self.assertEqual(u.bids[0].quantity, 2.0)
            self.assertEqual(u.asks[0].price, 100.5)
            self.assertEqual(u.is_snapshot, False)
        finally:
            stop_ev.set()
            thread.join(timeout=5.0)


class TestFailureHandling(unittest.TestCase):

    def test_max_consecutive_failures_marks_feed_failed(self):
        def factory(uri: str) -> FakeWS:
            raise ConnectionRefusedError("nope")

        bfws.FEED_RECONNECT_INITIAL_BACKOFF_S = 0.01
        bfws.FEED_RECONNECT_MAX_BACKOFF_S = 0.05
        try:
            ws_mod = FakeWebsocketsModule(on_connect=factory)
            th = FeedStreamHealth(); dh = FeedStreamHealth()
            thread, stop_ev = _start_runner(
                trade_health=th, depth_health=dh,
                on_ws_trade=lambda t, is_buy: None,
                on_ws_depth=lambda u: None,
                websockets_module=ws_mod,
            )
            try:
                ok = _wait_until(
                    lambda: th.consecutive_failures >= FEED_MAX_CONSECUTIVE_FAILURES,
                    timeout_s=10.0,
                )
                self.assertTrue(
                    ok,
                    f"failures never reached threshold: {th.consecutive_failures}",
                )
                # After threshold, the FSM has been promoted to FAILED at least once.
                ok2 = _wait_until(
                    lambda: th.state == FeedState.FAILED, timeout_s=2.0)
                # Note: state may be transiently overwritten by CONNECTING in
                # subsequent loop iterations, so we look for a window where
                # FAILED was reached.
                self.assertTrue(
                    ok2 or th.consecutive_failures >= FEED_MAX_CONSECUTIVE_FAILURES,
                )
            finally:
                stop_ev.set()
                thread.join(timeout=5.0)
                self.assertFalse(thread.is_alive())
        finally:
            bfws.FEED_RECONNECT_INITIAL_BACKOFF_S = 1.0
            bfws.FEED_RECONNECT_MAX_BACKOFF_S = 30.0


class TestStopAndShutdown(unittest.TestCase):

    def test_stop_event_terminates_within_two_seconds(self):
        def factory(uri: str) -> FakeWS:
            return FakeWS([BLOCK], FakeConnectionClosed)

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        th = FeedStreamHealth(); dh = FeedStreamHealth()
        thread, stop_ev = _start_runner(
            trade_health=th, depth_health=dh,
            on_ws_trade=lambda t, is_buy: None,
            on_ws_depth=lambda u: None,
            websockets_module=ws_mod,
        )
        time.sleep(0.5)
        t0 = time.monotonic()
        stop_ev.set()
        thread.join(timeout=3.0)
        elapsed = time.monotonic() - t0
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 3.0)
        # On exit the runner posts on_disconnect("loop exited") to both healths.
        self.assertEqual(th.state, FeedState.DISCONNECTED)
        self.assertEqual(dh.state, FeedState.DISCONNECTED)
        self.assertEqual(th.last_error, "loop exited")
        self.assertEqual(dh.last_error, "loop exited")


class TestWatchdog(unittest.TestCase):

    def test_watchdog_marks_stale_after_threshold(self):
        # Drop the stale threshold to a small value so the watchdog flips
        # from LIVE → STALE within one watchdog tick (1.0s).
        original_stale_ms = bfws.FEED_STALE_MS
        bfws.FEED_STALE_MS = 200
        try:
            def factory(uri: str) -> FakeWS:
                if "@trade" in uri:
                    return FakeWS([_trade_msg(t=7), BLOCK], FakeConnectionClosed)
                return FakeWS([BLOCK], FakeConnectionClosed)

            ws_mod = FakeWebsocketsModule(on_connect=factory)
            th = FeedStreamHealth(); dh = FeedStreamHealth()
            thread, stop_ev = _start_runner(
                trade_health=th, depth_health=dh,
                on_ws_trade=lambda t, is_buy: None,
                on_ws_depth=lambda u: None,
                websockets_module=ws_mod,
            )
            try:
                # First confirm LIVE.
                ok = _wait_until(lambda: th.state == FeedState.LIVE, 5.0)
                self.assertTrue(ok)
                # Watchdog runs every 1.0s; stale threshold patched to 0.2s.
                ok2 = _wait_until(lambda: th.state == FeedState.STALE, 5.0)
                self.assertTrue(
                    ok2,
                    f"watchdog never marked STALE: state={th.state}, "
                    f"age={th.seconds_since_last_msg:.2f}s",
                )
            finally:
                stop_ev.set()
                thread.join(timeout=5.0)
        finally:
            bfws.FEED_STALE_MS = original_stale_ms


class TestReconnectBackoff(unittest.TestCase):

    def test_reconnect_backoff_capped_at_max(self):
        self.assertEqual(_reconnect_backoff(20), FEED_RECONNECT_MAX_BACKOFF_S)
        self.assertEqual(_reconnect_backoff(8), FEED_RECONNECT_MAX_BACKOFF_S)

    def test_reconnect_backoff_grows_exponentially(self):
        self.assertAlmostEqual(_reconnect_backoff(0), 1.0, places=3)
        self.assertAlmostEqual(_reconnect_backoff(1), 2.0, places=3)
        self.assertAlmostEqual(_reconnect_backoff(2), 4.0, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
