"""Tests for Phase 8B — Strategy-Attributed PnL & Trade Tracking.

Validates:
- update_strategy_stats renders side/qty/upnl/realized/count correctly
- Mode label shows Paper/Live/Observe
- "Strategy Trades" counter increments
- Reason column populated from ripple_reason
- FIFO accumulator is absent (_last_entry_price attribute does not exist)
- Wiring: MainWindow._on_timer_tick calls account_panel.update_strategy_stats
"""
import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import Order, OrderSide, OrderStatus, OrderType
from ui.account_panel import (
    AccountPanel,
    _MODE_LABEL_PAPER, _MODE_LABEL_LIVE, _MODE_LABEL_OBSERVE,
    _ORDER_COLUMNS, _STRATEGY_NOT_ARMED,
    _COL_REASON,
)


def _make_order(
    side=OrderSide.BUY,
    fill_price=50000.0,
    fill_qty=0.001,
    status=OrderStatus.FILLED,
    signal_type="RIPPLE_ENTRY",
    ripple_reason="BOUNCE_SETUP",
    timestamp=None,
):
    return Order(
        symbol="BTCUSDT",
        side=side,
        quantity=fill_qty,
        order_type=OrderType.MARKET,
        status=status,
        fill_price=fill_price,
        fill_quantity=fill_qty,
        timestamp=timestamp or time.time(),
        signal_type=signal_type,
        ripple_reason=ripple_reason,
    )


class TestAccountPanelFIFOAbsent(unittest.TestCase):
    """FIFO accumulator attributes must not exist on AccountPanel."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_no_last_entry_price(self):
        """_last_entry_price must not exist (FIFO accumulator removed)."""
        self.assertFalse(
            hasattr(self.panel, "_last_entry_price"),
            "_last_entry_price FIFO attribute must be absent",
        )

    def test_no_last_entry_side(self):
        """_last_entry_side must not exist."""
        self.assertFalse(
            hasattr(self.panel, "_last_entry_side"),
            "_last_entry_side FIFO attribute must be absent",
        )

    def test_no_realized_pnl_attribute(self):
        """Raw _realized_pnl accumulator must not exist."""
        self.assertFalse(
            hasattr(self.panel, "_realized_pnl"),
            "_realized_pnl FIFO attribute must be absent",
        )


class TestUpdateStrategyStatsObserve(unittest.TestCase):
    """update_strategy_stats with mode_label=Observe shows clean state."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_observe_mode_label_text(self):
        """Mode label shows 'Observe' when called with _MODE_LABEL_OBSERVE."""
        self.panel.update_strategy_stats(None, 0.0, 0.0, 0.0, 0.0, 0,
                                         _MODE_LABEL_OBSERVE)
        self.assertIn("Observe", self.panel._mode_label.text())

    def test_observe_strategy_not_armed(self):
        """Session PnL label shows 'Strategy not armed' in Observe mode."""
        self.panel.update_strategy_stats(None, 0.0, 0.0, 0.0, 0.0, 0,
                                         _MODE_LABEL_OBSERVE)
        self.assertIn(_STRATEGY_NOT_ARMED, self.panel._rpnl_label.text())

    def test_observe_trades_dash(self):
        """Strategy Trades shows '—' in Observe mode."""
        self.panel.update_strategy_stats(None, 0.0, 0.0, 0.0, 0.0, 0,
                                         _MODE_LABEL_OBSERVE)
        self.assertIn("—", self.panel._trades_label.text())


