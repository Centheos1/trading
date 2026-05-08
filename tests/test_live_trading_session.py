"""
Phase 10 — Track C: orchestration tests for `LiveTradingSession`.

Covers `start_live`, `stop`, `_ws_apply_trade`, `_ws_apply_depth`, and
`_resync_depth_snapshot` end-to-end with a stub `ofe`, stub fetchers, and a
no-op `run_binance_usdm_futures_ws_feed` patch. No PySide6 widgets, no real
engine, no network.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import live_trading_session as lts
from ui.live_trading_session import LiveTradingSession


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


def _make_engine_stub() -> MagicMock:
    eng = MagicMock(name="OrderFlowEngine")
    eng.set_signal_callback = MagicMock()
    eng.set_ripple_callback = MagicMock()
    vp = MagicMock(name="VolumeProfile")
    eng.get_volume_profile.return_value = vp
    return eng


def _make_ofe_stub() -> SimpleNamespace:
    cfg = SimpleNamespace(
        tick_size=0.0,
        signal_params=SimpleNamespace(imbalance_threshold=0.0),
        ripple=SimpleNamespace(
            enable_diagnostics=False, console_diagnostics=False),
    )

    class _DepthLevel:
        def __init__(self):
            self.price = 0.0
            self.quantity = 0.0

    class _DepthUpdate:
        def __init__(self):
            self.timestamp = 0
            self.first_update_id = 0
            self.final_update_id = 0
            self.is_snapshot = False
            self.bids = []
            self.asks = []

    class _Trade:
        def __init__(self):
            self.timestamp = 0
            self.price = 0.0
            self.quantity = 0.0
            self.is_buyer_maker = False

    return SimpleNamespace(
        EngineConfig=lambda: cfg,
        OrderFlowEngine=lambda c: _make_engine_stub(),
        DepthLevel=_DepthLevel,
        DepthUpdate=_DepthUpdate,
        Trade=_Trade,
    )


class _FakeMainWindow:
    """Minimal mw stand-in: only attributes the session reads in start/stop."""

    def __init__(self, websockets_module=None):
        self._websockets_module = websockets_module


# --------------------------------------------------------------------------
# Patch helpers
# --------------------------------------------------------------------------


class _ModulePatcher:
    """Save & restore module attributes for clean teardown."""

    def __init__(self, module):
        self._module = module
        self._originals: dict = {}

    def set(self, name: str, value):
        if name not in self._originals:
            self._originals[name] = getattr(self._module, name)
        setattr(self._module, name, value)

    def restore(self):
        for name, val in self._originals.items():
            setattr(self._module, name, val)
        self._originals.clear()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestStartLiveOfeMissing(unittest.TestCase):

    def test_start_live_returns_false_when_ofe_missing(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", None)
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            ok = session.start_live(
                symbol="BTCUSDT",
                tick_size=0.5,
                imbalance=0.6,
                vp_window_ms=60_000,
                websockets_module=object(),
                signal_callback=lambda *a, **kw: None,
                ripple_callback=lambda *a, **kw: None,
            )
            self.assertFalse(ok)
            self.assertIsNone(session.engine)
        finally:
            patcher.restore()


class TestStartLiveHappyPath(unittest.TestCase):

    def setUp(self):
        self.patcher = _ModulePatcher(lts)
        self.patcher.set("ofe", _make_ofe_stub())
        self.patcher.set(
            "fetch_and_build_depth_snapshot",
            MagicMock(return_value=SimpleNamespace(bids=[], asks=[])),
        )
        # Stub the WS runner to just block on stop_event so the thread
        # exits cleanly when stop() is called.
        def _ws_stub(*, stop_event, **kwargs):
            stop_event.wait(timeout=10.0)
        self.patcher.set("run_binance_usdm_futures_ws_feed", _ws_stub)

    def tearDown(self):
        self.patcher.restore()

    def test_start_live_creates_engine_and_thread(self):
        mw = _FakeMainWindow(websockets_module=object())
        session = LiveTradingSession(mw)
        sig_cb = MagicMock()
        rip_cb = MagicMock()
        ok = session.start_live(
            symbol="BTCUSDT",
            tick_size=0.5,
            imbalance=0.55,
            vp_window_ms=30_000,
            websockets_module=object(),
            signal_callback=sig_cb,
            ripple_callback=rip_cb,
        )
        self.assertTrue(ok)
        self.assertIsNotNone(session.engine)
        # Signal/ripple wiring
        session.engine.set_signal_callback.assert_called_once_with(sig_cb)
        session.engine.set_ripple_callback.assert_called_once_with(rip_cb)
        # VP window applied
        session.engine.get_volume_profile.return_value.set_window.assert_called_once_with(30_000)
        # Health initialised with symbol
        self.assertEqual(session.trade_feed_health.symbol, "BTCUSDT")
        self.assertEqual(session.depth_feed_health.symbol, "BTCUSDT")
        # WS thread started
        self.assertIsNotNone(session._ws_thread)
        self.assertTrue(session._ws_thread.is_alive())
        # REST snapshot was attempted
        lts.fetch_and_build_depth_snapshot.assert_called_once()

        # Clean shutdown
        session.stop()
        self.assertIsNone(session.engine)
        self.assertIsNone(session._ws_thread)


class TestStopResets(unittest.TestCase):

    def test_stop_resets_health_and_clears_buffers(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", _make_ofe_stub())
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            # Simulate "previously started" state by populating state directly.
            session._engine = _make_engine_stub()
            session._trade_buffer.append((1, 100.0, 1.0, True))
            session._trade_buffer.append((2, 100.5, 0.5, False))
            session._recent_trade_keys.append(1)
            session._recent_trade_keys.append(2)
            session._trades_received_ws = 99
            session._trades_dropped_in_drain = 3
            session._latest_ws_trade_ts = 12345
            session._last_drained_trade_ts = 12345
            session._book_empty_ticks = 7
            session._book_crossed_ticks = 2
            session._book_resync_count = 1
            session._book_resync_pending = True
            session._vp_cache_key = (1, 2, 3, 4)

            session.stop()

            self.assertIsNone(session.engine)
            self.assertEqual(len(session._trade_buffer), 0)
            self.assertEqual(len(session._recent_trade_keys), 0)
            self.assertEqual(session.trades_received_ws, 0)
            self.assertEqual(session.trades_dropped_in_drain, 0)
            self.assertEqual(session.latest_ws_trade_ts, 0)
            self.assertEqual(session.last_drained_trade_ts, 0)
            self.assertEqual(session._book_empty_ticks, 0)
            self.assertEqual(session._book_crossed_ticks, 0)
            self.assertEqual(session._book_resync_count, 0)
            self.assertFalse(session._book_resync_pending)
            self.assertIsNone(session._vp_cache_key)
            # Health regenerated to defaults
            self.assertEqual(session.trade_feed_health.symbol, "")
            self.assertEqual(session.depth_feed_health.symbol, "")
        finally:
            patcher.restore()


class TestWsApplyTrade(unittest.TestCase):

    def test_ws_apply_trade_processes_and_buffers(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", _make_ofe_stub())
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            engine = _make_engine_stub()
            session._engine = engine

            t1 = SimpleNamespace(
                timestamp=1000, price=100.0, quantity=1.0,
                is_buyer_maker=False,
            )
            t2 = SimpleNamespace(
                timestamp=2000, price=101.0, quantity=0.5,
                is_buyer_maker=True,
            )

            session._ws_apply_trade(t1, True)
            session._ws_apply_trade(t2, False)

            self.assertEqual(engine.process_trade.call_count, 2)
            self.assertEqual(session.trades_received_ws, 2)
            self.assertEqual(session.latest_ws_trade_ts, 2000)
            self.assertEqual(len(session._trade_buffer), 2)
            self.assertEqual(session._trade_buffer[0], (1000, 100.0, 1.0, True))
            self.assertEqual(session._trade_buffer[1], (2000, 101.0, 0.5, False))
        finally:
            patcher.restore()

    def test_ws_apply_trade_no_engine_is_noop(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        # _engine is None by default
        t = SimpleNamespace(
            timestamp=1, price=100.0, quantity=1.0, is_buyer_maker=False)
        session._ws_apply_trade(t, True)
        self.assertEqual(session.trades_received_ws, 0)
        self.assertEqual(len(session._trade_buffer), 0)


class TestWsApplyDepth(unittest.TestCase):

    def test_ws_apply_depth_calls_engine(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        engine = _make_engine_stub()
        session._engine = engine

        u = SimpleNamespace(timestamp=1, bids=[], asks=[])
        session._ws_apply_depth(u)
        engine.process_depth.assert_called_once_with(u)

    def test_ws_apply_depth_no_engine_is_noop(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        u = SimpleNamespace(timestamp=1, bids=[], asks=[])
        session._ws_apply_depth(u)   # must not raise


class TestResyncDepthSnapshot(unittest.TestCase):

    def test_resync_depth_snapshot_processes_book(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", _make_ofe_stub())
        fake_book = SimpleNamespace(bids=[("100", "1")], asks=[("101", "2")])
        patcher.set("fetch_binance_depth_book",
                    MagicMock(return_value=fake_book))
        fake_update = SimpleNamespace(
            timestamp=int(time.time() * 1000), bids=[], asks=[])
        patcher.set("depth_book_to_engine_update",
                    MagicMock(return_value=fake_update))
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            session._engine = _make_engine_stub()
            session._book_resync_pending = True

            session._resync_depth_snapshot("BTCUSDT")

            session._engine.process_depth.assert_called_once_with(fake_update)
            lts.fetch_binance_depth_book.assert_called_once()
            self.assertFalse(session._book_resync_pending)
        finally:
            patcher.restore()

    def test_resync_swallows_fetch_errors(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", _make_ofe_stub())

        def _raise(*a, **kw):
            raise RuntimeError("rest failed")

        patcher.set("fetch_binance_depth_book", _raise)
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            session._engine = _make_engine_stub()
            session._book_resync_pending = True

            # Must not raise
            session._resync_depth_snapshot("BTCUSDT")

            self.assertFalse(session._book_resync_pending)
            session._engine.process_depth.assert_not_called()
        finally:
            patcher.restore()

    def test_resync_no_engine_returns_quietly(self):
        patcher = _ModulePatcher(lts)
        patcher.set("ofe", _make_ofe_stub())
        patcher.set("fetch_binance_depth_book",
                    MagicMock(return_value=SimpleNamespace(bids=[], asks=[])))
        try:
            mw = _FakeMainWindow()
            session = LiveTradingSession(mw)
            # No engine assigned
            session._book_resync_pending = True
            session._resync_depth_snapshot("BTCUSDT")
            # finally clause clears the flag
            self.assertFalse(session._book_resync_pending)
            lts.fetch_binance_depth_book.assert_not_called()
        finally:
            patcher.restore()


if __name__ == "__main__":
    unittest.main(verbosity=2)
