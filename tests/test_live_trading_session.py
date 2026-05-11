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


# ──────────────────────────────────────────────────────────────────────
# Phase 14F.3 — _push_layered_strategy cadence + snapshot translation
# ──────────────────────────────────────────────────────────────────────


def _make_layered_ofe_stub() -> SimpleNamespace:
    """Stub ``ofe`` module rich enough for :func:`wave_snapshot_to_ofe`.

    The translator reads ``ofe.WaveSnapshot``, ``ofe.WaveRegime``, and
    ``ofe.PermissionLevel``. Enum members are looked up by ``.name`` so
    the stubs need attribute access matching every Python enum member.
    """
    from schemas import WaveRegime as PyWaveRegime, PermissionLevel as PyPL

    class _Permissions:
        def __init__(self):
            self.long_bounce = None
            self.short_bounce = None
            self.long_breakout = None
            self.short_breakout = None
            self.reduced_size_fraction = 0.0

    class _WS:
        def __init__(self):
            self.timestamp = 0
            self.regime = None
            self.trend_efficiency = 0.0
            self.dispersion = 0.0
            self.absorption_ratio = 0.0
            self.permissions = _Permissions()

    wave_regime_ns = SimpleNamespace(**{m.name: m.name for m in PyWaveRegime})
    permission_ns = SimpleNamespace(**{m.name: m.name for m in PyPL})

    return SimpleNamespace(
        WaveSnapshot=_WS,
        WaveRegime=wave_regime_ns,
        PermissionLevel=permission_ns,
    )


def _make_layered_engine_stub() -> MagicMock:
    """Engine with a ripple handle whose three setters can be observed."""
    engine = MagicMock(name="OrderFlowEngine")
    ripple = MagicMock(name="RippleEngine")
    ripple.set_realized_vol = MagicMock()
    ripple.set_wave_snapshot = MagicMock()
    ripple.set_risk_budget = MagicMock()
    engine.get_ripple.return_value = ripple
    return engine