class TestUpdateStrategyStatsPaper(unittest.TestCase):
    """update_strategy_stats with mode_label=Paper shows live stats."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_paper_mode_label(self):
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 25.0, 3,
                                         _MODE_LABEL_PAPER)
        self.assertIn(_MODE_LABEL_PAPER, self.panel._mode_label.text())

    def test_paper_realized_pnl_rendered(self):
        """Session PnL shows formatted realized PnL value."""
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 25.75, 3,
                                         _MODE_LABEL_PAPER)
        text = self.panel._rpnl_label.text()
        self.assertIn("25.75", text)
        self.assertIn("Session PnL (strategy)", text)

    def test_paper_negative_pnl_rendered(self):
        """Negative realized PnL is rendered with sign."""
        self.panel.update_strategy_stats("SELL", 0.001, 50000.0, -3.0, -12.50, 1,
                                         _MODE_LABEL_PAPER)
        text = self.panel._rpnl_label.text()
        self.assertIn("-12.50", text)

    def test_paper_trade_count_rendered(self):
        """Strategy Trades counter shows the count."""
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 0.0, 7,
                                         _MODE_LABEL_PAPER)
        self.assertIn("7", self.panel._trades_label.text())

    def test_paper_trade_count_increments(self):
        """Calling update_strategy_stats twice with higher count reflects update."""
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 0.0, 3,
                                         _MODE_LABEL_PAPER)
        self.assertIn("3", self.panel._trades_label.text())
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 0.0, 4,
                                         _MODE_LABEL_PAPER)
        self.assertIn("4", self.panel._trades_label.text())

    def test_paper_not_armed_text_absent(self):
        """'Strategy not armed' must NOT appear in Paper mode."""
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 5.0, 10.0, 2,
                                         _MODE_LABEL_PAPER)
        self.assertNotIn(_STRATEGY_NOT_ARMED, self.panel._rpnl_label.text())


class TestUpdateStrategyStatsLive(unittest.TestCase):
    """update_strategy_stats with mode_label=Live."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_live_mode_label(self):
        self.panel.update_strategy_stats("SELL", 0.002, 49000.0, 0.0, 100.0, 5,
                                         _MODE_LABEL_LIVE)
        self.assertIn(_MODE_LABEL_LIVE, self.panel._mode_label.text())

    def test_live_trade_count(self):
        self.panel.update_strategy_stats("SELL", 0.002, 49000.0, 0.0, 100.0, 5,
                                         _MODE_LABEL_LIVE)
        self.assertIn("5", self.panel._trades_label.text())


