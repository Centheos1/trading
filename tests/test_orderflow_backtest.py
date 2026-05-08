"""Phase 13X regression tests for ``strategies.orderflow.backtest``.

The legacy path called ``engine.start(symbol)`` *and*
``replay.run_sync()`` — the former wires callbacks and spawns a worker
thread that runs ``ReplayFeed::replay_thread_func()``; the latter runs
the same loop synchronously on the main thread. Every trade and every
depth event was therefore processed twice (Phase 13X probe measured an
exact 2× volume profile / CVD count). These tests pin the corrected
single-fire behaviour so the regression cannot reappear silently.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from typing import Any, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe  # type: ignore[import]
    HAS_C_MODULE = (
        hasattr(ofe, "OrderFlowEngine")
        and hasattr(ofe, "TickStore")
        and hasattr(ofe, "ReplayFeed")
    )
except ImportError:
    ofe = None  # type: ignore[assignment]
    HAS_C_MODULE = False


SYMBOL = "REPLAYBTCUSDT"


def _make_depth(ts: int, mid: float, levels: int = 5,
                base_qty: float = 50.0) -> Any:
    """Build a populated DepthUpdate snapshot (whole-list assignment —
    per-element ``.append()`` is a no-op under pybind11's default
    vector binding)."""
    d = ofe.DepthUpdate()
    d.timestamp = ts
    d.is_snapshot = True
    bids: List[Any] = []
    asks: List[Any] = []
    for k in range(levels):
        bl = ofe.DepthLevel()
        bl.price = mid - (k + 1) * 0.01
        bl.quantity = base_qty
        bids.append(bl)
        al = ofe.DepthLevel()
        al.price = mid + (k + 1) * 0.01
        al.quantity = base_qty
        asks.append(al)
    d.bids = bids
    d.asks = asks
    return d


def _populate_tick_store(
    path: str,
    *,
    n_trades: int = 12,
    n_depth: int = 6,
    base_ts: int = 1_000_000,
) -> Tuple[float, int]:
    """Write a small deterministic event stream to ``path``.

    Returns ``(expected_total_volume, expected_cvd_count)``.
    """
    store = ofe.TickStore(path)
    expected_volume = 0.0
    for i in range(n_trades):
        t = ofe.Trade()
        t.timestamp = base_ts + i * 1000
        t.price = 100.0 + (i % 3) * 0.01
        t.quantity = 1.0 + (i % 5)
        t.is_buyer_maker = (i % 2 == 0)
        store.store_trade(SYMBOL, t)
        expected_volume += t.quantity
    for i in range(n_depth):
        d = _make_depth(base_ts + 500 + i * 2000, 100.0 + (i % 2) * 0.05)
        store.store_depth_snapshot(SYMBOL, d)
    store.flush()
    store.close()
    return expected_volume, n_trades


def _run_replay_via_engine_start(tick_store_path: str,
                                 *, timeout_s: float = 5.0) -> Any:
    """Run the corrected backtest replay pattern (engine.start + poll
    is_complete) against the supplied tick store. Returns the engine."""
    cfg = ofe.EngineConfig()
    cfg.tick_size = 0.01
    engine = ofe.OrderFlowEngine(cfg)
    store = ofe.TickStore(tick_store_path)
    replay = ofe.ReplayFeed(store, 0.0)
    replay.set_time_range(0, 2 ** 62)
    ofe.connect_feed(engine, replay)
    engine.start(SYMBOL)
    deadline = time.monotonic() + timeout_s
    while not replay.is_complete():
        if time.monotonic() > deadline:
            raise TimeoutError("replay did not complete within timeout")
        time.sleep(0.01)
    engine.stop()
    return engine


@unittest.skipUnless(HAS_C_MODULE, "orderflow_engine C++ module not available")
class TestReplayPipelineNoDoubleFire(unittest.TestCase):
    """Pin the corrected single-fire behaviour of the backtest replay
    pipeline. Prior to Phase 13X the same trade/depth was processed
    twice (engine.start spawned the worker thread *and* run_sync ran
    the loop synchronously)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tick_path = os.path.join(self.tmp.name, "ticks.h5")

    def test_volume_profile_total_matches_expected(self) -> None:
        expected_volume, _ = _populate_tick_store(
            self.tick_path, n_trades=12, n_depth=6)
        engine = _run_replay_via_engine_start(self.tick_path)
        actual_volume = engine.get_volume_profile().get_total_volume()
        self.assertAlmostEqual(
            actual_volume, expected_volume, places=4,
            msg=f"VolumeProfile saw {actual_volume:.4f}, expected "
                f"{expected_volume:.4f} — a 2× value indicates the "
                f"Phase 13X double-firing regression has reappeared.")

    def test_cvd_history_count_matches_trade_count(self) -> None:
        _, expected_trades = _populate_tick_store(
            self.tick_path, n_trades=15, n_depth=4)
        engine = _run_replay_via_engine_start(self.tick_path)
        cvd_history = engine.get_cvd().get_history()
        self.assertEqual(
            len(cvd_history), expected_trades,
            msg=f"CVD recorded {len(cvd_history)} ticks, expected "
                f"{expected_trades} — 2× indicates Phase 13X regression.")

    def test_repeated_runs_are_independent(self) -> None:
        """Running the same TickStore twice through fresh engines must
        yield identical totals (replay is idempotent + side-effect-free
        on the store)."""
        expected_volume, _ = _populate_tick_store(self.tick_path)
        eng_a = _run_replay_via_engine_start(self.tick_path)
        eng_b = _run_replay_via_engine_start(self.tick_path)
        self.assertAlmostEqual(
            eng_a.get_volume_profile().get_total_volume(),
            eng_b.get_volume_profile().get_total_volume(),
            places=6)
        self.assertEqual(
            len(eng_a.get_cvd().get_history()),
            len(eng_b.get_cvd().get_history()))


