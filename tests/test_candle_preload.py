"""Phase 8A — Candlestick historical preload tests.

Covers:

* :meth:`CandleChartView.preload_candles` ingests a batch of
  ``(ts_ms, o, h, l, c, v)`` tuples in chronological order with correct
  OHLC, and replaces any prior deque contents.
* A subsequent live ``process_trade`` extends the deque correctly: a
  tick in the current bucket updates h/l/c without resetting the open;
  a tick in the next bucket appends without duplicating.
* :meth:`CandleChartView.preload_candles` with an empty list is a
  no-op (network failure ⇒ ``[]`` must not nuke any existing data).
* :meth:`CandleChartView.set_loading` toggles the "Loading historical
  candles…" overlay; ``paintEvent`` renders that label instead of
  candle data while loading is ``True``.
* :meth:`LiveTradingSession.fetch_historical_klines` returns parsed
  klines via the data_feed helper, and swallows REST exceptions
  returning ``[]`` (Acceptance Criterion #4).
* ``UI_STRATEGY_INTEGRATION_PLAN.md`` §16.2 wiring: the public API
  (``preload_candles`` + ``fetch_historical_klines``) has at least one
  production caller outside ``tests/``.

See ``UI_STRATEGY_INTEGRATION_PLAN.md`` §16.2 and
``AGENT_STRATEGY_RULES.md`` §3.5.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force headless Qt before PySide6 import — matches the convention in
# tests/test_strategy_dashboard.py / tests/test_strategy_ui.py.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QPaintEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from ui.candle_chart_view import (  # noqa: E402
    CandleChartView,
    _LABEL_LOADING,
    _LABEL_WAITING,
    _PRELOAD_TRADE_COUNT,
)
from ui.market_state import MarketState  # noqa: E402
from ui import live_trading_session as lts  # noqa: E402
from ui.live_trading_session import LiveTradingSession  # noqa: E402


_BUCKET_MS = 60_000
# Bucket-aligned base timestamp — Binance klines always report
# ``open_time_ms`` aligned to the interval boundary, so the test data
# mirrors that shape.
_BASE_TS = (1_700_000_000_000 // _BUCKET_MS) * _BUCKET_MS


def _make_klines(n: int, *, base_ts: int = _BASE_TS, bucket: int = _BUCKET_MS):
    """Build ``n`` consecutive ``(ts, o, h, l, c, v)`` rows oldest-first."""
    rows: list[tuple[int, float, float, float, float, float]] = []
    for i in range(n):
        ts = base_ts + i * bucket
        o = 30_000.0 + i * 1.0
        c = o + (1.0 if i % 2 == 0 else -1.0)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        v = 10.0 + i * 0.25
        rows.append((ts, o, h, l, c, v))
    return rows


class _FakeMainWindow:
    """Lightweight stand-in used by ``LiveTradingSession`` tests."""

    def __init__(self, websockets_module=None):
        self._websockets_module = websockets_module


class _ModulePatcher:
    """Save & restore module attributes (mirrors test_live_trading_session)."""

    def __init__(self, module):
        self._module = module
        self._originals: dict = {}

    def set(self, name: str, value):
        if name not in self._originals:
            self._originals[name] = getattr(self._module, name)
        setattr(self._module, name, value)

    def restore(self):
        for name, val in self._originals.items():
            setattr(self._module, name, val)
        self._originals.clear()


# ---------------------------------------------------------------------------
# CandleChartView.preload_candles
# ---------------------------------------------------------------------------


class TestPreloadCandlesPopulatesOHLC(unittest.TestCase):
    """Acceptance #1 — preload populates ``_candles`` with correct OHLC."""

    def test_preload_100_tuples_populates_deque(self):
        v = CandleChartView(MarketState())
        rows = _make_klines(100)
        v.preload_candles(rows)
        self.assertEqual(len(v._candles), 100)
        for kline, candle in zip(rows, list(v._candles)):
            self.assertEqual(candle.ts, kline[0])
            self.assertAlmostEqual(candle.o, kline[1])
            self.assertAlmostEqual(candle.h, kline[2])
            self.assertAlmostEqual(candle.l, kline[3])
            self.assertAlmostEqual(candle.c, kline[4])
            self.assertAlmostEqual(candle.vol, kline[5])
            self.assertEqual(candle.trades, _PRELOAD_TRADE_COUNT)
            self.assertEqual(candle.buy_vol, 0.0)
            self.assertEqual(candle.sell_vol, 0.0)

    def test_preload_sorts_unordered_input(self):
        v = CandleChartView(MarketState())
        rows = _make_klines(10)
        v.preload_candles(list(reversed(rows)))
        out_ts = [c.ts for c in v._candles]
        self.assertEqual(out_ts, sorted(out_ts))
        self.assertEqual(out_ts[0], rows[0][0])
        self.assertEqual(out_ts[-1], rows[-1][0])

    def test_preload_replaces_existing_data(self):
        v = CandleChartView(MarketState())
        v.process_trade(_BASE_TS, 25_000.0, 0.5, True)
        self.assertEqual(len(v._candles), 1)
        rows = _make_klines(50)
        v.preload_candles(rows)
        self.assertEqual(len(v._candles), 50)
        # Old synthetic candle is gone.
        self.assertNotIn(25_000.0, [c.o for c in v._candles])


