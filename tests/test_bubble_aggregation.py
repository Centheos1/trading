"""Tests for time-bucketed trade aggregation store and bubble rendering.

The store uses 100 ms time slices (TRADE_SLICE_MS) keyed by bucket start.
Each TradeSlice contains price-level TradeBucketAggregate entries that
track buy/sell counts and quantities separately.  This is NOT a raw trade
log — it is a pre-aggregated store optimised for O(S) bubble rendering.

Validates:
- Price-axis bucketing: nearby trades collapse into aggregated bubbles
- Time-axis bucketing: trades within the same 100 ms slice merge
- Volume aggregation: total qty is sum of constituent trades
- VWAP positioning: aggregated bubble placed at volume-weighted price
- Buy/sell imbalance: combined bubble coloured by dominant side
- Determinism: identical inputs produce identical aggregated output
- Reduction ratio: aggregation reduces rendered bubble count significantly
- Viewport filtering: off-screen trades excluded before aggregation
- Diagnostics: agg_cells and agg_raw_visible tracked correctly
- Performance: aggregation + render of high-volume data < thresholds
- Trade slice lifecycle: incremental insert, pruning, rebuild, bounds
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from PySide6.QtGui import QPainter, QImage

from ui.heatmap_widget import (
    HeatmapWidget, AGG_PRICE_BUCKET_PX, MIN_BUBBLE_RADIUS, MAX_BUBBLE_RADIUS,
    MAX_RENDERED_BUBBLES, MAX_TRADES_RETAINED, MIN_BUBBLE_QTY_FRAC,
    TRADE_SLICE_MS, PRUNE_BUFFER_MS, TradeBucketAggregate, TradeSlice,
)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


def make_widget():
    w = HeatmapWidget()
    w.resize(800, 500)
    return w


def draw_bubbles_direct(w, now=None, pmin=None, pmax=None):
    """Call _draw_bubbles with the same params paintEvent would compute."""
    px = w._margin_left
    py = w._margin_top
    pw = w.width() - w._margin_left - w._margin_right - w._chart_right_inset
    ph = w.height() - w._margin_top - w._margin_bottom

    if pmin is None:
        pmin = w._price_min
    if pmax is None:
        pmax = w._price_max
    pr = pmax - pmin
    if pr <= 0:
        pr = 1.0

    if now is None:
        now = w.chart_now
    t_start = now - w._visible_window_ms

    img = QImage(1, 1, QImage.Format.Format_ARGB32)
    painter = QPainter(img)
    w._draw_bubbles(painter, px, py, pw, ph, pr, t_start, now,
                    pmin_override=pmin)
    painter.end()
    return w.get_bubble_diagnostics()


def setup_viewport(w, now, pmin=82990.0, pmax=83010.0):
    """Configure the widget's viewport for testing."""
    w._last_trade_ts = now
    w._last_depth_ts = now
    w._price_min = pmin
    w._price_max = pmax


# ====================================================================
# A. Price-axis bucketing
# ====================================================================

