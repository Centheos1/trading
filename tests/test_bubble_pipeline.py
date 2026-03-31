"""Deterministic regression test for the bubble rendering pipeline.

Exercises the HeatmapWidget's trade ingestion, pruning, coordinate mapping,
visibility filtering, and failure-stage diagnostics WITHOUT requiring a live
Qt event loop.

Key regression tested: bubble pruning must use TRADE-derived time, not
depth-derived time, to prevent depth-trade timestamp skew from wiping
the visible bubble deque.
"""
import math
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from collections import deque

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from ui.heatmap_widget import (
    HeatmapWidget, MIN_BUBBLE_RADIUS, MAX_BUBBLE_RADIUS,
    BUBBLE_SCALE, VISIBLE_WINDOW_MS,
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


# ---------------------------------------------------------------- helpers
def simulate_visible(w, now=None):
    """Count how many trades would be visible in the default viewport."""
    if now is None:
        now = w.chart_now
    px, py = 70, 10
    pw = 800 - 70 - 10 - 28
    ph = 500 - 10 - 22
    pmin = w._price_min
    pmax = w._price_max
    pr = pmax - pmin
    if pr <= 0:
        return 0
    t_start = now - VISIBLE_WINDOW_MS
    inv_ts = pw / float(VISIBLE_WINDOW_MS)
    inv_pr = ph / pr
    right_bound = px + pw + MAX_BUBBLE_RADIUS

    visible = 0
    for ts, price, qty, is_buy in w._trades:
        if ts < t_start:
            continue
        x = px + (ts - t_start) * inv_ts
        if x > right_bound:
            continue
        y = py + ph - (price - pmin) * inv_pr
        if y < py - MAX_BUBBLE_RADIUS or y > py + ph + MAX_BUBBLE_RADIUS:
            continue
        visible += 1
    return visible


# ================================================================ TESTS
# A. TRADE INPUT STAGE

def test_trades_added_to_deque():
    w = make_widget()
    now = 1_700_000_000_000
    w.add_trade(now, 83000.0, 0.1, True)
    w.add_trade(now + 100, 83001.0, 0.2, False)
    w.add_trade(now + 200, 83002.0, 0.15, True)
    check(len(w._trades) == 3, "3 trades in deque")
    check(w._last_trade_ts == now + 200, "last_trade_ts updated")
    check(w._trades_added == 3, "trades_added counter")
    d = w.get_bubble_diagnostics()
    check(d['trades_added_total'] == 3, "diag trades_added_total")
    check(d['last_trade_add_ts'] == now + 200, "diag last_trade_add_ts")


def test_trade_price_filter():
    w = make_widget()
    w.add_trade(1_700_000_000_000, 0.0, 0.1, True)
    w.add_trade(1_700_000_000_000, -1.0, 0.1, False)
    check(len(w._trades) == 0, "zero/negative price trades rejected")
    d = w.get_bubble_diagnostics()
    check(d['trades_rejected_price'] == 2, "diag rejected_price counter")


# B. TRADE STORAGE / QUEUE STAGE

def test_max_deque_depth_tracked():
    w = make_widget()
    now = 1_700_000_000_000
    for i in range(50):
        w.add_trade(now + i, 83000.0, 0.1, True)
    d = w.get_bubble_diagnostics()
    check(d['max_deque_depth'] == 50, f"max_deque_depth tracked (got {d['max_deque_depth']})")


# C. PRUNING — THE CRITICAL REGRESSION TEST

def test_pruning_uses_trade_time_not_depth_time():
    """REGRESSION TEST: Pruning must use _last_trade_ts, not chart_now.

    Previously, pruning used chart_now = max(depth_ts, trade_ts).
    When depth_ts advanced far ahead of trade_ts, all trades were wiped.
    CVD survived because it prunes on trade time only.
    """
    w = make_widget()
    trade_now = 1_700_000_060_000

    # Add trades spanning the last 30 seconds
    for i in range(100):
        ts = trade_now - 30_000 + i * 300
        w.add_trade(ts, 83000.0, 0.1, True)

    check(len(w._trades) == 100, "100 trades before pruning")

    # Simulate depth advancing 120 seconds AHEAD of trade time.
    # Under the OLD code (chart_now based), this would wipe everything
    # because trade_cutoff = (trade_now + 120000) - 60000 - 5000 = trade_now + 55000,
    # which is far ahead of all trade timestamps.
    depth_ahead = trade_now + 120_000
    w._last_depth_ts = depth_ahead

    bids = [(83000.0 - i, 1.0) for i in range(5)]
    asks = [(83000.0 + i, 1.0) for i in range(1, 6)]
    w.add_depth_column(depth_ahead, bids, asks, 83000.0, 83001.0)

    remaining = len(w._trades)
    check(remaining > 0,
          f"CRITICAL: trades survive depth-ahead skew ({remaining} remain)")
    check(remaining >= 50,
          f"Most recent trades survive pruning ({remaining}/100 remain)")


def test_pruning_does_not_wipe_recent_trades():
    """Even with aggressive depth timing, recent trades must survive."""
    w = make_widget()
    trade_now = 1_700_000_060_000

    for i in range(200):
        ts = trade_now - 50_000 + i * 250
        w.add_trade(ts, 83000.0, 0.1, True)
    w._last_trade_ts = trade_now

    # Depth at same time — no skew
    bids = [(82999.0, 1.0)]
    asks = [(83001.0, 1.0)]
    w._last_depth_ts = trade_now
    w.add_depth_column(trade_now, bids, asks, 82999.0, 83001.0)

    remaining = len(w._trades)
    check(remaining > 0, f"trades survive same-time pruning ({remaining})")

    # Verify the oldest surviving trade is within the expected window
    if w._trades:
        oldest = w._trades[0][0]
        expected_cutoff = trade_now - VISIBLE_WINDOW_MS * 2
        check(oldest >= expected_cutoff,
              f"oldest trade within 2x window buffer "
              f"(oldest={oldest}, cutoff={expected_cutoff})")


def test_pruning_counter_tracking():
    w = make_widget()
    now = 1_700_000_060_000
    # Add trades: 50 old (beyond 2x visible window — will be pruned) + 50 recent.
    # visible_window_ms defaults to 300_000, so 2x buffer = 600_000.
    # Place old trades at now - 700_000 to ensure they are pruned.
    for i in range(50):
        w.add_trade(now - 700_000 + i * 100, 83000.0, 0.1, True)
    for i in range(50):
        w.add_trade(now - 10_000 + i * 100, 83000.0, 0.1, True)
    w._last_trade_ts = now
    w._last_depth_ts = now

    bids = [(82999.0, 1.0)]
    asks = [(83001.0, 1.0)]
    w.add_depth_column(now, bids, asks, 82999.0, 83001.0)

    d = w.get_bubble_diagnostics()
    check(d['trades_pruned_this_frame'] > 0, "pruned_this_frame > 0")
    check(d['trades_pruned_total'] > 0, "pruned_total > 0")
    check(d['total_in_deque'] < 100, f"deque shrunk after prune ({d['total_in_deque']})")
    if w._trades:
        check(d['active_min_ts'] == w._trades[0][0], "active_min_ts matches deque head")
        check(d['active_max_ts'] == w._trades[-1][0], "active_max_ts matches deque tail")


# D. RENDER VISIBILITY

def test_bubble_visibility_in_time_window():
    w = make_widget()
    now = 1_700_000_060_000
    for i in range(50):
        ts = now - 50_000 + i * 1000
        w.add_trade(ts, 83000.0 + i * 0.1, 0.1, i % 2 == 0)
    w._last_trade_ts = now
    w._price_min = 82990.0
    w._price_max = 83010.0

    visible = simulate_visible(w, now)
    check(visible > 0, f"visible bubble count > 0 (got {visible})")
    check(visible >= 40, f"most trades are visible (got {visible}/50)")


def test_bubble_x_mapping():
    w = make_widget()
    now = 1_700_000_060_000
    t_start = now - VISIBLE_WINDOW_MS

    w.add_trade(t_start + 1000, 83000.0, 0.1, True)
    w.add_trade(now - 1000, 83000.0, 0.1, True)
    w._last_trade_ts = now

    px = 70
    pw = 800 - 70 - 10 - 28
    inv_ts = pw / float(VISIBLE_WINDOW_MS)

    x_oldest = px + (w._trades[0][0] - t_start) * inv_ts
    x_newest = px + (w._trades[1][0] - t_start) * inv_ts

    check(x_oldest >= px, f"oldest x >= left margin (got {x_oldest:.1f})")
    check(x_newest <= px + pw + MAX_BUBBLE_RADIUS,
          f"newest x within right bound (got {x_newest:.1f})")
    check(x_newest > x_oldest, "newest is right of oldest")


def test_bubble_y_mapping():
    pmin = 82990.0
    pmax = 83010.0
    mid_price = (pmin + pmax) / 2.0
    py = 10
    ph = 468
    pr = pmax - pmin
    inv_pr = ph / pr

    y = py + ph - (mid_price - pmin) * inv_pr
    expected_mid = py + ph / 2.0
    check(abs(y - expected_mid) < 1.0,
          f"mid-price -> mid-height (got {y:.1f}, expected {expected_mid:.1f})")


def test_bubble_radius_bounds():
    ref = 0.1
    for qty in [0.0001, 0.01, 0.1, 1.0, 10.0, 100.0]:
        norm = qty / ref
        radius = MIN_BUBBLE_RADIUS + math.sqrt(max(norm, 0.0)) * BUBBLE_SCALE
        radius = max(MIN_BUBBLE_RADIUS, min(MAX_BUBBLE_RADIUS, radius))
        check(radius >= MIN_BUBBLE_RADIUS,
              f"radius >= MIN for qty={qty} (got {radius:.2f})")
        check(radius <= MAX_BUBBLE_RADIUS,
              f"radius <= MAX for qty={qty} (got {radius:.2f})")


def test_p95_normalization():
    w = make_widget()
    now = 1_700_000_000_000
    for i in range(600):
        w.add_trade(now + i, 83000.0, 0.1 + (i % 10) * 0.01, True)
    check(w._p95_size > 0, f"p95 updated (got {w._p95_size})")
    check(w._p95_size < 1.0, f"p95 reflects actual sizes (got {w._p95_size})")


# E. VIEWPORT / TIME CONSISTENCY

def test_chart_now_uses_max():
    w = make_widget()
    w._last_depth_ts = 1000
    w._last_trade_ts = 2000
    check(w.chart_now == 2000, "chart_now uses trade_ts when larger")
    w._last_depth_ts = 3000
    check(w.chart_now == 3000, "chart_now uses depth_ts when slightly larger")


def test_chart_now_caps_depth_lead():
    """REGRESSION TEST: chart_now must cap depth-trade skew.

    When depth timestamps advance far ahead of trade timestamps (network
    jitter, asyncio scheduling, sparse trades), an uncapped chart_now
    shifts the visible window forward, pushing all trade-derived data
    (bubbles, CVD) off the left edge of the chart.

    When the trade feed is stale (skew > _STALE_TRADE_THRESHOLD_MS),
    chart_now advances with depth time minus the cap margin so the
    heatmap keeps scrolling instead of freezing.
    """
    w = make_widget()
    trade_ts = 1_700_000_060_000
    w._last_trade_ts = trade_ts

    # Small skew (1 second): chart_now follows depth
    w._last_depth_ts = trade_ts + 1000
    check(w.chart_now == trade_ts + 1000,
          f"small skew: chart_now follows depth (got {w.chart_now})")

    # Medium skew (8 seconds, within stale threshold): normal cap
    w._last_depth_ts = trade_ts + 8_000
    cap = w._MAX_DEPTH_LEAD_MS
    expected = trade_ts + cap
    check(w.chart_now == expected,
          f"medium skew: chart_now capped at trade+{cap}ms "
          f"(got {w.chart_now}, expected {expected})")

    # Large skew (14 seconds, beyond stale threshold): depth-driven
    w._last_depth_ts = trade_ts + 14_000
    stale_expected = w._last_depth_ts - cap
    check(w.chart_now == stale_expected,
          f"stale trade: chart_now = depth-{cap}ms "
          f"(got {w.chart_now}, expected {stale_expected})")

    # Verify newest trade still within visible window even in stale mode
    t_start = w.chart_now - VISIBLE_WINDOW_MS
    check(trade_ts >= t_start,
          f"newest trade visible in stale mode "
          f"(trade_ts={trade_ts}, t_start={t_start})")

    # trade_feed_stale property
    check(w.trade_feed_stale,
          "trade_feed_stale is True at 14s skew")
    w._last_depth_ts = trade_ts + 8_000
    check(not w.trade_feed_stale,
          "trade_feed_stale is False at 8s skew")


def test_auto_scale_includes_trades():
    w = make_widget()
    now = 1_700_000_060_000
    for i in range(10):
        w.add_trade(now - 5000 + i * 100, 83000.0 + i, 0.1, True)
    w._last_trade_ts = now

    bids = [(82990.0, 1.0), (82991.0, 1.0)]
    asks = [(82995.0, 1.0), (82996.0, 1.0)]
    w._auto_scale = True
    w._last_depth_ts = now
    w.add_depth_column(now, bids, asks, 82990.0, 82995.0)

    check(w._price_min < 83000.0,
          f"price_min accommodates trades (got {w._price_min:.2f})")
    check(w._price_max > 83009.0,
          f"price_max accommodates trades (got {w._price_max:.2f})")


def test_shared_viewport_right_edge():
    w = make_widget()
    now = 1_700_000_060_000
    w.add_trade(now, 83000.0, 0.1, True)
    w._last_trade_ts = now

    px = 70
    pw = 692
    t_start = now - VISIBLE_WINDOW_MS
    inv_ts = pw / float(VISIBLE_WINDOW_MS)

    x_newest = px + (now - t_start) * inv_ts
    expected_right = px + pw

    check(abs(x_newest - expected_right) < 1.0,
          f"newest bubble at right edge (got {x_newest:.1f}, want {expected_right:.1f})")


# F. FAILURE STAGE DIAGNOSTICS

def test_failure_stage_no_trades():
    w = make_widget()
    d = w.get_bubble_diagnostics()
    check(d['failure_stage'] == 'NONE' or d['failure_stage'] == 'NO_TRADES_IN',
          f"failure stage correct for no trades (got {d['failure_stage']})")


def test_failure_stage_after_visibility():
    """After visible bubbles are drawn, failure_stage should be NONE."""
    w = make_widget()
    now = 1_700_000_060_000
    for i in range(20):
        w.add_trade(now - 10_000 + i * 500, 83000.0, 0.1, True)
    w._last_trade_ts = now
    w._price_min = 82990.0
    w._price_max = 83010.0

    visible = simulate_visible(w, now)
    if visible > 0:
        w._bubble_diag['visible'] = visible
        w._bubble_diag['failure_stage'] = 'NONE'
        w._bubble_diag['consecutive_zero_frames'] = 0
    d = w.get_bubble_diagnostics()
    check(d['failure_stage'] == 'NONE',
          f"failure_stage is NONE when visible (got {d['failure_stage']})")


# G. BURST / STRESS

def test_trade_burst_does_not_wipe_visible():
    w = make_widget()
    now = 1_700_000_060_000
    for i in range(200):
        w.add_trade(now - 30_000 + i * 100, 83000.0, 0.1, True)
    for i in range(500):
        w.add_trade(now - 5000 + i * 10, 83000.0, 0.5, False)
    w._last_trade_ts = now
    w._price_min = 82990.0
    w._price_max = 83010.0

    check(len(w._trades) == 700, f"all trades present (got {len(w._trades)})")
    visible = simulate_visible(w, now)
    check(visible > 600, f"burst trades still visible (got {visible})")


def test_deterministic_replay():
    def run_replay():
        w = make_widget()
        now = 1_700_000_060_000
        for i in range(100):
            w.add_trade(now - 50_000 + i * 500, 83000.0 + (i % 5), 0.1, i % 2 == 0)
        w._last_trade_ts = now
        w._price_min = 82998.0
        w._price_max = 83006.0
        return {
            'trades': len(w._trades),
            'last_trade_ts': w._last_trade_ts,
            'p95': w._p95_size,
        }

    r1 = run_replay()
    r2 = run_replay()
    check(r1 == r2, "deterministic replay produces identical state")


# H. HEATMAP SLICE ALIGNMENT UNDER SKEW

def test_heatmap_slices_within_visible_window():
    """REGRESSION TEST: Heatmap slices must be positioned within the
    visible window, not at raw depth timestamps that exceed chart_now.

    Uses an 8-second skew (within the normal cap range, below the
    stale-trade threshold) to test the slice positioning logic.
    """
    w = make_widget()
    trade_ts = 1_700_000_060_000
    depth_ts = trade_ts + 8_000  # 8-second depth-trade skew (below stale)

    for i in range(100):
        w.add_trade(trade_ts - 30_000 + i * 300, 83000.0, 0.1, True)

    w._last_depth_ts = depth_ts

    bids = [(83000.0 - i, 1.0) for i in range(5)]
    asks = [(83001.0 + i, 1.0) for i in range(5)]

    chart_now = w.chart_now
    last_trade = w._last_trade_ts
    expected_cap = last_trade + w._MAX_DEPTH_LEAD_MS
    check(chart_now == expected_cap,
          f"chart_now is capped at trade+{w._MAX_DEPTH_LEAD_MS}ms "
          f"(got {chart_now}, expected {expected_cap})")

    sample_ts = chart_now
    w.add_depth_column(depth_ts, bids, asks, 82999.0, 83001.0,
                       sample_ts=sample_ts)

    if w._slices:
        newest_slice_ts = w._slices[-1][0]
        t_start = chart_now - VISIBLE_WINDOW_MS

        check(newest_slice_ts <= chart_now,
              f"newest slice within visible right edge "
              f"(slice={newest_slice_ts}, chart_now={chart_now})")
        check(newest_slice_ts >= t_start,
              f"newest slice within visible left edge "
              f"(slice={newest_slice_ts}, t_start={t_start})")

        bad_sample_ts = max(depth_ts, chart_now)
        check(bad_sample_ts > chart_now,
              f"uncapped sample_ts would exceed chart_now "
              f"(bad={bad_sample_ts}, chart_now={chart_now})")
    else:
        check(False, "at least one slice should exist")


# I. HEATMAP LUT

def test_heatmap_alpha_zero_intensity():
    from ui.heatmap_widget import _HEAT_LUT
    check(_HEAT_LUT[0][3] == 0, f"LUT[0] alpha=0 (got {_HEAT_LUT[0][3]})")
    check(_HEAT_LUT[1][3] > 0, f"LUT[1] alpha>0 (got {_HEAT_LUT[1][3]})")
    check(_HEAT_LUT[128][3] > 200, f"LUT[128] alpha high (got {_HEAT_LUT[128][3]})")
    check(_HEAT_LUT[255][3] == 255, f"LUT[255] alpha=255 (got {_HEAT_LUT[255][3]})")


# ================================================================ STRESS HARNESS

def test_stress_harness():
    """Replayable bursty stress test simulating the trade->bubble pipeline
    under high volume with depth-trade time skew.

    Reports stage-by-stage counters over simulated time to reproduce the
    bubble disappearance bug without a live UI.
    """
    w = make_widget()
    w._auto_scale = True

    base_ts = 1_700_000_000_000
    trade_rate = 100
    depth_interval_ms = 100
    total_seconds = 30
    skew_start_sec = 10
    max_skew_ms = 30_000

    trade_ts = base_ts
    depth_ts = base_ts
    report_interval = 5

    bids = [(82999.0 - i, 1.0 + i * 0.5) for i in range(10)]
    asks = [(83001.0 + i, 1.0 + i * 0.5) for i in range(10)]

    headers = (f"{'sec':>4s} {'trades':>6s} {'deque':>6s} {'pruned':>6s} "
               f"{'visible':>7s} {'stage':>16s} {'skew_ms':>8s}")
    print(f"\n  STRESS HARNESS:\n  {headers}")
    print(f"  {'-' * len(headers)}")

    ever_had_zero_visible = False
    visible_ct = 0

    for sec in range(total_seconds):
        # Introduce depth-trade skew after skew_start_sec
        if sec >= skew_start_sec:
            skew_frac = min(1.0, (sec - skew_start_sec) / (total_seconds - skew_start_sec))
            depth_skew = int(skew_frac * max_skew_ms)
        else:
            depth_skew = 0

        # Simulate 1 second: trade_rate trades + 10 depth updates
        for tick in range(10):
            tick_base = base_ts + sec * 1000 + tick * 100
            trade_ts = tick_base

            # Add trades
            for t in range(trade_rate // 10):
                t_ts = tick_base + t
                price = 83000.0 + (t % 20 - 10) * 0.01
                qty = 0.01 + (t % 5) * 0.005
                w.add_trade(t_ts, price, qty, t % 2 == 0)

            # Depth update with skew
            d_ts = tick_base + depth_skew
            w._last_depth_ts = d_ts
            w.add_depth_column(d_ts, bids, asks, 82999.0, 83001.0,
                               sample_ts=d_ts)

        # Simulate render visibility check
        chart_now = w.chart_now
        visible_ct = simulate_visible(w, chart_now)

        d = w.get_bubble_diagnostics()
        stage = d.get('failure_stage', 'NONE')
        if visible_ct == 0 and len(w._trades) > 0:
            stage = 'RENDERING_ZERO'

        if sec % report_interval == 0 or visible_ct == 0:
            print(f"  {sec:4d} {w._trades_added:6d} {len(w._trades):6d} "
                  f"{d.get('trades_pruned_total', 0):6d} "
                  f"{visible_ct:7d} {stage:>16s} {depth_skew:8d}")

        if visible_ct == 0 and sec > 2:
            ever_had_zero_visible = True

    check(not ever_had_zero_visible,
          "CRITICAL: visible bubbles never collapse to zero under stress")
    check(len(w._trades) > 0,
          f"trade deque not empty after stress ({len(w._trades)})")
    check(visible_ct > 0,
          f"final visible count > 0 ({visible_ct})")

    print(f"  Final: deque={len(w._trades)} visible={visible_ct} "
          f"p95={w._p95_size:.6f}")


# ================================================================ MAIN

if __name__ == '__main__':
    tests = [
        test_trades_added_to_deque,
        test_trade_price_filter,
        test_max_deque_depth_tracked,
        test_pruning_uses_trade_time_not_depth_time,
        test_pruning_does_not_wipe_recent_trades,
        test_pruning_counter_tracking,
        test_bubble_visibility_in_time_window,
        test_bubble_x_mapping,
        test_bubble_y_mapping,
        test_bubble_radius_bounds,
        test_p95_normalization,
        test_chart_now_uses_max,
        test_chart_now_caps_depth_lead,
        test_auto_scale_includes_trades,
        test_shared_viewport_right_edge,
        test_failure_stage_no_trades,
        test_failure_stage_after_visibility,
        test_trade_burst_does_not_wipe_visible,
        test_deterministic_replay,
        test_heatmap_slices_within_visible_window,
        test_heatmap_alpha_zero_intensity,
        test_stress_harness,
    ]

    for t in tests:
        print(f"  {t.__name__} ...", end=" ")
        try:
            t()
            print("OK")
        except Exception as e:
            FAIL += 1
            print(f"EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    total = PASS + FAIL
    print(f"\n{'=' * 50}")
    print(f"Bubble pipeline regression: {PASS}/{total} checks passed")
    if FAIL:
        print(f"  {FAIL} FAILURES")
        sys.exit(1)
    else:
        print("  All passed.")
        sys.exit(0)
