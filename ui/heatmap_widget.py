import logging
import math
import time
import numpy as np
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QImage, QPainterPath

logger = logging.getLogger(__name__)

# --------------- tunable configuration ---------------
VISIBLE_WINDOW_MS = 60_000  # kept for backward-compat (test imports)
HEATMAP_SLICE_MS = 100
DEFAULT_BUCKET_DURATION_MS = 60_000
DEFAULT_NUM_VISIBLE_BUCKETS = 1
MIN_BUBBLE_RADIUS = 3
MAX_BUBBLE_RADIUS = 14
BUBBLE_SCALE = 8.0
BUBBLE_ALPHA = 183
P95_UPDATE_INTERVAL = 500
AGG_PRICE_BUCKET_PX = 2  # aggregate trades within this many pixels vertically
AGG_TIME_BUCKET_PX = 2   # aggregate trades within this many pixels horizontally
MAX_RENDERED_BUBBLES = 2000  # safety net; natural cell count bounded by time/price buckets
MAX_TRADES_RETAINED = 3_000  # legacy constant kept for test imports; deque is unbounded
MIN_BUBBLE_QTY_FRAC = 0.0   # no volume culling; all visible data renders
PRUNE_BUFFER_MS = 5_000      # keep trades up to 5 s before visible window edge
GRID_REBUILD_TOL = 0.10  # rebuild trade slices when price bucket drifts > 10%
TRADE_SLICE_MS = 100     # time bucket width for trade aggregation store
AUTO_SCALE_MIN_SPAN_FRAC = 0.002  # min visible price range as fraction of mid
AUTO_SCALE_MARGIN_FRAC = 0.05     # margin on each side as fraction of span

HEAT_GRADIENT = [
    (0.00, (8, 12, 30)),
    (0.02, (12, 30, 90)),
    (0.08, (15, 55, 140)),
    (0.18, (20, 100, 175)),
    (0.30, (25, 150, 195)),
    (0.45, (60, 195, 200)),
    (0.60, (150, 210, 100)),
    (0.75, (220, 200, 50)),
    (0.87, (245, 140, 30)),
    (0.95, (250, 70, 20)),
    (1.00, (255, 40, 15)),
]


def _build_heat_lut():
    lut = np.zeros((256, 4), dtype=np.uint8)
    for i in range(256):
        frac = i / 255.0
        r, g, b = 8, 12, 30
        for j in range(1, len(HEAT_GRADIENT)):
            t1, c1 = HEAT_GRADIENT[j - 1]
            t2, c2 = HEAT_GRADIENT[j]
            if frac <= t2:
                blend = (frac - t1) / (t2 - t1) if t2 > t1 else 0
                r = int(c1[0] + (c2[0] - c1[0]) * blend)
                g = int(c1[1] + (c2[1] - c1[1]) * blend)
                b = int(c1[2] + (c2[2] - c1[2]) * blend)
                break
        alpha = min(255, int(frac * 255 * 3)) if i > 0 else 0
        lut[i] = [r, g, b, alpha]
    return lut


_HEAT_LUT = _build_heat_lut()


@dataclass
class TimeBucket:
    """Incremental OHLC summary for a single time bucket."""
    bucket_ts: int = 0
    high: float = 0.0
    low: float = float('inf')
    open_price: float = 0.0
    close_price: float = 0.0
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trade_count: int = 0


class TradeBucketAggregate:
    """Aggregated trade data for a single (time-slice, price-bucket) cell.

    Part of the time-bucketed trade aggregation store for bubble rendering.
    This is NOT a raw trade log — each instance summarises all trades that
    fell within one 100 ms time slice × price bucket.  Bubble size derives
    from total_qty; colour derives from buy_qty vs sell_qty imbalance.
    """
    __slots__ = ('trade_count', 'buy_count', 'sell_count',
                 'total_qty', 'buy_qty', 'sell_qty', 'sum_price_qty')

    def __init__(self):
        self.trade_count: int = 0
        self.buy_count: int = 0
        self.sell_count: int = 0
        self.total_qty: float = 0.0
        self.buy_qty: float = 0.0
        self.sell_qty: float = 0.0
        self.sum_price_qty: float = 0.0


class TradeSlice:
    """All trade aggregates within a single TRADE_SLICE_MS time bucket.

    Part of the time-bucketed trade aggregation store for bubble rendering.
    price_levels maps price-bucket index → TradeBucketAggregate.
    Sparse: only price buckets that received trades are populated.
    """
    __slots__ = ('bucket_start_ms', 'price_levels')

    def __init__(self, bucket_start_ms: int):
        self.bucket_start_ms: int = bucket_start_ms
        self.price_levels: dict[int, TradeBucketAggregate] = {}


