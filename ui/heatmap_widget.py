import math
import time
import numpy as np
from collections import deque
from datetime import datetime, timezone
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QImage

# --------------- tunable configuration ---------------
VISIBLE_WINDOW_MS = 60_000
HEATMAP_SLICE_MS = 100
MIN_BUBBLE_RADIUS = 2
MAX_BUBBLE_RADIUS = 14
BUBBLE_SCALE = 8.0
BUBBLE_ALPHA = 183
P95_UPDATE_INTERVAL = 500

# Time-axis tick intervals to choose from (ms, label format)
_TIME_TICK_CANDIDATES = [
    (1_000, "%H:%M:%S"),
    (2_000, "%H:%M:%S"),
    (5_000, "%H:%M:%S"),
    (10_000, "%H:%M:%S"),
    (15_000, "%H:%M:%S"),
    (30_000, "%H:%M:%S"),
    (60_000, "%H:%M"),
    (120_000, "%H:%M"),
    (300_000, "%H:%M"),
    (600_000, "%H:%M"),
]

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
    lut[:, 3] = 255
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
        lut[i] = [r, g, b, 255]
    return lut


_HEAT_LUT = _build_heat_lut()


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
        self._visible_window_ms = VISIBLE_WINDOW_MS
        self._slice_ms = HEATMAP_SLICE_MS

        self._cur_bids = {}
        self._cur_asks = {}

        self._trades = deque(maxlen=100_000)
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

    @property
    def plot_margin_top(self) -> int:
        return self._margin_top

    @property
    def plot_margin_bottom(self) -> int:
        return self._margin_bottom

    # ------------------------------------------------------------------ data

    @property
    def chart_now(self) -> int:
        """Unified 'now' timestamp — max of depth and trade streams."""
        return max(self._last_depth_ts, self._last_trade_ts)

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
        # Prune trades using the true visible edge (chart_now - window),
        # with a generous buffer so bubbles near the left edge remain
        # visible during rendering.  Do NOT use slice_ts which can jump
        # forward and over-prune.
        trade_cutoff = self.chart_now - self._visible_window_ms - 5000
        while self._trades and self._trades[0][0] < trade_cutoff:
            self._trades.popleft()

        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0
        if mid > 0 and self._auto_scale:
            if best_bid > 0:
                self._auto_scale_bid_lo = min(self._auto_scale_bid_lo, best_bid)
            if best_ask > 0:
                self._auto_scale_ask_hi = max(self._auto_scale_ask_hi, best_ask)

            if pruned:
                lo = float('inf')
                hi = float('-inf')
                step = max(1, len(self._slices) // 60)
                for idx in range(0, len(self._slices), step):
                    s = self._slices[idx]
                    if s[3] > 0 and s[3] < lo:
                        lo = s[3]
                    if s[4] > 0 and s[4] > hi:
                        hi = s[4]
                if self._slices:
                    s = self._slices[-1]
                    if s[3] > 0 and s[3] < lo:
                        lo = s[3]
                    if s[4] > 0 and s[4] > hi:
                        hi = s[4]
                if lo < float('inf') and hi > float('-inf'):
                    self._auto_scale_bid_lo = lo
                    self._auto_scale_ask_hi = hi

            lo = self._auto_scale_bid_lo
            hi = self._auto_scale_ask_hi

            # Widen the range to include recent trade prices so bubbles
            # are never pushed off-screen by a narrow depth-only range.
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
                if span < mid * 0.0004:
                    span = mid * 0.0004
                margin = span * 0.25
                new_min = lo - margin
                new_max = hi + margin
                if math.isfinite(new_min) and math.isfinite(new_max):
                    self._price_min = new_min
                    self._price_max = new_max

    def add_trade(self, timestamp, price, quantity, is_buy):
        if price <= 0:
            return
        self._trades.append((timestamp, price, quantity, is_buy))
        self._trade_sizes.append(quantity)
        self._trades_added += 1
        if timestamp > self._last_trade_ts:
            self._last_trade_ts = timestamp
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
            if span < lo * 0.0004:
                span = lo * 0.0004
            margin = span * 0.25
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
        self._draw_time_axis(painter, px, py, pw, ph, t_start, now)

        if not have_heatmap and self._book_empty_ticks > 10:
            painter.setPen(self._depth_lost_color)
            painter.drawText(px + 4, py + 16,
                             "Depth lost — bubbles from trade feed")

        painter.end()
        self._last_paint_ms = (time.monotonic() - _pt0) * 1000.0

    def _draw_depth(self, painter, px, py, pw, ph, pr, t_start, now):
        # One extra column so the newest slice (at ~now) is always in-bounds.
        n_img_cols = self._visible_window_ms // self._slice_ms + 1
        n_rows = min(int(ph), 400)
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

        # Quantize t_start to a slice boundary for stable integer column
        # mapping.  The fractional remainder drives sub-pixel scrolling.
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
            for dr in range(-1, 2):
                shifted = rows + dr
                mask = (shifted >= 0) & (shifted < n_rows)
                if mask.any():
                    np.maximum.at(col_slice, shifted[mask], vals[mask])

            prev_data_id = data_id
            prev_col = col

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

    def _draw_bubbles(self, painter, px, py, pw, ph, pr, t_start, now,
                      *, pmin_override=None):
        if not self._trades:
            return

        ts_span = float(self._visible_window_ms)
        if ts_span <= 0:
            return
        ref = self._p95_size
        pmin = pmin_override if pmin_override is not None else self._price_min
        inv_ts = pw / ts_span
        inv_pr = ph / pr if pr > 0 else 0.0

        buy_color = self._buy_color
        sell_color = self._sell_color

        painter.setPen(Qt.NoPen)
        visible_count = 0
        for ts, price, qty, is_buy in self._trades:
            if ts < t_start:
                continue

            x = px + (ts - t_start) * inv_ts
            if x > px + pw + MAX_BUBBLE_RADIUS:
                continue

            y = py + ph - (price - pmin) * inv_pr

            if y < py - MAX_BUBBLE_RADIUS or y > py + ph + MAX_BUBBLE_RADIUS:
                continue

            norm = qty / ref
            radius = MIN_BUBBLE_RADIUS + math.sqrt(max(norm, 0.0)) * BUBBLE_SCALE
            radius = max(MIN_BUBBLE_RADIUS, min(MAX_BUBBLE_RADIUS, radius))

            painter.setBrush(buy_color if is_buy else sell_color)
            painter.drawEllipse(QRectF(x - radius, y - radius,
                                       radius * 2, radius * 2))
            visible_count += 1

        self._last_visible_bubble_count = visible_count

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

    def _draw_time_axis(self, painter, px, py, pw, ph, t_start, now):
        """Render time tick labels along the bottom of the chart."""
        if now <= t_start or pw <= 0:
            return

        ts_span = float(now - t_start)

        # Choose tick interval: aim for 6-12 ticks across the width
        target_ticks = max(3, min(12, int(pw / 80)))
        ideal_interval = ts_span / target_ticks

        tick_ms = _TIME_TICK_CANDIDATES[-1][0]
        label_fmt = _TIME_TICK_CANDIDATES[-1][1]
        for interval, fmt in _TIME_TICK_CANDIDATES:
            if interval >= ideal_interval * 0.5:
                tick_ms = interval
                label_fmt = fmt
                break

        painter.setFont(self._font_time)

        # Grid lines (subtle)
        grid_pen = QPen(self._grid_color, 0.5, Qt.DotLine)
        text_pen = QPen(self._text_color)

        first_tick = ((t_start // tick_ms) + 1) * tick_ms
        y_base = py + ph + 2
        y_label = py + ph + 14

        tick_ts = first_tick
        while tick_ts <= now:
            x = px + (tick_ts - t_start) / ts_span * pw

            if px <= x <= px + pw:
                # Vertical grid line through heatmap
                painter.setPen(grid_pen)
                painter.drawLine(int(x), py, int(x), py + ph)

                # Time label below
                painter.setPen(text_pen)
                dt = datetime.fromtimestamp(tick_ts / 1000.0, tz=timezone.utc)
                label = dt.strftime(label_fmt)
                painter.drawText(int(x) - 20, y_label, label)

            tick_ts += tick_ms

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