# ---------------------------------------------------------------------------
# Live process_trade interaction after preload
# ---------------------------------------------------------------------------


class TestLiveTradesAfterPreload(unittest.TestCase):
    """Acceptance #2 — live ticks extend / update the preloaded set
    without creating duplicates or gaps."""

    def test_process_trade_in_last_preload_bucket_updates_in_place(self):
        v = CandleChartView(MarketState())
        rows = _make_klines(10)
        v.preload_candles(rows)
        last_ts, last_o, last_h, last_l, last_c, last_v = rows[-1]
        live_price = last_h + 50.0  # forces a new high
        v.process_trade(last_ts + 5_000, live_price, 0.2, True)
        self.assertEqual(len(v._candles), 10,
                         "Same-bucket tick must not append a new candle")
        tail = v._candles[-1]
        self.assertEqual(tail.ts, last_ts)
        self.assertAlmostEqual(tail.o, last_o,
                               msg="Open must remain the preloaded open")
        self.assertAlmostEqual(tail.h, live_price,
                               msg="High must extend to the live price")
        self.assertAlmostEqual(tail.l, last_l,
                               msg="Low must remain the preloaded low")
        self.assertAlmostEqual(tail.c, live_price,
                               msg="Close must update to the live price")
        self.assertAlmostEqual(tail.vol, last_v + 0.2)
        self.assertEqual(tail.trades, _PRELOAD_TRADE_COUNT + 1)
        self.assertAlmostEqual(tail.buy_vol, 0.2)

    def test_process_trade_in_next_bucket_appends_without_gap(self):
        v = CandleChartView(MarketState())
        rows = _make_klines(10)
        v.preload_candles(rows)
        last_ts = rows[-1][0]
        next_bucket = last_ts + _BUCKET_MS
        v.process_trade(next_bucket + 100, 31_234.5, 0.7, False)
        self.assertEqual(len(v._candles), 11,
                         "New-bucket tick must append exactly one candle")
        tail = v._candles[-1]
        self.assertEqual(tail.ts, next_bucket)
        self.assertAlmostEqual(tail.o, 31_234.5)
        self.assertAlmostEqual(tail.c, 31_234.5)
        self.assertEqual(tail.trades, 1)
        self.assertAlmostEqual(tail.sell_vol, 0.7)

    def test_old_bucket_tick_does_not_corrupt_preload(self):
        """A live tick that lands in an *older* bucket (already covered
        by the preloaded set) is dropped by ``process_trade`` — verify
        the preloaded OHLC is unchanged."""
        v = CandleChartView(MarketState())
        rows = _make_klines(10)
        v.preload_candles(rows)
        snapshot = [(c.ts, c.o, c.h, c.l, c.c, c.vol)
                    for c in v._candles]
        stale_ts = rows[2][0] + 1_000  # inside bucket #2 (not the tail)
        v.process_trade(stale_ts, 99_999.0, 5.0, True)
        self.assertEqual(len(v._candles), 10)
        for before, c in zip(snapshot, v._candles):
            self.assertEqual(c.ts, before[0])
            self.assertAlmostEqual(c.o, before[1])
            self.assertAlmostEqual(c.h, before[2])
            self.assertAlmostEqual(c.l, before[3])
            self.assertAlmostEqual(c.c, before[4])
            self.assertAlmostEqual(c.vol, before[5])


# ---------------------------------------------------------------------------
# Empty / loading behaviour
# ---------------------------------------------------------------------------


class TestPreloadEmptyIsNoop(unittest.TestCase):
    """Acceptance #4 — REST failure (``[]``) must not destroy existing
    data and must not raise."""

    def test_preload_empty_on_empty_deque(self):
        v = CandleChartView(MarketState())
        v.preload_candles([])
        self.assertEqual(len(v._candles), 0)

    def test_preload_empty_on_populated_deque_preserves_data(self):
        v = CandleChartView(MarketState())
        rows = _make_klines(5)
        v.preload_candles(rows)
        before = list(v._candles)
        v.preload_candles([])
        self.assertEqual(len(v._candles), 5)
        for b, a in zip(before, v._candles):
            self.assertEqual(b.ts, a.ts)
            self.assertAlmostEqual(b.c, a.c)


