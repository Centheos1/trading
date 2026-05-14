"""Candlestick chart view — Binance-style OHLCV chart in its own window.

Maintains its own candle store independent of the heatmap.  Trades are
fed in via process_trade() from the MainWindow drain loop.  The bucket
duration and visible window are controlled by an in-window toolbar,
completely decoupled from the Order Flow view's time axis.

Overlay hooks allow future indicators (SMA, EMA, Bollinger, etc.) to
paint on top without modifying this module.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, Signal as QtSignal
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QBrush

if TYPE_CHECKING:
    from ui.market_state import MarketState

MARGIN_LEFT = 10
MARGIN_RIGHT = 72
MARGIN_TOP = 10
MARGIN_BOTTOM = 28
VOLUME_ZONE_FRAC = 0.22
MIN_CANDLE_PX = 4
MAX_CANDLE_PX = 24
CANDLE_GAP_FRAC = 0.25

_BG = QColor(12, 14, 28)
_GRID = QColor(25, 28, 50)
_TEXT = QColor(130, 135, 165)
_BULL = QColor(14, 203, 129)
_BEAR = QColor(234, 57, 67)
_BULL_VOL = QColor(14, 203, 129, 80)
_BEAR_VOL = QColor(234, 57, 67, 80)
_PRICE_LINE = QColor(90, 95, 130, 100)
_PRICE_TAG_BG = QColor(50, 55, 90)

TIMEFRAMES = [
    ("1m",  60_000),
    ("5m",  300_000),
    ("15m", 900_000),
    ("30m", 1_800_000),
    ("1h",  3_600_000),
    ("4h",  14_400_000),
]

_VISIBLE_CANDLES = 80
_MAX_STORED_CANDLES = 1000

# Phase 8A — empty / loading state labels rendered by ``paintEvent`` when
# the deque is empty.  Centralised so tests can assert the exact strings
# without duplicating literals across modules (AGENT_STRATEGY_RULES.md
# §20).
_LABEL_WAITING = "Waiting for candle data\u2026"
_LABEL_LOADING = "Loading historical candles\u2026"

# Sentinel trade count given to preloaded candles so that a subsequent
# live ``process_trade`` lands in the ``c.trades > 0`` branch and
# updates h/l/c without resetting the preloaded open price.  See
# :meth:`CandleChartView.preload_candles`.
_PRELOAD_TRADE_COUNT = 1


@dataclass(slots=True)
class _Candle:
    ts: int = 0
    o: float = 0.0
    h: float = 0.0
    l: float = float('inf')
    c: float = 0.0
    vol: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    trades: int = 0


class CandleChartView(QWidget):
    """QPainter-based candlestick chart with independent candle bucketing."""

    timeframe_changed = QtSignal(int)

    def __init__(self, market_state: MarketState, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 400)
        self._ms = market_state

        self._bucket_ms: int = 60_000
        self._visible_candles: int = _VISIBLE_CANDLES
        self._candles: deque[_Candle] = deque(maxlen=_MAX_STORED_CANDLES)

        self._auto_scale = True
        self._price_min = 0.0
        self._price_max = 0.0
        self._last_paint_ms = 0.0
        # Phase 8A — render-state hooks.  ``_loading`` toggles the empty
        # placeholder text from "Waiting for candle data…" to
        # "Loading historical candles…" while a REST preload is in
        # flight.  ``_last_paint_label`` records the last empty-state
        # label drawn so tests can assert paint behaviour without
        # scraping pixel buffers.
        self._loading: bool = False
        self._last_paint_label: str = ""

        self._font_axis = QFont("Menlo", 9)
        self._font_price_tag = QFont("Menlo", 9, QFont.Bold)
        self._overlays: List = []

        # Default overlays (register before any external overlays so the
        # caller can append more / clear them via clear_overlays()).
        self._register_default_overlays()

    # ------------------------------------------------------------------ public

    @property
    def bucket_ms(self) -> int:
        return self._bucket_ms

    def set_bucket_duration(self, ms: int):
        if ms == self._bucket_ms:
            return
        self._bucket_ms = ms
        self._candles.clear()
        self._auto_scale = True
        self.timeframe_changed.emit(ms)
        self.update()

    # ----------------------------------------------------------- preload

    def set_loading(self, loading: bool) -> None:
        """Phase 8A — toggle the "Loading historical candles…" overlay.

        Called by ``MainWindow`` around the async REST preload kicked
        off from ``_on_connect`` / ``_on_chart_tf_changed``.  While
        ``True`` the chart shows the loading placeholder instead of the
        normal candle render, so the user gets explicit feedback that a
        fetch is in flight (Acceptance Criterion #3, §16.2).
        """
        new = bool(loading)
        if new == self._loading:
            return
        self._loading = new
        self.update()

    @property
    def loading(self) -> bool:
        return self._loading

    def preload_candles(
        self,
        candles: list[tuple[int, float, float, float, float, float]],
    ) -> None:
        """Phase 8A — replace ``_candles`` with a batch of historical
        OHLCV rows.

        Each row is ``(open_time_ms, open, high, low, close, volume)``
        as produced by :func:`data_feed.fetch_binance_klines`.  Empty
        input is a no-op (so a network failure that returns ``[]`` does
        not nuke any existing data).

        Preloaded candles use :data:`_PRELOAD_TRADE_COUNT` as the
        ``trades`` count so that subsequent ``process_trade`` calls
        landing in the same bucket update high/low/close correctly
        rather than re-seeding open from the live tick price.  Buy /
        sell volume splits are zeroed — kline rows do not expose taker
        side information.
        """
        if not candles:
            return

        ordered = sorted(candles, key=lambda r: r[0])
        new_deque: deque[_Candle] = deque(maxlen=self._candles.maxlen)
        for row in ordered:
            try:
                ts, o, h, l, c, v = row
            except (TypeError, ValueError):
                continue
            ts_i = int(ts)
            o_f = float(o)
            h_f = float(h)
            l_f = float(l)
            c_f = float(c)
            v_f = float(v)
            if ts_i <= 0 or o_f <= 0 or c_f <= 0:
                continue
            bkt_ts = (ts_i // self._bucket_ms) * self._bucket_ms
            new_deque.append(_Candle(
                ts=bkt_ts,
                o=o_f,
                h=h_f,
                l=l_f,
                c=c_f,
                vol=v_f,
                buy_vol=0.0,
                sell_vol=0.0,
                trades=_PRELOAD_TRADE_COUNT,
            ))
        if not new_deque:
            return
        self._candles = new_deque
        self._auto_scale = True
        self.update()

    def process_trade(self, ts: int, price: float, qty: float, is_buy: bool):
        if price <= 0 or qty <= 0:
            return
        bkt_ts = (ts // self._bucket_ms) * self._bucket_ms
        if self._candles and self._candles[-1].ts == bkt_ts:
            c = self._candles[-1]
        elif not self._candles or bkt_ts > self._candles[-1].ts:
            c = _Candle(ts=bkt_ts, o=price, h=price, l=price, c=price)
            self._candles.append(c)
        else:
            return
        if c.trades == 0:
            c.o = price
            c.h = price
            c.l = price
        else:
            c.h = max(c.h, price)
            c.l = min(c.l, price)
        c.c = price
        c.vol += qty
        if is_buy:
            c.buy_vol += qty
        else:
            c.sell_vol += qty
        c.trades += 1

    def register_overlay(self, overlay_fn):
        self._overlays.append(overlay_fn)

    def clear_overlays(self):
        """Remove all registered overlays (including defaults)."""
        self._overlays.clear()

    def _register_default_overlays(self):
        """Install the standard SMA / EMA / VWAP / structural / VP overlays.

        Called once from __init__. The full default set is installed so
        the chart is informative out-of-the-box. Callers that want a
        clean slate can call clear_overlays() then register their own.
        """
        # Local import keeps chart_overlays out of module-level cycle risk.
        from ui.chart_overlays import (
            SmaOverlay, EmaOverlay, VwapOverlay,
            StructuralLevelsOverlay, VolProfileOverlay,
        )
        self._overlays.append(SmaOverlay(period=20))
        self._overlays.append(EmaOverlay(period=50))
        self._overlays.append(VwapOverlay())
        self._structural_overlay = StructuralLevelsOverlay(
            extra_candles=self._candles)
        self._overlays.append(self._structural_overlay)
        self._overlays.append(VolProfileOverlay())

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event):
        t0 = time.monotonic()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, _BG)

        # Phase 8A — render the loading placeholder while a historical
        # REST preload is in flight (acceptance #3) and the empty-state
        # placeholder otherwise.  Painting the loading label suppresses
        # the candle/grid render so the user is not shown a half-drawn
        # chart during the fetch.
        if self._loading or not self._candles:
            label = _LABEL_LOADING if self._loading else _LABEL_WAITING
            self._last_paint_label = label
            painter.setPen(_TEXT)
            painter.setFont(self._font_axis)
            painter.drawText(w // 2 - 80, h // 2, label)
            painter.end()
            return
        self._last_paint_label = ""

        px = MARGIN_LEFT
        py = MARGIN_TOP
        pw = w - MARGIN_LEFT - MARGIN_RIGHT
        ph = h - MARGIN_TOP - MARGIN_BOTTOM

        if pw < 20 or ph < 20:
            painter.end()
            return

        visible = list(self._candles)[-self._visible_candles:]
        if not visible:
            painter.end()
            return

        n = len(visible)
        raw_w = pw / max(n, 1)
        candle_w = max(MIN_CANDLE_PX, min(MAX_CANDLE_PX, raw_w * (1.0 - CANDLE_GAP_FRAC)))
        step = pw / max(n, 1)

        if self._auto_scale:
            hi = max(c.h for c in visible if c.trades > 0)
            lo = min(c.l for c in visible if c.trades > 0)
            span = hi - lo
            if span <= 0:
                span = hi * 0.002 if hi > 0 else 1.0
            margin = span * 0.06
            self._price_min = lo - margin
            self._price_max = hi + margin

        pmin, pmax = self._price_min, self._price_max
        if pmax <= pmin:
            pmax = pmin + 1.0

        chart_ph = int(ph * (1.0 - VOLUME_ZONE_FRAC))

        self._draw_grid(painter, px, py, pw, chart_ph, pmin, pmax,
                        visible, step, n)
        self._draw_volume_bars(painter, px, py, pw, ph, chart_ph,
                               visible, step, candle_w, n)
        self._draw_candles(painter, px, py, pw, chart_ph, pmin, pmax,
                           visible, step, candle_w, n)
        self._draw_price_axis(painter, px, py, pw, chart_ph, pmin, pmax, w)
        self._draw_time_axis(painter, px, py, pw, ph, visible, step, n)
        self._draw_current_price(painter, px, py, pw, chart_ph, pmin, pmax, w)
        self._draw_overlays_fn(painter, px, py, pw, chart_ph, pmin, pmax, visible)

        painter.end()
        self._last_paint_ms = (time.monotonic() - t0) * 1000.0

    # --------------------------------------------------------------- helpers

    def _price_to_y(self, price: float, py: int, ph: int,
                    pmin: float, pmax: float) -> float:
        pr = pmax - pmin
        if pr <= 0:
            return float(py)
        return py + (1.0 - (price - pmin) / pr) * ph

    @staticmethod
    def _nice_tick(span: float, target_lines: int) -> float:
        raw = span / max(target_lines, 1)
        if raw <= 0:
            return 1.0
        mag = 10 ** math.floor(math.log10(raw))
        norm = raw / mag
        if norm < 1.5:
            return mag
        elif norm < 3.5:
            return 2 * mag
        elif norm < 7.5:
            return 5 * mag
        else:
            return 10 * mag

    # -------------------------------------------------------- grid

    def _draw_grid(self, painter, px, py, pw, ph, pmin, pmax,
                   visible, step, n):
        pen = QPen(_GRID, 1)
        painter.setPen(pen)

        pr = pmax - pmin
        if pr > 0:
            tick = self._nice_tick(pr, 6)
            p = math.ceil(pmin / tick) * tick
            while p <= pmax:
                y = int(self._price_to_y(p, py, ph, pmin, pmax))
                painter.drawLine(px, y, px + pw, y)
                p += tick

        interval = self._time_label_interval(n)
        for i in range(n):
            if i % interval == 0:
                x = int(px + (i + 0.5) * step)
                painter.drawLine(x, py, x, py + ph)

    def _time_label_interval(self, n: int) -> int:
        target_labels = max(1, n // 8)
        for iv in (1, 2, 3, 5, 10, 15, 20, 30, 60):
            if n // iv <= target_labels:
                return iv
        return max(1, n // 6)

    # -------------------------------------------------------- volume bars

    def _draw_volume_bars(self, painter, px, py, pw, ph, chart_ph,
                          visible, step, candle_w, n):
        max_vol = max((c.vol for c in visible), default=0)
        if max_vol <= 0:
            return
        vol_zone_h = ph * VOLUME_ZONE_FRAC
        vol_base_y = py + ph

        for i, c in enumerate(visible):
            if c.vol <= 0:
                continue
            x = px + (i + 0.5) * step
            bar_h = (c.vol / max_vol) * vol_zone_h * 0.9
            is_bull = c.c >= c.o
            painter.fillRect(
                QRectF(x - candle_w / 2, vol_base_y - bar_h, candle_w, bar_h),
                _BULL_VOL if is_bull else _BEAR_VOL,
            )

    # -------------------------------------------------------- candles

    def _draw_candles(self, painter, px, py, pw, ph, pmin, pmax,
                      visible, step, candle_w, n):
        for i, c in enumerate(visible):
            if c.trades == 0:
                continue
            is_bull = c.c >= c.o
            color = _BULL if is_bull else _BEAR
            x_mid = px + (i + 0.5) * step

            y_high = self._price_to_y(c.h, py, ph, pmin, pmax)
            y_low = self._price_to_y(c.l, py, ph, pmin, pmax)

            body_top_price = max(c.o, c.c)
            body_bot_price = min(c.o, c.c)
            y_top = self._price_to_y(body_top_price, py, ph, pmin, pmax)
            y_bot = self._price_to_y(body_bot_price, py, ph, pmin, pmax)
            body_h = max(1.0, y_bot - y_top)

            painter.setPen(QPen(color, 1))
            painter.drawLine(int(x_mid), int(y_high), int(x_mid), int(y_top))
            painter.drawLine(int(x_mid), int(y_bot), int(x_mid), int(y_low))

            body_rect = QRectF(x_mid - candle_w / 2, y_top, candle_w, body_h)
            painter.fillRect(body_rect, color)

    # -------------------------------------------------------- price axis (right)

    def _draw_price_axis(self, painter, px, py, pw, ph, pmin, pmax, w):
        painter.setFont(self._font_axis)
        pr = pmax - pmin
        if pr <= 0:
            return
        tick = self._nice_tick(pr, 8)
        p = math.ceil(pmin / tick) * tick
        ax_x = px + pw + 6
        painter.setPen(_TEXT)
        while p <= pmax:
            y = int(self._price_to_y(p, py, ph, pmin, pmax))
            painter.drawText(ax_x, y + 4, f"{p:,.2f}")
            p += tick

    # -------------------------------------------------------- time axis (bottom)

    def _draw_time_axis(self, painter, px, py, pw, ph, visible, step, n):
        painter.setFont(self._font_axis)
        painter.setPen(_TEXT)
        y_label = py + ph + 16
        interval = self._time_label_interval(n)

        dur = self._bucket_ms
        use_date = dur >= 3_600_000
        fmt = "%b %d" if use_date else "%H:%M"

        for i in range(n):
            if i % interval != 0:
                continue
            c = visible[i]
            x = int(px + (i + 0.5) * step)
            dt = datetime.fromtimestamp(c.ts / 1000.0, tz=timezone.utc)
            label = dt.strftime(fmt)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(label)
            painter.drawText(x - tw // 2, y_label, label)

    # -------------------------------------------------------- current price line

    def _draw_current_price(self, painter, px, py, pw, ph, pmin, pmax, w):
        if not self._candles:
            return
        price = self._candles[-1].c
        if price <= 0:
            return
        y = int(self._price_to_y(price, py, ph, pmin, pmax))
        if y < py or y > py + ph:
            return

        pen = QPen(_PRICE_LINE, 1, Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(px, y, px + pw, y)

        is_bull = True
        if len(self._candles) >= 2:
            prev = list(self._candles)[-2]
            is_bull = price >= prev.c
        tag_color = _BULL if is_bull else _BEAR

        painter.setFont(self._font_price_tag)
        label = f"{price:,.2f}"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(label) + 10
        th = fm.height() + 4
        tag_x = px + pw + 2
        tag_rect = QRectF(tag_x, y - th / 2, tw, th)
        painter.fillRect(tag_rect, tag_color)
        painter.setPen(Qt.white)
        painter.drawText(int(tag_x + 5), int(y + fm.ascent() / 2), label)

    # -------------------------------------------------------- overlays

    def _draw_overlays_fn(self, painter, px, py, pw, ph, pmin, pmax, visible):
        for fn in self._overlays:
            # Phase 9D — skip overlays explicitly toggled off by the QML
            # toolbar.  External overlays without an ``enabled`` attribute
            # default to True via ``getattr``.
            if not getattr(fn, "enabled", True):
                continue
            try:
                fn(painter, px, py, pw, ph, pmin, pmax, visible)
            except Exception:
                pass

    def set_overlay_enabled(self, name: str, enabled: bool) -> bool:
        """Phase 9D — toggle an overlay by ``OVERLAY_NAME``.

        Returns True if at least one matching overlay was toggled.  The
        QML toolbar drives this via ``MainWindowBridge.setOverlayEnabled``;
        unit tests assert that overlay drawing is suppressed when
        ``enabled=False``.  Triggers a repaint when the flag flips.
        """
        target = name.lower()
        changed = False
        for fn in self._overlays:
            overlay_name = getattr(fn, "OVERLAY_NAME", None)
            if not overlay_name:
                continue
            if overlay_name.lower() != target:
                continue
            current = bool(getattr(fn, "enabled", True))
            if current != bool(enabled):
                fn.enabled = bool(enabled)
                changed = True
        if changed:
            self.update()
        return changed

    def overlay_enabled_state(self) -> dict[str, bool]:
        """Phase 9D — snapshot of overlay enable flags by name."""
        out: dict[str, bool] = {}
        for fn in self._overlays:
            overlay_name = getattr(fn, "OVERLAY_NAME", None)
            if not overlay_name:
                continue
            out[overlay_name] = bool(getattr(fn, "enabled", True))
        return out

    # --------------------------------------------------------------- interact

    def mouseDoubleClickEvent(self, event):
        self._auto_scale = True
        self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if delta > 0:
            self._visible_candles = max(20, int(self._visible_candles * 0.85))
        else:
            self._visible_candles = min(500, int(self._visible_candles * 1.18))
        self._auto_scale = True
        self.update()
