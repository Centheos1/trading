"""Candle chart overlays.

Overlay callable contract (matches CandleChartView._draw_overlays_fn):

    fn(painter, px, py, pw, ph, pmin, pmax, visible)

where:
    painter: active QPainter
    px, py:  top-left of the candle plot area (ints)
    pw, ph:  plot width / height (ints — already excludes margins and
             volume zone)
    pmin, pmax: visible price range (floats; pmax > pmin guaranteed by caller)
    visible: list of `_Candle` objects in display order (oldest -> newest)

CandleChartView wraps each overlay call in a try/except, so overlay code
must be safe against empty / pathological `visible` lists, but should
still bail out early on degenerate inputs to keep paint time low.

All overlays are pure callables (`__call__` matching the signature) so
they can be registered via `chart.register_overlay(SmaOverlay(20))`.
"""
from __future__ import annotations

from typing import ClassVar, Iterable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainterPath, QPen


__all__ = [
    "SmaOverlay",
    "EmaOverlay",
    "VwapOverlay",
    "StructuralLevelsOverlay",
    "VolProfileOverlay",
]


# ────────────────────────────────────────────────────────────── helpers


def _price_to_y(price: float, py: int, ph: int,
                pmin: float, pmax: float) -> float:
    pr = pmax - pmin
    if pr <= 0:
        return float(py)
    return py + (1.0 - (price - pmin) / pr) * ph


def _x_for_index(i: int, n: int, px: int, pw: int) -> float:
    """Map candle index i in [0, n-1] to the centre-x of that candle.

    Mirrors CandleChartView's drawing math (linear step across the
    plot area).
    """
    if n <= 0:
        return float(px)
    step = pw / max(n, 1)
    return px + step * (i + 0.5)


# ────────────────────────────────────────────────────────────── B1: SMA


class SmaOverlay:
    """Simple moving average over candle close prices.

    `period` is the look-back length. The overlay draws a polyline
    starting at index `period - 1` (the first index with a full window).
    """

    # Phase 9D — stable identifier for the QML toggle row.
    OVERLAY_NAME: ClassVar[str] = "sma"
    # Phase 9D — instance-overridable; the candle chart skips overlays
    # where ``enabled`` is falsy.
    enabled: bool = True

    def __init__(self, period: int = 20,
                 color: QColor = QColor(255, 200, 80, 220),
                 width: int = 2):
        if period < 2:
            raise ValueError("SMA period must be >= 2")
        self.period = int(period)
        self.color = QColor(color)
        self.width = int(width)

    def __call__(self, painter, px, py, pw, ph, pmin, pmax, visible):
        n = len(visible)
        if n < self.period or pmax <= pmin or pw <= 0 or ph <= 0:
            return
        # Rolling-sum SMA in O(n)
        period = self.period
        sums = [0.0] * n
        sums[0] = visible[0].c
        for i in range(1, n):
            sums[i] = sums[i - 1] + visible[i].c
        path = QPainterPath()
        started = False
        for i in range(period - 1, n):
            window_sum = sums[i] - (sums[i - period] if i >= period else 0.0)
            avg = window_sum / period
            x = _x_for_index(i, n, px, pw)
            y = _price_to_y(avg, py, ph, pmin, pmax)
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
        pen = QPen(self.color, self.width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)


# ────────────────────────────────────────────────────────────── B1: EMA


class EmaOverlay:
    """Exponential moving average over candle close prices.

    Uses standard alpha = 2 / (period + 1). Warm-up: the first value is
    seeded with the simple mean of the first `period` candles, so the
    line starts at index `period - 1` (matching SMA visual).
    """

    OVERLAY_NAME: ClassVar[str] = "ema"
    enabled: bool = True

    def __init__(self, period: int = 50,
                 color: QColor = QColor(120, 180, 255, 220),
                 width: int = 2):
        if period < 2:
            raise ValueError("EMA period must be >= 2")
        self.period = int(period)
        self.color = QColor(color)
        self.width = int(width)

    def __call__(self, painter, px, py, pw, ph, pmin, pmax, visible):
        n = len(visible)
        if n < self.period or pmax <= pmin or pw <= 0 or ph <= 0:
            return
        period = self.period
        alpha = 2.0 / (period + 1.0)
        seed = sum(visible[i].c for i in range(period)) / period
        ema = seed
        path = QPainterPath()
        x = _x_for_index(period - 1, n, px, pw)
        y = _price_to_y(ema, py, ph, pmin, pmax)
        path.moveTo(x, y)
        for i in range(period, n):
            ema = alpha * visible[i].c + (1.0 - alpha) * ema
            x = _x_for_index(i, n, px, pw)
            y = _price_to_y(ema, py, ph, pmin, pmax)
            path.lineTo(x, y)
        pen = QPen(self.color, self.width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)