class TestOrderTableReasonColumn(unittest.TestCase):
    """add_order populates the Reason column from order.ripple_reason."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_reason_column_exists(self):
        """Order table has a Reason column."""
        headers = [
            self.panel._order_table.horizontalHeaderItem(c).text()
            for c in range(self.panel._order_table.columnCount())
        ]
        self.assertIn(_COL_REASON, headers)

    def test_reason_populated_from_ripple_reason(self):
        """Reason column shows order.ripple_reason."""
        order = _make_order(ripple_reason="EXIT_TARGET")
        self.panel.add_order(order)
        reason_col = _ORDER_COLUMNS.index(_COL_REASON)
        item = self.panel._order_table.item(0, reason_col)
        self.assertIsNotNone(item)
        self.assertEqual(item.text(), "EXIT_TARGET")

    def test_reason_empty_when_no_ripple_reason(self):
        """Reason column is empty string when ripple_reason is absent."""
        order = _make_order(ripple_reason="")
        self.panel.add_order(order)
        reason_col = _ORDER_COLUMNS.index(_COL_REASON)
        item = self.panel._order_table.item(0, reason_col)
        self.assertIsNotNone(item)
        self.assertEqual(item.text(), "")

    def test_multiple_orders_reason_column(self):
        """Reason column correctly populated across multiple orders."""
        reasons = ["BOUNCE_SETUP", "EXIT_EXHAUSTION", ""]
        for reason in reasons:
            self.panel.add_order(_make_order(ripple_reason=reason))
        reason_col = _ORDER_COLUMNS.index(_COL_REASON)
        for row, expected in enumerate(reasons):
            item = self.panel._order_table.item(row, reason_col)
            self.assertEqual(item.text(), expected,
                             f"row {row}: expected '{expected}' got '{item.text()}'")

    def test_eight_columns_total(self):
        """Order table has exactly 8 columns (Time/Side/Qty/Price/PnL/Status/Signal/Reason)."""
        self.assertEqual(self.panel._order_table.columnCount(), 8)


class TestBackwardCompatibility(unittest.TestCase):
    """Existing AccountPanel API must still work after Phase 8B."""

    def setUp(self):
        self.panel = AccountPanel()

    def test_update_account_connects(self):
        """update_account auto-connects the panel."""
        self.assertFalse(self.panel._connected)
        self.panel.update_account(1000.0, 900.0, [])
        self.assertTrue(self.panel._connected)

    def test_update_account_balance_label(self):
        self.panel.update_account(1234.56, 1000.0, [])
        self.assertIn("1234.56", self.panel._bal_label.text())

    def test_set_connected_toggles(self):
        self.panel.set_connected(True)
        self.assertTrue(self.panel._connected)
        self.panel.set_connected(False)
        self.assertFalse(self.panel._connected)

    def test_clear_disconnects(self):
        self.panel.set_connected(True)
        self.assertTrue(self.panel._connected)
        self.panel.clear()
        self.assertFalse(self.panel._connected)

    def test_clear_resets_mode_to_observe(self):
        """clear() resets mode label to Observe."""
        self.panel.update_strategy_stats("BUY", 0.001, 50000.0, 0.0, 100.0, 5,
                                         _MODE_LABEL_PAPER)
        self.panel.clear()
        self.assertIn("Observe", self.panel._mode_label.text())

    def test_add_order_does_not_raise(self):
        order = _make_order()
        try:
            self.panel.add_order(order)
        except Exception as exc:
            self.fail(f"add_order raised {exc}")


class TestTimerTickWiring(unittest.TestCase):
    """Wiring test: MainWindow._on_timer_tick calls account_panel.update_strategy_stats.

    Instantiates MainWindow with stubbed session and asserts that each
    call to _on_timer_tick reaches account_panel.update_strategy_stats.
    This satisfies AGENT_STRATEGY_RULES.md §3.5 integration contract.
    """

    def test_timer_tick_calls_update_strategy_stats(self):
        """_on_timer_tick must call account_panel.update_strategy_stats exactly once."""
        from ui.main_window import MainWindow
        mw = MainWindow()

        # Stub the session so on_timer_tick / block_status / layered_push_status
        # don't raise.
        mock_session = MagicMock()
        mock_session.block_status.return_value = ("", 0, 0)
        mock_session.layered_push_status.return_value = {}
        mw._session = mock_session

        # Spy on the account panel method.
        mw._strategy_dashboard.account_panel.update_strategy_stats = MagicMock()

        mw._on_timer_tick()

        mw._strategy_dashboard.account_panel.update_strategy_stats.assert_called_once()

    def test_timer_tick_observe_mode_passes_observe_label(self):
        """In OBSERVE mode, update_strategy_stats is called with _MODE_LABEL_OBSERVE."""
        from execution.models import StrategyMode
        from ui.main_window import MainWindow
        mw = MainWindow()

        mock_session = MagicMock()
        mock_session.block_status.return_value = ("", 0, 0)
        mock_session.layered_push_status.return_value = {}
        mw._session = mock_session
        mw._strategy_mode = StrategyMode.OBSERVE

        calls = []
        def capture(*args, **kwargs):
            calls.append((args, kwargs))
        mw._strategy_dashboard.account_panel.update_strategy_stats = capture

        mw._on_timer_tick()

        self.assertEqual(len(calls), 1, "update_strategy_stats called once")
        _args, _kwargs = calls[0]
        # Last positional argument is mode_label
        mode_label = _args[-1]
        self.assertEqual(mode_label, _MODE_LABEL_OBSERVE,
                         f"Expected {_MODE_LABEL_OBSERVE!r}, got {mode_label!r}")

    def test_timer_tick_paper_mode_passes_paper_label(self):
        """In PAPER mode with paper engine present, label is _MODE_LABEL_PAPER."""
        from execution.models import StrategyMode, SizingConfig, SizingMode
        from execution.paper_engine import PaperEngine
        from ui.main_window import MainWindow
        mw = MainWindow()

        mock_session = MagicMock()
        mock_session.block_status.return_value = ("", 0, 0)
        mock_session.layered_push_status.return_value = {}
        mw._session = mock_session
        mw._strategy_mode = StrategyMode.PAPER
        mw._paper_engine = PaperEngine(
            "BTCUSDT",
            SizingConfig(mode=SizingMode.FIXED_NOTIONAL, value=100.0),
        )

        calls = []
        def capture(*args, **kwargs):
            calls.append((args, kwargs))
        mw._strategy_dashboard.account_panel.update_strategy_stats = capture

        mw._on_timer_tick()

        self.assertEqual(len(calls), 1)
        mode_label = calls[0][0][-1]
        self.assertEqual(mode_label, _MODE_LABEL_PAPER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
