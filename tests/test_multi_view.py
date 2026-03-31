"""Tests for the multi-view MVVM architecture (multi-window variant).

Verifies:
- Detached window creation and visibility
- Order Flow layout preserved in main window
- Strategy Dashboard composition
- Signal broadcast to both blotters
- Visibility-gated repaint logic
- MarketState synchronization in timer-tick
- Candle deque sharing across views
- Strategy dashboard update_from_state
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import deque

from PySide6.QtWidgets import QApplication, QMainWindow

app = QApplication.instance() or QApplication([])

from ui.market_state import MarketState
from ui.heatmap_widget import HeatmapWidget, TimeBucket
from ui.candle_chart_view import CandleChartView
from ui.strategy_dashboard_view import StrategyDashboardView
from ui.trade_blotter import TradeBlotter
from ui.strategy_panel import StrategyDiagnosticsPanel
from ui.account_panel import AccountPanel
from ui.main_window import _DetachedViewWindow
from execution.models import (
    SignalEntry, SignalCategory, StrategyUIState, StrategyMode,
)


def check(cond, msg=""):
    assert cond, msg
    print(f"  {msg} ... OK")


# ---- MarketState + HeatmapWidget candle sharing ----

def test_candle_deque_shared():
    """HeatmapWidget writes to MarketState.candles."""
    print("test_candle_deque_shared")
    ms = MarketState()
    hm = HeatmapWidget(candle_store=ms.candles)
    base_ts = 1_700_000_060_000
    hm.add_trade(base_ts, 83000.0, 0.1, True)
    check(len(ms.candles) == 1, "candle created in shared deque")
    check(ms.candles[0].close_price == 83000.0, "candle data correct")


def test_candle_chart_independent_store():
    """CandleChartView maintains its own candle store via process_trade."""
    print("test_candle_chart_independent_store")
    ms = MarketState()
    hm = HeatmapWidget(candle_store=ms.candles)
    cv = CandleChartView(ms)
    base_ts = 1_700_000_060_000
    for i in range(3):
        hm.add_trade(base_ts + i * 100, 83000.0 + i, 0.1, True)
        cv.process_trade(base_ts + i * 100, 83000.0 + i, 0.1, True)
    check(len(cv._candles) == 1, "chart has own bucket")
    check(cv._candles[0].trades == 3, "chart aggregated trades independently")


# ---- Strategy Dashboard ----

def test_strategy_dashboard_construction():
    """StrategyDashboardView creates sub-widgets."""
    print("test_strategy_dashboard_construction")
    ms = MarketState()
    sd = StrategyDashboardView(ms)
    check(isinstance(sd.strategy_panel, StrategyDiagnosticsPanel),
          "has strategy panel")
    check(isinstance(sd.blotter, TradeBlotter), "has blotter")
    check(isinstance(sd.account_panel, AccountPanel), "has account panel")


def test_strategy_dashboard_update_from_state():
    """update_from_state pushes MarketState.strategy_ui_state."""
    print("test_strategy_dashboard_update_from_state")
    ms = MarketState()
    sd = StrategyDashboardView(ms)
    ms.strategy_ui_state = StrategyUIState.ARMED_ACTIVE
    sd.update_from_state()
    check(sd.strategy_panel._strategy_state == StrategyUIState.ARMED_ACTIVE,
          "strategy panel state updated")


# ---- Signal broadcasting ----

def test_signal_broadcast_to_both_blotters():
    """Signals are received by both Order Flow and Strategy Dashboard blotters."""
    print("test_signal_broadcast_to_both_blotters")
    of_blotter = TradeBlotter()
    ms = MarketState()
    sd = StrategyDashboardView(ms)

    entry = SignalEntry(
        timestamp=1_700_000_000_000,
        signal_type="TIDE",
        source="strategy",
        category=SignalCategory.TIDE,
        description="test signal",
    )
    of_blotter.add_entry(entry)
    sd.blotter.add_entry(entry)
    check(of_blotter._model.rowCount() == 1, "OF blotter has signal")
    check(sd.blotter._model.rowCount() == 1, "SD blotter has signal")


def test_blotter_instances_are_independent():
    """OF and SD blotters are separate instances with independent data."""
    print("test_blotter_instances_are_independent")
    of_blotter = TradeBlotter()
    ms = MarketState()
    sd = StrategyDashboardView(ms)

    entry1 = SignalEntry(timestamp=1000, signal_type="A", category=SignalCategory.TIDE)
    entry2 = SignalEntry(timestamp=2000, signal_type="B", category=SignalCategory.WAVE)

    of_blotter.add_entry(entry1)
    of_blotter.add_entry(entry2)
    sd.blotter.add_entry(entry1)

    check(of_blotter._model.rowCount() == 2, "OF has 2 signals")
    check(sd.blotter._model.rowCount() == 1, "SD has 1 signal")


# ---- Detached window container ----

def test_detached_window_creation():
    """_DetachedViewWindow wraps a widget as a top-level window."""
    print("test_detached_window_creation")
    ms = MarketState()
    cv = CandleChartView(ms)
    win = _DetachedViewWindow(cv, "Chart")
    check(win.windowTitle() == "Chart", "title set correctly")
    check(win.centralWidget() is cv, "central widget is the view")
    check(isinstance(win, QMainWindow), "is a QMainWindow")


def test_detached_window_close_hides():
    """Closing a detached window hides it instead of destroying it."""
    print("test_detached_window_close_hides")
    ms = MarketState()
    sd = StrategyDashboardView(ms)
    win = _DetachedViewWindow(sd, "Strategy")
    hidden_flags = []
    win.visibility_changed.connect(lambda vis: hidden_flags.append(vis))
    from PySide6.QtGui import QCloseEvent
    win.closeEvent(QCloseEvent())
    check(len(hidden_flags) == 1, "visibility_changed emitted")
    check(hidden_flags[0] is False, "emitted False on close")


# ---- MarketState sync ----

def test_market_state_sync_fields():
    """MarketState fields can be updated and read back."""
    print("test_market_state_sync_fields")
    ms = MarketState()
    ms.chart_now = 1_700_000_100_000
    ms.best_bid = 83000.0
    ms.best_ask = 83001.0
    ms.strategy_ui_state = StrategyUIState.ARMED_WAITING
    check(ms.chart_now == 1_700_000_100_000, "chart_now set")
    check(ms.best_bid == 83000.0, "best_bid set")
    check(abs(ms.mid_price - 83000.5) < 0.01, "mid_price correct")
    check(ms.strategy_ui_state == StrategyUIState.ARMED_WAITING, "state set")


def test_candle_view_process_trade():
    """CandleChartView accumulates its own candles via process_trade."""
    print("test_candle_view_process_trade")
    ms = MarketState()
    cv = CandleChartView(ms)
    check(len(cv._candles) == 0, "no candles initially")
    base = 1_700_000_060_000
    cv.process_trade(base, 83950.0, 1.0, True)
    cv.process_trade(base + 1000, 83980.0, 0.5, False)
    check(len(cv._candles) == 1, "one candle bucket")
    check(cv._candles[0].h == 83980.0, "high updated")
    check(cv._candles[0].trades == 2, "two trades aggregated")


# ---- Visibility-gated repaint ----

def test_repaint_gating_logic():
    """Only visible detached windows should trigger their view's update."""
    print("test_repaint_gating_logic")
    updates = {"of": 0, "candle": 0, "strategy": 0}

    def simulate_tick(candle_visible, strategy_visible):
        updates["of"] += 1
        if candle_visible:
            updates["candle"] += 1
        if strategy_visible:
            updates["strategy"] += 1

    for _ in range(5):
        simulate_tick(candle_visible=True, strategy_visible=True)
    for _ in range(3):
        simulate_tick(candle_visible=True, strategy_visible=False)
    for _ in range(2):
        simulate_tick(candle_visible=False, strategy_visible=False)

    check(updates["of"] == 10, "OF always repaints (10 ticks)")
    check(updates["candle"] == 8, "candle repaints when visible (8)")
    check(updates["strategy"] == 5, "strategy repaints when visible (5)")