# ────────────────────────────────────────────────────────────── B2: VWAP


class VwapOverlay:
    """Session VWAP across the visible candles.

    VWAP is computed as a running sum(close * volume) / sum(volume) so
    each candle's plotted point reflects the cumulative VWAP at that
    point in time. Candles with zero volume contribute nothing.

    Note: This is a close-price proxy because tick-level price*qty
    aggregates aren't available from `_Candle` data. Acceptable for a
    visual reference; not a precise tick VWAP.
    """

    OVERLAY_NAME: ClassVar[str] = "vwap"
    enabled: bool = True

    def __init__(self,
                 color: QColor = QColor(180, 100, 220, 220),
                 width: int = 2):
        self.color = QColor(color)
        self.width = int(width)

    def __call__(self, painter, px, py, pw, ph, pmin, pmax, visible):
        n = len(visible)
        if n < 2 or pmax <= pmin or pw <= 0 or ph <= 0:
            return
        cum_pv = 0.0
        cum_v = 0.0
        path = QPainterPath()
        started = False
        for i, c in enumerate(visible):
            if c.vol > 0:
                cum_pv += c.c * c.vol
                cum_v += c.vol
            if cum_v <= 0:
                continue
            vwap = cum_pv / cum_v
            x = _x_for_index(i, n, px, pw)
            y = _price_to_y(vwap, py, ph, pmin, pmax)
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
        if not started:
            return
        pen = QPen(self.color, self.width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)


# ────────────────────────────────────── B3: Structural levels (H/L, ATH/ATL)


