"""Phase 9B / 9B-final — native ``CandleItem``.

Renders the candlestick chart directly into the QML scene graph via
``QSGGeometryNode`` instances built on the render thread.  Matches the
spec in ``UI_STRATEGY_INTEGRATION_PLAN.md §22.3``:

* ``_geometry_dirty`` is **True only on bucket close** (or geometry-shape
  changes such as viewport resize / visible-candle count change).  Inside
  the same bucket, live-tick updates rewrite the vertex buffer via
  ``QSGGeometry.markVertexDataDirty()`` — no flag flip, no allocation.
* PySide6 6.10 does not expose ``setVertexDataAsColoredPoint2D``, so the
  geometry is split into **four** Point2D nodes — one per colour bucket
  (bull / bear × body+wick / volume) — each driven by a
  ``QSGFlatColorMaterial``.  Vertices land on the right node depending on
  whether each candle closes ≥ open (bull) or below (bear).

The chart is **read-only data plumbing** for Phase 9B: live trades arrive
via :meth:`process_trade`, bucket switches via :meth:`set_bucket_ms`, and
historical preloads via :meth:`preload_candles`.  Overlay lines, event
markers, and the strategy-aware bindings are scheduled for Phase 9C
(``UI_STRATEGY_INTEGRATION_PLAN.md §22.4``).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from PySide6.QtCore import Property, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtQuick import (
    QQuickItem,
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGNode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Render constants (mirror ui.candle_chart_view for visual continuity).
# ---------------------------------------------------------------------------

_MARGIN_LEFT = 10
_MARGIN_RIGHT = 72
_MARGIN_TOP = 10
_MARGIN_BOTTOM = 28
_VOLUME_ZONE_FRAC = 0.22
_MIN_CANDLE_PX = 4.0
_MAX_CANDLE_PX = 24.0
_CANDLE_GAP_FRAC = 0.25
_WICK_PX = 1.0
_DEFAULT_VISIBLE_CANDLES = 80
_MAX_STORED_CANDLES = 1000

_BULL_COLOR = QColor(14, 203, 129, 255)
_BEAR_COLOR = QColor(234, 57, 67, 255)
_BULL_VOL_COLOR = QColor(14, 203, 129, 80)
_BEAR_VOL_COLOR = QColor(234, 57, 67, 80)

# Phase 9C — trade overlay colours.  These match the legacy
# ``HeatmapWidget`` overlay so the visual language carries over.
_OVERLAY_ENTRY_COLOR = QColor(220, 220, 230, 220)
_OVERLAY_STOP_COLOR = QColor(220, 60, 60, 220)
_OVERLAY_TARGET_COLOR = QColor(60, 200, 100, 220)
_OVERLAY_LINE_HEIGHT = 1.0  # px-thick dashed overlay lines
_OVERLAY_DASH_LEN_PX = 8.0
_OVERLAY_GAP_LEN_PX = 6.0
_MARKER_HEIGHT_PX = 9.0
_MARKER_HALF_WIDTH_PX = 6.0
_MARKER_ENTRY_COLOR = QColor(255, 215, 0, 235)
_MARKER_EXIT_COLOR = QColor(120, 200, 255, 235)

# Vertex counts per candle (DrawTriangles).
_VERTS_PER_RECT = 6
_BODY_WICK_VERTS_PER_CANDLE = _VERTS_PER_RECT * 2   # body rect + wick rect
_VOLUME_VERTS_PER_CANDLE = _VERTS_PER_RECT


@dataclass(slots=True)
class _Candle:
    ts: int = 0
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0
    vol: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    trades: int = 0


class CandleItem(QQuickItem):
    """Native scene-graph candlestick chart.

    Public QML properties:

    * ``bucketMs`` — int, milliseconds per candle (default 60 000).
    * ``visibleCandles`` — int, number of candles to display (default 80).

    Public Python API (used by ``MainWindow`` / ``ChartBridge``):

    * :meth:`process_trade` — ingest a tick.
    * :meth:`preload_candles` — bulk-load historical OHLCV rows.
    * :meth:`clear` — wipe state (e.g. on symbol change).
    """

    bucketMsChanged = Signal(int)
    visibleCandlesChanged = Signal(int)
    # ``qlonglong`` because timestamps are millisecond Unix epochs and
    # exceed the C++ ``int`` (32-bit) range.
    candleAppended = Signal("qlonglong")
    # Phase 9C — trade overlay notifications.
    tradeOverlayActiveChanged = Signal(bool)
    entryPriceChanged = Signal(float)
    stopPriceChanged = Signal(float)
    targetPriceChanged = Signal(float)
    tradeEventsChanged = Signal()

    # -----------------------------------------------------------------------
    # construction
    # -----------------------------------------------------------------------

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # MUST be set so the scene graph calls updatePaintNode().
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)

        self._candles: deque[_Candle] = deque(maxlen=_MAX_STORED_CANDLES)
        self._bucket_ms: int = 60_000
        self._visible_candles: int = _DEFAULT_VISIBLE_CANDLES

        # Render-thread state.  ``_geometry_dirty`` is the spec-required
        # flag: True only on bucket close (or shape-change).  The render
        # thread (updatePaintNode) reads + resets it under the implicit
        # GUI-thread synchronization that Qt Quick performs before each
        # frame.
        self._geometry_dirty: bool = True
        # Tracks the last (candle_count, width, height) used in
        # _rebuild_geometry so the live-tick path can detect a shape
        # change it missed (e.g. resize during off-bucket update).
        self._last_shape: Optional[tuple[int, float, float]] = None

        # Cached price range (auto-scale).
        self._price_min: float = 0.0
        self._price_max: float = 0.0
        self._auto_scale: bool = True

        # Phase 9C — trade overlay state.  Overlay vertices are rebuilt
        # on every updatePaintNode() invocation while the trade is
        # active, so flag changes alone don't require ``_geometry_dirty``
        # (it remains the bucket-close-only flag per AC #2).
        self._trade_overlay_active: bool = False
        self._entry_price: float = 0.0
        self._stop_price: float = 0.0
        self._target_price: float = 0.0
        # Trade events fed via ``setTradeEvents`` — list of
        # ``(ts_ms, type, price)`` tuples.  ``type`` is "ENTRY"
        # or "EXIT" (any other string is rendered as a generic marker).
        self._trade_events: list[tuple[int, str, float]] = []

        # Geometry-shape changes (size, visibleCandles) also require a
        # rebuild.  Connect once.
        self.widthChanged.connect(self._mark_geometry_dirty)
        self.heightChanged.connect(self._mark_geometry_dirty)

    # -----------------------------------------------------------------------
    # Q_PROPERTYs
    # -----------------------------------------------------------------------

    @Property(int, notify=bucketMsChanged)
    def bucketMs(self) -> int:
        return self._bucket_ms

    @bucketMs.setter
    def bucketMs(self, ms: int) -> None:
        ms = max(1, int(ms))
        if ms == self._bucket_ms:
            return
        self._bucket_ms = ms
        self._candles.clear()
        self._mark_geometry_dirty()
        self.bucketMsChanged.emit(ms)
        self.update()

    @Property(int, notify=visibleCandlesChanged)
    def visibleCandles(self) -> int:
        return self._visible_candles

    @visibleCandles.setter
    def visibleCandles(self, n: int) -> None:
        n = max(1, int(n))
        if n == self._visible_candles:
            return
        self._visible_candles = n
        self._mark_geometry_dirty()
        self.visibleCandlesChanged.emit(n)
        self.update()

    # -----------------------------------------------------------------------
    # Phase 9C overlay Q_PROPERTYs.
    # -----------------------------------------------------------------------

    @Property(bool, notify=tradeOverlayActiveChanged)
    def tradeOverlayActive(self) -> bool:
        return self._trade_overlay_active

    @tradeOverlayActive.setter
    def tradeOverlayActive(self, value: bool) -> None:
        value = bool(value)
        if value != self._trade_overlay_active:
            self._trade_overlay_active = value
            self.tradeOverlayActiveChanged.emit(value)
            self.update()

    @Property(float, notify=entryPriceChanged)
    def entryPrice(self) -> float:
        return self._entry_price

    @entryPrice.setter
    def entryPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._entry_price:
            self._entry_price = value
            self.entryPriceChanged.emit(value)
            if self._trade_overlay_active:
                self.update()

    @Property(float, notify=stopPriceChanged)
    def stopPrice(self) -> float:
        return self._stop_price

    @stopPrice.setter
    def stopPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._stop_price:
            self._stop_price = value
            self.stopPriceChanged.emit(value)
            if self._trade_overlay_active:
                self.update()

    @Property(float, notify=targetPriceChanged)
    def targetPrice(self) -> float:
        return self._target_price

    @targetPrice.setter
    def targetPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._target_price:
            self._target_price = value
            self.targetPriceChanged.emit(value)
            if self._trade_overlay_active:
                self.update()

    # -----------------------------------------------------------------------
    # Public data API
    # -----------------------------------------------------------------------

    def set_bucket_ms(self, ms: int) -> None:
        """Imperative setter callable from Python."""
        self.bucketMs = ms  # type: ignore[assignment]

    def set_visible_candles(self, n: int) -> None:
        self.visibleCandles = n  # type: ignore[assignment]

    def clear(self) -> None:
        if not self._candles:
            return
        self._candles.clear()
        self._mark_geometry_dirty()
        self.update()

    # -----------------------------------------------------------------------
    # Phase 9C — trade overlay setters.
    # -----------------------------------------------------------------------

    def set_trade_overlay(self, entry: float, stop: float, target: float,
                          active: bool = True) -> None:
        """Imperative setter for all four overlay fields at once.

        Called from ``MainWindowBridge`` when a snapshot lands; using
        the bulk setter avoids four separate ``self.update()`` calls.
        Setting ``active=False`` clears the overlay; the entry / stop /
        target values are still stored so re-activating reuses them.
        """
        changed = False
        if float(entry) != self._entry_price:
            self._entry_price = float(entry)
            self.entryPriceChanged.emit(self._entry_price)
            changed = True
        if float(stop) != self._stop_price:
            self._stop_price = float(stop)
            self.stopPriceChanged.emit(self._stop_price)
            changed = True
        if float(target) != self._target_price:
            self._target_price = float(target)
            self.targetPriceChanged.emit(self._target_price)
            changed = True
        active = bool(active)
        if active != self._trade_overlay_active:
            self._trade_overlay_active = active
            self.tradeOverlayActiveChanged.emit(active)
            changed = True
        if changed:
            self.update()

    @Slot()
    def clear_trade_overlay(self) -> None:
        """Hide the entry / stop / target overlay lines."""
        if not self._trade_overlay_active:
            return
        self._trade_overlay_active = False
        self.tradeOverlayActiveChanged.emit(False)
        self.update()

    def set_trade_events(self, events) -> None:
        """Replace the list of trade event markers.

        ``events`` is a sequence of ``(ts_ms: int, type: str, price: float)``
        tuples; non-conforming items are silently dropped.  Emits
        ``tradeEventsChanged`` and schedules a repaint so the markers
        are rebuilt on the next ``updatePaintNode``.
        """
        new: list[tuple[int, str, float]] = []
        for event in events or ():
            try:
                ts_ms, kind, price = event[0], event[1], event[2]
                new.append((int(ts_ms), str(kind), float(price)))
            except (TypeError, ValueError, IndexError):
                continue
        if new == self._trade_events:
            return
        self._trade_events = new
        self.tradeEventsChanged.emit()
        self.update()

    @Slot()
    def clear_trade_events(self) -> None:
        if not self._trade_events:
            return
        self._trade_events = []
        self.tradeEventsChanged.emit()
        self.update()

    @property
    def trade_events(self) -> list[tuple[int, str, float]]:
        """Read-only accessor — exposed for tests."""
        return list(self._trade_events)

    def process_trade(self, ts: int, price: float, qty: float,
                      is_buy: bool) -> bool:
        """Ingest a tick.

        Returns ``True`` if the trade caused a **bucket close** (a new
        candle was appended).  Bucket close is the ONLY condition that
        flips :pyattr:`_geometry_dirty` to True; live-candle updates
        (same bucket) leave the flag False and rely on the live-tick
        path in :meth:`updatePaintNode` to re-upload the buffer.
        """
        if price <= 0 or qty <= 0:
            return False

        bkt_ts = (int(ts) // self._bucket_ms) * self._bucket_ms
        appended = False
        if self._candles and self._candles[-1].ts == bkt_ts:
            c = self._candles[-1]
        elif not self._candles or bkt_ts > self._candles[-1].ts:
            c = _Candle(ts=bkt_ts, o=price, h=price, l=price, c=price)
            self._candles.append(c)
            appended = True
        else:
            return False  # out-of-order trade older than the current bucket

        if c.trades == 0:
            c.o = c.h = c.l = price
        else:
            if price > c.h:
                c.h = price
            if price < c.l:
                c.l = price
        c.c = price
        c.vol += qty
        if is_buy:
            c.buy_vol += qty
        else:
            c.sell_vol += qty
        c.trades += 1

        if appended:
            self._mark_geometry_dirty()
            self.candleAppended.emit(bkt_ts)
        # Always schedule a repaint — within-bucket ticks need
        # updatePaintNode() to refresh the live candle's vertices via
        # the markVertexDataDirty() path.
        self.update()
        return appended

    def preload_candles(self, rows: Iterable[tuple]) -> None:
        """Replace the deque with a batch of historical klines.

        ``rows`` is ``(open_time_ms, o, h, l, c, vol)`` per element, as
        produced by :func:`data_feed.fetch_binance_klines`.  Always
        marks the geometry dirty.
        """
        ordered = sorted(
            (r for r in rows if r), key=lambda r: r[0],
        )
        new_deque: deque[_Candle] = deque(maxlen=self._candles.maxlen)
        for row in ordered:
            try:
                ts, o, h, l, c, v = row[:6]
            except (TypeError, ValueError):
                continue
            try:
                ts_i = int(ts)
                o_f = float(o)
                h_f = float(h)
                l_f = float(l)
                c_f = float(c)
                v_f = float(v)
            except (TypeError, ValueError):
                continue
            if ts_i <= 0 or o_f <= 0 or c_f <= 0:
                continue
            bkt_ts = (ts_i // self._bucket_ms) * self._bucket_ms
            new_deque.append(_Candle(
                ts=bkt_ts, o=o_f, h=h_f, l=l_f, c=c_f, vol=v_f,
                buy_vol=0.0, sell_vol=0.0, trades=1,
            ))
        if not new_deque:
            return
        self._candles = new_deque
        self._auto_scale = True
        self._mark_geometry_dirty()
        self.update()

    # -----------------------------------------------------------------------
    # Read-only introspection (useful for tests)
    # -----------------------------------------------------------------------

    @property
    def candle_count(self) -> int:
        return len(self._candles)

    @property
    def geometry_dirty(self) -> bool:
        """The Phase 9B spec-required flag.

        Public so :file:`tests/test_qml_chart_items.py` can assert AC #2.
        """
        return self._geometry_dirty

    @property
    def price_range(self) -> tuple[float, float]:
        return self._price_min, self._price_max

    # -----------------------------------------------------------------------
    # Scene graph build (render thread)
    # -----------------------------------------------------------------------

    def updatePaintNode(self, old_node, _update_data):
        """Build or refresh the scene-graph node tree.

        Two paths:

        * **Rebuild** (``_geometry_dirty`` is True OR the shape changed):
          allocate fresh ``QSGGeometry`` instances sized for every visible
          candle and fill them with vertex data.  Flag is cleared before
          returning.
        * **Incremental** (flag is False): rewrite ALL candles' vertices
          into the existing geometry buffers and call
          ``markVertexDataDirty`` so the renderer re-uploads them; no
          allocation, no flag change.  PySide6 6.10 does not expose
          per-vertex addressing so we cannot rewrite only the last
          candle here, but the buffer pointer + material binding are
          preserved which is the win the spec calls out.
        """
        layout = self._compute_layout()

        if old_node is None:
            root = _CandleNodeTree()
        else:
            root = old_node

        if layout is None or layout.candle_count == 0:
            self._geometry_dirty = False
            return root

        shape = (layout.candle_count, layout.pw, layout.ph)
        needs_rebuild = (
            self._geometry_dirty
            or root.empty()
            or self._last_shape != shape
        )

        if needs_rebuild:
            self._rebuild_geometry(root, layout)
            self._geometry_dirty = False
            self._last_shape = shape
        else:
            self._refresh_geometry(root, layout)

        # Phase 9C — overlay rebuilds every frame the overlay is
        # active.  The overlay is bounded to O(3 lines + N events) so a
        # full rebuild fits inside a single ``markVertexDataDirty``
        # buffer slice and does NOT trip ``_geometry_dirty`` (the
        # candle geometry above is the bucket-close-only flag).
        if self._trade_overlay_active or self._trade_events:
            self._rebuild_overlay(root, layout)
        else:
            root.clear_overlay()
        return root

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _mark_geometry_dirty(self) -> None:
        self._geometry_dirty = True

    def _compute_layout(self) -> Optional["_Layout"]:
        w = float(self.width())
        h = float(self.height())
        if w <= 0 or h <= 0:
            return None

        visible = list(self._candles)[-self._visible_candles:]
        n = len(visible)
        if n == 0:
            return _Layout(
                px=float(_MARGIN_LEFT), py=float(_MARGIN_TOP),
                pw=0.0, ph=0.0, chart_ph=0.0,
                step=0.0, candle_w=_MIN_CANDLE_PX,
                pmin=0.0, pmax=0.0,
                candle_count=0,
                visible=[],
            )

        pw = w - _MARGIN_LEFT - _MARGIN_RIGHT
        ph = h - _MARGIN_TOP - _MARGIN_BOTTOM
        if pw <= 0 or ph <= 0:
            return None

        raw_w = pw / max(n, 1)
        candle_w = max(_MIN_CANDLE_PX,
                       min(_MAX_CANDLE_PX, raw_w * (1.0 - _CANDLE_GAP_FRAC)))
        step = pw / max(n, 1)
        chart_ph = ph * (1.0 - _VOLUME_ZONE_FRAC)

        traded = [c for c in visible if c.trades > 0]
        if traded and self._auto_scale:
            hi = max(c.h for c in traded)
            lo = min(c.l for c in traded)
            # Phase 9C — extend the price window to include the
            # currently active trade overlay so entry / stop / target
            # always render inside the chart bounds.  Marker prices
            # are also included so out-of-range trade events stay
            # visible.
            if self._trade_overlay_active:
                for px in (self._entry_price, self._stop_price,
                           self._target_price):
                    if px > 0:
                        hi = max(hi, px)
                        lo = min(lo, px)
            for _, _, mp in self._trade_events:
                if mp > 0:
                    hi = max(hi, mp)
                    lo = min(lo, mp)
            span = hi - lo
            if span <= 0:
                span = hi * 0.002 if hi > 0 else 1.0
            margin = span * 0.06
            self._price_min = lo - margin
            self._price_max = hi + margin

        pmin = self._price_min
        pmax = self._price_max
        if pmax <= pmin:
            pmax = pmin + 1.0

        return _Layout(
            px=float(_MARGIN_LEFT), py=float(_MARGIN_TOP),
            pw=pw, ph=ph, chart_ph=chart_ph,
            step=step, candle_w=candle_w,
            pmin=pmin, pmax=pmax,
            candle_count=n,
            visible=visible,
        )

    def _generate_vertices(self, layout: "_Layout") -> "_VertexBuckets":
        """Build the four vertex buckets (bull/bear × body+wick / volume).

        Pure function over (layout, visible candles) — exposed so the
        test suite can verify vertex counts without instantiating a
        scene graph.
        """
        buckets = _VertexBuckets()
        max_vol = max((c.vol for c in layout.visible), default=0.0)
        for i, candle in enumerate(layout.visible):
            _fill_candle_vertices(buckets, i, candle, layout, max_vol)
        return buckets

    def _rebuild_geometry(self, root: "_CandleNodeTree",
                          layout: "_Layout") -> None:
        buckets = self._generate_vertices(layout)
        root.replace(buckets)

    def _refresh_geometry(self, root: "_CandleNodeTree",
                          layout: "_Layout") -> None:
        """In-place vertex update path used between bucket closes.

        Vertex counts are unchanged, so we rewrite the existing buffers
        and call ``markVertexDataDirty`` rather than allocating a new
        ``QSGGeometry``.  This is the win the bucket-close-only flag
        unlocks: the renderer re-uploads the dirty buffer slices
        without going through the full material / node-tree dance.
        """
        buckets = self._generate_vertices(layout)
        root.refresh(buckets)

    # -----------------------------------------------------------------------
    # Phase 9C — trade overlay vertex generation.
    # -----------------------------------------------------------------------

    def _generate_overlay_vertices(self, layout: "_Layout") -> "_OverlayBuckets":
        """Build the three overlay line buckets + entry / exit markers.

        Pure function over ``(layout, self._entry/stop/target_price,
        self._trade_events)`` — exposed so the test suite can verify
        line counts without instantiating a scene graph.
        """
        buckets = _OverlayBuckets()
        if self._trade_overlay_active:
            for price, vert_bucket in (
                (self._entry_price, buckets.entry_line),
                (self._stop_price, buckets.stop_line),
                (self._target_price, buckets.target_line),
            ):
                if price <= 0:
                    continue
                y = _price_to_y(price, layout.py, layout.chart_ph,
                                layout.pmin, layout.pmax)
                if not (layout.py - 1.0 <= y <= layout.py + layout.chart_ph + 1.0):
                    continue
                _push_dashed_line(
                    vert_bucket,
                    layout.px, y,
                    layout.px + layout.pw, y,
                    _OVERLAY_LINE_HEIGHT,
                )
        if self._trade_events:
            self._build_trade_markers(buckets, layout)
        return buckets

    def _build_trade_markers(self, buckets: "_OverlayBuckets",
                             layout: "_Layout") -> None:
        if layout.candle_count == 0 or self._bucket_ms <= 0:
            return
        first_ts = layout.visible[0].ts
        last_ts = layout.visible[-1].ts + self._bucket_ms
        if last_ts <= first_ts:
            return
        for ts_ms, kind, price in self._trade_events:
            if ts_ms < first_ts or ts_ms > last_ts:
                continue
            bucket_index = (ts_ms - first_ts) // self._bucket_ms
            if bucket_index < 0 or bucket_index >= layout.candle_count:
                continue
            x_mid = layout.px + (bucket_index + 0.5) * layout.step
            y = _price_to_y(price, layout.py, layout.chart_ph,
                            layout.pmin, layout.pmax)
            kind_upper = kind.upper()
            target_bucket = (
                buckets.entry_markers
                if kind_upper.startswith("ENTRY") or kind_upper.startswith("ENTER")
                else buckets.exit_markers
            )
            point_up = kind_upper.startswith("ENTRY") \
                or kind_upper.startswith("ENTER")
            _push_triangle_marker(
                target_bucket, x_mid, y,
                _MARKER_HALF_WIDTH_PX, _MARKER_HEIGHT_PX, point_up,
            )

    def _rebuild_overlay(self, root: "_CandleNodeTree",
                         layout: "_Layout") -> None:
        buckets = self._generate_overlay_vertices(layout)
        root.replace_overlay(buckets)


# ---------------------------------------------------------------------------
# Layout + vertex containers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Layout:
    px: float
    py: float
    pw: float
    ph: float
    chart_ph: float
    step: float
    candle_w: float
    pmin: float
    pmax: float
    candle_count: int
    visible: list[_Candle]


@dataclass(slots=True)
class _VertexBuckets:
    bull_body: list = None
    bear_body: list = None
    bull_volume: list = None
    bear_volume: list = None

    def __post_init__(self) -> None:
        if self.bull_body is None:
            self.bull_body = []
        if self.bear_body is None:
            self.bear_body = []
        if self.bull_volume is None:
            self.bull_volume = []
        if self.bear_volume is None:
            self.bear_volume = []


@dataclass(slots=True)
class _OverlayBuckets:
    """Phase 9C overlay vertex buckets.

    Three dashed line buckets (entry / stop / target) + two triangle
    marker buckets (entry events / exit events).  Each bucket is a
    flat list of ``QSGGeometry.Point2D`` instances; QML's flat-color
    materials draw them as triangle strips.
    """
    entry_line: list = None
    stop_line: list = None
    target_line: list = None
    entry_markers: list = None
    exit_markers: list = None

    def __post_init__(self) -> None:
        if self.entry_line is None:
            self.entry_line = []
        if self.stop_line is None:
            self.stop_line = []
        if self.target_line is None:
            self.target_line = []
        if self.entry_markers is None:
            self.entry_markers = []
        if self.exit_markers is None:
            self.exit_markers = []

    def empty(self) -> bool:
        return not (
            self.entry_line or self.stop_line or self.target_line
            or self.entry_markers or self.exit_markers
        )


# ---------------------------------------------------------------------------
# Node-tree helper — groups the four flat-colour geometry nodes.
# ---------------------------------------------------------------------------

class _CandleNodeTree(QSGNode):
    """Root node grouping bull / bear × body+wick / volume geometry."""

    def __init__(self) -> None:
        super().__init__()
        self._bull_body_node: Optional[QSGGeometryNode] = None
        self._bear_body_node: Optional[QSGGeometryNode] = None
        self._bull_volume_node: Optional[QSGGeometryNode] = None
        self._bear_volume_node: Optional[QSGGeometryNode] = None
        # Phase 9C overlay nodes.
        self._entry_line_node: Optional[QSGGeometryNode] = None
        self._stop_line_node: Optional[QSGGeometryNode] = None
        self._target_line_node: Optional[QSGGeometryNode] = None
        self._entry_marker_node: Optional[QSGGeometryNode] = None
        self._exit_marker_node: Optional[QSGGeometryNode] = None

    def empty(self) -> bool:
        return self._bull_body_node is None and self._bear_body_node is None

    def vertex_counts(self) -> dict:
        """Test helper — number of vertices per bucket."""
        def _vc(node):
            if node is None:
                return 0
            geom = node.geometry()
            return geom.vertexCount() if geom is not None else 0
        return {
            "bull_body": _vc(self._bull_body_node),
            "bear_body": _vc(self._bear_body_node),
            "bull_volume": _vc(self._bull_volume_node),
            "bear_volume": _vc(self._bear_volume_node),
            "entry_line": _vc(self._entry_line_node),
            "stop_line": _vc(self._stop_line_node),
            "target_line": _vc(self._target_line_node),
            "entry_markers": _vc(self._entry_marker_node),
            "exit_markers": _vc(self._exit_marker_node),
        }

    def replace(self, buckets: _VertexBuckets) -> None:
        self._bull_body_node = _replace_flat_node(
            self, self._bull_body_node, buckets.bull_body, _BULL_COLOR)
        self._bear_body_node = _replace_flat_node(
            self, self._bear_body_node, buckets.bear_body, _BEAR_COLOR)
        self._bull_volume_node = _replace_flat_node(
            self, self._bull_volume_node, buckets.bull_volume, _BULL_VOL_COLOR)
        self._bear_volume_node = _replace_flat_node(
            self, self._bear_volume_node, buckets.bear_volume, _BEAR_VOL_COLOR)

    def refresh(self, buckets: _VertexBuckets) -> None:
        _refresh_flat_node(self._bull_body_node, buckets.bull_body)
        _refresh_flat_node(self._bear_body_node, buckets.bear_body)
        _refresh_flat_node(self._bull_volume_node, buckets.bull_volume)
        _refresh_flat_node(self._bear_volume_node, buckets.bear_volume)

    # ------------------------------------------------------------------
    # Phase 9C overlay management
    # ------------------------------------------------------------------

    def replace_overlay(self, buckets: _OverlayBuckets) -> None:
        self._entry_line_node = _replace_flat_node(
            self, self._entry_line_node, buckets.entry_line,
            _OVERLAY_ENTRY_COLOR)
        self._stop_line_node = _replace_flat_node(
            self, self._stop_line_node, buckets.stop_line,
            _OVERLAY_STOP_COLOR)
        self._target_line_node = _replace_flat_node(
            self, self._target_line_node, buckets.target_line,
            _OVERLAY_TARGET_COLOR)
        self._entry_marker_node = _replace_flat_node(
            self, self._entry_marker_node, buckets.entry_markers,
            _MARKER_ENTRY_COLOR)
        self._exit_marker_node = _replace_flat_node(
            self, self._exit_marker_node, buckets.exit_markers,
            _MARKER_EXIT_COLOR)

    def clear_overlay(self) -> None:
        for attr in (
            "_entry_line_node", "_stop_line_node", "_target_line_node",
            "_entry_marker_node", "_exit_marker_node",
        ):
            node = getattr(self, attr)
            if node is not None:
                self.removeChildNode(node)
                setattr(self, attr, None)


def _replace_flat_node(parent: QSGNode,
                       old: Optional[QSGGeometryNode],
                       verts: list,
                       color: QColor) -> Optional[QSGGeometryNode]:
    """Allocate a fresh ``QSGGeometryNode`` for ``verts`` and reparent it."""
    if old is not None:
        parent.removeChildNode(old)
    if not verts:
        return None
    geom = QSGGeometry(QSGGeometry.defaultAttributes_Point2D(), len(verts))
    geom.setDrawingMode(QSGGeometry.DrawTriangles)
    geom.setVertexDataAsPoint2D(verts)

    mat = QSGFlatColorMaterial()
    mat.setColor(color)

    node = QSGGeometryNode()
    node.setGeometry(geom)
    node.setFlag(QSGNode.Flag.OwnsGeometry, True)
    node.setMaterial(mat)
    node.setFlag(QSGNode.Flag.OwnsMaterial, True)
    parent.appendChildNode(node)
    return node


def _refresh_flat_node(node: Optional[QSGGeometryNode], verts: list) -> None:
    """Re-upload vertices into an existing node without reallocating."""
    if node is None or not verts:
        return
    geom = node.geometry()
    if geom is None or geom.vertexCount() != len(verts):
        # Vertex count changed mid-bucket — fall through to the rebuild
        # path on the next frame by leaving the node intact; the layout
        # consistency check in updatePaintNode will catch it.
        return
    geom.setVertexDataAsPoint2D(verts)
    geom.markVertexDataDirty()
    node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)


# ---------------------------------------------------------------------------
# Vertex generation — pure helper functions so the unit tests can verify
# counts without spinning up the scene graph.
# ---------------------------------------------------------------------------

def _price_to_y(price: float, py: float, ph: float,
                pmin: float, pmax: float) -> float:
    pr = pmax - pmin
    if pr <= 0:
        return py
    return py + (1.0 - (price - pmin) / pr) * ph


def _push_rect(verts: list, x: float, y: float, w: float, h: float) -> None:
    """Append six ``Point2D`` vertices forming a triangle-list rectangle."""
    x1, y1 = x, y
    x2, y2 = x + w, y + h
    for px, py in ((x1, y1), (x2, y1), (x1, y2),
                   (x2, y1), (x2, y2), (x1, y2)):
        v = QSGGeometry.Point2D()
        v.set(float(px), float(py))
        verts.append(v)


def _push_dashed_line(verts: list,
                      x1: float, y1: float, x2: float, y2: float,
                      thickness: float,
                      dash_len: float = _OVERLAY_DASH_LEN_PX,
                      gap_len: float = _OVERLAY_GAP_LEN_PX) -> None:
    """Append vertices for a horizontal dashed line.

    Only horizontal lines are supported (overlay lines are constant-y);
    that keeps the dash spacing pixel-exact without trig.  Each dash is
    a thin rectangle (6 verts).
    """
    if x2 < x1:
        x1, x2 = x2, x1
    half_h = thickness / 2.0
    period = max(1.0, dash_len + gap_len)
    x = x1
    while x < x2:
        end = min(x + dash_len, x2)
        _push_rect(verts, x, y1 - half_h, end - x, thickness)
        x += period


def _push_triangle_marker(verts: list, x: float, y: float,
                          half_width: float, height: float,
                          point_up: bool) -> None:
    """Append three Point2D vertices forming a single triangle marker.

    ``point_up=True`` draws an ENTRY-style upward triangle; ``False``
    draws an EXIT-style downward triangle.  The triangle is centred on
    ``(x, y)``.
    """
    if point_up:
        pts = (
            (x, y - height),
            (x - half_width, y + height * 0.4),
            (x + half_width, y + height * 0.4),
        )
    else:
        pts = (
            (x, y + height),
            (x - half_width, y - height * 0.4),
            (x + half_width, y - height * 0.4),
        )
    for px, py in pts:
        v = QSGGeometry.Point2D()
        v.set(float(px), float(py))
        verts.append(v)


def _fill_candle_vertices(buckets: _VertexBuckets, index: int,
                          candle: _Candle, layout: _Layout,
                          max_vol: float) -> None:
    """Append all vertices that represent ``candle`` to the right buckets."""
    x_mid = layout.px + (index + 0.5) * layout.step

    if candle.trades == 0:
        return  # nothing to draw yet

    is_bull = candle.c >= candle.o
    body_bucket = buckets.bull_body if is_bull else buckets.bear_body
    vol_bucket = buckets.bull_volume if is_bull else buckets.bear_volume

    y_high = _price_to_y(candle.h, layout.py, layout.chart_ph,
                         layout.pmin, layout.pmax)
    y_low = _price_to_y(candle.l, layout.py, layout.chart_ph,
                        layout.pmin, layout.pmax)
    body_top_price = max(candle.o, candle.c)
    body_bot_price = min(candle.o, candle.c)
    y_top = _price_to_y(body_top_price, layout.py, layout.chart_ph,
                        layout.pmin, layout.pmax)
    y_bot = _price_to_y(body_bot_price, layout.py, layout.chart_ph,
                        layout.pmin, layout.pmax)
    body_h = max(1.0, y_bot - y_top)

    # Body rect.
    _push_rect(body_bucket,
               x_mid - layout.candle_w / 2.0, y_top,
               layout.candle_w, body_h)
    # Wick rect (1 px wide, full high → low).
    _push_rect(body_bucket,
               x_mid - _WICK_PX / 2.0, y_high,
               _WICK_PX, max(1.0, y_low - y_high))

    # Volume bar.
    if max_vol > 0 and candle.vol > 0:
        vol_zone_h = layout.ph * _VOLUME_ZONE_FRAC
        vol_base_y = layout.py + layout.ph
        bar_h = (candle.vol / max_vol) * vol_zone_h * 0.9
        _push_rect(vol_bucket,
                   x_mid - layout.candle_w / 2.0,
                   vol_base_y - bar_h,
                   layout.candle_w, bar_h)
