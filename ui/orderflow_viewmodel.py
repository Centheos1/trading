"""ViewModel for the Order Flow (heatmap + bubbles) view.

All heavy data storage, aggregation, and frame computation lives here.
The HeatmapWidget becomes a thin renderer that draws pre-computed
FrameData produced by this ViewModel each timer tick.

Data flow:
  MainWindow timer tick
    -> vm.add_trade() / vm.add_depth_column()   (data ingestion)
    -> vm.compute_frame(pw, ph, margins)         (heavy computation)
    -> HeatmapWidget.set_frame(frame)            (store reference)
    -> HeatmapWidget.update()                    (schedule repaint)
    -> HeatmapWidget.paintEvent()                (draw cached frame)
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QImage, QPainterPath

logger = logging.getLogger(__name__)

# Hard cap on retained trades after time-based prune. High-volume symbols can
# exceed this in a long visible window; without a cap, bubble paths and
# _rebuild_trade_slices scan O(n) with n≈40k+ and the UI thread stalls.
_MAX_TRADES_HELD = 20_000

# Re-export shared data classes so existing imports keep working.
from ui.heatmap_widget import (
    TimeBucket,
    TradeBucketAggregate,
    TradeSlice,
    HEATMAP_SLICE_MS,
    DEFAULT_BUCKET_DURATION_MS,
    DEFAULT_NUM_VISIBLE_BUCKETS,
    P95_UPDATE_INTERVAL,
    AGG_PRICE_BUCKET_PX,
    MAX_RENDERED_BUBBLES,
    MIN_BUBBLE_QTY_FRAC,
    MIN_BUBBLE_RADIUS,
    MAX_BUBBLE_RADIUS,
    BUBBLE_SCALE,
    BUBBLE_ALPHA,
    PRUNE_BUFFER_MS,
    GRID_REBUILD_TOL,
    TRADE_SLICE_MS,
    AUTO_SCALE_MIN_SPAN_FRAC,
    DEFAULT_ZOOM_FRACTION,
    DEPTH_NORM_PCTILE,
    DEPTH_GAMMA,
    DEPTH_PRICE_BUCKETS,
    DEPTH_MIN_INTENSITY,
    _HEAT_LUT,
    _HEAT_LUT_BID,
    _HEAT_LUT_ASK,
)


@dataclass(slots=True)
class FrameData:
    """Pre-computed render artifacts for one frame.

    Produced by OrderFlowViewModel.compute_frame(), consumed by
    HeatmapWidget.paintEvent().  Contains only pixel-ready data —
    no iteration or aggregation needed by the View.
    """
    # Depth heatmap
    depth_image: QImage | None = None
    depth_img_bytes: bytes | None = None  # prevent GC of backing buffer
    depth_shift_px: float = 0.0
    have_heatmap: bool = False

    # Bubble paths (already aggregated + culled)
    buy_path: QPainterPath = field(default_factory=QPainterPath)
    sell_path: QPainterPath = field(default_factory=QPainterPath)

    # Coordinate system (View needs these for axes / overlays)
    price_min: float = 0.0
    price_max: float = 0.0
    price_range: float = 0.0
    chart_now: int = 0
    t_start: int = 0

    # Health / diagnostics
    visible_bubble_count: int = 0
    book_empty_ticks: int = 0
    trade_feed_stale: bool = False
    depth_trade_skew_s: float = 0.0
    last_paint_ms: float = 0.0  # filled by View after paint

    # Bubble diagnostics (passed through for logging)
    bubble_diag: dict = field(default_factory=dict)


class OrderFlowViewModel:
    """Owns all order-flow data and pre-computes render-ready frames.

    MainWindow feeds raw data via add_trade / add_depth_column.
    Each timer tick, MainWindow calls compute_frame() which produces
    a FrameData containing pixel-ready QImage and QPainterPaths.
    """

    _MAX_DEPTH_LEAD_MS = 5000
    _STALE_TRADE_THRESHOLD_MS = 10_000

    def __init__(self, candle_store: deque | None = None):
        # --- Price range / auto-scale ---
        self._price_min = 0.0
        self._price_max = 0.0
        self._auto_scale = True
        self._zoom_fraction = DEFAULT_ZOOM_FRACTION

        # --- Depth slices ---
        self._slices: deque = deque(maxlen=10_000)
        self._cur_bids: dict = {}
        self._cur_asks: dict = {}
        self._cur_depth_prices = np.empty(0, dtype=np.float64)
        self._cur_depth_log_qtys = np.empty(0, dtype=np.float64)
        self._cur_log_max: float = 0.0
        self._last_log_max: float = 0.0
        self._last_raw_depth_ts: int = 0
        self._last_depth_ts: int = 0
        self._depth_updates: int = 0
        self._heatmap_samples: int = 0
        self._missed_slices: int = 0
        self._last_sample_ts: int = 0
        self._book_empty_ticks: int = 0
        self._last_valid_book_ts: int = 0

        # --- Time bucket model ---
        self._bucket_duration_ms = DEFAULT_BUCKET_DURATION_MS
        self._num_visible_buckets = DEFAULT_NUM_VISIBLE_BUCKETS
        self._visible_window_ms = (self._bucket_duration_ms
                                   * self._num_visible_buckets)
        self._slice_ms = max(HEATMAP_SLICE_MS,
                             self._visible_window_ms // 1200)
        self._buckets: deque = (candle_store if candle_store is not None
                                else deque(maxlen=200))

        # --- Trade data ---
        self._trades: deque = deque()
        self._trade_sizes: deque = deque(maxlen=2_000)
        self._p95_size: float = 1.0
        self._p95_counter: int = 0
        self._last_trade_ts: int = 0
        self._trades_added: int = 0
        self._trades_dropped: int = 0

        # --- Trade slice aggregation store ---
        self._trade_slices: dict[int, TradeSlice] = {}
        self._slice_price_bucket_size: float = 0.0
        self._slice_trade_total: int = 0

        # --- Signals ---
        self._signals: deque = deque(maxlen=2000)

        # --- Strategy overlay ---
        self._overlay_entry_price: float = 0.0
        self._overlay_stop_price: float = 0.0
        self._overlay_target_price: float = 0.0

        # --- Depth image cache ---
        self._cached_intensity: np.ndarray | None = None
        self._cached_intensity_shape: tuple = (0, 0)
        self._depth_img_data: bytes | None = None

        # --- Bubble diagnostics ---
        self._bubble_diag: dict = {
            'trades_added_total': 0,
            'trades_rejected_price': 0,
            'last_trade_add_ts': 0,
            'total_in_deque': 0,
            'max_deque_depth': 0,
            'trades_pruned_this_frame': 0,
            'trades_pruned_total': 0,
            'last_prune_ts': 0.0,
            'active_min_ts': 0,
            'active_max_ts': 0,
            'filtered_by_time': 0,
            'filtered_by_x': 0,
            'filtered_by_y': 0,
            'visible': 0,
            'draw_called': False,
            'last_draw_ts': 0.0,
            'last_nonzero_ts': 0.0,
            'consecutive_zero_frames': 0,
            'empty_draw_count': 0,
            'min_radius': 0.0,
            'max_radius': 0.0,
            'avg_radius': 0.0,
            'min_alpha': 0,
            'max_alpha': 0,
            'newest_x': 0.0,
            'oldest_x': 0.0,
            'p95_size': 1.0,
            't_start': 0,
            'now': 0,
            'pmin': 0.0,
            'pmax': 0.0,
            'pr': 0.0,
            'pw': 0,
            'failure_stage': 'NONE',
            'agg_cells': 0,
            'agg_raw_visible': 0,
            'agg_culled': 0,
            'agg_coarsen_passes': 0,
        }
        self._bubble_diag_log_throttle: float = 0.0
        self._last_visible_bubble_count: int = 0

    # ---------------------------------------------------------------- properties

    @property
    def chart_now(self) -> int:
        d, t = self._last_depth_ts, self._last_trade_ts
        if d > 0 and t > 0:
            skew = d - t
            if skew > self._STALE_TRADE_THRESHOLD_MS:
                return d - self._MAX_DEPTH_LEAD_MS
            return max(t, min(d, t + self._MAX_DEPTH_LEAD_MS))
        return max(d, t)

    @property
    def trade_feed_stale(self) -> bool:
        d, t = self._last_depth_ts, self._last_trade_ts
        return d > 0 and t > 0 and (d - t) > self._STALE_TRADE_THRESHOLD_MS

    @property
    def price_min(self) -> float:
        return self._price_min

    @property
    def price_max(self) -> float:
        return self._price_max

    @property
    def visible_window_ms(self) -> int:
        return self._visible_window_ms

    @property
    def bucket_duration_ms(self) -> int:
        return self._bucket_duration_ms

    @property
    def zoom_fraction(self) -> float:
        return self._zoom_fraction

    # ---------------------------------------------------------------- config

    def set_bucket_duration_ms(self, ms: int):
        ms = max(5000, ms)
        if ms == self._bucket_duration_ms:
            return
        self._bucket_duration_ms = ms
        self._visible_window_ms = ms * self._num_visible_buckets
        self._slice_ms = max(HEATMAP_SLICE_MS,
                             self._visible_window_ms // 1200)
        self._buckets.clear()

    def set_auto_scale(self, enabled: bool, reset_zoom: bool = False):
        self._auto_scale = enabled
        if reset_zoom:
            self._zoom_fraction = DEFAULT_ZOOM_FRACTION

    def set_zoom_fraction(self, frac: float):
        self._zoom_fraction = max(frac, 0.00005)

    def set_price_range(self, pmin: float, pmax: float):
        self._auto_scale = False
        self._price_min = pmin
        self._price_max = pmax

    def set_strategy_overlay(self, entry_price: float = 0.0,
                             stop_price: float = 0.0,
                             target_price: float = 0.0):
        self._overlay_entry_price = entry_price
        self._overlay_stop_price = stop_price
        self._overlay_target_price = target_price

    def clear_strategy_overlay(self):
        self._overlay_entry_price = 0.0
        self._overlay_stop_price = 0.0
        self._overlay_target_price = 0.0

    # ---------------------------------------------------------------- data in

    def sync_trade_time(self, ws_trade_ts: int):
        if ws_trade_ts > self._last_trade_ts:
            self._last_trade_ts = ws_trade_ts

    def add_trade(self, timestamp: int, price: float,
                  quantity: float, is_buy: bool):
        if price <= 0:
            self._bubble_diag['trades_rejected_price'] = (
                self._bubble_diag.get('trades_rejected_price', 0) + 1)
            return
        self._trades.append((timestamp, price, quantity, is_buy))
        self._trade_sizes.append(quantity)
        self._trades_added += 1
        self._bubble_diag['trades_added_total'] = self._trades_added
        self._bubble_diag['last_trade_add_ts'] = timestamp
        n = len(self._trades)
        if n > self._bubble_diag.get('max_deque_depth', 0):
            self._bubble_diag['max_deque_depth'] = n
        if timestamp > self._last_trade_ts:
            self._last_trade_ts = timestamp
        self._update_bucket(timestamp, price, quantity, is_buy)
        self._insert_trade_slice(timestamp, price, quantity, is_buy)
        self._update_p95()

    def _update_bucket(self, ts: int, price: float, qty: float,
                       is_buy: bool):
        dur = self._bucket_duration_ms
        bkt_ts = (ts // dur) * dur
        if self._buckets and self._buckets[-1].bucket_ts == bkt_ts:
            b = self._buckets[-1]
        elif not self._buckets or bkt_ts > self._buckets[-1].bucket_ts:
            b = TimeBucket(bucket_ts=bkt_ts)
            self._buckets.append(b)
        else:
            return
        if b.trade_count == 0:
            b.open_price = price
            b.high = price
            b.low = price
        else:
            if price > b.high:
                b.high = price
            if price < b.low:
                b.low = price
        b.close_price = price
        b.volume += qty
        if is_buy:
            b.buy_volume += qty
        else:
            b.sell_volume += qty
        b.trade_count += 1

    def _insert_trade_slice(self, timestamp: int, price: float,
                            quantity: float, is_buy: bool):
        pbs = self._slice_price_bucket_size
        if pbs <= 0:
            return
        bucket_ms = (timestamp // TRADE_SLICE_MS) * TRADE_SLICE_MS
        ts_obj = self._trade_slices.get(bucket_ms)
        if ts_obj is None:
            ts_obj = TradeSlice(bucket_ms)
            self._trade_slices[bucket_ms] = ts_obj
        p_key = round(price / pbs)
        agg = ts_obj.price_levels.get(p_key)
        if agg is None:
            agg = TradeBucketAggregate()
            ts_obj.price_levels[p_key] = agg
        agg.trade_count += 1
        agg.total_qty += quantity
        agg.sum_price_qty += price * quantity
        if is_buy:
            agg.buy_count += 1
            agg.buy_qty += quantity
        else:
            agg.sell_count += 1
            agg.sell_qty += quantity
        self._slice_trade_total += 1

    def _update_p95(self):
        self._p95_counter += 1
        if self._p95_counter >= P95_UPDATE_INTERVAL:
            self._p95_counter = 0
            n = len(self._trade_sizes)
            if n >= 20:
                idx = min(int(n * 0.95), n - 1)
                arr = np.array(self._trade_sizes)
                arr.partition(idx)
                self._p95_size = max(float(arr[idx]), 1e-10)

    def add_signal(self, timestamp, price, signal_type, strength):
        self._signals.append({
            'timestamp': timestamp,
            'price': price,
            'type': signal_type,
            'strength': strength,
        })

    def add_depth_column(self, timestamp, bids, asks, best_bid, best_ask,
                         *, sample_ts=None):
        new_depth = (timestamp != self._last_raw_depth_ts)
        if new_depth:
            new_bids = {p: q for p, q in bids}
            new_asks = {p: q for p, q in asks}
            if new_bids or new_asks:
                self._cur_bids = new_bids
                self._cur_asks = new_asks
                self._book_empty_ticks = 0
                self._last_valid_book_ts = timestamp
                all_p = list(new_bids.keys()) + list(new_asks.keys())
                all_q = list(new_bids.values()) + list(new_asks.values())
                if all_p:
                    _p = np.array(all_p, dtype=np.float64)
                    _q = np.array(all_q, dtype=np.float64)
                    valid = _q > 0
                    self._cur_depth_prices = _p[valid]
                    self._cur_depth_log_qtys = np.log1p(_q[valid])
                    self._cur_log_max = (
                        float(self._cur_depth_log_qtys.max())
                        if len(self._cur_depth_log_qtys) else 0.0)
                else:
                    self._cur_depth_prices = np.empty(0, dtype=np.float64)
                    self._cur_depth_log_qtys = np.empty(0, dtype=np.float64)
            else:
                self._book_empty_ticks += 1
            self._last_raw_depth_ts = timestamp
        if timestamp > self._last_depth_ts:
            self._last_depth_ts = timestamp
        self._depth_updates += 1

        effective_ts = sample_ts if sample_ts is not None else timestamp
        slice_ts = (effective_ts // self._slice_ms) * self._slice_ms
        self._last_sample_ts = slice_ts

        if new_depth:
            bids_snap = dict(self._cur_bids)
            asks_snap = dict(self._cur_asks)
            snap_prices = self._cur_depth_prices
            snap_log_qtys = self._cur_depth_log_qtys
        elif self._slices:
            bids_snap = self._slices[-1][1]
            asks_snap = self._slices[-1][2]
            snap_prices = self._slices[-1][5]
            snap_log_qtys = self._slices[-1][6]
        else:
            bids_snap = dict(self._cur_bids)
            asks_snap = dict(self._cur_asks)
            snap_prices = self._cur_depth_prices
            snap_log_qtys = self._cur_depth_log_qtys

        if self._slices and self._slices[-1][0] == slice_ts:
            if new_depth:
                self._slices[-1] = (slice_ts, bids_snap, asks_snap,
                                    best_bid, best_ask,
                                    snap_prices, snap_log_qtys)
        else:
            if self._slices:
                prev_ts = self._slices[-1][0]
                gap = (slice_ts - prev_ts) // self._slice_ms - 1
                if 0 < gap <= 20:
                    fill_ts = prev_ts + self._slice_ms
                    while fill_ts < slice_ts:
                        self._slices.append(
                            (fill_ts, bids_snap, asks_snap,
                             best_bid, best_ask,
                             snap_prices, snap_log_qtys))
                        fill_ts += self._slice_ms
                    self._missed_slices += gap
            self._slices.append(
                (slice_ts, bids_snap, asks_snap, best_bid, best_ask,
                 snap_prices, snap_log_qtys))
            self._heatmap_samples += 1

        cutoff = slice_ts - self._visible_window_ms
        pruned = False
        while self._slices and self._slices[0][0] < cutoff:
            self._slices.popleft()
            pruned = True

        trade_now = self._last_trade_ts
        if trade_now > 0:
            visible_start = trade_now - self._visible_window_ms
            trade_cutoff = visible_start - PRUNE_BUFFER_MS
        else:
            trade_cutoff = 0
        pruned_trades = 0
        while self._trades and self._trades[0][0] < trade_cutoff:
            self._trades.popleft()
            pruned_trades += 1
        self._bubble_diag['trades_pruned_this_frame'] = pruned_trades
        self._bubble_diag['trades_pruned_total'] = (
            self._bubble_diag.get('trades_pruned_total', 0) + pruned_trades)

        if trade_cutoff > 0:
            bucket_cutoff = (trade_cutoff // TRADE_SLICE_MS) * TRADE_SLICE_MS
            expired = [k for k in self._trade_slices if k < bucket_cutoff]
            for k in expired:
                sl = self._trade_slices.pop(k)
                self._slice_trade_total -= sum(
                    a.trade_count for a in sl.price_levels.values())

        if pruned_trades > 0:
            self._bubble_diag['last_prune_ts'] = time.monotonic()
        if self._trades:
            self._bubble_diag['active_min_ts'] = self._trades[0][0]
            self._bubble_diag['active_max_ts'] = self._trades[-1][0]
        self._bubble_diag['total_in_deque'] = len(self._trades)

        # Drop oldest trades if the window still holds too many (time prune
        # alone is insufficient on very active markets).
        # FIXME - this is pruning the trades by count. It is better to prune the trades by time.
        count_pruned = 0
        while len(self._trades) > _MAX_TRADES_HELD:
            self._trades.popleft()
            count_pruned += 1
        if count_pruned:
            self._bubble_diag['trades_pruned_count_cap'] = (
                self._bubble_diag.get('trades_pruned_count_cap', 0) + count_pruned)
            pbs = self._slice_price_bucket_size
            if pbs > 0:
                self._rebuild_trade_slices(pbs)
            else:
                self._trade_slices.clear()
                self._slice_trade_total = 0

        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0
        if mid > 0 and self._auto_scale:
            half = mid * self._zoom_fraction
            half = max(half, mid * AUTO_SCALE_MIN_SPAN_FRAC * 0.5)
            p_lo = mid - half
            p_hi = mid + half
            # Include recent trade prices in the vertical band. Depth-mid-only
            # scaling drifts from active prints during fast moves, causing
            # OFFSCREEN_PRICE / huge filtY and wasted bubble work each tick.
            if self._trades:
                n = len(self._trades)
                tail = min(768, n)
                i0 = n - tail
                tp_lo = tp_hi = self._trades[i0][1]
                for i in range(i0 + 1, n):
                    p = self._trades[i][1]
                    if p < tp_lo:
                        tp_lo = p
                    if p > tp_hi:
                        tp_hi = p
                p_lo = min(p_lo, tp_lo)
                p_hi = max(p_hi, tp_hi)
            self._price_min = p_lo
            self._price_max = p_hi

    # ---------------------------------------------------------------- queries

    def get_bubble_diagnostics(self) -> dict:
        return dict(self._bubble_diag)

    def get_visible_slices(self, start_ms: int,
                           end_ms: int) -> list[TradeSlice]:
        return [ts for bms, ts in self._trade_slices.items()
                if start_ms <= bms <= end_ms]

    # --------------------------------------------------------- frame compute

    def compute_frame(self, pw: int, ph: int,
                      margin_left: int, margin_top: int,
                      chart_right_inset: int) -> FrameData:
        """Pre-compute all render data for the current tick.

        Called once per timer tick from MainWindow, BEFORE
        HeatmapWidget.update().  Returns a FrameData that the
        View draws without further iteration or aggregation.
        """
        frame = FrameData()

        px = margin_left
        py = margin_top
        pw_draw = pw - chart_right_inset

        if pw_draw <= 0 or ph <= 0:
            return frame

        pmin = self._price_min
        pmax = self._price_max
        have_heatmap = (self._slices and pmax > pmin
                        and math.isfinite(pmin) and math.isfinite(pmax))

        if not have_heatmap and self._trades:
            trade_mid = float(self._trades[-1][1])
            if trade_mid > 0:
                half = trade_mid * self._zoom_fraction
                half = max(half, trade_mid * AUTO_SCALE_MIN_SPAN_FRAC * 0.5)
                pmin = trade_mid - half
                pmax = trade_mid + half
            if not (pmax > pmin and math.isfinite(pmin)
                    and math.isfinite(pmax)):
                return frame
        elif not have_heatmap:
            return frame

        pr = pmax - pmin
        if pr <= 0:
            pr = 1.0

        now = self.chart_now
        if now <= 0 and self._slices:
            now = self._slices[-1][0]
        if now <= 0 and self._trades:
            now = self._trades[-1][0]
        t_start = now - self._visible_window_ms

        frame.price_min = pmin
        frame.price_max = pmax
        frame.price_range = pr
        frame.chart_now = now
        frame.t_start = t_start
        frame.have_heatmap = have_heatmap
        frame.book_empty_ticks = self._book_empty_ticks
        frame.trade_feed_stale = self.trade_feed_stale
        if self.trade_feed_stale:
            frame.depth_trade_skew_s = (
                (self._last_depth_ts - self._last_trade_ts) / 1000.0)

        if have_heatmap:
            self._compute_depth_image(
                frame, px, py, pw_draw, ph, pr, pmin, t_start, now)

        self._compute_bubbles(
            frame, px, py, pw_draw, ph, pr, pmin, t_start, now)

        frame.bubble_diag = dict(self._bubble_diag)
        return frame

    # --------------------------------------------------------- depth image

    def _compute_depth_image(self, frame: FrameData,
                             px, py, pw, ph, pr, pmin, t_start, now):
        n_img_cols = self._visible_window_ms // self._slice_ms + 1
        n_rows = min(int(ph), 800)
        if n_rows < 2 or n_img_cols < 2:
            return

        log_max = self._cur_log_max
        self._last_log_max = log_max
        if log_max <= 0:
            return

        lq = self._cur_depth_log_qtys
        if len(lq) > 20:
            norm_ref = float(np.percentile(lq, DEPTH_NORM_PCTILE))
        else:
            norm_ref = log_max
        norm_ref = max(norm_ref, 1e-10)

        n_buckets = DEPTH_PRICE_BUCKETS
        bucket_size = pr / n_buckets
        rpb = n_rows / n_buckets  # float rows-per-bucket for gap-free mapping

        shape = (n_rows, n_img_cols)
        if self._cached_intensity_shape != shape:
            self._cached_intensity = np.zeros(shape, dtype=np.float32)
            self._cached_intensity_shape = shape
        intensity = self._cached_intensity
        intensity[:] = 0
        inv_lm = 1.0 / norm_ref
        inv_bs = 1.0 / bucket_size if bucket_size > 0 else 0.0
        nrm1 = n_rows - 1

        mid_row_latest = n_rows // 2

        bucket_row_tops = np.clip(
            nrm1 - ((np.arange(n_buckets) + 1) * rpb).astype(np.int32),
            0, nrm1)
        bucket_row_bots = np.clip(
            nrm1 - (np.arange(n_buckets) * rpb).astype(np.int32),
            0, nrm1)

        _bucket_sums = np.empty(n_buckets, dtype=np.float64)
        _bucket_counts = np.empty(n_buckets, dtype=np.int32)

        sm = self._slice_ms
        t_start_q = (t_start // sm) * sm

        prev_data_id = None
        prev_col = -1

        for s in self._slices:
            col = (s[0] - t_start_q) // sm
            if col < 0 or col >= n_img_cols:
                continue

            data_id = id(s[5])
            if data_id == prev_data_id and 0 <= prev_col < n_img_cols:
                intensity[:, col] = intensity[:, prev_col]
                prev_data_id = data_id
                prev_col = col
                continue

            prices = s[5]
            log_qtys = s[6]
            if len(prices) == 0:
                prev_data_id = data_id
                prev_col = col
                continue

            best_bid = s[3]
            best_ask = s[4]
            if best_bid > 0 and best_ask > 0:
                mid = (best_bid + best_ask) * 0.5
                mid_row_latest = int((1.0 - (mid - pmin) / pr) * nrm1 + 0.5)
                mid_row_latest = max(0, min(nrm1, mid_row_latest))

            bucket_idx = np.clip(
                ((prices - pmin) * inv_bs).astype(np.int32),
                0, n_buckets - 1)
            raw = np.clip(log_qtys * inv_lm, 0.0, 1.0)
            gamma_vals = np.power(raw, DEPTH_GAMMA)

            _bucket_sums[:] = 0.0
            np.add.at(_bucket_sums, bucket_idx, gamma_vals)
            _bucket_counts[:] = 0
            np.add.at(_bucket_counts, bucket_idx, 1)

            active = _bucket_counts > 0
            _bucket_sums[active] /= _bucket_counts[active]
            _bucket_sums[_bucket_sums < DEPTH_MIN_INTENSITY] = 0.0

            active_bi = np.nonzero(_bucket_sums > 0)[0]
            col_slice = intensity[:, col]
            for bi in active_bi:
                rt = bucket_row_tops[bi]
                rb = bucket_row_bots[bi]
                if rt <= rb:
                    col_slice[rt:rb + 1] = np.maximum(
                        col_slice[rt:rb + 1], _bucket_sums[bi])

            prev_data_id = data_id
            prev_col = col

        self._forward_fill_intensity(intensity)

        mid_row = max(0, min(nrm1, mid_row_latest))
        idx = np.clip((intensity * 255).astype(np.int32), 0, 255)
        rgba = np.empty((n_rows, n_img_cols, 4), dtype=np.uint8)
        if mid_row > 0:
            rgba[:mid_row, :] = _HEAT_LUT_ASK[idx[:mid_row, :]]
        if mid_row < n_rows:
            rgba[mid_row:, :] = _HEAT_LUT_BID[idx[mid_row:, :]]

        buf = np.ascontiguousarray(rgba)
        self._depth_img_data = buf.tobytes()

        img = QImage(self._depth_img_data, n_img_cols, n_rows,
                     n_img_cols * 4, QImage.Format.Format_RGBA8888)

        frac = (t_start - t_start_q) / sm if sm > 0 else 0.0
        col_px = pw / max(n_img_cols - 1, 1)
        shift = frac * col_px

        frame.depth_image = img
        frame.depth_img_bytes = self._depth_img_data
        frame.depth_shift_px = shift

    @staticmethod
    def _forward_fill_intensity(intensity):
        n_cols = intensity.shape[1]
        if n_cols < 2:
            return 0
        has_data = np.any(intensity > 0, axis=0)
        if has_data.all() or not has_data.any():
            return 0
        filled = 0
        last_src = -1
        for c in range(n_cols):
            if has_data[c]:
                last_src = c
            elif last_src >= 0:
                intensity[:, c] = intensity[:, last_src]
                filled += 1
        if not has_data[0] and last_src >= 0:
            first_src = int(np.argmax(has_data))
            for c in range(first_src):
                intensity[:, c] = intensity[:, first_src]
                filled += 1
        return filled

    # --------------------------------------------------------- bubbles

    def _compute_bubbles(self, frame: FrameData,
                         px, py, pw, ph, pr, pmin, t_start, now):
        diag = self._bubble_diag
        diag['draw_called'] = True
        diag['last_draw_ts'] = time.monotonic()
        diag['t_start'] = t_start
        diag['now'] = now
        diag['pw'] = pw

        n_trades = len(self._trades)
        diag['total_in_deque'] = n_trades
        if not self._trades:
            diag['visible'] = 0
            diag['filtered_by_time'] = 0
            diag['filtered_by_x'] = 0
            diag['filtered_by_y'] = 0
            diag['agg_cells'] = 0
            diag['agg_raw_visible'] = 0
            diag['agg_culled'] = 0
            diag['agg_coarsen_passes'] = 0
            self._last_visible_bubble_count = 0
            self._bubble_diag_inc_zero()
            return

        ts_span = float(self._visible_window_ms)
        if ts_span <= 0:
            self._last_visible_bubble_count = 0
            self._bubble_diag_inc_zero()
            return

        ref = self._p95_size
        diag['pmin'] = pmin
        diag['pmax'] = pmin + pr
        diag['pr'] = pr
        diag['p95_size'] = ref
        inv_ts = pw / ts_span
        inv_pr = ph / pr if pr > 0 else 0.0

        price_bucket_size = (pr * AGG_PRICE_BUCKET_PX / ph) if ph > 0 else pr
        if price_bucket_size <= 0:
            price_bucket_size = pr

        if (self._slice_price_bucket_size <= 0
                or abs(price_bucket_size - self._slice_price_bucket_size)
                > self._slice_price_bucket_size * GRID_REBUILD_TOL):
            self._rebuild_trade_slices(price_bucket_size)

        t_start_bucket = (int(t_start) // TRADE_SLICE_MS) * TRADE_SLICE_MS
        t_end_bucket = ((int(now) + TRADE_SLICE_MS) // TRADE_SLICE_MS) * TRADE_SLICE_MS

        render_cells: dict[tuple[int, int], list] = {}
        filt_time = 0
        filt_y = 0
        raw_visible = 0

        for bucket_ms, ts_obj in self._trade_slices.items():
            if bucket_ms < t_start_bucket or bucket_ms > t_end_bucket:
                filt_time += sum(
                    a.trade_count for a in ts_obj.price_levels.values())
                continue
            t_idx = bucket_ms // TRADE_SLICE_MS
            for p_key, agg in ts_obj.price_levels.items():
                vwap = (agg.sum_price_qty / agg.total_qty
                        if agg.total_qty > 0
                        else (p_key + 0.5) * price_bucket_size)
                y = py + ph - (vwap - pmin) * inv_pr
                if y < py - MAX_BUBBLE_RADIUS or y > py + ph + MAX_BUBBLE_RADIUS:
                    filt_y += agg.trade_count
                    continue
                raw_visible += agg.trade_count
                render_cells[(t_idx, p_key)] = [
                    agg.total_qty, agg.trade_count,
                    agg.buy_qty, agg.sell_qty, agg.sum_price_qty]

        coarsen_passes = 0
        eff_slice_ms = TRADE_SLICE_MS
        eff_pbs = price_bucket_size
        while len(render_cells) > MAX_RENDERED_BUBBLES and coarsen_passes < 6:
            coarsened: dict[tuple[int, int], list] = {}
            for (t_idx, p_key), cell in render_cells.items():
                ck = (t_idx // 2, p_key // 2)
                existing = coarsened.get(ck)
                if existing is not None:
                    for i in range(5):
                        existing[i] += cell[i]
                else:
                    coarsened[ck] = list(cell)
            render_cells = coarsened
            eff_slice_ms *= 2
            eff_pbs *= 2
            coarsen_passes += 1

        culled = 0
        if render_cells:
            max_qty = max(c[0] for c in render_cells.values())
            qty_threshold = max_qty * MIN_BUBBLE_QTY_FRAC
            if qty_threshold > 0:
                to_remove = [k for k, c in render_cells.items()
                             if c[0] < qty_threshold]
                for k in to_remove:
                    del render_cells[k]
                culled = len(to_remove)

        if len(render_cells) > MAX_RENDERED_BUBBLES:
            ranked = sorted(render_cells.items(),
                            key=lambda kv: kv[1][0], reverse=True)
            render_cells = dict(ranked[:MAX_RENDERED_BUBBLES])
            culled += len(ranked) - MAX_RENDERED_BUBBLES

        buy_path = QPainterPath()
        buy_path.setFillRule(Qt.FillRule.WindingFill)
        sell_path = QPainterPath()
        sell_path.setFillRule(Qt.FillRule.WindingFill)

        visible_count = 0
        r_min = MAX_BUBBLE_RADIUS + 1.0
        r_max = 0.0
        r_sum = 0.0
        newest_x = -1e9
        oldest_x = 1e9
        _sqrt = math.sqrt

        for (t_idx, p_key), cell in render_cells.items():
            total_qty, count, buy_qty, sell_qty, sum_pq = cell
            center_ts = (t_idx + 0.5) * eff_slice_ms
            vwap = (sum_pq / total_qty if total_qty > 0
                    else (p_key + 0.5) * eff_pbs)
            x = px + (center_ts - t_start) * inv_ts
            y = py + ph - (vwap - pmin) * inv_pr

            norm = total_qty / ref
            radius = MIN_BUBBLE_RADIUS + _sqrt(max(norm, 0.0)) * BUBBLE_SCALE
            if radius > MAX_BUBBLE_RADIUS:
                radius = MAX_BUBBLE_RADIUS

            path = buy_path if buy_qty >= sell_qty else sell_path
            path.addEllipse(x - radius, y - radius, radius * 2, radius * 2)

            visible_count += 1
            r_sum += radius
            if radius < r_min:
                r_min = radius
            if radius > r_max:
                r_max = radius
            if x > newest_x:
                newest_x = x
            if x < oldest_x:
                oldest_x = x

        frame.buy_path = buy_path
        frame.sell_path = sell_path
        frame.visible_bubble_count = visible_count

        self._last_visible_bubble_count = visible_count
        diag['visible'] = visible_count
        diag['agg_cells'] = len(render_cells) + culled
        diag['agg_raw_visible'] = raw_visible
        diag['agg_culled'] = culled
        diag['agg_coarsen_passes'] = coarsen_passes
        diag['filtered_by_time'] = filt_time
        diag['filtered_by_x'] = 0
        diag['filtered_by_y'] = filt_y
        diag['min_radius'] = r_min if visible_count > 0 else 0.0
        diag['max_radius'] = r_max
        diag['avg_radius'] = (r_sum / visible_count) if visible_count > 0 else 0.0
        diag['min_alpha'] = BUBBLE_ALPHA
        diag['max_alpha'] = BUBBLE_ALPHA
        diag['newest_x'] = newest_x if visible_count > 0 else 0.0
        diag['oldest_x'] = oldest_x if visible_count > 0 else 0.0

        if visible_count > 0:
            diag['last_nonzero_ts'] = time.monotonic()
            diag['consecutive_zero_frames'] = 0
            diag['failure_stage'] = 'NONE'
        else:
            self._bubble_diag_inc_zero()

    # FIXME - this is rebuilding all slices. 
    # It is better to sort the trades by price and then add them to the slices.
    # A dictionary of {ts: [price, qty, is_buy]} would be a better data structure.
    # Pruning should be done when the time bucket is outside of the visible window.
    def _rebuild_trade_slices(self, new_price_bucket_size: float) -> None:
        self._trade_slices.clear()
        self._slice_price_bucket_size = new_price_bucket_size
        self._slice_trade_total = 0
        for ts, price, qty, is_buy in self._trades:
            bucket_ms = (ts // TRADE_SLICE_MS) * TRADE_SLICE_MS
            ts_obj = self._trade_slices.get(bucket_ms)
            if ts_obj is None:
                ts_obj = TradeSlice(bucket_ms)
                self._trade_slices[bucket_ms] = ts_obj
            p_key = round(price / new_price_bucket_size)
            agg = ts_obj.price_levels.get(p_key)
            if agg is None:
                agg = TradeBucketAggregate()
                ts_obj.price_levels[p_key] = agg
            agg.trade_count += 1
            agg.total_qty += qty
            agg.sum_price_qty += price * qty
            if is_buy:
                agg.buy_count += 1
                agg.buy_qty += qty
            else:
                agg.sell_count += 1
                agg.sell_qty += qty
            self._slice_trade_total += 1

    def _bubble_diag_inc_zero(self):
        diag = self._bubble_diag
        diag['consecutive_zero_frames'] = diag.get('consecutive_zero_frames', 0) + 1
        diag['empty_draw_count'] = diag.get('empty_draw_count', 0) + 1
        n_trades = len(self._trades)

        if self._trades_added == 0:
            diag['failure_stage'] = 'NO_TRADES_IN'
        elif diag.get('trades_rejected_price', 0) > 0 and n_trades == 0:
            diag['failure_stage'] = 'PARSE_FAILING'
        elif n_trades == 0 and diag.get('trades_pruned_total', 0) > 0:
            diag['failure_stage'] = 'PRUNED_TO_ZERO'
        elif n_trades == 0:
            diag['failure_stage'] = 'MODEL_NOT_UPDATING'
        elif diag.get('filtered_by_time', 0) == n_trades:
            diag['failure_stage'] = 'OFFSCREEN_TIME'
        elif diag.get('filtered_by_y', 0) > 0 and diag.get('visible', 0) == 0:
            diag['failure_stage'] = 'OFFSCREEN_PRICE'
        elif diag.get('filtered_by_x', 0) > 0 and diag.get('visible', 0) == 0:
            diag['failure_stage'] = 'OFFSCREEN_X'
        elif diag.get('max_radius', 0) < 1.0 and n_trades > 0:
            diag['failure_stage'] = 'INVISIBLE_RADIUS'
        else:
            diag['failure_stage'] = 'UNKNOWN'

        now_mono = time.monotonic()
        if n_trades > 0 and (now_mono - self._bubble_diag_log_throttle) > 5.0:
            self._bubble_diag_log_throttle = now_mono
            sample = self._trades[-1] if self._trades else (0, 0, 0, False)
            logger.warning(
                "BUBBLE_ZERO_VISIBLE stage=%s | trades=%d "
                "filtTime=%d filtX=%d filtY=%d "
                "zeroFrames=%d p95=%.6g pmin=%.2f pmax=%.2f "
                "chartNow=%d tStart=%d sampleTs=%d samplePrice=%.2f "
                "depthTs=%d tradeTs=%d pw=%d pruned=%d",
                diag['failure_stage'],
                n_trades,
                diag.get('filtered_by_time', 0),
                diag.get('filtered_by_x', 0),
                diag.get('filtered_by_y', 0),
                diag.get('consecutive_zero_frames', 0),
                diag.get('p95_size', 0),
                diag.get('pmin', 0), diag.get('pmax', 0),
                diag.get('now', 0), diag.get('t_start', 0),
                sample[0], sample[1],
                self._last_depth_ts, self._last_trade_ts,
                diag.get('pw', 0),
                diag.get('trades_pruned_this_frame', 0),
            )
        elif n_trades == 0 and (now_mono - self._bubble_diag_log_throttle) > 5.0:
            self._bubble_diag_log_throttle = now_mono
            logger.warning(
                "BUBBLE_ZERO_VISIBLE stage=%s | deque_empty "
                "added=%d rejected=%d pruned_total=%d "
                "depthTs=%d tradeTs=%d",
                diag['failure_stage'],
                self._trades_added,
                diag.get('trades_rejected_price', 0),
                diag.get('trades_pruned_total', 0),
                self._last_depth_ts, self._last_trade_ts,
            )
