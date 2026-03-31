"""Phase 5: Performance profiling of the full UI render cycle.

Measures timing of the key hot paths in the heatmap render pipeline
with bucketing, strategy overlays, and signal logging active:

1. Heatmap paintEvent (depth + bubbles + bucket axis + strategy overlay)
2. Bucket model update throughput (trade → OHLC assignment)
3. Signal log append throughput
4. HDF5 write throughput (signals, snapshots, events)
5. Strategy overlay rendering cost

All timing assertions use generous thresholds to avoid flaky failures
on CI/slow machines.  The goal is regression detection, not absolute
performance targets.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import SignalCategory, SignalEntry
from strategy_store import StrategyStore
from ui.heatmap_widget import HeatmapWidget
from ui.trade_blotter import TradeBlotter

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


# ====================================================================
# Helpers
# ====================================================================

def _populate_heatmap(hm, n_trades=2000, n_depth=200):
    """Feed realistic data into a heatmap widget for profiling."""
    base_ts = 1700000000000
    base_price = 50000.0

    for i in range(n_depth):
        ts = base_ts + i * 1500
        mid = base_price + (i % 20 - 10) * 2
        bids = [(mid - j * 5, 1.0 + j * 0.3) for j in range(1, 20)]
        asks = [(mid + j * 5, 1.0 + j * 0.3) for j in range(1, 20)]
        hm.add_depth_column(ts, bids, asks, mid - 5, mid + 5, sample_ts=ts)

    for i in range(n_trades):
        ts = base_ts + i * 150
        p = base_price + (i % 50 - 25) * 3
        q = 0.01 * (1 + i % 5)
        hm.add_trade(ts, p, q, i % 2 == 0)


class _FakeSnap:
    class _Wave:
        regime = "MEAN_REVERSION"
        trend_efficiency = 0.72
    class _Tide:
        bias = "LONG"
    class _Ripple:
        liquidity_state = "BALANCED"
        microprice = 100.12
        imbalance = 0.015
    class _Trade:
        state = "CONFIRMATION"
        archetype = "BOUNCE"
        unrealized_pnl = 0.05
        stop_price = 99.5
        target_price = 101.0
        hold_time_ms = 15000
        cumulative_pnl = 0.12
    class _Risk:
        consumed_es = 0.3
        es_budget = 1.0
    wave = _Wave()
    tide = _Tide()
    ripple = _Ripple()
    trade = _Trade()
    risk = _Risk()


# ====================================================================
# 1. Heatmap paintEvent timing
# ====================================================================

def test_heatmap_paint_time():
    """Full paintEvent with depth + bubbles + bucket axis + overlay < 100ms."""
    print("test_heatmap_paint_time")

    hm = HeatmapWidget()
    hm.resize(1200, 800)
    _populate_heatmap(hm)
    hm.set_strategy_overlay(50000.0, 49900.0, 50100.0)

    # Warm up
    hm.repaint()

    iterations = 10
    t0 = time.monotonic()
    for _ in range(iterations):
        hm.repaint()
    elapsed = (time.monotonic() - t0) * 1000.0
    avg_ms = elapsed / iterations

    print(f"  paintEvent avg: {avg_ms:.2f}ms ({iterations} iterations)")
    check(avg_ms < 100.0, f"paintEvent avg < 100ms (got {avg_ms:.2f}ms)")
    check(hm._last_paint_ms < 200.0,
          f"_last_paint_ms < 200ms (got {hm._last_paint_ms:.2f}ms)")


# ====================================================================
# 2. Bucket model update throughput
# ====================================================================

def test_bucket_update_throughput():
    """10,000 trade insertions into bucket model < 500ms."""
    print("test_bucket_update_throughput")

    hm = HeatmapWidget()
    hm.resize(800, 500)
    base_ts = 1700000000000
    n = 10_000

    t0 = time.monotonic()
    for i in range(n):
        ts = base_ts + i * 60
        p = 50000.0 + (i % 100 - 50) * 2
        q = 0.01 * (1 + i % 3)
        hm.add_trade(ts, p, q, i % 2 == 0)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    print(f"  {n} trades: {elapsed_ms:.1f}ms ({elapsed_ms / n * 1000:.1f}µs/trade)")
    check(elapsed_ms < 500.0, f"10k trades < 500ms (got {elapsed_ms:.1f}ms)")
    check(len(hm._buckets) >= 1, f"buckets created (got {len(hm._buckets)})")
    total_trades = sum(b.trade_count for b in hm._buckets)
    check(total_trades == n, f"all trades bucketed (got {total_trades})")


# ====================================================================
# 3. Signal log append throughput
# ====================================================================

def test_signal_log_throughput():
    """2,000 signal appends < 500ms."""
    print("test_signal_log_throughput")

    blotter = TradeBlotter()
    n = 2000
    base_ts = int(time.time() * 1000)

    t0 = time.monotonic()
    for i in range(n):
        entry = SignalEntry(
            timestamp=base_ts + i * 10,
            signal_type="RIPPLE_ENTER_BOUNCE_LONG",
            source="ripple",
            category=SignalCategory.RIPPLE_ENTRY,
            side="BUY",
            price=50000.0 + i,
            strength=0.8,
            description=f"signal {i}",
        )
        blotter.add_entry(entry)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    print(f"  {n} signals: {elapsed_ms:.1f}ms ({elapsed_ms / n * 1000:.1f}µs/signal)")
    check(elapsed_ms < 500.0, f"2k signals < 500ms (got {elapsed_ms:.1f}ms)")
    check(blotter._model.rowCount() == n,
          f"all signals in model (got {blotter._model.rowCount()})")


# ====================================================================
# 4. HDF5 write throughput
# ====================================================================

def test_hdf5_write_throughput():
    """100 signal writes + 100 snapshot buffers + flush + 20 events < 2s."""
    print("test_hdf5_write_throughput")

    tmpdir = tempfile.mkdtemp()
    store = StrategyStore("BTCUSDT", data_dir=tmpdir)
    base_ts = 1700000000000
    snap = _FakeSnap()

    t0 = time.monotonic()

    for i in range(100):
        entry = SignalEntry(
            timestamp=base_ts + i * 100,
            signal_type="RIPPLE_ENTRY",
            source="ripple",
            category=SignalCategory.RIPPLE_ENTRY,
            side="BUY",
            price=50000.0,
            strength=0.8,
            description=f"perf_signal_{i}",
        )
        store.write_signal(entry)

    for i in range(100):
        store.buffer_snapshot(base_ts + i * 500, snap)
    store.flush_snapshots()

    for i in range(20):
        store.write_event(base_ts + i * 1000, "TICK", {"i": i})

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    print(f"  100 signals + 100 snapshots + 20 events: {elapsed_ms:.1f}ms")

    check(elapsed_ms < 2000.0, f"total HDF5 writes < 2s (got {elapsed_ms:.1f}ms)")
    check(store.signal_count() == 100, f"100 signals persisted")
    check(store.snapshot_count() == 100, f"100 snapshots persisted")
    check(store.event_count() == 20, f"20 events persisted")

    store.close()


# ====================================================================
# 5. Strategy overlay rendering cost (marginal)
# ====================================================================

def test_overlay_marginal_cost():
    """Strategy overlay adds < 10ms to paintEvent on average."""
    print("test_overlay_marginal_cost")

    hm = HeatmapWidget()
    hm.resize(1200, 800)
    _populate_heatmap(hm, n_trades=1000, n_depth=100)

    # Measure without overlay
    hm.clear_strategy_overlay()
    hm.repaint()  # warm up

    iterations = 20
    t0 = time.monotonic()
    for _ in range(iterations):
        hm.repaint()
    base_ms = (time.monotonic() - t0) * 1000.0 / iterations

    # Measure with overlay
    hm.set_strategy_overlay(50000.0, 49900.0, 50100.0)
    hm.repaint()  # warm up

    t0 = time.monotonic()
    for _ in range(iterations):
        hm.repaint()
    overlay_ms = (time.monotonic() - t0) * 1000.0 / iterations

    marginal = overlay_ms - base_ms
    print(f"  base: {base_ms:.2f}ms, with overlay: {overlay_ms:.2f}ms, "
          f"marginal: {marginal:.2f}ms")
    check(marginal < 10.0,
          f"overlay marginal < 10ms (got {marginal:.2f}ms)")


# ====================================================================
# 6. Depth + bubble rendering with large trade set
# ====================================================================

def test_large_trade_set_render():
    """5000 trades + 500 depth columns renders in < 150ms."""
    print("test_large_trade_set_render")

    hm = HeatmapWidget()
    hm.resize(1200, 800)
    _populate_heatmap(hm, n_trades=5000, n_depth=500)
    hm.set_strategy_overlay(50000.0, 49900.0, 50100.0)

    hm.repaint()  # warm up

    iterations = 5
    t0 = time.monotonic()
    for _ in range(iterations):
        hm.repaint()
    avg_ms = (time.monotonic() - t0) * 1000.0 / iterations

    print(f"  5k trades + 500 depth: {avg_ms:.2f}ms avg paint")
    check(avg_ms < 150.0, f"large render < 150ms (got {avg_ms:.2f}ms)")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_heatmap_paint_time,
        test_bucket_update_throughput,
        test_signal_log_throughput,
        test_hdf5_write_throughput,
        test_overlay_marginal_cost,
        test_large_trade_set_render,
    ]
    for t in tests:
        t()
    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"Performance profile tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