class TestPushLayeredStrategyDisabled(unittest.TestCase):
    """Phase 14F.3: an explicit safety pin on the two short-circuit
    branches so a future refactor cannot silently start pushing while
    the feature flag is off or the engine handle is missing."""

    def test_noop_when_disabled(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        session._engine = _make_layered_engine_stub()
        session._enable_layered_strategy = False
        session._push_layered_strategy()
        session._engine.get_ripple.assert_not_called()

    def test_noop_when_no_engine(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        session._engine = None
        session._enable_layered_strategy = True
        # Must not raise — branch returns before touching anything.
        session._push_layered_strategy()


class TestPushLayeredStrategyCadence(unittest.TestCase):
    """Phase 14F.3: pin the cadence contract documented in §5.3 + AGENT_
    STRATEGY_RULES.md §7.5. UI timer fires at 100 ms; the counters
    convert that to 1 s / 5 s / 60 s wall-clock pushes."""

    def setUp(self):
        self.patcher = _ModulePatcher(lts)
        self.patcher.set("ofe", _make_layered_ofe_stub())

    def tearDown(self):
        self.patcher.restore()

    def _make_session(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        session._engine = _make_layered_engine_stub()
        session._enable_layered_strategy = True
        # Seed prices so realized-vol computation returns a non-zero
        # (and therefore observable) value.
        for p in (100.0, 101.0, 100.5, 101.5):
            session._rv_price_buf.append(p)
        # Seed a non-zero last-trade ts so the Wave / Tide branches
        # exercise the ``last_ts > 0`` update path.
        session._last_drained_trade_ts = 1_700_000_000_000
        return session

    def test_rv_fires_every_RV_PUSH_EVERY_ticks(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        # _RV_PUSH_EVERY = 10. After 9 ticks: zero pushes. The 10th
        # tick triggers exactly one push.
        for _ in range(LiveTradingSession._RV_PUSH_EVERY - 1):
            session._push_layered_strategy()
        self.assertEqual(ripple.set_realized_vol.call_count, 0)
        session._push_layered_strategy()
        self.assertEqual(ripple.set_realized_vol.call_count, 1)
        self.assertEqual(session._layered_pushes_rv, 1)

    def test_wave_fires_every_WAVE_PUSH_EVERY_ticks(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        # _WAVE_PUSH_EVERY = 50.
        for _ in range(LiveTradingSession._WAVE_PUSH_EVERY - 1):
            session._push_layered_strategy()
        self.assertEqual(ripple.set_wave_snapshot.call_count, 0)
        session._push_layered_strategy()
        self.assertEqual(ripple.set_wave_snapshot.call_count, 1)
        self.assertEqual(session._layered_pushes_wave, 1)

    def test_tide_fires_every_TIDE_PUSH_EVERY_ticks(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        # _TIDE_PUSH_EVERY = 600 (60 s wall-clock).
        for _ in range(LiveTradingSession._TIDE_PUSH_EVERY - 1):
            session._push_layered_strategy()
        self.assertEqual(ripple.set_risk_budget.call_count, 0)
        session._push_layered_strategy()
        self.assertEqual(ripple.set_risk_budget.call_count, 1)
        self.assertEqual(session._layered_pushes_tide, 1)


class TestPushLayeredStrategySnapshotTranslation(unittest.TestCase):
    """Phase 14F.3: verify each setter receives a translated payload
    sourced from the embedded Python ``TideEngine`` / ``WaveEngine``
    rather than a raw Python dataclass — the C++ surface only accepts
    its own enum/struct types."""

    def setUp(self):
        self.patcher = _ModulePatcher(lts)
        self.patcher.set("ofe", _make_layered_ofe_stub())

    def tearDown(self):
        self.patcher.restore()

    def _make_session(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        session._engine = _make_layered_engine_stub()
        session._enable_layered_strategy = True
        for p in (100.0, 101.0, 100.5, 101.5):
            session._rv_price_buf.append(p)
        session._last_drained_trade_ts = 1_700_000_000_000
        return session

    def test_wave_setter_receives_translated_ofe_wavesnapshot(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        # Skip to the wave-firing tick.
        for _ in range(LiveTradingSession._WAVE_PUSH_EVERY):
            session._push_layered_strategy()
        ripple.set_wave_snapshot.assert_called_once()
        ws = ripple.set_wave_snapshot.call_args.args[0]
        # The payload is the stub's _WS class instance produced by
        # wave_snapshot_to_ofe — NOT a raw schemas.WaveSnapshot.
        self.assertTrue(hasattr(ws, "regime"))
        self.assertTrue(hasattr(ws, "permissions"))
        # Regime is translated by name (string from our stub enum ns).
        self.assertIsInstance(ws.regime, str)
        # Permissions block carries all four directional levels.
        for attr in ("long_bounce", "short_bounce",
                     "long_breakout", "short_breakout"):
            self.assertIsNotNone(getattr(ws.permissions, attr))

    def test_tide_setter_receives_three_scalars_from_snapshot(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        for _ in range(LiveTradingSession._TIDE_PUSH_EVERY):
            session._push_layered_strategy()
        ripple.set_risk_budget.assert_called_once()
        args = ripple.set_risk_budget.call_args.args
        # (es_budget, max_position_usd, risk_multiplier) — three floats.
        self.assertEqual(len(args), 3)
        for v in args:
            self.assertIsInstance(v, float)
        # Default TideConfig ships positive budget + non-negative
        # multiplier (exact regime multiplier depends on default
        # vol_regime, which is environment-driven; the contract under
        # test is just "all three scalars survived translation").
        self.assertGreater(args[0], 0.0)  # es_budget > 0
        self.assertGreater(args[1], 0.0)  # max_position_usd > 0
        self.assertGreaterEqual(args[2], 0.0)

    def test_rv_setter_uses_compute_realized_vol_from_prices(self):
        session = self._make_session()
        ripple = session._engine.get_ripple.return_value
        for _ in range(LiveTradingSession._RV_PUSH_EVERY):
            session._push_layered_strategy()
        ripple.set_realized_vol.assert_called_once()
        rv = ripple.set_realized_vol.call_args.args[0]
        self.assertIsInstance(rv, float)
        self.assertGreaterEqual(rv, 0.0)


class TestPushLayeredStrategyCounterStall(unittest.TestCase):
    """Phase 14F.3 — counter parity regression. The headless
    ``execution.live_runner._layered_push_step`` had a bug where
    counters incremented *before* the ``get_ripple()`` guard, drifting
    its cadence relative to the UI path. Both code paths now stall
    counters on failure; this test pins the UI side."""

    def setUp(self):
        self.patcher = _ModulePatcher(lts)
        self.patcher.set("ofe", _make_layered_ofe_stub())

    def tearDown(self):
        self.patcher.restore()

    def test_counters_stall_on_get_ripple_failure(self):
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        engine = MagicMock(name="OrderFlowEngine")
        engine.get_ripple.side_effect = RuntimeError("binding gone")
        session._engine = engine
        session._enable_layered_strategy = True

        # Establish a baseline.
        before = (session._rv_push_counter,
                  session._wave_push_counter,
                  session._tide_push_counter)

        # Many failed iterations.
        for _ in range(25):
            session._push_layered_strategy()

        after = (session._rv_push_counter,
                 session._wave_push_counter,
                 session._tide_push_counter)
        self.assertEqual(before, after,
                         "counters must not advance when get_ripple "
                         "raises — otherwise cadence drifts relative "
                         "to live_runner._layered_push_step")
        # Setter success counters also untouched.
        self.assertEqual(session._layered_pushes_rv, 0)
        self.assertEqual(session._layered_pushes_wave, 0)
        self.assertEqual(session._layered_pushes_tide, 0)

    def test_counter_parity_with_live_runner_after_failure_cluster(self):
        """Drive both code paths through the same sequence of get_ripple
        outcomes and verify the resulting counter / push tallies are
        identical. This is the explicit cross-path contract."""
        from execution import live_runner as lr

        # ── UI session ────────────────────────────────────────────
        mw = _FakeMainWindow()
        session = LiveTradingSession(mw)
        ui_engine = MagicMock(name="UI_Engine")
        ui_ripple = MagicMock(name="UI_Ripple")
        ui_engine.get_ripple.return_value = ui_ripple
        session._engine = ui_engine
        session._enable_layered_strategy = True
        for p in (100.0, 101.0, 100.5):
            session._rv_price_buf.append(p)
        session._last_drained_trade_ts = 1_700_000_000_000

        # ── Headless live_runner pieces ──────────────────────────
        hl_state: dict = {}
        hl_engine = MagicMock(name="HL_Engine")
        hl_ripple = MagicMock(name="HL_Ripple")
        hl_engine.get_ripple.return_value = hl_ripple
        # Drive a real TideEngine/WaveEngine so the two paths see
        # equivalent snapshot behaviour. (UI session creates them in
        # __init__; headless requires explicit instances.)
        from tide.tide_engine import TideEngine
        from wave.wave_engine import WaveEngine
        hl_tide = TideEngine()
        hl_wave = WaveEngine()
        hl_buf: deque = deque([100.0, 101.0, 100.5], maxlen=60)
        hl_last_ts = [1_700_000_000_000]

        def _step_headless():
            lr._layered_push_step(
                state=hl_state,
                engine=hl_engine,
                tide_engine=hl_tide,
                wave_engine=hl_wave,
                rv_price_buf=hl_buf,
                ofe_module=lts.ofe,
                last_trade_ts_holder=hl_last_ts,
                rv_every=LiveTradingSession._RV_PUSH_EVERY,
                wave_every=LiveTradingSession._WAVE_PUSH_EVERY,
                tide_every=LiveTradingSession._TIDE_PUSH_EVERY,
            )

        # Step both paths in lockstep for one full Wave cycle.
        for _ in range(LiveTradingSession._WAVE_PUSH_EVERY):
            session._push_layered_strategy()
            _step_headless()

        # Counter parity: both reset rv/wave at the same boundary.
        self.assertEqual(session._rv_push_counter,
                         hl_state.get("rv_counter", 0))
        self.assertEqual(session._wave_push_counter,
                         hl_state.get("wave_counter", 0))
        self.assertEqual(session._tide_push_counter,
                         hl_state.get("tide_counter", 0))
        # Push tallies should match too.
        self.assertEqual(session._layered_pushes_rv,
                         hl_state.get("pushes_rv", 0))
        self.assertEqual(session._layered_pushes_wave,
                         hl_state.get("pushes_wave", 0))
        self.assertEqual(session._layered_pushes_tide,
                         hl_state.get("pushes_tide", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
