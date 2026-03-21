"""Thin rendering widget for the Order Flow heatmap view.

All data storage and heavy computation lives in OrderFlowViewModel.
This widget only draws pre-computed FrameData produced by the ViewModel.

Data classes (TimeBucket, TradeBucketAggregate, TradeSlice) and tunable
constants are defined here so existing imports keep working.
"""
import logging
import math
import time
import numpy as np
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QImage, QPainterPath, QNativeGestureEvent

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
AUTO_SCALE_PCTILE_LO = 2          # percentile for lower bound of depth range
AUTO_SCALE_PCTILE_HI = 98         # percentile for upper bound of depth range
DEFAULT_ZOOM_FRACTION = 0.002     # default half-span as fraction of mid-price (+/- ~$134 @ 67k)
DEPTH_NORM_PCTILE = 95            # normalize intensity to this percentile (orders above saturate to red)
DEPTH_GAMMA = 0.55                # gamma < 1 expands contrast in the moderate-liquidity zone

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
    """Aggregated trade data for a single (time-slice, price-bucket) cell."""
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
    """All trade aggregates within a single TRADE_SLICE_MS time bucket."""
    __slots__ = ('bucket_start_ms', 'price_levels')

    def __init__(self, bucket_start_ms: int):
        self.bucket_start_ms: int = bucket_start_ms
        self.price_levels: dict[int, TradeBucketAggregate] = {}


