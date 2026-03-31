"""
Phase 6: full-pipeline replay determinism test.

Feeds identical event sequences through OrderFlowEngine twice (or more)
and asserts identical StrategySnapshot output.  Tests multiple parameter
configurations to verify determinism across the full parameter space.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
    HAS_MODULE = hasattr(ofe, "OrderFlowEngine") and hasattr(ofe, "StrategySnapshot")
except ImportError:
    HAS_MODULE = False


def _make_depth(ts, mid, levels=10, base_qty=50.0):
    """Build a synthetic DepthUpdate snapshot."""
    d = ofe.DepthUpdate()
    d.timestamp = ts
    d.is_snapshot = True
    for i in range(levels):
        bl = ofe.DepthLevel()
        bl.price = mid - (i + 1) * 0.01
        bl.quantity = base_qty + (i % 3) * 10.0
        d.bids.append(bl)
        al = ofe.DepthLevel()
        al.price = mid + (i + 1) * 0.01
        al.quantity = base_qty + (i % 3) * 10.0
        d.asks.append(al)
    return d


def _make_trade(ts, price, qty, buyer_maker):
    t = ofe.Trade()
    t.timestamp = ts
    t.price = price
    t.quantity = qty
    t.is_buyer_maker = buyer_maker
    return t


def _build_event_sequence(n_events=200, base_price=100.0, start_ts=1_000_000):
    """Generate a deterministic sequence of depth + trade events."""
    events = []
    ts = start_ts
    import math
    for i in range(n_events):
        drift = 0.02 * math.sin(i * 0.1)
        mid = base_price + drift
        ts += 300
        events.append(("depth", _make_depth(ts, mid)))
        ts += 100
        side = (i % 3) != 0
        tp = mid + (0.01 if side else -0.01)
        events.append(("trade", _make_trade(ts, tp, 1.0 + (i % 7), side)))
    return events


def _make_config(**overrides):
    """Build EngineConfig with optional RippleConfig overrides."""
    cfg = ofe.EngineConfig()
    cfg.tick_size = overrides.pop("tick_size", 0.01)

    rcfg = cfg.ripple
    rcfg.tick_size = cfg.tick_size
    rcfg.pipeline_min_interval_ms = overrides.pop("pipeline_min_interval_ms", 0)
    rcfg.paper_fills = overrides.pop("paper_fills", True)
    rcfg.enable_diagnostics = False

    for k, v in overrides.items():
        if hasattr(rcfg, k):
            setattr(rcfg, k, v)

    cfg.ripple = rcfg
    return cfg


def _run_engine(config, events):
    """Feed events through a fresh OrderFlowEngine, return snapshots list."""
    engine = ofe.OrderFlowEngine(config)
    engine.start("")

    snapshots = []
    for kind, evt in events:
        if kind == "depth":
            engine.process_depth(evt)
        else:
            engine.process_trade(evt)
        snapshots.append(engine.get_strategy_snapshot())

    engine.stop()
    return snapshots


def _compare_snapshots(a, b):
    """Deep-compare two StrategySnapshot lists. Returns (equal, first_diff_idx, details)."""
    if len(a) != len(b):
        return False, 0, f"length mismatch: {len(a)} vs {len(b)}"

    for i, (sa, sb) in enumerate(zip(a, b)):
        diffs = []

        if sa.timestamp != sb.timestamp:
            diffs.append(f"timestamp: {sa.timestamp} vs {sb.timestamp}")
        if sa.risk.consumed_es != sb.risk.consumed_es:
            diffs.append(f"risk.consumed_es: {sa.risk.consumed_es} vs {sb.risk.consumed_es}")
        if sa.risk.es_budget != sb.risk.es_budget:
            diffs.append(f"risk.es_budget: {sa.risk.es_budget} vs {sb.risk.es_budget}")
        if sa.ripple.liquidity_state != sb.ripple.liquidity_state:
            diffs.append(f"ripple.liquidity_state: {sa.ripple.liquidity_state} vs {sb.ripple.liquidity_state}")
        if sa.ripple.microprice != sb.ripple.microprice:
            diffs.append(f"ripple.microprice: {sa.ripple.microprice} vs {sb.ripple.microprice}")
        if sa.ripple.imbalance != sb.ripple.imbalance:
            diffs.append(f"ripple.imbalance: {sa.ripple.imbalance} vs {sb.ripple.imbalance}")
        if sa.ripple.ofi != sb.ripple.ofi:
            diffs.append(f"ripple.ofi: {sa.ripple.ofi} vs {sb.ripple.ofi}")
        if sa.trade.state != sb.trade.state:
            diffs.append(f"trade.state: {sa.trade.state} vs {sb.trade.state}")
        if sa.trade.unrealized_pnl != sb.trade.unrealized_pnl:
            diffs.append(f"trade.unrealized_pnl: {sa.trade.unrealized_pnl} vs {sb.trade.unrealized_pnl}")
        if sa.wave.regime != sb.wave.regime:
            diffs.append(f"wave.regime: {sa.wave.regime} vs {sb.wave.regime}")
        fa = sa.features.ripple_features
        fb = sb.features.ripple_features
        if fa.top1_imbalance != fb.top1_imbalance:
            diffs.append(f"features.top1_imbalance: {fa.top1_imbalance} vs {fb.top1_imbalance}")
        if fa.short_horizon_volatility != fb.short_horizon_volatility:
            diffs.append(f"features.volatility: {fa.short_horizon_volatility} vs {fb.short_horizon_volatility}")

        if diffs:
            return False, i, "; ".join(diffs)

    return True, -1, ""


@unittest.skipUnless(HAS_MODULE, "orderflow_engine C++ module not available")
class TestReplayDeterminism(unittest.TestCase):
    """Replay determinism: same events + config → identical snapshots."""

    def test_default_config_determinism(self):
        events = _build_event_sequence(200)
        config = _make_config()
        snaps_a = _run_engine(config, events)
        snaps_b = _run_engine(config, events)
        ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
        self.assertTrue(ok, f"Divergence at event {idx}: {detail}")

    def test_determinism_with_tight_thresholds(self):
        events = _build_event_sequence(150)
        config = _make_config(
            absorption_entry=0.3,
            exhaustion_entry=0.3,
            idle_exit_threshold=0.2,
        )
        snaps_a = _run_engine(config, events)
        snaps_b = _run_engine(config, events)
        ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
        self.assertTrue(ok, f"Divergence at event {idx}: {detail}")

    def test_determinism_with_fast_pipeline(self):
        events = _build_event_sequence(300)
        config = _make_config(
            pipeline_min_interval_ms=0,
            feature_window_ms=2000,
        )
        snaps_a = _run_engine(config, events)
        snaps_b = _run_engine(config, events)
        ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
        self.assertTrue(ok, f"Divergence at event {idx}: {detail}")

    def test_determinism_with_paper_fills(self):
        events = _build_event_sequence(200)
        config = _make_config(paper_fills=True)
        snaps_a = _run_engine(config, events)
        snaps_b = _run_engine(config, events)
        ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
        self.assertTrue(ok, f"Divergence at event {idx}: {detail}")

    def test_determinism_without_paper_fills(self):
        events = _build_event_sequence(200)
        config = _make_config(paper_fills=False)
        snaps_a = _run_engine(config, events)
        snaps_b = _run_engine(config, events)
        ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
        self.assertTrue(ok, f"Divergence at event {idx}: {detail}")

    def test_triple_replay_identical(self):
        events = _build_event_sequence(100)
        config = _make_config()
        runs = [_run_engine(config, events) for _ in range(3)]
        for i in range(1, 3):
            ok, idx, detail = _compare_snapshots(runs[0], runs[i])
            self.assertTrue(ok, f"Run 0 vs {i} diverged at event {idx}: {detail}")

    def test_snapshot_fields_populated(self):
        events = _build_event_sequence(50)
        config = _make_config()
        snaps = _run_engine(config, events)
        last = snaps[-1]
        self.assertIsNotNone(last.risk)
        self.assertIsNotNone(last.trade)
        self.assertIsNotNone(last.ripple)
        self.assertGreater(last.timestamp, 0, "snapshot timestamp should be populated")
        self.assertGreater(last.ripple.timestamp, 0, "ripple.timestamp should be populated")
        self.assertGreater(last.features.timestamp, 0, "features.timestamp should be populated")


@unittest.skipUnless(HAS_MODULE, "orderflow_engine C++ module not available")
class TestMultiConfigDeterminism(unittest.TestCase):
    """Verify determinism across multiple parameter configurations."""

    CONFIGS = [
        {"tick_size": 0.01, "wall_min_relative_size": 3.0},
        {"tick_size": 0.01, "wall_min_relative_size": 5.0},
        {"tick_size": 0.01, "absorption_entry": 0.5, "breakout_entry": 0.6},
        {"tick_size": 0.01, "feature_window_ms": 3000, "volatility_window_ms": 5000},
    ]

    def test_each_config_is_deterministic(self):
        events = _build_event_sequence(100)
        for i, overrides in enumerate(self.CONFIGS):
            with self.subTest(config_idx=i):
                config = _make_config(**overrides)
                snaps_a = _run_engine(config, events)
                snaps_b = _run_engine(config, events)
                ok, idx, detail = _compare_snapshots(snaps_a, snaps_b)
                self.assertTrue(ok, f"Config {i} diverged at event {idx}: {detail}")


if __name__ == "__main__":
    unittest.main()
