"""Tests for CandleChartView (independent candle bucketing, Binance-style).

Verifies:
- Construction with MarketState
- process_trade bucketing
- set_bucket_duration clears and re-buckets
- Paint cycle with empty / populated candles
- Auto-scale behaviour
- Overlay registration
- Price coordinate helper
- Wheel zoom changes visible_candles
- _nice_tick produces reasonable values
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPainter

app = QApplication.instance() or QApplication([])

from ui.market_state import MarketState
from ui.candle_chart_view import CandleChartView, TIMEFRAMES


def check(cond, msg=""):
    assert cond, msg
    print(f"  {msg} ... OK")


BASE_TS = 1_700_000_060_000


def _feed_trades(v, n=50, base=BASE_TS, dur_ms=60_000):
    for i in range(n):
        ts = base + i * (dur_ms // 10)
        price = 83000.0 + (i % 20)
        v.process_trade(ts, price, 0.1, i % 2 == 0)


def test_construction():
    print("test_construction")
    ms = MarketState()
    v = CandleChartView(ms)
    check(v._ms is ms, "market state reference stored")
    check(v._auto_scale is True, "auto-scale on by default")
    check(len(v._overlays) == 0, "no overlays initially")
    check(v._bucket_ms == 60_000, "default bucket 1m")
    check(len(v._candles) == 0, "no candles initially")


def test_process_trade_creates_candle():
    print("test_process_trade_creates_candle")
    ms = MarketState()
    v = CandleChartView(ms)
    v.process_trade(BASE_TS, 83000.0, 0.5, True)
    check(len(v._candles) == 1, "one candle created")
    c = v._candles[0]
    check(c.o == 83000.0, f"open = {c.o}")
    check(c.h == 83000.0, f"high = {c.h}")
    check(c.l == 83000.0, f"low = {c.l}")
    check(c.c == 83000.0, f"close = {c.c}")
    check(c.vol == 0.5, f"volume = {c.vol}")
    check(c.buy_vol == 0.5, f"buy_vol = {c.buy_vol}")
    check(c.trades == 1, "trade count = 1")


def test_process_trade_aggregation():
    print("test_process_trade_aggregation")
    ms = MarketState()
    v = CandleChartView(ms)
    v.process_trade(BASE_TS, 83000.0, 0.1, True)
    v.process_trade(BASE_TS + 1000, 83050.0, 0.2, False)
    v.process_trade(BASE_TS + 2000, 82980.0, 0.3, True)
    check(len(v._candles) == 1, "all in same bucket")
    c = v._candles[0]
    check(c.o == 83000.0, "open from first trade")
    check(c.h == 83050.0, "high updated")
    check(c.l == 82980.0, "low updated")
    check(c.c == 82980.0, "close from last trade")
    check(c.trades == 3, "3 trades aggregated")
    check(abs(c.vol - 0.6) < 1e-9, "volume summed")
    check(abs(c.buy_vol - 0.4) < 1e-9, "buy volume correct")
    check(abs(c.sell_vol - 0.2) < 1e-9, "sell volume correct")


def test_multiple_buckets():
    print("test_multiple_buckets")
    ms = MarketState()
    v = CandleChartView(ms)
    v.process_trade(BASE_TS, 83000.0, 0.1, True)
    v.process_trade(BASE_TS + 60_001, 83100.0, 0.2, False)
    v.process_trade(BASE_TS + 120_001, 83200.0, 0.3, True)
    check(len(v._candles) == 3, "3 separate candles")
    check(v._candles[0].c == 83000.0, "bucket 0 close")
    check(v._candles[1].c == 83100.0, "bucket 1 close")
    check(v._candles[2].c == 83200.0, "bucket 2 close")


def test_set_bucket_duration():
    print("test_set_bucket_duration")
    ms = MarketState()
    v = CandleChartView(ms)
    _feed_trades(v, 20)
    initial_count = len(v._candles)
    check(initial_count > 0, f"candles before change: {initial_count}")
    v.set_bucket_duration(300_000)
    check(v._bucket_ms == 300_000, "bucket duration changed to 5m")
    check(len(v._candles) == 0, "candles cleared on timeframe change")


def test_paint_empty():
    print("test_paint_empty")
    ms = MarketState()
    v = CandleChartView(ms)
    v.resize(800, 600)
    v.repaint()
    check(True, "paint with empty candles succeeds")


def test_paint_with_candles():
    print("test_paint_with_candles")
    ms = MarketState()
    v = CandleChartView(ms)
    v.resize(800, 600)
    _feed_trades(v, 80)
    from PySide6.QtGui import QPaintEvent
    from PySide6.QtCore import QRect
    v.paintEvent(QPaintEvent(QRect(0, 0, 800, 600)))
    check(v._last_paint_ms >= 0, f"paint time recorded ({v._last_paint_ms:.2f}ms)")


def test_auto_scale():
    print("test_auto_scale")
    ms = MarketState()
    v = CandleChartView(ms)
    v.resize(800, 600)
    _feed_trades(v, 50)
    v._auto_scale = True
    from PySide6.QtGui import QPaintEvent
    from PySide6.QtCore import QRect
    v.paintEvent(QPaintEvent(QRect(0, 0, 800, 600)))
    check(v._price_min > 0, f"price_min set ({v._price_min:.2f})")
    check(v._price_max > v._price_min, "price_max > price_min")


def test_auto_scale_disabled():
    print("test_auto_scale_disabled")
    ms = MarketState()
    v = CandleChartView(ms)
    v.resize(800, 600)
    _feed_trades(v, 20)
    v._auto_scale = False
    v._price_min = 10.0
    v._price_max = 20.0
    v.repaint()
    check(v._price_min == 10.0, "price_min unchanged")
    check(v._price_max == 20.0, "price_max unchanged")


def test_overlay_registration():
    print("test_overlay_registration")
    ms = MarketState()
    v = CandleChartView(ms)
    v.resize(800, 600)
    _feed_trades(v, 20)

    overlay_called = [False]
    def my_overlay(painter, px, py, pw, ph, pmin, pmax, visible):
        overlay_called[0] = True

    v.register_overlay(my_overlay)
    check(len(v._overlays) == 1, "overlay registered")

    from PySide6.QtGui import QPixmap
    pixmap = QPixmap(800, 600)
    painter = QPainter(pixmap)
    candles = list(v._candles)
    v._draw_overlays_fn(painter, 10, 10, 700, 500, 82900.0, 83100.0, candles)
    painter.end()
    check(overlay_called[0], "overlay was called")


def test_price_to_y_helper():
    print("test_price_to_y_helper")
    ms = MarketState()
    v = CandleChartView(ms)
    py, ph = 10, 600
    pmin, pmax = 100.0, 200.0
    y_lo = v._price_to_y(pmin, py, ph, pmin, pmax)
    y_hi = v._price_to_y(pmax, py, ph, pmin, pmax)
    y_mid = v._price_to_y(150.0, py, ph, pmin, pmax)
    check(abs(y_lo - (py + ph)) < 0.01, f"low price at bottom ({y_lo})")
    check(abs(y_hi - py) < 0.01, f"high price at top ({y_hi})")
    check(abs(y_mid - (py + ph / 2)) < 0.01, f"mid price at center ({y_mid})")


def test_double_click_resets_auto_scale():
    print("test_double_click_resets_auto_scale")
    ms = MarketState()
    v = CandleChartView(ms)
    v._auto_scale = False
    from PySide6.QtCore import QPointF, QEvent, Qt
    from PySide6.QtGui import QMouseEvent
    ev = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(100, 100),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    v.mouseDoubleClickEvent(ev)
    check(v._auto_scale is True, "auto-scale re-enabled on double click")


def test_wheel_zoom():
    print("test_wheel_zoom")
    ms = MarketState()
    v = CandleChartView(ms)
    initial = v._visible_candles
    from PySide6.QtCore import QPointF, QPoint, Qt
    from PySide6.QtGui import QWheelEvent
    ev = QWheelEvent(
        QPointF(400, 300), QPointF(400, 300),
        QPoint(0, 0), QPoint(0, 120),
        Qt.NoButton, Qt.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    v.wheelEvent(ev)
    check(v._visible_candles < initial, f"zoom in: {v._visible_candles} < {initial}")

    v._visible_candles = initial
    ev_out = QWheelEvent(
        QPointF(400, 300), QPointF(400, 300),
        QPoint(0, 0), QPoint(0, -120),
        Qt.NoButton, Qt.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    v.wheelEvent(ev_out)
    check(v._visible_candles > initial, f"zoom out: {v._visible_candles} > {initial}")


def test_nice_tick():
    print("test_nice_tick")
    check(CandleChartView._nice_tick(100, 5) in (20, 25, 50),
          f"tick for 100/5 = {CandleChartView._nice_tick(100, 5)}")
    check(CandleChartView._nice_tick(0.05, 5) > 0,
          "tick for small span is positive")
    check(CandleChartView._nice_tick(10000, 4) > 0,
          "tick for large span is positive")


def test_timeframes_constant():
    print("test_timeframes_constant")
    check(len(TIMEFRAMES) >= 4, f"at least 4 timeframes ({len(TIMEFRAMES)})")
    check(TIMEFRAMES[0][1] == 60_000, "first timeframe is 1m")
    for label, ms in TIMEFRAMES:
        check(ms > 0, f"{label} has positive ms ({ms})")


# ---- runner ----

_TESTS = [
    test_construction,
    test_process_trade_creates_candle,
    test_process_trade_aggregation,
    test_multiple_buckets,
    test_set_bucket_duration,
    test_paint_empty,
    test_paint_with_candles,
    test_auto_scale,
    test_auto_scale_disabled,
    test_overlay_registration,
    test_price_to_y_helper,
    test_double_click_resets_auto_scale,
    test_wheel_zoom,
    test_nice_tick,
    test_timeframes_constant,
]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for t in _TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  FAILED: {e}")
            failed += 1
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Candle chart view tests: {passed}/{total} passed, {failed} failed")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