class HeatmapWidget(QWidget):
    """Thin renderer for the Order Flow heatmap.

    All data storage and computation lives in OrderFlowViewModel.
    This widget receives a FrameData via set_frame() and draws it.
    Mouse interaction (zoom/pan) delegates to the ViewModel via _vm.

    For backward compatibility, if no viewmodel is passed at construction,
    one is created automatically (used by tests and standalone usage).
    """

    def __init__(self, parent=None, viewmodel=None, candle_store=None):
        super().__init__(parent)
        self.setMinimumSize(600, 400)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.grabGesture(Qt.PinchGesture)

        if viewmodel is not None:
            self._vm = viewmodel
        else:
            from ui.orderflow_viewmodel import OrderFlowViewModel
            self._vm = OrderFlowViewModel(candle_store=candle_store)

        self._frame = None    # current FrameData to render

        self._last_paint_ms = 0.0
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_price_min = 0.0
        self._drag_start_price_max = 0.0

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

        self._overlay_entry_color = QColor(220, 220, 230)
        self._overlay_stop_color = QColor(220, 60, 60, 200)
        self._overlay_target_color = QColor(60, 200, 100, 200)

    # ------- ViewModel / frame access used by MainWindow -------

    def set_viewmodel(self, vm):
        self._vm = vm

    def set_frame(self, frame):
        """Store the pre-computed FrameData for the next paint."""
        self._frame = frame

    def get_viewport_params(self):
        """Return (pw, ph, margin_left, margin_top, chart_right_inset)."""
        pw = self.width() - self._margin_left - self._margin_right
        ph = self.height() - self._margin_top - self._margin_bottom
        return pw, ph, self._margin_left, self._margin_top, self._chart_right_inset

    @property
    def plot_margin_top(self) -> int:
        return self._margin_top

    @property
    def plot_margin_bottom(self) -> int:
        return self._margin_bottom

    # ------- Backward-compat properties (delegate to VM) -------

    @property
    def chart_now(self) -> int:
        return self._vm.chart_now if self._vm else 0

    @property
    def trade_feed_stale(self) -> bool:
        return self._vm.trade_feed_stale if self._vm else False

    @property
    def _visible_window_ms(self):
        return self._vm.visible_window_ms if self._vm else 60_000

    @property
    def _bucket_duration_ms(self):
        return self._vm.bucket_duration_ms if self._vm else 60_000

    @property
    def _price_min(self):
        return self._vm.price_min if self._vm else 0.0

    @_price_min.setter
    def _price_min(self, val):
        if self._vm:
            self._vm._price_min = val

    @property
    def _price_max(self):
        return self._vm.price_max if self._vm else 0.0

    @_price_max.setter
    def _price_max(self, val):
        if self._vm:
            self._vm._price_max = val

    @property
    def _last_depth_ts(self):
        return self._vm._last_depth_ts if self._vm else 0

    @_last_depth_ts.setter
    def _last_depth_ts(self, val):
        if self._vm:
            self._vm._last_depth_ts = val

    @property
    def _last_trade_ts(self):
        return self._vm._last_trade_ts if self._vm else 0

    @_last_trade_ts.setter
    def _last_trade_ts(self, val):
        if self._vm:
            self._vm._last_trade_ts = val

    @property
    def _trades(self):
        return self._vm._trades if self._vm else deque()

    @property
    def _slices(self):
        return self._vm._slices if self._vm else deque()

    @property
    def _auto_scale(self):
        return self._vm._auto_scale if self._vm else True

    @_auto_scale.setter
    def _auto_scale(self, val):
        if self._vm:
            self._vm._auto_scale = val

    @property
    def _book_empty_ticks(self):
        return self._vm._book_empty_ticks if self._vm else 0

    @property
    def _last_visible_bubble_count(self):
        return self._vm._last_visible_bubble_count if self._vm else 0

    @property
    def _p95_size(self):
        return self._vm._p95_size if self._vm else 1.0

    @property
    def _trade_slices(self):
        return self._vm._trade_slices if self._vm else {}

    @property
    def _slice_price_bucket_size(self):
        return self._vm._slice_price_bucket_size if self._vm else 0.0

    @property
    def _buckets(self):
        return self._vm._buckets if self._vm else deque()

    @property
    def _slice_ms(self):
        return self._vm._slice_ms if self._vm else HEATMAP_SLICE_MS

    @property
    def _cur_log_max(self):
        return self._vm._cur_log_max if self._vm else 0.0

    @property
    def _trades_added(self):
        return self._vm._trades_added if self._vm else 0

    @property
    def _heatmap_samples(self):
        return self._vm._heatmap_samples if self._vm else 0

    @property
    def _last_log_max(self):
        return self._vm._last_log_max if self._vm else 0.0

    @property
    def _missed_slices(self):
        return self._vm._missed_slices if self._vm else 0

    @property
    def _depth_updates(self):
        return self._vm._depth_updates if self._vm else 0

    @property
    def _trades_dropped(self):
        return self._vm._trades_dropped if self._vm else 0

    @property
    def _overlay_entry_price(self):
        return self._vm._overlay_entry_price if self._vm else 0.0

    @property
    def _overlay_stop_price(self):
        return self._vm._overlay_stop_price if self._vm else 0.0

    @property
    def _overlay_target_price(self):
        return self._vm._overlay_target_price if self._vm else 0.0

    @property
    def _bubble_diag(self):
        return self._vm._bubble_diag if self._vm else {}

    @property
    def _MAX_DEPTH_LEAD_MS(self):
        return self._vm._MAX_DEPTH_LEAD_MS if self._vm else 5000

    @staticmethod
    def _forward_fill_intensity(intensity):
        """Delegate to ViewModel's static method for backward compat."""
        from ui.orderflow_viewmodel import OrderFlowViewModel
        return OrderFlowViewModel._forward_fill_intensity(intensity)

    def _draw_bubbles(self, painter, px, py, pw, ph, pr, t_start, now,
                      *, pmin_override=None):
        """Backward-compat shim: compute bubble paths via ViewModel,
        then draw them immediately.  Used by tests only."""
        if not self._vm:
            return
        from ui.orderflow_viewmodel import FrameData
        pmin = pmin_override if pmin_override is not None else self._vm.price_min
        frame = FrameData()
        frame.price_min = pmin
        frame.price_max = pmin + pr
        frame.price_range = pr
        frame.chart_now = now
        frame.t_start = t_start
        self._vm._compute_bubbles(
            frame, px, py, pw, ph, pr, pmin, t_start, now)
        painter.setPen(Qt.NoPen)
        if not frame.buy_path.isEmpty():
            painter.setBrush(self._buy_color)
            painter.drawPath(frame.buy_path)
        if not frame.sell_path.isEmpty():
            painter.setBrush(self._sell_color)
            painter.drawPath(frame.sell_path)

    # ------- Delegate methods for MainWindow backward compat -------

    def set_bucket_duration_ms(self, ms: int):
        if self._vm:
            self._vm.set_bucket_duration_ms(ms)

    def add_trade(self, *args, **kwargs):
        if self._vm:
            self._vm.add_trade(*args, **kwargs)

    def add_depth_column(self, *args, **kwargs):
        if self._vm:
            self._vm.add_depth_column(*args, **kwargs)

    def sync_trade_time(self, ws_trade_ts: int):
        if self._vm:
            self._vm.sync_trade_time(ws_trade_ts)

    def add_signal(self, *args, **kwargs):
        if self._vm:
            self._vm.add_signal(*args, **kwargs)

    def set_strategy_overlay(self, *args, **kwargs):
        if self._vm:
            self._vm.set_strategy_overlay(*args, **kwargs)

    def clear_strategy_overlay(self):
        if self._vm:
            self._vm.clear_strategy_overlay()

    def get_bubble_diagnostics(self) -> dict:
        if self._vm:
            return self._vm.get_bubble_diagnostics()
        return {}

    # ------- paintEvent — thin renderer -------

    def paintEvent(self, event):
        _pt0 = time.monotonic()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self._bg_color)

        frame = self._frame
        if frame is None:
            painter.setPen(self._text_color)
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for data...")
            painter.end()
            return

        px = self._margin_left
        py = self._margin_top
        pw_full = self.width() - self._margin_left - self._margin_right
        ph = self.height() - self._margin_top - self._margin_bottom
        pw = pw_full - self._chart_right_inset

        if pw <= 0 or ph <= 0:
            painter.end()
            return

        pmin = frame.price_min
        pmax = frame.price_max
        pr = frame.price_range
        now = frame.chart_now
        t_start = frame.t_start
        have_heatmap = frame.have_heatmap

        if pr <= 0:
            painter.setPen(self._text_color)
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for data...")
            painter.end()
            return

        # --- Draw depth heatmap image (pre-computed by ViewModel) ---
        if have_heatmap and frame.depth_image is not None:
            img = frame.depth_image
            n_img_cols = img.width()
            n_rows = img.height()
            col_px = pw / max(n_img_cols - 1, 1)
            shift = frame.depth_shift_px

            painter.save()
            painter.setClipRect(QRectF(px, py, pw, ph))
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(
                QRectF(px - shift, py, pw + col_px, ph),
                img,
                QRectF(0, 0, n_img_cols, n_rows))
            painter.restore()

        # --- Draw bubble paths (pre-computed by ViewModel) ---
        painter.setPen(Qt.NoPen)
        if not frame.buy_path.isEmpty():
            painter.setBrush(self._buy_color)
            painter.drawPath(frame.buy_path)
        if not frame.sell_path.isEmpty():
            painter.setBrush(self._sell_color)
            painter.drawPath(frame.sell_path)

        # --- Draw axes ---
        self._draw_axes(painter, px, py, pw, ph, pr, pmin_override=pmin)
        self._draw_bucket_axis(painter, px, py, pw, ph, t_start, now)
        self._draw_strategy_overlay(painter, px, py, pw, ph, pmin, pr)

        if not have_heatmap and frame.book_empty_ticks > 10:
            painter.setPen(self._depth_lost_color)
            painter.drawText(px + 4, py + 16,
                             "Depth lost — bubbles from trade feed")

        if frame.trade_feed_stale:
            painter.setPen(self._depth_lost_color)
            painter.drawText(px + 4, py + ph - 4,
                             f"Trade feed stale ({frame.depth_trade_skew_s:.0f}s)")

        painter.end()
        self._last_paint_ms = (time.monotonic() - _pt0) * 1000.0

    def _draw_axes(self, painter, px, py, pw, ph, pr, *, pmin_override=None):
        painter.setPen(QPen(self._grid_color, 1))
        painter.drawLine(px, py, px, py + ph)
        painter.drawLine(px, py + ph, px + pw, py + ph)

        painter.setFont(self._font_axis)
        painter.setPen(self._text_color)

        base_pmin = pmin_override if pmin_override is not None else 0.0
        n_labels = min(10, int(ph / 40))
        for i in range(n_labels + 1):
            price = base_pmin + (pr * i / max(n_labels, 1))
            y = py + ph - (i / max(n_labels, 1)) * ph
            painter.drawText(5, int(y + 4), f"{price:.2f}")
            painter.setPen(QPen(self._grid_color, 0.5, Qt.DotLine))
            painter.drawLine(px, int(y), px + pw, int(y))
            painter.setPen(self._text_color)

        if self._vm and not self._vm._auto_scale:
            painter.setPen(QColor(255, 200, 60))
            painter.setFont(self._font_zoom)
            painter.drawText(px + 4, py + 12, "MANUAL ZOOM  (dbl-click to reset)")

    def _draw_bucket_axis(self, painter, px, py, pw, ph, t_start, now):
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
        if pr <= 0 or not self._vm:
            return
        lines = [
            (self._vm._overlay_entry_price,  self._overlay_entry_color,  Qt.SolidLine, "Entry"),
            (self._vm._overlay_stop_price,   self._overlay_stop_color,   Qt.DashLine,  "Stop"),
            (self._vm._overlay_target_price, self._overlay_target_color, Qt.DashLine,  "Target"),
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

    # ------- Mouse interaction (delegates price changes to VM) -------

    def _y_to_price(self, y_pixel):
        plot_y = self._margin_top
        plot_h = self.height() - self._margin_top - self._margin_bottom
        if plot_h <= 0 or not self._vm:
            return 0.0
        frac = 1.0 - (y_pixel - plot_y) / plot_h
        return self._vm.price_min + frac * (self._vm.price_max - self._vm.price_min)

    def event(self, event):
        if event.type() == event.Type.Gesture:
            return self._gesture_event(event)
        if event.type() == event.Type.NativeGesture:
            return self._native_gesture_event(event)
        return super().event(event)

    def _native_gesture_event(self, event):
        """Handle macOS trackpad pinch (ZoomNativeGesture)."""
        if not self._vm:
            return False
        if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            # value() is the incremental scale factor delta (e.g. +0.05 or -0.05)
            delta = event.value()
            scale = 1.0 + delta
            if scale <= 0:
                return True
            new_frac = self._vm.zoom_fraction / scale
            self._vm.set_zoom_fraction(new_frac)
            self._vm._auto_scale = False
            self.update()
            return True
        return False

    def _gesture_event(self, event):
        pinch = event.gesture(Qt.PinchGesture)
        if pinch is None or not self._vm:
            return False
        if self._vm.price_max <= self._vm.price_min:
            return True
        scale = pinch.scaleFactor()
        if scale == 0 or scale == 1.0:
            return True
        self._apply_zoom(scale, anchor_price=self._y_to_price(
            pinch.centerPoint().y()))
        return True

    def _native_gesture_event(self, event):
        """Handle macOS trackpad pinch (ZoomNativeGesture)."""
        if not self._vm:
            return False
        if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            # value() is the incremental magnification delta (+0.05 = 5% bigger)
            scale = 1.0 + event.value()
            if scale <= 0:
                return True
            self._apply_zoom(scale,
                             anchor_price=self._y_to_price(event.position().y()))
            return True
        return False

    def _apply_zoom(self, scale: float, anchor_price: float):
        """Zoom around anchor_price by scale factor.  scale > 1 = zoom out."""
        vm = self._vm
        if vm.price_max <= vm.price_min or scale == 0:
            return
        zoom_factor = 1.0 / scale
        new_min = anchor_price - (anchor_price - vm.price_min) * zoom_factor
        new_max = anchor_price + (vm.price_max - anchor_price) * zoom_factor
        mid = (new_min + new_max) / 2
        if mid > 0 and (new_max - new_min) < mid * 0.00002:
            return
        # Keep zoom_fraction in sync so double-click reset is proportional
        if mid > 0:
            vm.set_zoom_fraction((new_max - new_min) / 2.0 / mid)
        vm.set_price_range(new_min, new_max)
        self.update()

    def wheelEvent(self, event):
        if not self._vm or self._vm.price_max <= self._vm.price_min:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        if delta == 0:
            return
        scale = 1.0 / 0.85 if delta > 0 else 0.85
        self._apply_zoom(scale, anchor_price=self._y_to_price(event.position().y()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._vm:
            self._dragging = True
            self._drag_start_y = event.position().y()
            self._drag_start_price_min = self._vm.price_min
            self._drag_start_price_max = self._vm.price_max
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging and self._vm:
            dy = event.position().y() - self._drag_start_y
            plot_h = self.height() - self._margin_top - self._margin_bottom
            if plot_h <= 0:
                return
            price_range = self._drag_start_price_max - self._drag_start_price_min
            price_shift = (dy / plot_h) * price_range
            self._vm.set_price_range(
                self._drag_start_price_min + price_shift,
                self._drag_start_price_max + price_shift)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event):
        if self._vm:
            self._vm.set_auto_scale(True, reset_zoom=True)
        self.setCursor(Qt.ArrowCursor)
        self.update()