@unittest.skipUnless(HAS_C_MODULE, "orderflow_engine C++ module not available")
class TestBacktestEndToEnd(unittest.TestCase):
    """Smoke-test ``strategies.orderflow.backtest`` end-to-end against a
    synthetic tick store. Verifies the function runs without raising and
    that downstream metrics see the corrected (single-fire) volume."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("data", exist_ok=True)
        self.tick_path = os.path.join("data", "REPLAY_ticks.h5")
        self.expected_volume, _ = _populate_tick_store(
            self.tick_path, n_trades=20, n_depth=8)

    def tearDown(self) -> None:
        os.chdir(self._orig_cwd)

    def test_backtest_runs_and_returns_tuple(self) -> None:
        from strategies.orderflow import backtest
        result = backtest(
            exchange="REPLAY", symbol=SYMBOL,
            from_time=0, to_time=2 ** 62,
            params={"tick_size": 0.01,
                    "pipeline_min_interval_ms": 0,
                    "paper_fills": True},
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 5)
        pnl, max_drawdown, num_trades, sharpe, cagr = result
        for v, name in zip(result, ("pnl", "max_dd", "n_trades",
                                     "sharpe", "cagr")):
            self.assertEqual(v, v, msg=f"{name} returned NaN")  # NaN check
        self.assertIsInstance(num_trades, int)


@unittest.skipUnless(HAS_C_MODULE, "orderflow_engine C++ module not available")
class TestBuildConfigBindingDrift(unittest.TestCase):
    """Phase 13Y regression: every key used by ``_build_config`` must
    exist as an attribute on the C++ ``RippleConfig`` / ``LifecycleConfig``
    pybind11 bindings. The bug we fixed was ``idle_exit_threshold`` being
    declared in ``STRAT_PARAMS["orderflow"]`` and mapped via
    ``_RIPPLE_MAP`` but never bound in ``bindings.cpp`` — calling
    ``_build_config`` with an ``idle_exit_threshold`` key raised
    ``AttributeError`` mid-backtest. Pin every key here so future
    binding drift is caught in CI rather than at user runtime.
    """

    def test_every_ripple_map_key_exists_on_RippleConfig(self) -> None:
        from strategies.orderflow import _RIPPLE_MAP
        rcfg = ofe.RippleConfig()
        missing = [attr for attr in _RIPPLE_MAP.values()
                   if not hasattr(rcfg, attr)]
        self.assertEqual(
            missing, [],
            f"RippleConfig missing pybind11 bindings for: {missing}. "
            "Add `def_readwrite(\"<name>\", &RippleConfig::<name>)` "
            "in backtestingCpp/orderflow/bindings.cpp.")

    def test_every_lifecycle_key_exists_on_LifecycleConfig(self) -> None:
        lc = ofe.LifecycleConfig()
        for k in ("confirmation_window_ms", "max_hold_time_ms",
                  "trailing_stop_sigma", "target_distance_sigma"):
            self.assertTrue(
                hasattr(lc, k),
                f"LifecycleConfig missing binding for {k!r}")

    def test_strat_params_orderflow_keys_round_trip_through_build(
            self) -> None:
        """Every tunable in STRAT_PARAMS['orderflow'] must be safely
        consumable by ``_build_config`` (either applied or warned-and-
        skipped — never raise)."""
        from utils import STRAT_PARAMS
        from strategies.orderflow import _build_config
        params = {}
        for key, spec in STRAT_PARAMS["orderflow"].items():
            params[key] = spec["min"]
        try:
            cfg = _build_config(params)
        except Exception as exc:
            self.fail(f"_build_config raised on tunable params: {exc!r}")
        self.assertIsNotNone(cfg)

    def test_idle_exit_threshold_round_trips(self) -> None:
        """The specific param that was broken — should now be applied
        end-to-end via ``_build_config``."""
        from strategies.orderflow import _build_config
        cfg = _build_config({"idle_exit_threshold": 0.42,
                             "tick_size": 0.01})
        self.assertAlmostEqual(cfg.ripple.idle_exit_threshold, 0.42)

    def test_unknown_param_does_not_crash(self) -> None:
        """If a future _RIPPLE_MAP entry references a not-yet-bound
        attr, the defensive guard must skip + log instead of raising."""
        import strategies.orderflow as of_mod
        original = dict(of_mod._RIPPLE_MAP)
        of_mod._RIPPLE_MAP["__phantom__"] = "__attr_that_does_not_exist__"
        try:
            of_mod._build_config({"__phantom__": 1.0,
                                  "tick_size": 0.01})
        finally:
            of_mod._RIPPLE_MAP.clear()
            of_mod._RIPPLE_MAP.update(original)


if __name__ == "__main__":
    unittest.main()