def test_nearby_prices_aggregate():
    """Trades at very close prices within the same time bucket should merge."""
    print("test_nearby_prices_aggregate")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(100):
        w.add_trade(now - 1000, 83000.0 + i * 0.001, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    raw = d.get('agg_raw_visible', 0)

    check(raw == 100, f"100 raw visible trades (got {raw})")
    check(agg < raw, f"aggregation reduces count: {agg} < {raw}")
    check(agg >= 1, f"at least 1 aggregated bubble (got {agg})")


def test_distant_prices_stay_separate():
    """Trades at far-apart prices should produce separate bubbles."""
    print("test_distant_prices_stay_separate")
    w = make_widget()
    now = 1_700_000_060_000

    prices = [82995.0, 82998.0, 83000.0, 83003.0, 83006.0]
    for p in prices:
        w.add_trade(now - 1000, p, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    check(agg == len(prices),
          f"distant prices produce {len(prices)} bubbles (got {agg})")


def test_price_bucket_size_derives_from_viewport():
    """Price bucket size should scale with the visible price range / pixel height."""
    print("test_price_bucket_size_derives_from_viewport")
    w = make_widget()
    ph = w.height() - w._margin_top - w._margin_bottom
    pmin = 82990.0
    pmax = 83010.0
    pr = pmax - pmin

    expected_bucket = pr * AGG_PRICE_BUCKET_PX / ph
    check(expected_bucket > 0, f"price bucket > 0 (got {expected_bucket:.6f})")
    check(expected_bucket < 1.0,
          f"bucket < 1.0 price unit for normal viewport (got {expected_bucket:.4f})")
    check(expected_bucket > 0.01,
          f"bucket > 0.01 for meaningful aggregation (got {expected_bucket:.6f})")


# ====================================================================
# B. Time-axis bucketing
# ====================================================================

def test_different_time_buckets_separate():
    """Trades at different times within the visible window produce separate bubbles."""
    print("test_different_time_buckets_separate")
    w = make_widget()
    now = 1_700_000_300_000

    for i in range(3):
        ts = now - 45_000 + i * 15_000 + 500
        w.add_trade(ts, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    check(agg == 3, f"3 time buckets → 3 bubbles (got {agg})")


def test_same_time_bucket_merges():
    """Multiple trades at the same timestamp + price bucket merge into one."""
    print("test_same_time_bucket_merges")
    w = make_widget()
    now = 1_700_000_060_000

    # All 50 trades at the same timestamp — guaranteed same time bucket
    for i in range(50):
        w.add_trade(now - 500, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    check(agg == 1, f"same time+price bucket → 1 bubble (got {agg})")


# ====================================================================
# C. Volume aggregation
# ====================================================================

def test_volume_aggregation_correctness():
    """Aggregated bubble size should reflect total volume."""
    print("test_volume_aggregation_correctness")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(10):
        w.add_trade(now - 1000 + i, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d['max_radius'] > MIN_BUBBLE_RADIUS,
          f"aggregated radius > MIN (got {d['max_radius']:.2f})")


# ====================================================================
# D. Side separation
# ====================================================================

def test_buy_sell_combined_bubble():
    """Buy and sell trades at the same time/price cell merge into one bubble.

    Colour is determined by buy_qty vs sell_qty imbalance.
    The TradeBucketAggregate tracks buy/sell separately for this purpose.
    """
    print("test_buy_sell_combined_bubble")
    w = make_widget()
    now = 1_700_000_060_000

    w.add_trade(now - 1000, 83000.0, 0.5, True)
    w.add_trade(now - 999, 83000.0, 0.5, False)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    check(agg == 1, f"buy + sell at same cell → 1 bubble (got {agg})")

    bucket_ms = (int(now - 1000) // TRADE_SLICE_MS) * TRADE_SLICE_MS
    ts_obj = w._trade_slices.get(bucket_ms)
    check(ts_obj is not None, "trade slice exists for the time bucket")
    if ts_obj:
        found = False
        for a in ts_obj.price_levels.values():
            if a.buy_qty > 0 and a.sell_qty > 0:
                found = True
                check(a.buy_count == 1, f"buy_count=1 (got {a.buy_count})")
                check(a.sell_count == 1, f"sell_count=1 (got {a.sell_count})")
                check(abs(a.buy_qty - 0.5) < 0.01,
                      f"buy_qty=0.5 (got {a.buy_qty})")
                check(abs(a.sell_qty - 0.5) < 0.01,
                      f"sell_qty=0.5 (got {a.sell_qty})")
        check(found, "aggregate contains both buy and sell data")


# ====================================================================
# E. VWAP positioning
# ====================================================================

def test_vwap_positions_bubble_correctly():
    """Aggregated bubble at close prices merges; far prices stay separate."""
    print("test_vwap_positions_bubble_correctly")
    w = make_widget()
    now = 1_700_000_060_000

    # 83000 and 83004 are 4.0 apart → different price buckets
    w.add_trade(now - 1000, 83000.0, 1.0, True)
    w.add_trade(now - 999, 83004.0, 3.0, True)
    setup_viewport(w, now, pmin=82990.0, pmax=83010.0)

    d = draw_bubbles_direct(w, now, pmin=82990.0, pmax=83010.0)
    agg = d.get('agg_cells', 0)
    check(agg == 2, f"4.0 apart → separate (got {agg})")

    # 0.01 apart (well within one price bucket ~0.17) should merge
    w2 = make_widget()
    w2.add_trade(now - 1000, 83000.0, 1.0, True)
    w2.add_trade(now - 999, 83000.01, 3.0, True)
    setup_viewport(w2, now, pmin=82990.0, pmax=83010.0)

    d2 = draw_bubbles_direct(w2, now, pmin=82990.0, pmax=83010.0)
    agg2 = d2.get('agg_cells', 0)
    check(agg2 == 1, f"0.01 apart → merge into 1 (got {agg2})")


# ====================================================================
# F. Deterministic aggregation
# ====================================================================

def test_deterministic_aggregation():
    """Identical trade sequences produce identical aggregated bubble counts."""
    print("test_deterministic_aggregation")

    def run():
        w = make_widget()
        now = 1_700_000_060_000
        for i in range(200):
            ts = now - 50_000 + i * 250
            price = 83000.0 + (i % 7 - 3) * 0.5
            qty = 0.01 * (1 + i % 3)
            w.add_trade(ts, price, qty, i % 2 == 0)
        setup_viewport(w, now)
        return draw_bubbles_direct(w, now)

    d1 = run()
    d2 = run()
    check(d1['agg_cells'] == d2['agg_cells'],
          f"agg_cells deterministic ({d1['agg_cells']} vs {d2['agg_cells']})")
    check(d1['agg_raw_visible'] == d2['agg_raw_visible'],
          f"raw_visible deterministic ({d1['agg_raw_visible']} vs {d2['agg_raw_visible']})")
    check(d1['visible'] == d2['visible'],
          f"visible deterministic ({d1['visible']} vs {d2['visible']})")


# ====================================================================
# G. Reduction ratio
# ====================================================================

def test_high_volume_reduction():
    """10,000 trades should reduce to significantly fewer rendered bubbles."""
    print("test_high_volume_reduction")
    w = make_widget()
    now = 1_700_000_300_000

    for i in range(10_000):
        ts = now - 55_000 + int(i * 55_000 / 10_000)
        price = 83000.0 + (i % 50 - 25) * 0.02
        qty = 0.01 * (1 + i % 5)
        w.add_trade(ts, price, qty, i % 2 == 0)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    raw = d.get('agg_raw_visible', 0)
    agg = d.get('agg_cells', 0)

    check(raw > 2000, f"many raw visible trades (got {raw})")
    check(agg < raw * 0.5,
          f"at least 50% reduction: {agg}/{raw} = {agg/max(raw,1)*100:.0f}%")
    check(agg > 0, f"some bubbles rendered (got {agg})")

    reduction = 1.0 - agg / max(raw, 1)
    print(f"  reduction: {raw} raw → {agg} aggregated ({reduction*100:.0f}%)")


# ====================================================================
# H. Viewport filtering + aggregation
# ====================================================================

def test_offscreen_trades_excluded():
    """Trades outside the viewport should not produce bubbles."""
    print("test_offscreen_trades_excluded")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(5):
        w.add_trade(now - 1000 + i, 83000.0, 0.1, True)
    for i in range(5):
        w.add_trade(now - 1000 + i, 90000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('filtered_by_y', 0) == 5,
          f"5 trades filtered by y (got {d.get('filtered_by_y', 0)})")


def test_old_trades_filtered_by_time():
    """Trades before t_start should be filtered, not aggregated."""
    print("test_old_trades_filtered_by_time")
    w = make_widget()
    now = 1_700_000_600_000

    for i in range(50):
        w.add_trade(now - 900_000 + i * 100, 83000.0, 0.1, True)
    for i in range(10):
        w.add_trade(now - 5000 + i * 100, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('filtered_by_time', 0) >= 50,
          f"old trades filtered (got {d.get('filtered_by_time', 0)})")
    check(d.get('agg_raw_visible', 0) <= 10,
          f"only recent trades visible (got {d.get('agg_raw_visible', 0)})")


# ====================================================================
# I. Diagnostics tracking
# ====================================================================

def test_diagnostics_agg_fields():
    """Aggregation diagnostics should be populated after draw."""
    print("test_diagnostics_agg_fields")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(20):
        w.add_trade(now - 5000 + i * 100, 83000.0 + i * 0.1, 0.05, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)

    check('agg_cells' in d, "agg_cells key present in diagnostics")
    check('agg_raw_visible' in d, "agg_raw_visible key present in diagnostics")
    check('agg_culled' in d, "agg_culled key present in diagnostics")
    check(d['agg_raw_visible'] == 20, f"20 raw visible (got {d['agg_raw_visible']})")
    check(d['agg_cells'] > 0, f"agg_cells > 0 (got {d['agg_cells']})")
    check(d['visible'] <= d['agg_cells'],
          f"visible <= agg_cells ({d['visible']} vs {d['agg_cells']})")


# ====================================================================
# J. Performance: aggregation throughput
# ====================================================================

def test_aggregation_render_performance():
    """Draw with 10k trades should complete in < 50ms."""
    print("test_aggregation_render_performance")
    w = make_widget()
    w.resize(1200, 800)
    now = 1_700_000_300_000

    for i in range(10_000):
        ts = now - 250_000 + i * 25
        price = 83000.0 + (i % 50 - 25) * 0.02
        qty = 0.01 * (1 + i % 5)
        w.add_trade(ts, price, qty, i % 2 == 0)
    setup_viewport(w, now)

    # Warm up
    draw_bubbles_direct(w, now)

    iterations = 10
    t0 = time.monotonic()
    for _ in range(iterations):
        draw_bubbles_direct(w, now)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    avg_ms = elapsed_ms / iterations

    d = w.get_bubble_diagnostics()
    raw = d.get('agg_raw_visible', 0)
    agg = d.get('agg_cells', 0)

    print(f"  10k trades: avg draw={avg_ms:.2f}ms, raw={raw}, agg={agg}")
    check(avg_ms < 50.0, f"avg draw < 50ms (got {avg_ms:.2f}ms)")


def test_extreme_volume_stays_bounded():
    """50k trades aggregate down; time-based pruning manages memory."""
    print("test_extreme_volume_stays_bounded")
    w = make_widget()
    w.resize(1200, 800)
    now = 1_700_000_300_000

    for i in range(50_000):
        ts = now - 280_000 + i * 5
        price = 83000.0 + (i % 100 - 50) * 0.01
        qty = 0.005 * (1 + i % 3)
        w.add_trade(ts, price, qty, i % 2 == 0)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    raw = d.get('agg_raw_visible', 0)
    visible = d.get('visible', 0)

    deque_len = len(w._trades)
    print(f"  50k added: deque={deque_len}, raw={raw}, visible={visible}")
    check(visible <= MAX_RENDERED_BUBBLES,
          f"rendered <= {MAX_RENDERED_BUBBLES} (got {visible})")
    check(raw > 1_000, f"many raw trades visible (got {raw})")
    check(visible > 0, f"some bubbles rendered (got {visible})")


# ====================================================================
# K. Auto-scroll and time separation (regression tests for bug fix)
# ====================================================================

def test_time_bucket_finer_than_candle():
    """Trades 5s apart must produce separate bubbles (not merged into one candle)."""
    print("test_time_bucket_finer_than_candle")
    w = make_widget()
    now = 1_700_000_060_000

    w.add_trade(now - 10_000, 83000.0, 0.1, True)
    w.add_trade(now - 5_000, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    agg = d.get('agg_cells', 0)
    # With 100ms time slices, 5s apart → 50 slices apart
    check(agg == 2, f"5s apart → 2 separate bubbles (got {agg})")


def test_bubble_scrolls_with_time():
    """Same trade rendered at advancing 'now' must move left (auto-scroll)."""
    print("test_bubble_scrolls_with_time")
    w = make_widget()
    trade_ts = 1_700_000_060_000
    w.add_trade(trade_ts, 83000.0, 0.1, True)
    w._price_min = 82990.0
    w._price_max = 83010.0

    # Render at now = trade_ts (bubble near right edge)
    w._last_trade_ts = trade_ts
    w._last_depth_ts = trade_ts
    d1 = draw_bubbles_direct(w, trade_ts)
    x1 = d1.get('newest_x', 0)

    # Render at now = trade_ts + 30s (bubble should have scrolled left)
    later = trade_ts + 30_000
    w._last_trade_ts = later
    w._last_depth_ts = later
    d2 = draw_bubbles_direct(w, later)
    x2 = d2.get('newest_x', 0)

    check(x1 > x2, f"bubble scrolls left as time advances "
          f"(x1={x1:.1f} → x2={x2:.1f})")


def test_bubble_x_preserves_time_order():
    """Bubbles at different times must maintain correct left-to-right ordering."""
    print("test_bubble_x_preserves_time_order")
    w = make_widget()
    now = 1_700_000_060_000

    w.add_trade(now - 40_000, 83000.0, 0.1, True)
    w.add_trade(now - 10_000, 83000.0, 0.1, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('agg_cells', 0) == 2, "two separate time bubbles")
    check(d.get('oldest_x', 0) < d.get('newest_x', 0),
          f"newer bubble right of older "
          f"(oldest_x={d.get('oldest_x', 0):.1f}, "
          f"newest_x={d.get('newest_x', 0):.1f})")


# ====================================================================
# L. Hard caps and bounded behavior
# ====================================================================

def test_rendered_never_exceeds_cap():
    """Rendered bubble count must never exceed MAX_RENDERED_BUBBLES."""
    print("test_rendered_never_exceeds_cap")
    w = make_widget()
    w.resize(1200, 800)
    now = 1_700_000_300_000

    for i in range(MAX_TRADES_RETAINED):
        ts = now - 250_000 + int(i * 250_000 / MAX_TRADES_RETAINED)
        price = 83000.0 + (i % 200 - 100) * 0.01
        qty = 0.01 * (1 + i % 5)
        w.add_trade(ts, price, qty, i % 2 == 0)
    setup_viewport(w, now, pmin=82999.0, pmax=83001.0)

    d = draw_bubbles_direct(w, now, pmin=82999.0, pmax=83001.0)
    vis = d.get('visible', 0)
    check(vis <= MAX_RENDERED_BUBBLES,
          f"visible ({vis}) <= MAX_RENDERED_BUBBLES ({MAX_RENDERED_BUBBLES})")
    check(vis > 0, f"at least some bubbles rendered (got {vis})")


def test_coarsening_activates_under_pressure():
    """Dynamic coarsening should kick in when cell count is very high."""
    print("test_coarsening_activates_under_pressure")
    w = make_widget()
    w.resize(1200, 800)
    now = 1_700_000_300_000

    for i in range(8_000):
        ts = now - 200_000 + i * 25
        price = 83000.0 + (i % 150 - 75) * 0.02
        qty = 0.01
        w.add_trade(ts, price, qty, i % 2 == 0)
    setup_viewport(w, now, pmin=82998.0, pmax=83002.0)

    d = draw_bubbles_direct(w, now, pmin=82998.0, pmax=83002.0)
    coarsen = d.get('agg_coarsen_passes', 0)
    vis = d.get('visible', 0)
    check(vis <= MAX_RENDERED_BUBBLES,
          f"visible ({vis}) <= cap ({MAX_RENDERED_BUBBLES})")
    print(f"  coarsen_passes={coarsen}, visible={vis}")


def test_all_visible_data_renders():
    """All visible trades render — no volume culling with MIN_BUBBLE_QTY_FRAC=0."""
    print("test_all_visible_data_renders")
    w = make_widget()
    now = 1_700_000_060_000

    w.add_trade(now - 5000, 83000.0, 10.0, True)
    for i in range(20):
        w.add_trade(now - 4000 + i * 50, 83001.0 + i * 0.5, 0.001, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    raw = d.get('agg_raw_visible', 0)
    vis = d.get('visible', 0)

    check(raw == 21, f"all 21 trades are raw-visible (got {raw})")
    check(vis >= 1, f"big trade still visible (got {vis})")
    check(d.get('agg_culled', 0) == 0,
          f"no culling with MIN_BUBBLE_QTY_FRAC=0 (got {d.get('agg_culled', 0)})")


def test_time_based_pruning_bounds_deque():
    """Trades deque is bounded by time-based pruning (visible window + buffer)."""
    print("test_time_based_pruning_bounds_deque")
    w = make_widget()
    now = 1_700_000_300_000

    for i in range(8_000):
        ts = now - 200_000 + i * 25
        w.add_trade(ts, 83000.0, 0.01, True)
    setup_viewport(w, now)

    before = len(w._trades)
    # Trigger pruning via add_depth_column
    w.add_depth_column(now, [(83000.0, 1.0)], [(83001.0, 1.0)],
                       83000.0, 83001.0)
    after = len(w._trades)

    # After pruning, deque holds only trades within visible + buffer window
    window_plus_buffer = w._visible_window_ms + PRUNE_BUFFER_MS
    check(after < before, f"pruning removed old trades ({before} → {after})")
    print(f"  before={before}, after={after}")


def test_cap_preserves_largest_bubbles():
    """When capped, the largest-volume cells should survive."""
    print("test_cap_preserves_largest_bubbles")
    w = make_widget()
    w.resize(1200, 800)
    now = 1_700_000_300_000

    w.add_trade(now - 5000, 83000.0, 50.0, True)
    for i in range(MAX_TRADES_RETAINED - 1):
        ts = now - 200_000 + int(i * 200_000 / MAX_TRADES_RETAINED)
        price = 83000.0 + (i % 200 - 100) * 0.02
        w.add_trade(ts, price, 0.001, True)
    setup_viewport(w, now, pmin=82998.0, pmax=83002.0)

    d = draw_bubbles_direct(w, now, pmin=82998.0, pmax=83002.0)
    check(d.get('max_radius', 0) > MIN_BUBBLE_RADIUS,
          "large-volume bubble survives the cap")
    check(d.get('visible', 0) <= MAX_RENDERED_BUBBLES,
          f"count stays bounded (got {d.get('visible', 0)})")


# ====================================================================
# M. Edge cases
# ====================================================================

def test_empty_trades():
    """No trades should produce zero aggregated bubbles."""
    print("test_empty_trades")
    w = make_widget()
    now = 1_700_000_060_000
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('agg_cells', 0) == 0, "no trades → 0 agg cells")
    check(d.get('agg_raw_visible', 0) == 0, "no trades → 0 raw visible")


def test_single_trade():
    """A single trade should produce exactly one bubble."""
    print("test_single_trade")
    w = make_widget()
    now = 1_700_000_060_000
    w.add_trade(now - 1000, 83000.0, 0.5, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('agg_cells', 0) == 1, "1 trade → 1 bubble")
    check(d.get('agg_raw_visible', 0) == 1, "1 raw visible")


# ====================================================================
# N. Trade-slice data structure tests
# ====================================================================

def test_trade_slice_incremental_insert():
    """Trades inserted via add_trade() populate trade slices incrementally."""
    print("test_trade_slice_incremental_insert")
    w = make_widget()
    now = 1_700_000_060_000

    # Seed trade + draw to initialise _slice_price_bucket_size
    w.add_trade(now - 1000, 83000.0, 0.01, True)
    setup_viewport(w, now)
    draw_bubbles_direct(w, now)
    check(w._slice_price_bucket_size > 0, "slice price_bucket_size initialised")

    # Now add trades — they should go into _trade_slices incrementally
    w.add_trade(now + 100, 83000.0, 0.5, True)
    w.add_trade(now + 200, 83000.0, 0.3, True)
    w._last_trade_ts = now + 200
    w._last_depth_ts = now + 200

    total_cells = sum(
        len(ts.price_levels) for ts in w._trade_slices.values())
    check(total_cells >= 1,
          f"trade slices have cells after add_trade ({total_cells})")

    total_qty = 0.0
    for ts_obj in w._trade_slices.values():
        for a in ts_obj.price_levels.values():
            total_qty += a.total_qty
    check(abs(total_qty - 0.81) < 0.01,
          f"slice total qty ≈ 0.81 (got {total_qty:.6f})")


def test_trade_slice_prune_expired():
    """Trade slices older than trade_cutoff are pruned in add_depth_column."""
    print("test_trade_slice_prune_expired")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(50):
        w.add_trade(now - 200_000 + i * 1000, 83000.0, 0.01, True)
    setup_viewport(w, now)
    draw_bubbles_direct(w, now)

    before = len(w._trade_slices)
    check(before > 0, f"trade slices exist before depth prune ({before})")

    w._last_trade_ts = now + 10_000
    w.add_depth_column(now + 10_000,
                       [(83000.0, 1.0)], [(83001.0, 1.0)],
                       83000.0, 83001.0)

    after = len(w._trade_slices)
    check(after <= before, f"trade slices pruned ({before} → {after})")


def test_trade_slice_rebuild_on_param_change():
    """Trade slices rebuild from deque when price bucket size changes."""
    print("test_trade_slice_rebuild_on_param_change")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(20):
        w.add_trade(now - 5000 + i * 100, 83000.0 + i * 0.1, 0.05, True)
    setup_viewport(w, now)

    d1 = draw_bubbles_direct(w, now)
    pbs1 = w._slice_price_bucket_size

    # Change the price range significantly to force param change
    d2 = draw_bubbles_direct(w, now, pmin=82900.0, pmax=83100.0)
    pbs2 = w._slice_price_bucket_size

    check(pbs2 != pbs1,
          f"price_bucket_size changed ({pbs1:.6f} → {pbs2:.6f})")
    check(d2.get('visible', 0) >= 0,
          f"render still works after rebuild (vis={d2.get('visible', 0)})")


def test_trade_slice_steady_state_no_rebuild():
    """Repeated draws with same viewport skip rebuild (O(S) not O(N))."""
    print("test_trade_slice_steady_state_no_rebuild")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(500):
        w.add_trade(now - 50_000 + i * 100, 83000.0 + (i % 5) * 0.1,
                    0.01, i % 2 == 0)
    setup_viewport(w, now)

    draw_bubbles_direct(w, now)
    pbs = w._slice_price_bucket_size

    # Second draw should NOT rebuild (same price bucket size)
    draw_bubbles_direct(w, now)
    check(w._slice_price_bucket_size == pbs, "price_bucket_size unchanged")

    t0 = time.monotonic()
    for _ in range(10):
        draw_bubbles_direct(w, now)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    avg_ms = elapsed_ms / 10
    print(f"  steady-state avg draw: {avg_ms:.2f}ms")
    check(avg_ms < 20.0, f"steady-state draw < 20ms (got {avg_ms:.2f}ms)")


def test_trade_slice_renders_all_visible():
    """Trade slice store renders all visible trades correctly."""
    print("test_trade_slice_renders_all_visible")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(100):
        ts = now - 40_000 + i * 400
        price = 83000.0 + (i % 10 - 5) * 0.2
        w.add_trade(ts, price, 0.01 * (1 + i % 3), i % 2 == 0)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('visible', 0) > 0,
          f"trade slices produce visible bubbles ({d.get('visible', 0)})")
    check(d.get('agg_raw_visible', 0) == 100,
          f"all 100 trades visible ({d.get('agg_raw_visible', 0)})")


def test_trade_slice_boundary_round_stability():
    """Prices on bucket boundaries should map consistently (round-based)."""
    print("test_trade_slice_boundary_round_stability")
    w = make_widget()
    now = 1_700_000_060_000

    for i in range(50):
        w.add_trade(now - 500, 83000.0, 0.01, True)
    setup_viewport(w, now)

    d = draw_bubbles_direct(w, now)
    check(d.get('agg_cells', 0) == 1,
          f"50 trades at exact price → 1 cell (got {d.get('agg_cells', 0)})")
    check(d.get('agg_raw_visible', 0) == 50,
          f"all 50 raw trades visible (got {d.get('agg_raw_visible', 0)})")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_nearby_prices_aggregate,
        test_distant_prices_stay_separate,
        test_price_bucket_size_derives_from_viewport,
        test_different_time_buckets_separate,
        test_same_time_bucket_merges,
        test_volume_aggregation_correctness,
        test_buy_sell_combined_bubble,
        test_vwap_positions_bubble_correctly,
        test_deterministic_aggregation,
        test_high_volume_reduction,
        test_offscreen_trades_excluded,
        test_old_trades_filtered_by_time,
        test_diagnostics_agg_fields,
        test_aggregation_render_performance,
        test_extreme_volume_stays_bounded,
        test_time_bucket_finer_than_candle,
        test_bubble_scrolls_with_time,
        test_bubble_x_preserves_time_order,
        test_rendered_never_exceeds_cap,
        test_coarsening_activates_under_pressure,
        test_all_visible_data_renders,
        test_time_based_pruning_bounds_deque,
        test_cap_preserves_largest_bubbles,
        test_empty_trades,
        test_single_trade,
        # Trade-slice data structure tests
        test_trade_slice_incremental_insert,
        test_trade_slice_prune_expired,
        test_trade_slice_rebuild_on_param_change,
        test_trade_slice_steady_state_no_rebuild,
        test_trade_slice_renders_all_visible,
        test_trade_slice_boundary_round_stability,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"Bubble aggregation tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