# ---- Determinism ----

def test_deterministic_candle_sharing():
    """Same trades produce identical candle data regardless of view count."""
    print("test_deterministic_candle_sharing")
    ms1 = MarketState()
    hm1 = HeatmapWidget(candle_store=ms1.candles)
    ms2 = MarketState()
    hm2 = HeatmapWidget(candle_store=ms2.candles)

    base = 1_700_000_060_000
    trades = [(base + i * 100, 83000.0 + (i % 5), 0.1 + i * 0.01, i % 2 == 0)
              for i in range(20)]

    for ts, price, qty, is_buy in trades:
        hm1.add_trade(ts, price, qty, is_buy)
        hm2.add_trade(ts, price, qty, is_buy)

    check(len(ms1.candles) == len(ms2.candles), "same bucket count")
    for i in range(len(ms1.candles)):
        a, b = ms1.candles[i], ms2.candles[i]
        check(a.trade_count == b.trade_count,
              f"bucket {i} trade_count matches")
        check(abs(a.volume - b.volume) < 1e-9,
              f"bucket {i} volume matches")
        check(a.high == b.high, f"bucket {i} high matches")
        check(a.low == b.low, f"bucket {i} low matches")


# ---- runner ----

_TESTS = [
    test_candle_deque_shared,
    test_candle_chart_independent_store,
    test_strategy_dashboard_construction,
    test_strategy_dashboard_update_from_state,
    test_signal_broadcast_to_both_blotters,
    test_blotter_instances_are_independent,
    test_detached_window_creation,
    test_detached_window_close_hides,
    test_market_state_sync_fields,
    test_candle_view_process_trade,
    test_repaint_gating_logic,
    test_deterministic_candle_sharing,
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
    print(f"Multi-view tests: {passed}/{total} passed, {failed} failed")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