class TestLoadingOverlay(unittest.TestCase):
    """Acceptance #3 — ``set_loading(True)`` causes ``paintEvent`` to
    render the loading label instead of candle data."""

    def _paint(self, view: CandleChartView):
        view.resize(800, 600)
        view.paintEvent(QPaintEvent(QRect(0, 0, 800, 600)))

    def test_set_loading_toggles_flag(self):
        v = CandleChartView(MarketState())
        self.assertFalse(v.loading)
        v.set_loading(True)
        self.assertTrue(v.loading)
        v.set_loading(False)
        self.assertFalse(v.loading)

    def test_paint_when_loading_renders_loading_label(self):
        v = CandleChartView(MarketState())
        v.set_loading(True)
        self._paint(v)
        self.assertEqual(v._last_paint_label, _LABEL_LOADING)

    def test_paint_when_loading_with_candles_still_shows_loading(self):
        """Even after data lands, if ``set_loading`` is still True we
        keep the placeholder up.  In practice this is just a transient
        between ``preload_candles`` returning and the GUI clearing
        ``set_loading(False)``, but verifying it isolates the contract."""
        v = CandleChartView(MarketState())
        v.preload_candles(_make_klines(10))
        v.set_loading(True)
        self._paint(v)
        self.assertEqual(v._last_paint_label, _LABEL_LOADING)

    def test_paint_when_idle_and_empty_renders_waiting_label(self):
        v = CandleChartView(MarketState())
        self._paint(v)
        self.assertEqual(v._last_paint_label, _LABEL_WAITING)

    def test_paint_when_populated_and_not_loading_clears_label(self):
        v = CandleChartView(MarketState())
        v.preload_candles(_make_klines(20))
        self._paint(v)
        self.assertEqual(v._last_paint_label, "")


# ---------------------------------------------------------------------------
# LiveTradingSession.fetch_historical_klines
# ---------------------------------------------------------------------------


class TestFetchHistoricalKlines(unittest.TestCase):

    def test_fetch_returns_parsed_klines(self):
        patcher = _ModulePatcher(lts)
        fake = [(_BASE_TS + i * _BUCKET_MS,
                 30_000.0 + i, 30_010.0 + i,
                 29_990.0 + i, 30_005.0 + i, 1.5 + i)
                for i in range(5)]
        patcher.set("fetch_binance_klines",
                    MagicMock(return_value=fake))
        try:
            session = LiveTradingSession(_FakeMainWindow())
            out = session.fetch_historical_klines("BTCUSDT", _BUCKET_MS,
                                                  limit=5)
            self.assertEqual(out, fake)
            lts.fetch_binance_klines.assert_called_once()
            args, kwargs = lts.fetch_binance_klines.call_args
            self.assertEqual(args[0], lts.BINANCE_FUTURES_USDM_KLINES_URL)
            self.assertEqual(args[1], "BTCUSDT")
            self.assertEqual(args[2], _BUCKET_MS)
            self.assertEqual(kwargs.get("limit"), 5)
        finally:
            patcher.restore()

    def test_fetch_swallows_exceptions_returns_empty(self):
        patcher = _ModulePatcher(lts)

        def _boom(*a, **kw):
            raise RuntimeError("rest unavailable")

        patcher.set("fetch_binance_klines", _boom)
        try:
            session = LiveTradingSession(_FakeMainWindow())
            out = session.fetch_historical_klines("BTCUSDT", _BUCKET_MS)
            self.assertEqual(out, [])
        finally:
            patcher.restore()

    def test_fetch_empty_symbol_returns_empty_without_network(self):
        patcher = _ModulePatcher(lts)
        mock_fetch = MagicMock(return_value=[])
        patcher.set("fetch_binance_klines", mock_fetch)
        try:
            session = LiveTradingSession(_FakeMainWindow())
            out = session.fetch_historical_klines("   ", _BUCKET_MS)
            self.assertEqual(out, [])
            mock_fetch.assert_not_called()
        finally:
            patcher.restore()

    def test_fetch_unsupported_interval_returns_empty(self):
        """``fetch_binance_klines`` raises ``ValueError`` for unknown
        intervals; the session wrapper must swallow it."""
        session = LiveTradingSession(_FakeMainWindow())
        out = session.fetch_historical_klines("BTCUSDT", 1234)
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# Wiring check — production code must reference both APIs (§3.5)
# ---------------------------------------------------------------------------


class TestProductionWiring(unittest.TestCase):
    """Phase 8A integration: per AGENT_STRATEGY_RULES.md §3.5 the new
    capabilities must be invoked from a live runtime entry point. The
    spec wires them in ``ui/main_window.py``."""

    def test_main_window_references_both_apis(self):
        src = (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("fetch_historical_klines", src,
                      "main_window must call session.fetch_historical_klines")
        self.assertIn("preload_candles", src,
                      "main_window must call candle_view.preload_candles")
        self.assertIn("set_loading", src,
                      "main_window must toggle candle_view.set_loading")


if __name__ == "__main__":
    unittest.main()
