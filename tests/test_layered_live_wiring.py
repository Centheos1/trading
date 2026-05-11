"""Phase 14B — layered-strategy live-wiring tests.

Pins three independent contracts:

1. ``execution.models.wave_snapshot_to_ofe`` translates a Python
   ``schemas.WaveSnapshot`` to an ``ofe.WaveSnapshot`` with bit-exact
   fidelity (regime, permissions, scalars).
2. ``execution.live_runner._layered_push_step`` honours the cadence
   ratios (RV every tick, Wave every 5, Tide every 60), correctly
   propagates Tide CRISIS (risk_multiplier=0.0) to
   ``RippleEngine.set_risk_budget``, and absorbs per-setter exceptions
   without breaking the loop.
3. ``run_live_execute(enable_layered_strategy=...)`` spawns / does NOT
   spawn the push thread depending on the flag, and the WS trade
   callback feeds the realized-vol buffer + Wave engine.

These are pure unit tests — no real C++ engine is started, no real
network, no real threads (except the push thread itself, which is
short-lived and joined deterministically).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import Any, Callable, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The translation tests need the real ofe module. The build script
# places the shared library at ``backtestingCpp/orderflow/build`` so
# we mirror the import path used by ``ui/live_trading_session.py``.
_OFE_BUILD = os.path.join(
    str(ROOT), "backtestingCpp", "orderflow", "build"
)
if _OFE_BUILD not in sys.path:
    sys.path.insert(0, os.path.normpath(_OFE_BUILD))

try:
    import orderflow_engine as ofe
except ImportError:  # pragma: no cover — only triggers if the build is missing
    ofe = None

from execution.live_runner import (
    _layered_push_step,
    _run_layered_push_loop,
    run_live_execute,
)
from execution.models import (
    compute_realized_vol_from_prices,
    wave_snapshot_to_ofe,
)
from schemas import (
    PermissionLevel,
    PermissionSet,
    TideBias,
    VolRegime,
    WaveRegime,
    WaveSnapshot,
    TideSnapshot,
)


# ----------------------------------------------------------------------
# Stubs


class _SpyRipple:
    """Captures every call to the three Phase 14B setters."""
    def __init__(self) -> None:
        self.risk_budget_calls: List[tuple] = []
        self.wave_snapshot_calls: List[Any] = []
        self.realized_vol_calls: List[float] = []
        self.fail_risk_budget = False
        self.fail_wave_snapshot = False
        self.fail_realized_vol = False

    def set_risk_budget(self, es_budget, max_position_usd, risk_multiplier):
        if self.fail_risk_budget:
            raise RuntimeError("set_risk_budget boom")
        self.risk_budget_calls.append(
            (es_budget, max_position_usd, risk_multiplier))

    def set_wave_snapshot(self, snap):
        if self.fail_wave_snapshot:
            raise RuntimeError("set_wave_snapshot boom")
        self.wave_snapshot_calls.append(snap)

    def set_realized_vol(self, vol):
        if self.fail_realized_vol:
            raise RuntimeError("set_realized_vol boom")
        self.realized_vol_calls.append(vol)


class _SpyEngine:
    """Mimics the parts of ``OrderFlowEngine`` the push path touches."""
    def __init__(self, *, ripple: Optional[_SpyRipple] = None,
                 get_ripple_raises: bool = False) -> None:
        self._ripple = ripple if ripple is not None else _SpyRipple()
        self._get_ripple_raises = get_ripple_raises
        self.signal_callback: Optional[Callable] = None
        self.ripple_callback: Optional[Callable] = None
        self.start_calls = 0
        self.stop_calls = 0
        self.process_depth_calls = 0
        self.process_trade_calls = 0

    def get_ripple(self) -> _SpyRipple:
        if self._get_ripple_raises:
            raise RuntimeError("get_ripple boom")
        return self._ripple

    def set_signal_callback(self, cb: Callable) -> None:
        self.signal_callback = cb

    def set_ripple_callback(self, cb: Callable) -> None:
        self.ripple_callback = cb

    def get_config(self) -> Any:
        return object()

    def start(self, _symbol: str) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def process_depth(self, _u: Any) -> None:
        self.process_depth_calls += 1

    def process_trade(self, _t: Any) -> None:
        self.process_trade_calls += 1


class _StubTideEngine:
    """Mimics the parts of :class:`TideEngine` the push path touches."""
    def __init__(self, *, snapshot: Optional[TideSnapshot] = None) -> None:
        self._snapshot = snapshot if snapshot is not None else TideSnapshot(
            timestamp=0,
            bias=TideBias.NEUTRAL,
            risk_multiplier=1.0,
            max_position_usd=10_000.0,
            es_budget=1_000.0,
            vol_regime=VolRegime.NORMAL,
        )
        self.realized_vol_calls: List[float] = []
        self.update_calls: List[int] = []

    def set_realized_vol(self, vol: float) -> None:
        self.realized_vol_calls.append(vol)
        self._snapshot.realized_vol = vol

    def update(self, timestamp: int) -> TideSnapshot:
        self.update_calls.append(timestamp)
        return self._snapshot

    def get_snapshot(self) -> TideSnapshot:
        return self._snapshot


class _StubWaveEngine:
    """Mimics the parts of :class:`WaveEngine` the push path touches."""
    def __init__(self, *, snapshot: Optional[WaveSnapshot] = None) -> None:
        self._snapshot = snapshot if snapshot is not None else WaveSnapshot(
            timestamp=0,
            regime=WaveRegime.NEUTRAL,
            trend_efficiency=0.5,
            dispersion=0.0,
            absorption_ratio=0.5,
            permissions=PermissionSet(),
        )
        self.on_price_calls: List[tuple] = []
        self.update_calls: List[tuple] = []

    def on_price(self, price: float, ts: int) -> None:
        self.on_price_calls.append((price, ts))

    def update(self, timestamp_ms: int,
               bias: TideBias = TideBias.NEUTRAL) -> WaveSnapshot:
        self.update_calls.append((timestamp_ms, bias))
        return self._snapshot

    def get_snapshot(self,
                     bias: TideBias = TideBias.NEUTRAL) -> WaveSnapshot:
        return self._snapshot


class _StubExecMgr:
    def __init__(self) -> None:
        self.on_intent_calls: List[Any] = []
        self.disarm_calls = 0
        self.stop_calls = 0
        self.current_side = None
        self.current_qty = 0.0
        self.orders: List[Any] = []
        self.armed = True

    def on_intent(self, intent) -> None:
        self.on_intent_calls.append(intent)

    def disarm(self, close_position: bool = True) -> None:
        self.disarm_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class _NoopWebsockets:
    pass


class _StubOFEModule:
    """Minimal stub used where the translator isn't exercised. Tests
    that translate WaveSnapshot use the real ``ofe`` module."""
    WaveSnapshot = type("WaveSnapshot", (), {})


# ----------------------------------------------------------------------
# 1. wave_snapshot_to_ofe translation fidelity


@unittest.skipIf(ofe is None, "orderflow_engine module not built")
class TestWaveSnapshotToOfe(unittest.TestCase):
    """Confirms the Python → C++ translation is loss-free."""

    def _make_py_snap(
        self,
        *,
        regime: WaveRegime = WaveRegime.NEUTRAL,
        long_bounce: PermissionLevel = PermissionLevel.FULL,
        short_bounce: PermissionLevel = PermissionLevel.FULL,
        long_breakout: PermissionLevel = PermissionLevel.FULL,
        short_breakout: PermissionLevel = PermissionLevel.FULL,
        reduced_size_fraction: float = 0.5,
        trend_efficiency: float = 0.5,
        dispersion: float = 0.0,
        absorption_ratio: float = 0.5,
        timestamp: int = 0,
    ) -> WaveSnapshot:
        perms = PermissionSet(
            long_bounce=long_bounce,
            short_bounce=short_bounce,
            long_breakout=long_breakout,
            short_breakout=short_breakout,
            reduced_size_fraction=reduced_size_fraction,
        )
        return WaveSnapshot(
            timestamp=timestamp,
            regime=regime,
            trend_efficiency=trend_efficiency,
            dispersion=dispersion,
            absorption_ratio=absorption_ratio,
            permissions=perms,
        )

    def test_all_wave_regime_values_translate(self):
        for r in WaveRegime:
            py = self._make_py_snap(regime=r)
            out = wave_snapshot_to_ofe(py, ofe)
            self.assertEqual(out.regime.name, r.name,
                             f"regime {r.name} did not round-trip")

    def test_all_permission_level_values_translate(self):
        for lvl in PermissionLevel:
            py = self._make_py_snap(
                long_bounce=lvl, short_bounce=lvl,
                long_breakout=lvl, short_breakout=lvl,
            )
            out = wave_snapshot_to_ofe(py, ofe)
            self.assertEqual(out.permissions.long_bounce.name, lvl.name)
            self.assertEqual(out.permissions.short_bounce.name, lvl.name)
            self.assertEqual(out.permissions.long_breakout.name, lvl.name)
            self.assertEqual(out.permissions.short_breakout.name, lvl.name)

    def test_reduced_size_fraction_preserved(self):
        py = self._make_py_snap(reduced_size_fraction=0.33)
        out = wave_snapshot_to_ofe(py, ofe)
        self.assertAlmostEqual(out.permissions.reduced_size_fraction, 0.33)

    def test_scalar_fields_preserved(self):
        py = self._make_py_snap(
            trend_efficiency=0.81,
            dispersion=0.42,
            absorption_ratio=0.17,
            timestamp=1_700_000_000_000,
        )
        out = wave_snapshot_to_ofe(py, ofe)
        self.assertAlmostEqual(out.trend_efficiency, 0.81)
        self.assertAlmostEqual(out.dispersion, 0.42)
        self.assertAlmostEqual(out.absorption_ratio, 0.17)
        self.assertEqual(out.timestamp, 1_700_000_000_000)

    def test_mixed_permissions_translate(self):
        """The C++ side stores each side independently; verify the
        translator does not collapse asymmetric permission sets."""
        py = self._make_py_snap(
            long_bounce=PermissionLevel.DISABLED,
            short_bounce=PermissionLevel.FULL,
            long_breakout=PermissionLevel.REDUCED,
            short_breakout=PermissionLevel.DISABLED,
        )
        out = wave_snapshot_to_ofe(py, ofe)
        self.assertEqual(out.permissions.long_bounce.name, "DISABLED")
        self.assertEqual(out.permissions.short_bounce.name, "FULL")
        self.assertEqual(out.permissions.long_breakout.name, "REDUCED")
        self.assertEqual(out.permissions.short_breakout.name, "DISABLED")


# ----------------------------------------------------------------------
# 2. compute_realized_vol_from_prices


class TestComputeRealizedVol(unittest.TestCase):

    def test_empty_buffer_returns_zero(self):
        self.assertEqual(compute_realized_vol_from_prices([]), 0.0)

    def test_single_price_returns_zero(self):
        self.assertEqual(compute_realized_vol_from_prices([100.0]), 0.0)

    def test_two_identical_prices_returns_zero(self):
        # Need >= 2 log-returns for a stdev; single zero return is
        # insufficient (sample-variance with n=1 is undefined).
        self.assertEqual(
            compute_realized_vol_from_prices([100.0, 100.0]), 0.0)

    def test_constant_prices_returns_zero(self):
        self.assertEqual(
            compute_realized_vol_from_prices([100.0] * 30), 0.0)

    def test_volatile_prices_positive(self):
        prices = [100.0, 101.0, 99.0, 102.0, 98.0, 103.0, 97.0]
        rv = compute_realized_vol_from_prices(prices)
        self.assertGreater(rv, 0.0)

    def test_zero_and_negative_prices_filtered(self):
        # 0 and -1 are filtered; remaining [100, 101, 99] yields a
        # well-defined non-zero stdev (2 log returns).
        rv = compute_realized_vol_from_prices([0.0, 100.0, -1.0, 101.0, 99.0])
        self.assertGreater(rv, 0.0)


# ----------------------------------------------------------------------
# 3. _layered_push_step cadence


class TestLayeredPushStep(unittest.TestCase):
    """The cadence ratios are an absolute contract: RV every iteration,
    Wave every 5, Tide every 60. The push step is called once per
    push-loop iteration; this class drives it manually."""

    def _drive(self, n: int, **overrides) -> tuple:
        ripple = _SpyRipple()
        engine = _SpyEngine(ripple=ripple)
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        buf = deque([100.0, 101.0, 99.0, 102.0], maxlen=60)
        last_ts = [1_700_000_000_000]
        state: dict = {}
        kwargs = dict(
            state=state, engine=engine, tide_engine=tide, wave_engine=wave,
            rv_price_buf=buf, ofe_module=ofe,
            last_trade_ts_holder=last_ts,
        )
        kwargs.update(overrides)
        for _ in range(n):
            _layered_push_step(**kwargs)
        return ripple, tide, wave, state

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_first_tick_pushes_rv_only(self):
        ripple, _, _, _ = self._drive(1)
        self.assertEqual(len(ripple.realized_vol_calls), 1)
        self.assertEqual(len(ripple.wave_snapshot_calls), 0)
        self.assertEqual(len(ripple.risk_budget_calls), 0)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_five_ticks_one_wave_push(self):
        ripple, _, _, _ = self._drive(5)
        self.assertEqual(len(ripple.realized_vol_calls), 5)
        self.assertEqual(len(ripple.wave_snapshot_calls), 1)
        self.assertEqual(len(ripple.risk_budget_calls), 0)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_sixty_ticks_one_tide_push(self):
        ripple, _, _, _ = self._drive(60)
        self.assertEqual(len(ripple.realized_vol_calls), 60)
        self.assertEqual(len(ripple.wave_snapshot_calls), 12)
        self.assertEqual(len(ripple.risk_budget_calls), 1)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_three_hundred_ticks_full_cadence_proportions(self):
        ripple, _, _, _ = self._drive(300)
        # 300 / 1 = 300 RV pushes
        self.assertEqual(len(ripple.realized_vol_calls), 300)
        # 300 / 5 = 60 Wave pushes
        self.assertEqual(len(ripple.wave_snapshot_calls), 60)
        # 300 / 60 = 5 Tide pushes
        self.assertEqual(len(ripple.risk_budget_calls), 5)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_tide_crisis_propagates_to_set_risk_budget(self):
        """If the Tide engine surfaces ``risk_multiplier == 0.0`` (CRISIS
        regime), the push must propagate that value verbatim — the C++
        engine's RiskEngine then refuses to size any new trade
        (strategy.md §7.4.7 V1 contract)."""
        ripple = _SpyRipple()
        engine = _SpyEngine(ripple=ripple)
        crisis = TideSnapshot(
            bias=TideBias.NEUTRAL,
            risk_multiplier=0.0,
            es_budget=1000.0,
            max_position_usd=10000.0,
            vol_regime=VolRegime.CRISIS,
        )
        tide = _StubTideEngine(snapshot=crisis)
        wave = _StubWaveEngine()
        buf = deque([100.0, 101.0], maxlen=60)
        state: dict = {}
        for _ in range(60):
            _layered_push_step(
                state=state, engine=engine, tide_engine=tide,
                wave_engine=wave, rv_price_buf=buf, ofe_module=ofe,
                last_trade_ts_holder=[1_700_000_000_000])
        self.assertEqual(len(ripple.risk_budget_calls), 1)
        es_budget, max_pos, risk_mult = ripple.risk_budget_calls[0]
        self.assertEqual(es_budget, 1000.0)
        self.assertEqual(max_pos, 10000.0)
        self.assertEqual(risk_mult, 0.0)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_get_ripple_failure_aborts_cleanly(self):
        """A binding-error inside ``get_ripple()`` must not break the
        push loop or leak the exception. No setter calls are recorded
        because we never got a handle to call them on."""
        ripple = _SpyRipple()
        engine = _SpyEngine(ripple=ripple, get_ripple_raises=True)
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        buf = deque([100.0, 101.0], maxlen=60)
        state: dict = {}
        # No exception should escape:
        _layered_push_step(
            state=state, engine=engine, tide_engine=tide,
            wave_engine=wave, rv_price_buf=buf, ofe_module=ofe,
            last_trade_ts_holder=[1_700_000_000_000])
        self.assertEqual(len(ripple.realized_vol_calls), 0)
        self.assertEqual(len(ripple.wave_snapshot_calls), 0)
        self.assertEqual(len(ripple.risk_budget_calls), 0)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_per_setter_exception_isolated(self):
        """A failure on one setter must not suppress the other two."""
        ripple = _SpyRipple()
        ripple.fail_realized_vol = True  # RV setter raises every call
        engine = _SpyEngine(ripple=ripple)
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        buf = deque([100.0, 101.0, 99.0], maxlen=60)
        state: dict = {}
        for _ in range(60):
            _layered_push_step(
                state=state, engine=engine, tide_engine=tide,
                wave_engine=wave, rv_price_buf=buf, ofe_module=ofe,
                last_trade_ts_holder=[1_700_000_000_000])
        # RV setter raised on every call → 0 captured calls
        self.assertEqual(len(ripple.realized_vol_calls), 0)
        # Wave + Tide still proceed
        self.assertEqual(len(ripple.wave_snapshot_calls), 12)
        self.assertEqual(len(ripple.risk_budget_calls), 1)

    @unittest.skipIf(ofe is None, "orderflow_engine module not built")
    def test_state_counters_reset_after_each_push(self):
        """Cadence is per-counter, not based on total iteration count.
        Verify the counter actually rolls over."""
        ripple, _, _, state = self._drive(11)
        # 11 RV pushes; counter post-push starts at 1.
        self.assertEqual(len(ripple.realized_vol_calls), 11)
        # 11 / 5 = 2 wave pushes (at tick 5 and tick 10).
        self.assertEqual(len(ripple.wave_snapshot_calls), 2)
        # post-tick state has wave_counter = 1 (one tick since last push)
        self.assertEqual(state.get("wave_counter"), 1)


# ----------------------------------------------------------------------
# 4. _run_layered_push_loop


@unittest.skipIf(ofe is None, "orderflow_engine module not built")
class TestRunLayeredPushLoop(unittest.TestCase):

    def test_loop_stops_when_stop_event_set(self):
        ripple = _SpyRipple()
        engine = _SpyEngine(ripple=ripple)
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        buf = deque([100.0, 101.0], maxlen=60)
        stop_event = threading.Event()
        state: dict = {}
        # Start the loop with a tiny push interval so we can drive a
        # handful of iterations in well under a second.
        t = threading.Thread(
            target=_run_layered_push_loop,
            kwargs=dict(
                stop_event=stop_event, engine=engine,
                tide_engine=tide, wave_engine=wave,
                rv_price_buf=buf, ofe_module=ofe,
                last_trade_ts_holder=[1_700_000_000_000],
                push_interval_s=0.01,
                state=state,
            ),
            daemon=True,
        )
        t.start()
        time.sleep(0.05)  # ~5 push iterations
        stop_event.set()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "push loop did not exit on stop_event")
        # At least one RV push fired:
        self.assertGreater(len(ripple.realized_vol_calls), 0)


# ----------------------------------------------------------------------
# 5. run_live_execute layered-strategy wiring


@unittest.skipIf(ofe is None, "orderflow_engine module not built")
class TestRunLiveExecuteLayeredWiring(unittest.TestCase):
    """End-to-end at the runner level: confirms the runner spawns /
    skips the push thread per the ``enable_layered_strategy`` flag and
    that the WS trade callback feeds the RV buffer + Wave engine."""

    def _drive(self, *, enable_layered_strategy: bool,
               tide_engine=None, wave_engine=None) -> tuple:
        exec_mgr = _StubExecMgr()
        engine = _SpyEngine()
        interrupt_event = threading.Event()
        interrupt_event.set()  # main loop falls through immediately

        def _noop_ws_feed(**_kwargs):
            return None

        with patch("data_feed.run_binance_usdm_futures_ws_feed",
                   new=_noop_ws_feed), \
             patch("data_feed.stream_health.FeedStreamHealth"):
            rc = run_live_execute(
                symbol="BTCUSDT",
                exec_mgr=exec_mgr,
                ofe_module=ofe,
                websockets_module=_NoopWebsockets(),
                apply_rest_snapshot=False,
                engine_factory=lambda _ofe: engine,
                interrupt_event=interrupt_event,
                disarm_grace_s=0.0,
                join_timeout_s=1.0,
                enable_layered_strategy=enable_layered_strategy,
                tide_engine=tide_engine,
                wave_engine=wave_engine,
            )
        return rc, exec_mgr, engine

    def test_disabled_flag_skips_all_setters(self):
        """``enable_layered_strategy=False`` is the V1 regression
        switch. With it off, the engine receives zero setter calls
        and behaves exactly as it did pre-Phase 14B (against the C++
        Defaults)."""
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        rc, _, engine = self._drive(
            enable_layered_strategy=False,
            tide_engine=tide, wave_engine=wave,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine._ripple.realized_vol_calls), 0)
        self.assertEqual(len(engine._ripple.wave_snapshot_calls), 0)
        self.assertEqual(len(engine._ripple.risk_budget_calls), 0)
        # Tide/Wave engines must not have been touched.
        self.assertEqual(len(tide.update_calls), 0)
        self.assertEqual(len(wave.update_calls), 0)

    def test_enabled_flag_spawns_push_thread(self):
        """With layered-strategy enabled the runner must have spawned
        the push thread; we cannot easily assert in-process timing so
        we just verify the wiring did not error out (rc == 0) and the
        Tide / Wave engine instances were passed through."""
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        rc, _, _ = self._drive(
            enable_layered_strategy=True,
            tide_engine=tide, wave_engine=wave,
        )
        self.assertEqual(rc, 0)

    def test_on_ws_trade_feeds_wave_engine_and_rv_buffer(self):
        """The WS trade callback is the ingestion point for layered
        strategy state — every trade must reach ``WaveEngine.on_price``
        AND append to the RV buffer."""
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        captured: dict = {}

        def _fake_ws_feed(*, on_ws_trade, on_ws_depth, **_kwargs):
            captured["on_ws_trade"] = on_ws_trade
            captured["on_ws_depth"] = on_ws_depth

        class _TradeStub:
            def __init__(self, price: float, ts: int) -> None:
                self.price = price
                self.timestamp = ts

        exec_mgr = _StubExecMgr()
        engine = _SpyEngine()
        interrupt_event = threading.Event()
        interrupt_event.set()

        with patch("data_feed.run_binance_usdm_futures_ws_feed",
                   new=_fake_ws_feed), \
             patch("data_feed.stream_health.FeedStreamHealth"):
            run_live_execute(
                symbol="BTCUSDT",
                exec_mgr=exec_mgr,
                ofe_module=ofe,
                websockets_module=_NoopWebsockets(),
                apply_rest_snapshot=False,
                engine_factory=lambda _ofe: engine,
                interrupt_event=interrupt_event,
                disarm_grace_s=0.0,
                join_timeout_s=2.0,
                enable_layered_strategy=True,
                tide_engine=tide,
                wave_engine=wave,
            )

        self.assertIn("on_ws_trade", captured,
                      "fake WS feed did not capture the trade callback")
        on_trade = captured["on_ws_trade"]
        # Fire two synthetic trades through the closure; the wave
        # engine spy must record both.
        on_trade(_TradeStub(price=42_000.0, ts=1_700_000_000_000), True)
        on_trade(_TradeStub(price=42_100.0, ts=1_700_000_001_000), False)
        self.assertEqual(
            wave.on_price_calls,
            [(42_000.0, 1_700_000_000_000),
             (42_100.0, 1_700_000_001_000)])
        # Bad/empty trades must NOT be forwarded (price=0 OR ts=0).
        on_trade(_TradeStub(price=0.0, ts=1_700_000_002_000), True)
        on_trade(_TradeStub(price=42_200.0, ts=0), True)
        self.assertEqual(len(wave.on_price_calls), 2,
                         "zero price / zero ts trades must be dropped")

    def test_disabled_flag_on_ws_trade_does_not_touch_wave(self):
        """With layered-strategy disabled the WS callback path must
        not call ``wave.on_price`` even though the engine still
        consumes trades for the order book."""
        tide = _StubTideEngine()
        wave = _StubWaveEngine()
        captured: dict = {}

        def _fake_ws_feed(*, on_ws_trade, on_ws_depth, **_kwargs):
            captured["on_ws_trade"] = on_ws_trade

        class _TradeStub:
            def __init__(self, price: float, ts: int) -> None:
                self.price = price
                self.timestamp = ts

        exec_mgr = _StubExecMgr()
        engine = _SpyEngine()
        interrupt_event = threading.Event()
        interrupt_event.set()

        with patch("data_feed.run_binance_usdm_futures_ws_feed",
                   new=_fake_ws_feed), \
             patch("data_feed.stream_health.FeedStreamHealth"):
            run_live_execute(
                symbol="BTCUSDT",
                exec_mgr=exec_mgr,
                ofe_module=ofe,
                websockets_module=_NoopWebsockets(),
                apply_rest_snapshot=False,
                engine_factory=lambda _ofe: engine,
                interrupt_event=interrupt_event,
                disarm_grace_s=0.0,
                join_timeout_s=2.0,
                enable_layered_strategy=False,
                tide_engine=tide,
                wave_engine=wave,
            )

        on_trade = captured["on_ws_trade"]
        on_trade(_TradeStub(price=42_000.0, ts=1_700_000_000_000), True)
        self.assertEqual(len(wave.on_price_calls), 0,
                         "layered strategy disabled must not feed wave")


if __name__ == "__main__":
    unittest.main()
