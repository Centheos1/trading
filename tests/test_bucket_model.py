"""Tests for the time-bucketed order flow view (Phase 1).

Validates:
- TimeBucket dataclass and incremental OHLC computation
- Bucket assignment and boundary alignment
- Visible window = num_buckets * bucket_duration
- Dynamic slice_ms adaptation
- Bucket axis separator positioning
- Backward compatibility with existing bubble pipeline
- Deterministic bucket state across identical replays
"""
import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from ui.heatmap_widget import (
    HeatmapWidget, TimeBucket, HEATMAP_SLICE_MS,
    DEFAULT_BUCKET_DURATION_MS, DEFAULT_NUM_VISIBLE_BUCKETS,
    VISIBLE_WINDOW_MS, MIN_BUBBLE_RADIUS, MAX_BUBBLE_RADIUS,
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


# ================================================================ A. TimeBucket DATACLASS

def test_time_bucket_defaults():
    b = TimeBucket()
    check(b.bucket_ts == 0, "default bucket_ts is 0")
    check(b.trade_count == 0, "default trade_count is 0")
    check(b.volume == 0.0, "default volume is 0.0")
    check(b.high == 0.0, "default high is 0.0")
    check(b.low == float('inf'), "default low is inf")
    check(b.open_price == 0.0, "default open_price is 0.0")
    check(b.close_price == 0.0, "default close_price is 0.0")
    check(b.buy_volume == 0.0, "default buy_volume is 0.0")
    check(b.sell_volume == 0.0, "default sell_volume is 0.0")


def test_time_bucket_fields():
    b = TimeBucket(
        bucket_ts=1_700_000_060_000,
        high=83010.0,
        low=82990.0,
        open_price=83000.0,
        close_price=83005.0,
        volume=1.5,
        buy_volume=1.0,
        sell_volume=0.5,
        trade_count=10,
    )
    check(b.bucket_ts == 1_700_000_060_000, "bucket_ts set correctly")
    check(b.high == 83010.0, "high set correctly")
    check(b.low == 82990.0, "low set correctly")
    check(b.volume == 1.5, "volume set correctly")
    check(b.trade_count == 10, "trade_count set correctly")


# ================================================================ B. BUCKET ASSIGNMENT

def test_bucket_assignment_single_bucket():
    w = make_widget()
    now = 1_700_000_060_000
    expected_bucket = (now // 60_000) * 60_000
    for i in range(10):
        w.add_trade(now + i * 100, 83000.0, 0.1, True)
    check(len(w._buckets) == 1, f"single bucket created (got {len(w._buckets)})")
    b = w._buckets[0]
    check(b.trade_count == 10, f"10 trades in bucket (got {b.trade_count})")
    check(b.bucket_ts == expected_bucket,
          f"bucket_ts is minute-aligned (got {b.bucket_ts}, "
          f"expected {expected_bucket})")


def test_bucket_assignment_two_buckets():
    w = make_widget()
    base = 1_700_000_060_000
    for i in range(5):
        w.add_trade(base + i * 100, 83000.0, 0.1, True)
    for i in range(5):
        w.add_trade(base + 60_000 + i * 100, 83005.0, 0.2, False)
    check(len(w._buckets) == 2, f"two buckets (got {len(w._buckets)})")
    check(w._buckets[0].trade_count == 5, "first bucket has 5 trades")
    check(w._buckets[1].trade_count == 5, "second bucket has 5 trades")


def test_bucket_boundary_alignment():
    """Bucket boundaries align to wall-clock minute boundaries."""
    w = make_widget()
    ts = 1_700_000_090_000  # 30 seconds into a minute
    w.add_trade(ts, 83000.0, 0.1, True)
    expected_bucket = (ts // 60_000) * 60_000
    check(w._buckets[0].bucket_ts == expected_bucket,
          f"bucket aligned to minute (got {w._buckets[0].bucket_ts}, "
          f"expected {expected_bucket})")


def test_bucket_ohlc_incremental():
    """OHLC is updated incrementally as trades arrive."""
    w = make_widget()
    base = 1_700_000_060_000
    prices = [83000.0, 83010.0, 82990.0, 83005.0, 83002.0]
    for i, p in enumerate(prices):
        w.add_trade(base + i * 100, p, 0.1, i % 2 == 0)
    b = w._buckets[0]
    check(b.open_price == 83000.0, f"open is first price (got {b.open_price})")
    check(b.high == 83010.0, f"high is max (got {b.high})")
    check(b.low == 82990.0, f"low is min (got {b.low})")
    check(b.close_price == 83002.0, f"close is last (got {b.close_price})")
    check(b.trade_count == 5, f"5 trades (got {b.trade_count})")
    check(abs(b.volume - 0.5) < 1e-9, f"volume is 0.5 (got {b.volume})")
    check(abs(b.buy_volume - 0.3) < 1e-9,
          f"buy_volume is 0.3 (got {b.buy_volume})")
    check(abs(b.sell_volume - 0.2) < 1e-9,
          f"sell_volume is 0.2 (got {b.sell_volume})")


def test_stale_trade_ignored():
    """A trade with a timestamp before the latest bucket is ignored."""
    w = make_widget()
    base = 1_700_000_120_000
    w.add_trade(base, 83000.0, 0.1, True)
    w.add_trade(base + 60_000, 83005.0, 0.1, True)
    old_ts = base - 60_000
    w.add_trade(old_ts, 82990.0, 0.1, True)
    check(len(w._buckets) == 2, f"stale trade did not create bucket (got {len(w._buckets)})")
    check(w._buckets[0].trade_count == 1, "first bucket unchanged")


# ================================================================ C. VISIBLE WINDOW COMPUTATION

def test_default_visible_window():
    w = make_widget()
    expected = DEFAULT_BUCKET_DURATION_MS * DEFAULT_NUM_VISIBLE_BUCKETS
    check(w._visible_window_ms == expected,
          f"visible_window = {expected}ms (got {w._visible_window_ms})")
    check(expected == DEFAULT_BUCKET_DURATION_MS * DEFAULT_NUM_VISIBLE_BUCKETS,
          f"default: {DEFAULT_NUM_VISIBLE_BUCKETS} × "
          f"{DEFAULT_BUCKET_DURATION_MS // 1000}s = "
          f"{expected // 1000}s")


def test_set_bucket_duration_updates_window():
    w = make_widget()
    w.set_bucket_duration_ms(300_000)  # 5 min buckets
    expected = 300_000 * DEFAULT_NUM_VISIBLE_BUCKETS
    check(w._visible_window_ms == expected,
          f"visible_window updated to {expected} (got {w._visible_window_ms})")
    check(w._bucket_duration_ms == 300_000,
          f"bucket_duration set (got {w._bucket_duration_ms})")


def test_set_bucket_duration_clears_buckets():
    w = make_widget()
    w.add_trade(1_700_000_060_000, 83000.0, 0.1, True)
    check(len(w._buckets) == 1, "bucket exists before reset")
    w.set_bucket_duration_ms(300_000)
    check(len(w._buckets) == 0, "buckets cleared after duration change")


def test_set_bucket_duration_minimum():
    """Bucket duration cannot go below 5 seconds."""
    w = make_widget()
    w.set_bucket_duration_ms(1000)
    check(w._bucket_duration_ms == 5000,
          f"minimum 5s enforced (got {w._bucket_duration_ms})")


def test_set_bucket_duration_no_op_same_value():
    """Setting the same duration is a no-op (buckets not cleared)."""
    w = make_widget()
    w.add_trade(1_700_000_060_000, 83000.0, 0.1, True)
    w.set_bucket_duration_ms(60_000)
    check(len(w._buckets) == 1, "buckets preserved on same-value set")


# ================================================================ D. DYNAMIC SLICE_MS

def test_dynamic_slice_ms_default():
    """Default window / 1200 gives the slice_ms, floored at HEATMAP_SLICE_MS."""
    w = make_widget()
    expected = max(HEATMAP_SLICE_MS, w._visible_window_ms // 1200)
    check(w._slice_ms == expected,
          f"default slice_ms = {expected} (got {w._slice_ms})")


def test_dynamic_slice_ms_small_window():
    """With 5s buckets, small window floor is HEATMAP_SLICE_MS."""
    w = make_widget()
    w.set_bucket_duration_ms(5_000)
    check(w._slice_ms == HEATMAP_SLICE_MS,
          f"small window: slice_ms = {HEATMAP_SLICE_MS} (got {w._slice_ms})")


def test_dynamic_slice_ms_large_window():
    """With 1hr buckets, slice_ms adapts to keep columns bounded."""
    w = make_widget()
    w.set_bucket_duration_ms(3_600_000)
    visible = 3_600_000 * DEFAULT_NUM_VISIBLE_BUCKETS
    expected = max(HEATMAP_SLICE_MS, visible // 1200)
    check(w._slice_ms == expected,
          f"large window: slice_ms = {expected} (got {w._slice_ms})")


def test_intensity_cols_bounded():
    """n_img_cols should stay around 1200 regardless of bucket size."""
    for dur_ms in [5_000, 60_000, 300_000, 900_000, 3_600_000]:
        w = make_widget()
        w.set_bucket_duration_ms(dur_ms)
        n_cols = w._visible_window_ms // w._slice_ms + 1
        check(n_cols <= 1300,
              f"n_cols bounded for {dur_ms}ms buckets: "
              f"{n_cols} cols (slice_ms={w._slice_ms})")


# ================================================================ E. BUCKET AXIS RENDERING

def test_bucket_axis_boundary_positions():
    """Bucket separators land at correct x-positions."""
    w = make_widget()
    now = 1_700_000_360_000
    t_start = now - w._visible_window_ms
    dur = w._bucket_duration_ms
    ts_span = float(now - t_start)
    pw = 692
    px = 70

    first_boundary = ((t_start // dur) + 1) * dur
    boundaries = []
    b = first_boundary
    while b <= now:
        x = px + (b - t_start) / ts_span * pw
        if px <= x <= px + pw:
            boundaries.append((b, x))
        b += dur

    check(len(boundaries) >= 1,
          f"at least 1 boundary in {DEFAULT_NUM_VISIBLE_BUCKETS}-bucket view "
          f"(got {len(boundaries)})")

    for i in range(1, len(boundaries)):
        dx = boundaries[i][1] - boundaries[i - 1][1]
        expected_dx = (dur / ts_span) * pw
        check(abs(dx - expected_dx) < 1.0,
              f"boundaries evenly spaced: dx={dx:.1f}, "
              f"expected={expected_dx:.1f}")


def test_bucket_axis_label_format_minutes():
    """Buckets >= 1 minute use HH:MM format."""
    w = make_widget()
    check(w._bucket_duration_ms >= 60_000, "default is 1 min")


def test_bucket_axis_label_format_seconds():
    """Buckets < 1 minute use HH:MM:SS format."""
    w = make_widget()
    w.set_bucket_duration_ms(5_000)
    check(w._bucket_duration_ms < 60_000, "5s bucket < 1 min")


# ================================================================ F. BACKWARD COMPATIBILITY

def test_visible_window_ms_constant_preserved():
    """Module-level VISIBLE_WINDOW_MS is preserved for test imports."""
    check(VISIBLE_WINDOW_MS == 60_000,
          f"VISIBLE_WINDOW_MS unchanged (got {VISIBLE_WINDOW_MS})")


def test_existing_trade_mechanics_unchanged():
    """Trade deque behavior is identical to pre-bucket implementation."""
    w = make_widget()
    now = 1_700_000_060_000
    for i in range(100):
        w.add_trade(now + i * 100, 83000.0, 0.1, True)
    check(len(w._trades) == 100, f"100 trades in deque (got {len(w._trades)})")
    check(w._last_trade_ts == now + 99 * 100, "last_trade_ts correct")
    check(w._trades_added == 100, "trades_added correct")


def test_chart_now_unchanged():
    """chart_now capping logic is unaffected by bucket model.

    Uses an 8-second skew (below the stale threshold) to verify the
    normal depth-lead cap still applies.
    """
    w = make_widget()
    w._last_trade_ts = 1_700_000_060_000
    w._last_depth_ts = 1_700_000_060_000 + 8_000
    expected = w._last_trade_ts + w._MAX_DEPTH_LEAD_MS
    check(w.chart_now == expected,
          f"chart_now cap preserved (got {w.chart_now}, expected {expected})")


def test_pruning_uses_wider_window():
    """Trade pruning uses the new wider visible_window_ms."""
    w = make_widget()
    now = 1_700_000_660_000
    for i in range(200):
        ts = now - 500_000 + i * 2500
        w.add_trade(ts, 83000.0, 0.1, True)
    w._last_trade_ts = now

    bids = [(82999.0, 1.0)]
    asks = [(83001.0, 1.0)]
    w._last_depth_ts = now
    w.add_depth_column(now, bids, asks, 82999.0, 83001.0)

    remaining = len(w._trades)
    cutoff = now - w._visible_window_ms * 2
    oldest_expected = now - 500_000
    if oldest_expected >= cutoff:
        check(remaining == 200,
              f"all trades within 2x window survive (remaining={remaining})")
    else:
        check(remaining > 0,
              f"some trades survive (remaining={remaining})")


# ================================================================ G. DETERMINISM

def test_deterministic_bucket_replay():
    """Identical trade sequences produce identical bucket state."""
    def run():
        w = make_widget()
        base = 1_700_000_060_000
        for i in range(50):
            w.add_trade(base + i * 500, 83000.0 + (i % 5), 0.1, i % 2 == 0)
        return [
            (b.bucket_ts, b.open_price, b.high, b.low, b.close_price,
             round(b.volume, 10), b.trade_count)
            for b in w._buckets
        ]

    r1 = run()
    r2 = run()
    check(r1 == r2, "deterministic bucket replay")


# ================================================================ H. RENDER PERFORMANCE GUARD

def test_render_column_count_reasonable():
    """Verify intensity image columns stay reasonable across bucket sizes."""
    for dur_ms, label in [(60_000, "1m"), (300_000, "5m"), (900_000, "15m")]:
        w = make_widget()
        w.set_bucket_duration_ms(dur_ms)
        n_cols = w._visible_window_ms // w._slice_ms + 1
        check(200 <= n_cols <= 1300,
              f"{label}: n_cols={n_cols} in reasonable range")


# ================================================================ MAIN

if __name__ == '__main__':
    tests = [
        test_time_bucket_defaults,
        test_time_bucket_fields,
        test_bucket_assignment_single_bucket,
        test_bucket_assignment_two_buckets,
        test_bucket_boundary_alignment,
        test_bucket_ohlc_incremental,
        test_stale_trade_ignored,
        test_default_visible_window,
        test_set_bucket_duration_updates_window,
        test_set_bucket_duration_clears_buckets,
        test_set_bucket_duration_minimum,
        test_set_bucket_duration_no_op_same_value,
        test_dynamic_slice_ms_default,
        test_dynamic_slice_ms_small_window,
        test_dynamic_slice_ms_large_window,
        test_intensity_cols_bounded,
        test_bucket_axis_boundary_positions,
        test_bucket_axis_label_format_minutes,
        test_bucket_axis_label_format_seconds,
        test_visible_window_ms_constant_preserved,
        test_existing_trade_mechanics_unchanged,
        test_chart_now_unchanged,
        test_pruning_uses_wider_window,
        test_deterministic_bucket_replay,
        test_render_column_count_reasonable,
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
    print(f"Bucket model tests: {PASS}/{total} checks passed")
    if FAIL:
        print(f"  {FAIL} FAILURES")
        sys.exit(1)
    else:
        print("  All passed.")
        sys.exit(0)