class HeatmapWidget(QWidget):
    """Bookmap-style order book heatmap with time-based scrolling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 400)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.grabGesture(Qt.PinchGesture)

        self._price_min = 0.0
        self._price_max = 0.0

        self._slices = deque(maxlen=10_000)

        # Bucket model: visible window = num_buckets * bucket_duration
        self._bucket_duration_ms = DEFAULT_BUCKET_DURATION_MS
        self._num_visible_buckets = DEFAULT_NUM_VISIBLE_BUCKETS
        self._visible_window_ms = (self._bucket_duration_ms
                                   * self._num_visible_buckets)
        self._slice_ms = max(HEATMAP_SLICE_MS,
                             self._visible_window_ms // 1200)
        self._buckets: deque = deque(maxlen=200)

        self._cur_bids = {}
        self._cur_asks = {}

        self._trades = deque()
        self._trade_sizes = deque(maxlen=2_000)
        self._p95_size = 1.0
        self._p95_counter = 0

        self._signals = deque(maxlen=2000)

        self._auto_scale_bid_lo = float('inf')
        self._auto_scale_ask_hi = float('-inf')

        # Unified time reference: max of depth and trade timestamps
        self._last_depth_ts = 0
        self._last_trade_ts = 0
        self._last_raw_depth_ts = 0
        self._trades_added = 0
        self._trades_dropped = 0
        self._depth_updates = 0

        # Heatmap sampling health
        self._heatmap_samples = 0
        self._missed_slices = 0
        self._last_sample_ts = 0
        self._book_empty_ticks = 0
        self._last_valid_book_ts = 0
        self._last_log_max = 0.0

        self._auto_scale = True
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_price_min = 0.0
        self._drag_start_price_max = 0.0

        self._depth_img_data = None
        self._cached_intensity = None
        self._cached_intensity_shape = (0, 0)

        self._cur_depth_prices = np.empty(0, dtype=np.float64)
        self._cur_depth_log_qtys = np.empty(0, dtype=np.float64)
        self._cur_log_max = 0.0
        self._last_paint_ms = 0.0

        self._last_visible_bubble_count = 0

        # Time-bucketed trade aggregation store for bubble rendering.
        # Key: floor(trade_ts / TRADE_SLICE_MS) * TRADE_SLICE_MS (100 ms).
        # Value: TradeSlice with per-price-bucket TradeBucketAggregate.
        # NOT a raw trade log — pre-aggregated for O(S) rendering where
        # S = visible slices (typ. 600 for a 1-min window).
        # Eviction: time-based pruning (trade_cutoff aligned to TRADE_SLICE_MS).
        # Bounded by: retention window / TRADE_SLICE_MS × price-bucket fan-out.
        self._trade_slices: dict[int, TradeSlice] = {}
        self._slice_price_bucket_size: float = 0.0
        self._slice_trade_total: int = 0

        # Bubble pipeline diagnostics — stage-by-stage (cheap, always on)
        self._bubble_diag = {
            # A. TRADE INPUT
            'trades_added_total': 0,
            'trades_rejected_price': 0,
            'last_trade_add_ts': 0,
            # B. TRADE STORAGE / QUEUE
            'total_in_deque': 0,
            'max_deque_depth': 0,
            'trades_pruned_this_frame': 0,
            'trades_pruned_total': 0,
            'last_prune_ts': 0.0,
            # C. BUBBLE MODEL (deque IS the model for this arch)
            'active_min_ts': 0,
            'active_max_ts': 0,
            # D. BUBBLE PRUNING / FILTER (render-time)
            'filtered_by_time': 0,
            'filtered_by_x': 0,
            'filtered_by_y': 0,
            'visible': 0,
            # E. SNAPSHOT (no separate snapshot — direct render)
            'draw_called': False,
            'last_draw_ts': 0.0,
            'last_nonzero_ts': 0.0,
            'consecutive_zero_frames': 0,
            'empty_draw_count': 0,
            # F. RENDER
            'min_radius': 0.0,
            'max_radius': 0.0,
            'avg_radius': 0.0,
            'min_alpha': 0,
            'max_alpha': 0,
            'newest_x': 0.0,
            'oldest_x': 0.0,
            # viewport/mapping context
            'p95_size': 1.0,
            't_start': 0,
            'now': 0,
            'pmin': 0.0,
            'pmax': 0.0,
            'pr': 0.0,
            'pw': 0,
            # derived failure stage
            'failure_stage': 'NONE',
        }
        self._bubble_diag_log_throttle = 0.0

        self._bg_color = QColor(8, 12, 30)
        self._grid_color = QColor(20, 25, 45)
        self._text_color = QColor(120, 130, 150)

        self._margin_left = 70
        self._margin_right = 10
        self._margin_top = 10
        self._margin_bottom = 22
        self._chart_right_inset = 28

        self._font_axis = QFont("Menlo", 8)
        self._font_time = QFont("Menlo", 7)
        self._font_zoom = QFont("Menlo", 7)

        self._buy_color = QColor(40, 200, 80, BUBBLE_ALPHA)
        self._sell_color = QColor(220, 70, 70, BUBBLE_ALPHA)
        self._depth_lost_color = QColor(255, 200, 60, 140)
        self._bucket_sep_color = QColor(42, 42, 69)

        # Strategy overlay lines (Phase 2)
        self._overlay_entry_price = 0.0
        self._overlay_stop_price = 0.0
        self._overlay_target_price = 0.0
        self._overlay_entry_color = QColor(220, 220, 230)
        self._overlay_stop_color = QColor(220, 60, 60, 200)
        self._overlay_target_color = QColor(60, 200, 100, 200)

    def set_strategy_overlay(self, entry_price: float = 0.0,
                             stop_price: float = 0.0,
                             target_price: float = 0.0):
        """Set strategy price lines. Pass 0 to clear a line."""
        self._overlay_entry_price = entry_price
        self._overlay_stop_price = stop_price
        self._overlay_target_price = target_price

    def clear_strategy_overlay(self):
        self._overlay_entry_price = 0.0
        self._overlay_stop_price = 0.0
        self._overlay_target_price = 0.0

    @property
    def plot_margin_top(self) -> int:
        return self._margin_top

    @property
    def plot_margin_bottom(self) -> int:
        return self._margin_bottom

    # ------------------------------------------------------------------ data

    # Max allowed depth-trade skew before chart_now is capped (ms).
    # Must be large enough that brief asyncio scheduling jitter between
    # the trade and depth coroutines doesn't freeze heatmap scrolling.
    _MAX_DEPTH_LEAD_MS = 5000
    _STALE_TRADE_THRESHOLD_MS = 10_000

    @property
    def chart_now(self) -> int:
        """Unified 'now' timestamp, capped to prevent depth-trade skew.

        When depth WS event time (E) advances ahead of trade WS trade
        time (T) — due to network batching, asyncio scheduling, or
        sparse trade periods — an uncapped max(depth, trade) shifts the
        visible window forward and filters out all trade-derived data.
        Capping the lead keeps bubbles and CVD visible.

        When the trade feed has been stale for longer than
        _STALE_TRADE_THRESHOLD_MS, chart_now advances with depth time
        (minus the cap margin) so the heatmap keeps scrolling instead
        of freezing the display.
        """
        d, t = self._last_depth_ts, self._last_trade_ts
        if d > 0 and t > 0:
            skew = d - t
            if skew > self._STALE_TRADE_THRESHOLD_MS:
                return d - self._MAX_DEPTH_LEAD_MS
            return max(t, min(d, t + self._MAX_DEPTH_LEAD_MS))
        return max(d, t)

    @property
    def trade_feed_stale(self) -> bool:
        """True when the trade feed has been silent long enough to
        trigger the depth-driven chart_now fallback."""
        d, t = self._last_depth_ts, self._last_trade_ts
        return d > 0 and t > 0 and (d - t) > self._STALE_TRADE_THRESHOLD_MS

    def set_bucket_duration_ms(self, ms: int):
        """Set bucket duration and recompute visible window + slice_ms."""
        ms = max(5000, ms)
        if ms == self._bucket_duration_ms:
            return
        self._bucket_duration_ms = ms
        self._visible_window_ms = ms * self._num_visible_buckets
        self._slice_ms = max(HEATMAP_SLICE_MS,
                             self._visible_window_ms // 1200)
        self._buckets.clear()

    def _update_bucket(self, ts: int, price: float, qty: float,
                       is_buy: bool):
        """Assign a trade to its time bucket and update OHLC incrementally."""
        dur = self._bucket_duration_ms
        bkt_ts = (ts // dur) * dur

        if self._buckets and self._buckets[-1].bucket_ts == bkt_ts:
            b = self._buckets[-1]
        elif not self._buckets or bkt_ts > self._buckets[-1].bucket_ts:
            b = TimeBucket(bucket_ts=bkt_ts)
            self._buckets.append(b)
        else:
            return  # stale trade for an old bucket

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

    def add_depth_column(self, timestamp, bids, asks, best_bid, best_ask,
                         *, sample_ts=None):
        """Write a heatmap slice from the current book state.

        Args:
            timestamp: raw depth-event exchange timestamp (ms).
            bids/asks: current order book levels.
            best_bid/best_ask: current top-of-book prices.
            sample_ts: chart-time timestamp used for slice positioning.
                       When provided, the heatmap advances according to
                       chart_now rather than depth-event arrival timing.
                       This prevents the "lag then snap" visual artifact.
        """
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
        # Prune trades using TRADE-derived time only (not depth time).
        # Cutoff = visible_start - PRUNE_BUFFER_MS.  Anything older is
        # off-screen and safe to discard.  The small buffer prevents
        # edge flicker during time-jitter.
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
        # Prune expired 100 ms trade slices.
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

        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0
        if mid > 0 and self._auto_scale:
            # Use the full depth book price range so the heatmap shows
            # all resting liquidity at all times, not just near the quote.
            if len(self._cur_depth_prices) > 0:
                self._auto_scale_bid_lo = float(self._cur_depth_prices.min())
                self._auto_scale_ask_hi = float(self._cur_depth_prices.max())
            else:
                if best_bid > 0:
                    self._auto_scale_bid_lo = min(self._auto_scale_bid_lo, best_bid)
                if best_ask > 0:
                    self._auto_scale_ask_hi = max(self._auto_scale_ask_hi, best_ask)

            if pruned:
                lo = float('inf')
                hi = float('-inf')
                step = max(1, len(self._slices) // 60)
                for idx in range(0, len(self._slices), step):
                    prices = self._slices[idx][5]
                    if len(prices) > 0:
                        plo = float(prices.min())
                        phi = float(prices.max())
                        if plo < lo:
                            lo = plo
                        if phi > hi:
                            hi = phi
                if self._slices:
                    prices = self._slices[-1][5]
                    if len(prices) > 0:
                        plo = float(prices.min())
                        phi = float(prices.max())
                        if plo < lo:
                            lo = plo
                        if phi > hi:
                            hi = phi
                if lo < float('inf') and hi > float('-inf'):
                    self._auto_scale_bid_lo = lo
                    self._auto_scale_ask_hi = hi

            lo = self._auto_scale_bid_lo
            hi = self._auto_scale_ask_hi

            # Trades are always within the depth range; include them
            # only as a safety net so bubbles are never clipped.
            if self._trades:
                step = max(1, len(self._trades) // 100)
                for i in range(0, len(self._trades), step):
                    tp = self._trades[i][1]
                    if tp > 0:
                        if tp < lo:
                            lo = tp
                        if tp > hi:
                            hi = tp
                tp = self._trades[-1][1]
                if tp > 0:
                    if tp < lo:
                        lo = tp
                    if tp > hi:
                        hi = tp

            if (lo < hi and math.isfinite(lo) and math.isfinite(hi)):
                span = hi - lo
                min_span = mid * AUTO_SCALE_MIN_SPAN_FRAC
                if span < min_span:
                    span = min_span
                margin = span * AUTO_SCALE_MARGIN_FRAC
                new_min = lo - margin
                new_max = hi + margin
                if math.isfinite(new_min) and math.isfinite(new_max):
                    self._price_min = new_min
                    self._price_max = new_max

    def sync_trade_time(self, ws_trade_ts: int):
        """Advance _last_trade_ts to the latest WS trade timestamp.

        Called each timer tick so chart_now tracks real-time even when
        the trade-drain buffer has a backlog.  Without this, chart_now
        freezes at _last_trade_ts + _MAX_DEPTH_LEAD_MS and the heatmap
        stops scrolling.
        """
        if ws_trade_ts > self._last_trade_ts:
            self._last_trade_ts = ws_trade_ts

    def add_trade(self, timestamp, price, quantity, is_buy):
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

        # Incremental trade-slice insert — O(1) amortised.
        pbs = self._slice_price_bucket_size
        if pbs > 0:
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

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event):
        _pt0 = time.monotonic()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self._bg_color)

        px = self._margin_left
        py = self._margin_top
        pw_full = self.width() - self._margin_left - self._margin_right
        ph = self.height() - self._margin_top - self._margin_bottom
        pw = pw_full - self._chart_right_inset

        if pw <= 0 or ph <= 0:
            painter.end()
            return

        pmin = self._price_min
        pmax = self._price_max
        have_heatmap = (self._slices and pmax > pmin
                        and math.isfinite(pmin) and math.isfinite(pmax))

        # Derive price range from trades if heatmap hasn't initialised yet
        if not have_heatmap and self._trades:
            lo = hi = self._trades[-1][1]
            sample_step = max(1, len(self._trades) // 200)
            for i in range(0, len(self._trades), sample_step):
                p = self._trades[i][1]
                if p < lo:
                    lo = p
                if p > hi:
                    hi = p
            span = hi - lo
            trade_mid = (lo + hi) / 2.0 if lo > 0 else lo
            min_span = trade_mid * AUTO_SCALE_MIN_SPAN_FRAC
            if span < min_span:
                span = min_span
            margin = span * AUTO_SCALE_MARGIN_FRAC
            pmin = lo - margin
            pmax = hi + margin
            if pmax > pmin and math.isfinite(pmin) and math.isfinite(pmax):
                have_heatmap = False  # still no depth data
            else:
                painter.setPen(self._text_color)
                msg = "Waiting for data..."
                if self._book_empty_ticks > 10:
                    msg = "Order book empty — waiting for resync..."
                painter.drawText(self.rect(), Qt.AlignCenter, msg)
                painter.end()
                return
        elif not have_heatmap:
            painter.setPen(self._text_color)
            msg = "Waiting for data..."
            if self._book_empty_ticks > 10:
                msg = "Order book empty — waiting for resync..."
            painter.drawText(self.rect(), Qt.AlignCenter, msg)
            painter.end()
            return

        pr = pmax - pmin
        if pr <= 0:
            pr = 1.0

        now = self.chart_now
        if now <= 0 and self._slices:
            now = self._slices[-1][0]
        if now <= 0 and self._trades:
            now = self._trades[-1][0]
        t_start = now - self._visible_window_ms

        if have_heatmap:
            self._draw_depth(painter, px, py, pw, ph, pr, t_start, now)
        self._draw_bubbles(painter, px, py, pw, ph, pr, t_start, now,
                           pmin_override=pmin)
        self._draw_axes(painter, px, py, pw, ph, pr, pmin_override=pmin)
        self._draw_bucket_axis(painter, px, py, pw, ph, t_start, now)
        self._draw_strategy_overlay(painter, px, py, pw, ph, pmin, pr)

        if not have_heatmap and self._book_empty_ticks > 10:
            painter.setPen(self._depth_lost_color)
            painter.drawText(px + 4, py + 16,
                             "Depth lost — bubbles from trade feed")

        if self.trade_feed_stale:
            painter.setPen(self._depth_lost_color)
            skew_s = (self._last_depth_ts - self._last_trade_ts) / 1000.0
            painter.drawText(px + 4, py + ph - 4,
                             f"Trade feed stale ({skew_s:.0f}s)")

        painter.end()
        self._last_paint_ms = (time.monotonic() - _pt0) * 1000.0

    def _draw_depth(self, painter, px, py, pw, ph, pr, t_start, now):
        n_img_cols = self._visible_window_ms // self._slice_ms + 1
        n_rows = min(int(ph), 800)
        if n_rows < 2 or n_img_cols < 2:
            return

        log_max = self._cur_log_max
        self._last_log_max = log_max
        if log_max <= 0:
            return

        shape = (n_rows, n_img_cols)
        if self._cached_intensity_shape != shape:
            self._cached_intensity = np.zeros(shape, dtype=np.float32)
            self._cached_intensity_shape = shape
        intensity = self._cached_intensity
        intensity[:] = 0
        pmin = self._price_min
        inv_pr = 1.0 / pr if pr > 0 else 0.0
        nrm1 = n_rows - 1
        inv_lm = 1.0 / log_max

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

            rows = ((1.0 - (prices - pmin) * inv_pr) * nrm1 + 0.5).astype(np.int32)
            vals = np.maximum(0.08, log_qtys * inv_lm)

            col_slice = intensity[:, col]

            # Sort by row position so we can fill spans between levels.
            order = np.argsort(rows)
            s_rows = rows[order]
            s_vals = vals[order]
            n_lvl = len(s_rows)

            # Scatter each level at its exact row.
            valid = (s_rows >= 0) & (s_rows < n_rows)
            if valid.any():
                np.maximum.at(col_slice, s_rows[valid], s_vals[valid])

            # Fill pixel spans between adjacent levels so there are no
            # dark gaps in the heatmap.  Each intermediate pixel row gets
            # the intensity of the nearer level (split at midpoint).
            for i in range(n_lvl - 1):
                r0 = int(s_rows[i])
                r1 = int(s_rows[i + 1])
                if r0 < 0 or r1 >= n_rows or r1 - r0 <= 1:
                    continue
                r0c = max(r0, 0)
                r1c = min(r1, n_rows - 1)
                mid = (r0c + r1c) // 2
                if mid > r0c:
                    col_slice[r0c:mid + 1] = np.maximum(
                        col_slice[r0c:mid + 1], s_vals[i])
                if r1c > mid:
                    col_slice[mid + 1:r1c + 1] = np.maximum(
                        col_slice[mid + 1:r1c + 1], s_vals[i + 1])

            prev_data_id = data_id
            prev_col = col

        self._forward_fill_intensity(intensity)

        idx = np.clip((intensity * 255).astype(np.int32), 0, 255)
        rgba = _HEAT_LUT[idx]
        buf = np.ascontiguousarray(rgba)
        self._depth_img_data = buf.tobytes()

        img = QImage(self._depth_img_data, n_img_cols, n_rows,
                     n_img_cols * 4, QImage.Format.Format_RGBA8888)

        # Sub-pixel smooth scroll: shift the image by the fractional
        # part of t_start relative to the quantized slice boundary.
        frac = (t_start - t_start_q) / sm if sm > 0 else 0.0
        col_px = pw / max(n_img_cols - 1, 1)
        shift = frac * col_px

        painter.save()
        painter.setClipRect(QRectF(px, py, pw, ph))
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(px - shift, py, pw + col_px, ph),
            img,
            QRectF(0, 0, n_img_cols, n_rows))
        painter.restore()

    @staticmethod
    def _forward_fill_intensity(intensity):
        """Forward-fill depth intensity columns to eliminate rendering gaps.

        Order-book semantics: resting liquidity persists across time until
        an explicit update changes it.  Columns that were never populated
        by a slice (gaps in the deque) are filled from the nearest earlier
        populated column.  The left edge is backward-filled from the first
        known column so the display starts cleanly.

        Returns the number of columns that were filled.
        """
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

    @staticmethod
    def _build_grid(trades, t_start, px, py, pw, ph, pmin, pr,
                    inv_ts, inv_pr, time_bucket_ms, price_bucket_size):
        """Viewport-filter trades and aggregate into a (t_idx, p_idx, side) grid.

        Returns (grid, filt_time, filt_x, filt_y, raw_visible).
        Grid values: [total_qty, trade_count, sum_price_x_qty].
        """
        grid = {}
        filt_time = 0
        filt_x = 0
        filt_y = 0
        raw_visible = 0
        right_bound = px + pw + MAX_BUBBLE_RADIUS

        for ts, price, qty, is_buy in trades:
            if ts < t_start:
                filt_time += 1
                continue
            x = px + (ts - t_start) * inv_ts
            if x > right_bound:
                filt_x += 1
                continue
            y = py + ph - (price - pmin) * inv_pr
            if y < py - MAX_BUBBLE_RADIUS or y > py + ph + MAX_BUBBLE_RADIUS:
                filt_y += 1
                continue
            raw_visible += 1
            t_idx = int(ts // time_bucket_ms)
            p_idx = int((price - pmin) / price_bucket_size)
            key = (t_idx, p_idx, is_buy)
            cell = grid.get(key)
            if cell is not None:
                cell[0] += qty
                cell[1] += 1
                cell[2] += price * qty
            else:
                grid[key] = [qty, 1, price * qty]
        return grid, filt_time, filt_x, filt_y, raw_visible

    def _rebuild_trade_slices(self, new_price_bucket_size: float) -> None:
        """Rebuild trade slice store from the raw trades deque.

        Called when price-bucket size changes (viewport resize / zoom).
        Complexity: O(N) where N = len(_trades), but infrequent.
        The time-bucket width is always TRADE_SLICE_MS (100 ms), so only
        the price dimension needs re-bucketing.
        """
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

    def get_visible_slices(self, start_ms: int,
                           end_ms: int) -> list[TradeSlice]:
        """Return trade slices whose bucket falls within [start_ms, end_ms]."""
        return [ts for bms, ts in self._trade_slices.items()
                if start_ms <= bms <= end_ms]

    def prune_older_than(self, cutoff_ms: int) -> int:
        """Remove all trade slices older than *cutoff_ms*. Returns count."""
        bucket_cutoff = (cutoff_ms // TRADE_SLICE_MS) * TRADE_SLICE_MS
        expired = [k for k in self._trade_slices if k < bucket_cutoff]
        removed = 0
        for k in expired:
            sl = self._trade_slices.pop(k)
            n = sum(a.trade_count for a in sl.price_levels.values())
            self._slice_trade_total -= n
            removed += n
        return removed

    def _draw_bubbles(self, painter, px, py, pw, ph, pr, t_start, now,
                      *, pmin_override=None):
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
        pmin = pmin_override if pmin_override is not None else self._price_min
        diag['pmin'] = pmin
        diag['pmax'] = pmin + pr
        diag['pr'] = pr
        diag['p95_size'] = ref
        inv_ts = pw / ts_span
        inv_pr = ph / pr if pr > 0 else 0.0

        # --- Phase 0: ensure price-bucket size is current ---
        price_bucket_size = (pr * AGG_PRICE_BUCKET_PX / ph) if ph > 0 else pr
        if price_bucket_size <= 0:
            price_bucket_size = pr

        if (self._slice_price_bucket_size <= 0
                or abs(price_bucket_size - self._slice_price_bucket_size)
                > self._slice_price_bucket_size * GRID_REBUILD_TOL):
            self._rebuild_trade_slices(price_bucket_size)

        # --- Phase 1: collect visible cells from trade slices (O(S)) ---
        t_start_bucket = (int(t_start) // TRADE_SLICE_MS) * TRADE_SLICE_MS
        t_end_bucket = ((int(now) + TRADE_SLICE_MS) // TRADE_SLICE_MS) * TRADE_SLICE_MS

        # render_cells: (t_idx, p_key) -> [total_qty, trade_count,
        #                                   buy_qty, sell_qty, sum_pq]
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

        # --- Phase 1b: coarsen on visible subset (O(G)) ---
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

        # --- Phase 1c: volume-based culling ---
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

        # --- Phase 1d: final hard cap by volume rank ---
        if len(render_cells) > MAX_RENDERED_BUBBLES:
            ranked = sorted(render_cells.items(),
                            key=lambda kv: kv[1][0], reverse=True)
            render_cells = dict(ranked[:MAX_RENDERED_BUBBLES])
            culled += len(ranked) - MAX_RENDERED_BUBBLES

        # --- Phase 2: render bubbles (colour from buy/sell imbalance) ---
        buy_path = QPainterPath()
        sell_path = QPainterPath()

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

        painter.setPen(Qt.NoPen)
        if not buy_path.isEmpty():
            painter.setBrush(self._buy_color)
            painter.drawPath(buy_path)
        if not sell_path.isEmpty():
            painter.setBrush(self._sell_color)
            painter.drawPath(sell_path)

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

    def _bubble_diag_inc_zero(self):
        diag = self._bubble_diag
        diag['consecutive_zero_frames'] = diag.get('consecutive_zero_frames', 0) + 1
        diag['empty_draw_count'] = diag.get('empty_draw_count', 0) + 1
        n_trades = len(self._trades)

        # Infer failure stage
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

    def get_bubble_diagnostics(self) -> dict:
        """Return a shallow copy of the current bubble pipeline diagnostics."""
        return dict(self._bubble_diag)

    def _draw_axes(self, painter, px, py, pw, ph, pr, *, pmin_override=None):
        painter.setPen(QPen(self._grid_color, 1))
        painter.drawLine(px, py, px, py + ph)
        painter.drawLine(px, py + ph, px + pw, py + ph)

        painter.setFont(self._font_axis)
        painter.setPen(self._text_color)

        base_pmin = (pmin_override if pmin_override is not None
                     else self._price_min)
        n_labels = min(10, int(ph / 40))
        for i in range(n_labels + 1):
            price = base_pmin + (pr * i / max(n_labels, 1))
            y = py + ph - (i / max(n_labels, 1)) * ph
            painter.drawText(5, int(y + 4), f"{price:.2f}")
            painter.setPen(QPen(self._grid_color, 0.5, Qt.DotLine))
            painter.drawLine(px, int(y), px + pw, int(y))
            painter.setPen(self._text_color)

        if not self._auto_scale:
            painter.setPen(QColor(255, 200, 60))
            painter.setFont(self._font_zoom)
            painter.drawText(px + 4, py + 12, "MANUAL ZOOM  (dbl-click to reset)")

    def _draw_bucket_axis(self, painter, px, py, pw, ph, t_start, now):
        """Render bucket boundary separators and time labels."""
        if now <= t_start or pw <= 0:
            return

        ts_span = float(now - t_start)
        dur = self._bucket_duration_ms

        label_fmt = "%H:%M" if dur >= 60_000 else "%H:%M:%S"

        painter.setFont(self._font_time)
        sep_pen = QPen(self._bucket_sep_color, 1)
        text_pen = QPen(self._text_color)

        first_boundary = ((t_start // dur) + 1) * dur
        y_label = py + ph + 14

        boundary = first_boundary
        while boundary <= now:
            x = px + (boundary - t_start) / ts_span * pw

            if px <= x <= px + pw:
                painter.setPen(sep_pen)
                painter.drawLine(int(x), py, int(x), py + ph)

                painter.setPen(text_pen)
                dt = datetime.fromtimestamp(boundary / 1000.0, tz=timezone.utc)
                label = dt.strftime(label_fmt)
                painter.drawText(int(x) - 20, y_label, label)

            boundary += dur

    def _draw_strategy_overlay(self, painter, px, py, pw, ph, pmin, pr):
        """Draw horizontal stop / target / entry lines when set."""
        if pr <= 0:
            return
        lines = [
            (self._overlay_entry_price,  self._overlay_entry_color,  Qt.SolidLine, "Entry"),
            (self._overlay_stop_price,   self._overlay_stop_color,   Qt.DashLine,  "Stop"),
            (self._overlay_target_price, self._overlay_target_color, Qt.DashLine,  "Target"),
        ]
        painter.setFont(self._font_time)
        for price, color, style, label in lines:
            if not price or price <= 0:
                continue
            frac = 1.0 - (price - pmin) / pr
            y = py + frac * ph
            if y < py or y > py + ph:
                continue
            pen = QPen(color, 1, style)
            painter.setPen(pen)
            painter.drawLine(int(px), int(y), int(px + pw), int(y))
            painter.drawText(int(px + pw + 4), int(y + 4), f"{label} {price:.2f}")

    # --------------------------------------------------------------- interact
    def _y_to_price(self, y_pixel):
        plot_y = self._margin_top
        plot_h = self.height() - self._margin_top - self._margin_bottom
        if plot_h <= 0:
            return (self._price_min + self._price_max) / 2
        frac = 1.0 - (y_pixel - plot_y) / plot_h
        return self._price_min + frac * (self._price_max - self._price_min)

    def event(self, event):
        if event.type() == event.Type.Gesture:
            return self._gesture_event(event)
        return super().event(event)

    def _gesture_event(self, event):
        pinch = event.gesture(Qt.PinchGesture)
        if pinch is None:
            return False
        if self._price_max <= self._price_min:
            return True
        scale = pinch.scaleFactor()
        if scale == 0 or scale == 1.0:
            return True
        zoom_factor = 1.0 / scale
        center = pinch.centerPoint()
        anchor_price = self._y_to_price(center.y())
        new_min = anchor_price - (anchor_price - self._price_min) * zoom_factor
        new_max = anchor_price + (self._price_max - anchor_price) * zoom_factor
        mid = (new_min + new_max) / 2
        if mid > 0 and (new_max - new_min) < mid * 0.00002:
            return True
        self._auto_scale = False
        self._price_min = new_min
        self._price_max = new_max
        self.update()
        return True

    def wheelEvent(self, event):
        if self._price_max <= self._price_min:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        zoom_factor = 0.85 if delta > 0 else 1.0 / 0.85
        anchor_price = self._y_to_price(event.position().y())
        new_min = anchor_price - (anchor_price - self._price_min) * zoom_factor
        new_max = anchor_price + (self._price_max - anchor_price) * zoom_factor
        mid = (new_min + new_max) / 2
        if mid > 0 and (new_max - new_min) < mid * 0.00002:
            return
        self._auto_scale = False
        self._price_min = new_min
        self._price_max = new_max
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start_y = event.position().y()
            self._drag_start_price_min = self._price_min
            self._drag_start_price_max = self._price_max
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = event.position().y() - self._drag_start_y
            plot_h = self.height() - self._margin_top - self._margin_bottom
            if plot_h <= 0:
                return
            price_range = self._drag_start_price_max - self._drag_start_price_min
            price_shift = (dy / plot_h) * price_range
            self._auto_scale = False
            self._price_min = self._drag_start_price_min + price_shift
            self._price_max = self._drag_start_price_max + price_shift
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event):
        self._auto_scale = True
        self.setCursor(Qt.ArrowCursor)
        self.update()
