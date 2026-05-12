"""Phase 15 — PaperEngine LIMIT / OCO / partial-fill / timeout tests.

All acceptance criteria from implementation_plan.md §Phase 15:
  6.  LIMIT BUY fills at limit price when trade crosses; no fill when market stays above.
  7.  Timeout-cancels OPEN orders after limit_timeout_ms.
  8.  Partial fill: fill_quantity < order.quantity when simulated volume is insufficient.
  9.  OCO sibling cancellation: after one leg fills the other is CANCELLED.
 10.  paper_engine_parity: all Phase 15 behaviours tested here before live routing.

Also verifies:
  - Exit intent with reference_price=0.0 falls through to MARKET (not blocked).
  - SELL LIMIT fills when trade_price >= limit_price.
  - reset() clears open LIMIT orders.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.models import (
    ExecutionIntent,
    ExitType,
    Order,
    OrderLifecycle,
    OrderSide,
    OrderStatus,
    OrderType,
    SizingConfig,
    SizingMode,
    SuppressionReason,
)
from execution.paper_engine import PaperEngine, PaperPosition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TS = 1_700_000_000_000  # fixed epoch ms


def _intent(
    intent_type: str,
    side,
    ref_price: float,
    action: str = "ENTER_BOUNCE_LONG",
    ts_ms: int = BASE_TS,
    urgency: str = "NORMAL",
    exit_type=None,
    reason: str = "",
) -> ExecutionIntent:
    return ExecutionIntent(
        timestamp=ts_ms,
        source="Ripple",
        action=action,
        side=side,
        intent_type=intent_type,
        reference_price=ref_price,
        confidence=0.9,
        reason=reason,
        urgency=urgency,
        exit_type=exit_type,
    )


def _engine(timeout_ms: int = 30_000) -> PaperEngine:
    sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=0.01, max_position=1.0)
    return PaperEngine("BTCUSDT", sizing, limit_timeout_ms=timeout_ms,
                       use_limit_orders=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLimitBuyFill(unittest.TestCase):
    """AC 6 — LIMIT BUY fills at limit price when trade crosses."""

    def test_limit_buy_fills_when_trade_at_or_below_limit(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # One open LIMIT order should be registered
        self.assertEqual(len(eng._open_limits), 1)
        order = list(eng._open_limits.values())[0].order
        self.assertEqual(order.order_type, OrderType.LIMIT)
        self.assertEqual(order.lifecycle, OrderLifecycle.OPEN)

        # Trade at exactly the limit price → should fill
        eng.on_trade(trade_price=50_000.0, trade_qty=10.0, now_ms=BASE_TS + 1000)

        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(order.lifecycle, OrderLifecycle.FILLED)
        self.assertEqual(order.fill_price, 50_000.0)
        self.assertAlmostEqual(order.fill_quantity, 0.01, places=6)
        # Position should be updated
        self.assertEqual(eng.position.side, OrderSide.BUY)
        self.assertAlmostEqual(eng.position.quantity, 0.01, places=6)

    def test_limit_buy_no_fill_when_trade_above_limit(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # Trade above limit — should NOT fill
        eng.on_trade(trade_price=50_100.0, trade_qty=10.0, now_ms=BASE_TS + 1000)

        self.assertEqual(len(eng._open_limits), 1)
        order = list(eng._open_limits.values())[0].order
        self.assertEqual(order.lifecycle, OrderLifecycle.OPEN)
        self.assertAlmostEqual(order.fill_quantity, 0.0, places=8)

    def test_limit_buy_fills_below_limit_price(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)
        # Trade below limit → fill
        eng.on_trade(trade_price=49_900.0, trade_qty=10.0, now_ms=BASE_TS + 500)
        order = list(eng._orders)[-1]
        self.assertEqual(order.lifecycle, OrderLifecycle.FILLED)


class TestLimitSellFill(unittest.TestCase):
    """SELL LIMIT fills when trade_price >= limit_price."""

    def test_sell_limit_fills_when_trade_at_or_above_limit(self):
        eng = _engine()
        # First open a LONG position via MARKET (breakout)
        entry = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                        action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(entry)
        self.assertEqual(eng.position.side, OrderSide.BUY)

        # Now place a TARGET exit (LIMIT) at 51_000
        exit_intent = _intent(
            "exit", None, ref_price=51_000.0,
            action="EXIT_BOUNCE", ts_ms=BASE_TS + 5000,
            exit_type=ExitType.TARGET,
        )
        exit_intent.side = OrderSide.SELL
        eng.on_intent(exit_intent)

        # Trade at exactly the target price → SELL LIMIT fills
        eng.on_trade(trade_price=51_000.0, trade_qty=10.0, now_ms=BASE_TS + 6000)
        # Position should be flat after fill
        self.assertIsNone(eng.position.side)
        self.assertGreater(eng.metrics.realized_pnl, 0)

    def test_sell_limit_no_fill_when_trade_below_limit(self):
        eng = _engine()
        entry = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                        action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(entry)

        exit_intent = _intent(
            "exit", None, ref_price=51_000.0,
            action="EXIT_BOUNCE", ts_ms=BASE_TS + 5000,
            exit_type=ExitType.TARGET,
        )
        exit_intent.side = OrderSide.SELL
        eng.on_intent(exit_intent)

        # Trade below target → no fill
        eng.on_trade(trade_price=50_500.0, trade_qty=10.0, now_ms=BASE_TS + 6000)
        self.assertEqual(eng.position.side, OrderSide.BUY)


class TestLimitTimeout(unittest.TestCase):
    """AC 7 — Timeout cancels OPEN LIMIT orders after limit_timeout_ms."""

    def test_open_order_cancelled_after_timeout(self):
        eng = _engine(timeout_ms=5_000)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)
        self.assertEqual(len(eng._open_limits), 1)

        # Advance event-time past timeout; trade price doesn't cross
        eng.on_trade(trade_price=50_100.0, trade_qty=1.0, now_ms=BASE_TS + 6_000)

        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(eng.metrics.limit_timeouts, 1)
        # The order itself should be CANCELLED
        orders = eng.orders
        limit_order = next(o for o in orders if o.order_type == OrderType.LIMIT)
        self.assertEqual(limit_order.lifecycle, OrderLifecycle.CANCELLED)

    def test_partial_order_cancelled_after_timeout(self):
        eng = _engine(timeout_ms=5_000)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # Partial fill first (small trade qty)
        eng.on_trade(trade_price=49_900.0, trade_qty=0.001, now_ms=BASE_TS + 100)
        order = list(eng._open_limits.values())[0].order
        self.assertEqual(order.lifecycle, OrderLifecycle.PARTIAL)

        # Then timeout
        eng.on_trade(trade_price=50_100.0, trade_qty=1.0, now_ms=BASE_TS + 6_000)
        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)

    def test_order_within_timeout_not_cancelled(self):
        eng = _engine(timeout_ms=5_000)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # Only 3s elapsed — should NOT expire
        eng.on_trade(trade_price=50_100.0, trade_qty=1.0, now_ms=BASE_TS + 3_000)
        self.assertEqual(len(eng._open_limits), 1)
        self.assertEqual(eng.metrics.limit_timeouts, 0)


class TestPartialFill(unittest.TestCase):
    """AC 8 — Partial fill: fill_quantity < order.quantity when trade volume is small."""

    def test_partial_fill_leaves_residual(self):
        sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=1.0, max_position=10.0)
        eng = PaperEngine("BTCUSDT", sizing, limit_timeout_ms=60_000,
                          use_limit_orders=True)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # Trade qty only 0.3 — should partially fill the 1.0 qty order
        eng.on_trade(trade_price=49_900.0, trade_qty=0.3, now_ms=BASE_TS + 1000)

        self.assertEqual(len(eng._open_limits), 1)
        order = list(eng._open_limits.values())[0].order
        self.assertEqual(order.lifecycle, OrderLifecycle.PARTIAL)
        self.assertAlmostEqual(order.fill_quantity, 0.3, places=6)
        self.assertAlmostEqual(order.quantity - order.fill_quantity, 0.7, places=6)

    def test_partial_then_full_fill(self):
        sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=1.0, max_position=10.0)
        eng = PaperEngine("BTCUSDT", sizing, limit_timeout_ms=60_000,
                          use_limit_orders=True)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # Partial fill
        eng.on_trade(trade_price=49_900.0, trade_qty=0.3, now_ms=BASE_TS + 500)
        self.assertEqual(len(eng._open_limits), 1)

        # Full fill of remaining 0.7
        eng.on_trade(trade_price=49_900.0, trade_qty=0.7, now_ms=BASE_TS + 1000)
        self.assertEqual(len(eng._open_limits), 0)
        last_order = [o for o in eng.orders if o.order_type == OrderType.LIMIT][0]
        self.assertEqual(last_order.lifecycle, OrderLifecycle.FILLED)
        self.assertAlmostEqual(last_order.fill_quantity, 1.0, places=6)


class TestOCOSiblingCancellation(unittest.TestCase):
    """AC 9 — OCO sibling cancellation: after one leg fills the other is CANCELLED."""

    def _setup_oco(self):
        eng = _engine()
        # Open a LONG position first (MARKET breakout)
        entry = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                        action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(entry)

        # Place target LIMIT (sell side)
        target_intent = _intent(
            "exit", OrderSide.SELL, ref_price=51_000.0,
            action="EXIT_TARGET", ts_ms=BASE_TS + 1000,
            exit_type=ExitType.TARGET,
        )
        eng.on_intent(target_intent)

        # Find the registered LIMIT entry
        self.assertEqual(len(eng._open_limits), 1)
        entries = list(eng._open_limits.values())
        target_order = entries[0].order

        # Manually plant a fake stop companion with cross-reference
        from execution.models import Order as _Order, OrderLifecycle as _LC
        stop_order = _Order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=0.01,
            order_type=OrderType.MARKET,
            price=49_000.0,
            lifecycle=_LC.OPEN,
            placed_ms=BASE_TS + 1000,
        )
        from execution.paper_engine import _LimitEntry
        stop_entry = _LimitEntry(
            order=stop_order,
            limit_price=49_000.0,
            placed_ms=BASE_TS + 1000,
            is_buy=False,
        )
        eng._open_limits[stop_order.id] = stop_entry

        # Cross-reference
        target_order.limit_order_id = stop_order.id
        stop_order.limit_order_id = target_order.id

        return eng, target_order, stop_order

    def test_target_fills_cancels_stop_sibling(self):
        eng, target_order, stop_order = self._setup_oco()

        # Trade at target price → target fills, stop should be cancelled
        eng.on_trade(trade_price=51_000.0, trade_qty=10.0, now_ms=BASE_TS + 2000)

        self.assertEqual(target_order.lifecycle, OrderLifecycle.FILLED)
        self.assertEqual(stop_order.lifecycle, OrderLifecycle.CANCELLED)
        # Both should be removed from the open book
        self.assertEqual(len(eng._open_limits), 0)


class TestExitNotBlockedByMissingPrice(unittest.TestCase):
    """AC 5 — Exit intent with reference_price=0.0 → MARKET (not blocked)."""

    def test_exit_with_zero_ref_price_closes_position(self):
        eng = _engine()
        # Open a position
        entry = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                        action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(entry)
        self.assertIsNotNone(eng.position.side)

        # Exit with ref_price=0.0 (MARKET fallback) — must not be blocked
        eng._last_price = 50_500.0  # engine has price context
        exit_intent = _intent(
            "exit", None, ref_price=0.0, action="EXIT_BOUNCE",
            ts_ms=BASE_TS + 2000,
            exit_type=ExitType.INVALIDATION,
        )
        result = eng.on_intent(exit_intent)
        self.assertIsNone(result)  # not suppressed
        self.assertIsNone(eng.position.side)


class TestBreakoutUsesMarket(unittest.TestCase):
    """Breakout entry with IMMEDIATE urgency must use MARKET (immediate fill)."""

    def test_breakout_immediate_fills_as_market(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(intent)

        # No open LIMIT orders — filled immediately as MARKET
        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(eng.position.side, OrderSide.BUY)
        orders = eng.orders
        self.assertEqual(orders[-1].order_type, OrderType.MARKET)
        self.assertEqual(orders[-1].lifecycle, OrderLifecycle.FILLED)


class TestCancelPassiveClearsLimits(unittest.TestCase):
    """cancel intent clears all open LIMIT orders."""

    def test_cancel_intent_clears_open_limits(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)
        self.assertEqual(len(eng._open_limits), 1)

        cancel = ExecutionIntent(
            timestamp=BASE_TS + 1000,
            intent_type="cancel",
            action="CANCEL_PASSIVE_ORDERS",
        )
        eng.on_intent(cancel)
        self.assertEqual(len(eng._open_limits), 0)

        orders = [o for o in eng.orders if o.order_type == OrderType.LIMIT]
        self.assertTrue(all(o.lifecycle == OrderLifecycle.CANCELLED for o in orders))


class TestResetClearsLimits(unittest.TestCase):
    """reset() must clear open LIMIT orders."""

    def test_reset_clears_open_limits(self):
        eng = _engine()
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)
        self.assertEqual(len(eng._open_limits), 1)

        eng.reset()
        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(len(eng.orders), 0)


class TestV1DefaultFallback(unittest.TestCase):
    """V2 guard rail: use_limit_orders=False (default) preserves V1 MARKET-only behaviour."""

    def test_bounce_entry_uses_market_when_limit_orders_disabled(self):
        sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=0.01, max_position=1.0)
        # Default: use_limit_orders=False → V1 behaviour
        eng = PaperEngine("BTCUSDT", sizing)
        intent = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                         action="ENTER_BOUNCE_LONG", urgency="NORMAL")
        eng.on_intent(intent)

        # No LIMIT orders queued; position filled immediately (MARKET)
        self.assertEqual(len(eng._open_limits), 0)
        self.assertEqual(eng.position.side, OrderSide.BUY)
        orders = eng.orders
        self.assertEqual(orders[-1].order_type, OrderType.MARKET)
        self.assertEqual(orders[-1].lifecycle, OrderLifecycle.FILLED)

    def test_exit_with_target_uses_market_when_limit_orders_disabled(self):
        sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=0.01, max_position=1.0)
        eng = PaperEngine("BTCUSDT", sizing)
        entry = _intent("entry", OrderSide.BUY, ref_price=50_000.0,
                        action="ENTER_BREAKOUT_LONG", urgency="IMMEDIATE")
        eng.on_intent(entry)

        exit_intent = _intent("exit", None, ref_price=51_000.0,
                              action="EXIT_BOUNCE", ts_ms=BASE_TS + 1000,
                              exit_type=ExitType.TARGET)
        eng.on_intent(exit_intent)

        # V1: immediate MARKET close, no LIMIT queued
        self.assertEqual(len(eng._open_limits), 0)
        self.assertIsNone(eng.position.side)


if __name__ == "__main__":
    unittest.main()