class StructuralLevelsOverlay:
    """Horizontal dashed lines at session high / low.

    `extra_candles` (optional iterable) lets the caller provide a wider
    deque (e.g. the full in-memory candle store) so ATH / ATL can be
    drawn over a window larger than the on-screen `visible` slice. When
    `extra_candles` is None, the session H/L are derived from the
    visible window only.
    """

    OVERLAY_NAME: ClassVar[str] = "structural"
    enabled: bool = True

    def __init__(self,
                 session_color: QColor = QColor(220, 220, 220, 160),
                 ath_color: QColor = QColor(40, 220, 130, 200),
                 atl_color: QColor = QColor(230, 80, 80, 200),
                 width: int = 1,
                 extra_candles: Optional[Iterable] = None):
        self.session_color = QColor(session_color)
        self.ath_color = QColor(ath_color)
        self.atl_color = QColor(atl_color)
        self.width = int(width)
        self._extra = extra_candles  # iterable or None (resolved per-call)

    def set_extra_candles(self, extra):
        self._extra = extra

    @staticmethod
    def _hi_lo(candles) -> Optional[tuple[float, float]]:
        hi = float('-inf')
        lo = float('inf')
        any_seen = False
        for c in candles:
            if getattr(c, 'trades', 0) <= 0:
                continue
            hi = max(hi, c.h)
            lo = min(lo, c.l)
            any_seen = True
        if not any_seen:
            return None
        return hi, lo

    @staticmethod
    def _draw_dashed_line(painter, color, width, px, pw, y, label=None):
        pen = QPen(color, width, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(px, int(y), px + pw, int(y))
        if label:
            painter.setPen(color)
            painter.drawText(px + 4, int(y) - 2, label)

    def __call__(self, painter, px, py, pw, ph, pmin, pmax, visible):
        if not visible or pmax <= pmin or pw <= 0 or ph <= 0:
            return
        sess = self._hi_lo(visible)
        if sess is None:
            return
        sess_hi, sess_lo = sess

        def in_range(p):
            return pmin <= p <= pmax

        # Session H / L
        if in_range(sess_hi):
            y = _price_to_y(sess_hi, py, ph, pmin, pmax)
            self._draw_dashed_line(painter, self.session_color,
                                   self.width, px, pw, y,
                                   label=f"H {sess_hi:.2f}")
        if in_range(sess_lo):
            y = _price_to_y(sess_lo, py, ph, pmin, pmax)
            self._draw_dashed_line(painter, self.session_color,
                                   self.width, px, pw, y,
                                   label=f"L {sess_lo:.2f}")

        # Optional ATH / ATL from a wider window
        if self._extra is not None:
            try:
                wide = list(self._extra)
            except TypeError:
                wide = []
            wide_stats = self._hi_lo(wide) if wide else None
            if wide_stats is not None:
                ath, atl = wide_stats
                # Only draw if they extend the visible session range
                if ath > sess_hi and in_range(ath):
                    y = _price_to_y(ath, py, ph, pmin, pmax)
                    self._draw_dashed_line(painter, self.ath_color,
                                           self.width, px, pw, y,
                                           label=f"ATH {ath:.2f}")
                if atl < sess_lo and in_range(atl):
                    y = _price_to_y(atl, py, ph, pmin, pmax)
                    self._draw_dashed_line(painter, self.atl_color,
                                           self.width, px, pw, y,
                                           label=f"ATL {atl:.2f}")


# ────────────────────────────────────── B4: Volume Profile right-edge bar


class VolProfileOverlay:
    """Right-edge horizontal histogram of volume bucketed by price.

    Buckets `visible` candles into `n_bins` price bins (using each
    candle's mid-price h+l/2 weighted by its volume). The widest bin is
    the POC (Point of Control) and is highlighted.

    `width_px` controls the maximum overlay width; the bar starts at
    `px + pw` (right edge of plot) and grows leftward into the chart.
    """

    OVERLAY_NAME: ClassVar[str] = "volprofile"
    enabled: bool = True

    def __init__(self,
                 n_bins: int = 32,
                 width_px: int = 60,
                 fill: QColor = QColor(200, 200, 240, 60),
                 poc: QColor = QColor(255, 220, 80, 200),
                 outline: QColor = QColor(180, 180, 220, 120)):
        if n_bins < 4:
            raise ValueError("n_bins must be >= 4")
        self.n_bins = int(n_bins)
        self.width_px = int(width_px)
        self.fill = QColor(fill)
        self.poc = QColor(poc)
        self.outline = QColor(outline)

    def __call__(self, painter, px, py, pw, ph, pmin, pmax, visible):
        if not visible or pmax <= pmin or pw <= 0 or ph <= 0:
            return
        pr = pmax - pmin
        if pr <= 0:
            return
        bins = [0.0] * self.n_bins
        max_bin_idx = self.n_bins - 1
        for c in visible:
            if c.vol <= 0 or c.trades <= 0:
                continue
            mid = 0.5 * (c.h + c.l)
            if mid <= pmin or mid >= pmax:
                continue
            bi = int((mid - pmin) / pr * self.n_bins)
            if bi < 0:
                bi = 0
            elif bi > max_bin_idx:
                bi = max_bin_idx
            bins[bi] += c.vol
        max_v = max(bins)
        if max_v <= 0:
            return
        poc_idx = bins.index(max_v)
        bar_h = ph / self.n_bins
        right_x = px + pw
        # Draw from highest price (top) to lowest (bottom)
        for bi, v in enumerate(bins):
            if v <= 0:
                continue
            frac = v / max_v
            bw = self.width_px * frac
            # bin bi covers price [pmin + bi * pr/n, pmin + (bi+1)*pr/n)
            # higher bi -> higher price -> smaller y (top)
            y_top = py + (1.0 - (bi + 1) / self.n_bins) * ph
            color = self.poc if bi == poc_idx else self.fill
            painter.fillRect(int(right_x - bw), int(y_top),
                             int(bw), max(1, int(bar_h)),
                             color)
        # Outline the histogram strip
        pen = QPen(self.outline, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(int(right_x - self.width_px), py,
                         int(right_x - self.width_px), py + ph)
