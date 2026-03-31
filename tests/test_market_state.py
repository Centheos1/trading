"""Tests for the shared MarketState model.

Verifies:
- Construction defaults
- Field updates
- Property helpers (mid_price, has_candles, has_strategy)
- Candle deque sharing between MarketState and HeatmapWidget
- Signal deque behaviour
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import deque

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ui.market_state import MarketState
from ui.heatmap_widget import HeatmapWidget, TimeBucket
from execution.models import SignalEntry, SignalCategory, StrategyUIState


def check(cond, msg=""):
    assert cond, msg
    print(f"  {msg} ... OK")


def test_defaults():
    """MarketState initializes with sane defaults."""
    print("test_defaults")
    ms = MarketState()
    check(ms.chart_now == 0, "chart_now default 0")
    check(ms.best_bid == 0.0, "best_bid default 0")
    check(ms.best_ask == 0.0, "best_ask default 0")
    check(ms.strategy_snapshot is None, "no strategy snapshot initially")
    check(ms.strategy_ui_state == StrategyUIState.DISARMED, "disarmed default")
    check(len(ms.candles) == 0, "candles empty")
    check(len(ms.signals) == 0, "signals empty")
    check(ms.bucket_duration_ms == 60_000, "default bucket duration")
    check(ms.visible_window_ms == 60_000, "default visible window")


def test_mid_price():
    """mid_price property computes correctly."""
    print("test_mid_price")
    ms = MarketState()
    check(ms.mid_price == 0.0, "mid_price zero when both zero")
    ms.best_bid = 100.0
    check(ms.mid_price == 100.0, "mid_price uses bid when ask zero")
    ms.best_ask = 102.0
    check(abs(ms.mid_price - 101.0) < 0.01, "mid_price is average")
    ms.best_bid = 0.0
    check(ms.mid_price == 102.0, "mid_price uses ask when bid zero")


def test_has_candles():
    """has_candles reflects deque state."""
    print("test_has_candles")
    ms = MarketState()
    check(not ms.has_candles, "empty = no candles")
    ms.candles.append(TimeBucket(bucket_ts=1000))
    check(ms.has_candles, "one candle = has_candles")


def test_has_strategy():
    """has_strategy reflects snapshot."""
    print("test_has_strategy")
    ms = MarketState()
    check(not ms.has_strategy, "no strategy initially")
    ms.strategy_snapshot = object()
    check(ms.has_strategy, "strategy present after assignment")


def test_shared_candle_deque():
    """HeatmapWidget and MarketState share the same candle deque."""
    print("test_shared_candle_deque")
    ms = MarketState()
    hm = HeatmapWidget(candle_store=ms.candles)
    check(hm._buckets is ms.candles, "same deque object")

    base_ts = 1_700_000_060_000
    hm.add_trade(base_ts, 83000.0, 0.1, True)
    check(len(ms.candles) == 1, "trade creates candle in shared deque")
    check(ms.candles[0].trade_count == 1, "candle has 1 trade")
    check(ms.candles[0].open_price == 83000.0, "candle open correct")


def test_shared_candle_multiple_trades():
    """Multiple trades into shared deque update the same bucket."""
    print("test_shared_candle_multiple_trades")
    ms = MarketState()
    hm = HeatmapWidget(candle_store=ms.candles)

    base_ts = 1_700_000_060_000
    for i in range(5):
        hm.add_trade(base_ts + i * 100, 83000.0 + i, 0.1, i % 2 == 0)

    check(len(ms.candles) == 1, "all trades in same bucket")
    check(ms.candles[0].trade_count == 5, "5 trades aggregated")
    check(ms.candles[0].high == 83004.0, "high correct")
    check(ms.candles[0].low == 83000.0, "low correct")


def test_signal_deque_bounded():
    """Signal deque respects maxlen."""
    print("test_signal_deque_bounded")
    ms = MarketState()
    for i in range(2500):
        ms.signals.append(SignalEntry(timestamp=i))
    check(len(ms.signals) == 2000, "signal deque bounded at 2000")


def test_custom_params():
    """Custom bucket_duration_ms and visible_window_ms are respected."""
    print("test_custom_params")
    ms = MarketState(bucket_duration_ms=300_000, visible_window_ms=300_000)
    check(ms.bucket_duration_ms == 300_000, "custom bucket duration")
    check(ms.visible_window_ms == 300_000, "custom visible window")


def test_external_candle_deque():
    """An externally-provided candle deque is used."""
    print("test_external_candle_deque")
    ext = deque(maxlen=50)
    ms = MarketState(candles=ext)
    check(ms.candles is ext, "external deque used")
    ms.candles.append(TimeBucket(bucket_ts=5000))
    check(len(ext) == 1, "append goes through to external deque")


# ---- runner ----

_TESTS = [
    test_defaults,
    test_mid_price,
    test_has_candles,
    test_has_strategy,
    test_shared_candle_deque,
    test_shared_candle_multiple_trades,
    test_signal_deque_bounded,
    test_custom_params,
    test_external_candle_deque,
]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for t in _TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Market state tests: {passed}/{total} passed, {failed} failed")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
