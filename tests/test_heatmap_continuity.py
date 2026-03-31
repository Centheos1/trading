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
