"""
Phase 12 — Track A: PaperEngine unit tests.

Synchronous, single-thread coverage for `execution/paper_engine.py`. No
broker, no asyncio, no Qt. Exercises every branch of `on_intent`, all
sizing modes, the inventory / suppression rules, PnL signing on long /
short closes, and the `order_callback` safety net.
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
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    SizingConfig,
    SizingMode,
    SuppressionReason,
)
from execution.paper_engine import PaperEngine, PaperPosition


# ----------------------------------------------------------------------
# Helpers

def _intent(intent_type: str, side, ref_price: float, action: str = "TEST",
            ts_ms: int = 1_700_000_000_000, reason: str = "") -> ExecutionIntent:
    return ExecutionIntent(
        timestamp=ts_ms,
        source="Ripple",
        action=action,
        side=side,
        intent_type=intent_type,
        reference_price=ref_price,
        confidence=0.9,
        reason=reason,
    )


def _engine(value: float = 0.001,
            mode: SizingMode = SizingMode.FIXED_QTY,
            callback=None) -> PaperEngine:
    return PaperEngine(
        symbol="BTCUSDT",
        sizing=SizingConfig(mode=mode, value=value, max_position=10.0),
        order_callback=callback,
    )


# ----------------------------------------------------------------------
# Construction

class TestPaperEngineConstruction(unittest.TestCase):

    def test_defaults(self):
        e = _engine()
        self.assertEqual(e.balance, 10_000.0)
        self.assertEqual(e.realized_pnl, 0.0)
        self.assertIsNone(e.position.side)
        self.assertEqual(e.position.quantity, 0.0)
        self.assertEqual(e.orders, [])
        self.assertEqual(e.metrics.intents_received, 0)
        self.assertEqual(e.metrics.entries_filled, 0)
        self.assertEqual(e.metrics.exits_filled, 0)


# ----------------------------------------------------------------------
# on_intent dispatch

class TestPaperEngineDispatch(unittest.TestCase):

    def test_intents_received_increments_for_every_intent_type(self):
        e = _engine()
        for kind, side in (
            ("entry", OrderSide.BUY),
            ("exit", None),       # no position; will suppress
            ("cancel", None),
            ("rearm", None),
            ("unknown_type", None),
        ):
            e.on_intent(_intent(kind, side, ref_price=100.0))
        self.assertEqual(e.metrics.intents_received, 5)

    def test_unknown_intent_type_returns_none(self):
        e = _engine()
        result = e.on_intent(_intent("garbage", None, ref_price=100.0))
        self.assertIsNone(result)
        self.assertEqual(len(e.orders), 0)

    def test_cancel_and_rearm_are_no_ops(self):
        e = _engine()
        self.assertIsNone(e.on_intent(_intent("cancel", None, ref_price=100.0)))
        self.assertIsNone(e.on_intent(_intent("rearm", None, ref_price=100.0)))
        self.assertEqual(len(e.orders), 0)
        self.assertIsNone(e.position.side)


# ----------------------------------------------------------------------
# Entry path

class TestPaperEngineEntry(unittest.TestCase):

    def test_entry_from_flat_fills_and_records_order(self):
        e = _engine(value=0.5)
        result = e.on_intent(
            _intent("entry", OrderSide.BUY, ref_price=100.0,
                    action="ENTER_BOUNCE_LONG"))
        self.assertIsNone(result)
        self.assertEqual(e.position.side, OrderSide.BUY)
        self.assertEqual(e.position.quantity, 0.5)
        self.assertEqual(e.position.entry_price, 100.0)
        self.assertEqual(e.metrics.entries_filled, 1)
        self.assertEqual(len(e.orders), 1)
        order = e.orders[0]
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.order_type, OrderType.MARKET)
        self.assertEqual(order.side, OrderSide.BUY)
        self.assertEqual(order.fill_price, 100.0)
        self.assertEqual(order.fill_quantity, 0.5)
        self.assertEqual(order.signal_type, "ENTER_BOUNCE_LONG")

    def test_same_side_re_entry_is_suppressed_inventory(self):
        e = _engine(value=0.5)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        # Identical side → INVENTORY, no new order, no counter bump.
        result = e.on_intent(_intent("entry", OrderSide.BUY, ref_price=101.0))
        self.assertEqual(result, SuppressionReason.INVENTORY)
        self.assertEqual(e.metrics.suppressed_inventory, 1)
        self.assertEqual(e.metrics.entries_filled, 1)
        self.assertEqual(len(e.orders), 1)

    def test_opposite_side_entry_flips_with_realized_pnl_long_to_short(self):
        e = _engine(value=1.0)
        # Long @ 100
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0,
                            action="ENTER_BOUNCE_LONG"))
        # Reverse to short @ 110 → close long for +10, open short
        result = e.on_intent(_intent("entry", OrderSide.SELL, ref_price=110.0,
                                     action="ENTER_BOUNCE_SHORT"))
        self.assertIsNone(result)
        self.assertEqual(e.position.side, OrderSide.SELL)
        self.assertAlmostEqual(e.position.quantity, 1.0)
        self.assertEqual(e.position.entry_price, 110.0)
        self.assertAlmostEqual(e.realized_pnl, 10.0)
        self.assertAlmostEqual(e.balance, 10_010.0)
        self.assertEqual(e.metrics.entries_filled, 2)  # both legs counted
        # close + new-entry orders
        self.assertEqual(len(e.orders), 3)
        self.assertEqual(e.orders[1].side, OrderSide.SELL)  # close-of-long
        self.assertTrue(e.orders[1].signal_type.startswith("CLOSE_"))
        self.assertEqual(e.orders[2].side, OrderSide.SELL)  # new short
        self.assertEqual(e.orders[2].signal_type, "ENTER_BOUNCE_SHORT")

    def test_opposite_side_entry_flips_with_realized_pnl_short_to_long(self):
        e = _engine(value=1.0)
        # Short @ 100
        e.on_intent(_intent("entry", OrderSide.SELL, ref_price=100.0))
        # Reverse to long @ 90 → close short for +10
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=90.0))
        self.assertEqual(e.position.side, OrderSide.BUY)
        self.assertAlmostEqual(e.realized_pnl, 10.0)
        # Reverse to long @ 110 from a short would be a loss; verify sign too.
        e2 = _engine(value=1.0)
        e2.on_intent(_intent("entry", OrderSide.SELL, ref_price=100.0))
        e2.on_intent(_intent("entry", OrderSide.BUY, ref_price=110.0))
        self.assertAlmostEqual(e2.realized_pnl, -10.0)
        self.assertAlmostEqual(e2.balance, 9_990.0)

    def test_entry_with_none_side_is_inventory(self):
        e = _engine()
        result = e.on_intent(_intent("entry", None, ref_price=100.0))
        self.assertEqual(result, SuppressionReason.INVENTORY)
        self.assertEqual(len(e.orders), 0)

    def test_entry_with_no_price_and_no_last_price_is_inventory(self):
        e = _engine()
        result = e.on_intent(_intent("entry", OrderSide.BUY, ref_price=0.0))
        self.assertEqual(result, SuppressionReason.INVENTORY)
        self.assertEqual(len(e.orders), 0)

    def test_entry_uses_last_price_when_ref_price_zero(self):
        # Prime _last_price via an earlier intent.
        e = _engine(value=0.1)
        e.on_intent(_intent("cancel", None, ref_price=200.0))  # primes _last_price
        result = e.on_intent(_intent("entry", OrderSide.BUY, ref_price=0.0))
        self.assertIsNone(result)
        self.assertEqual(e.position.entry_price, 200.0)

    def test_entry_with_zero_quantity_sizing_is_inventory(self):
        e = _engine(value=0.0)  # FIXED_QTY=0 → qty=0 → suppressed
        result = e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertEqual(result, SuppressionReason.INVENTORY)
        self.assertEqual(len(e.orders), 0)


# ----------------------------------------------------------------------
# Exit path

class TestPaperEngineExit(unittest.TestCase):

    def test_exit_with_open_long_position_realises_positive_pnl(self):
        e = _engine(value=2.0)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        result = e.on_intent(_intent("exit", None, ref_price=105.0,
                                     action="EXIT_BOUNCE"))
        self.assertIsNone(result)
        self.assertIsNone(e.position.side)
        self.assertEqual(e.position.quantity, 0.0)
        self.assertEqual(e.metrics.exits_filled, 1)
        self.assertAlmostEqual(e.realized_pnl, 10.0)  # (105-100) * 2
        self.assertAlmostEqual(e.balance, 10_010.0)
        # last order is the close, side=SELL
        self.assertEqual(e.orders[-1].side, OrderSide.SELL)
        self.assertEqual(e.orders[-1].status, OrderStatus.FILLED)

    def test_exit_with_open_short_position_realises_positive_pnl(self):
        e = _engine(value=2.0)
        e.on_intent(_intent("entry", OrderSide.SELL, ref_price=100.0))
        e.on_intent(_intent("exit", None, ref_price=95.0))
        self.assertAlmostEqual(e.realized_pnl, 10.0)  # (100-95) * 2
        self.assertEqual(e.orders[-1].side, OrderSide.BUY)  # close-of-short

    def test_exit_without_position_is_inventory(self):
        e = _engine()
        result = e.on_intent(_intent("exit", None, ref_price=100.0))
        self.assertEqual(result, SuppressionReason.INVENTORY)
        self.assertEqual(e.metrics.suppressed_no_position, 1)
        self.assertEqual(len(e.orders), 0)


# ----------------------------------------------------------------------
# Sizing modes

class TestPaperEngineSizing(unittest.TestCase):

    def test_fixed_qty_returns_value_directly(self):
        e = _engine(value=0.25, mode=SizingMode.FIXED_QTY)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertAlmostEqual(e.position.quantity, 0.25)

    def test_fixed_notional_divides_by_price(self):
        e = _engine(value=500.0, mode=SizingMode.FIXED_NOTIONAL)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertAlmostEqual(e.position.quantity, 5.0)  # 500 / 100

    def test_pct_balance_uses_balance_and_price(self):
        e = _engine(value=10.0, mode=SizingMode.PCT_BALANCE)
        # 10% of $10_000 = $1_000 → / 100 price = 10 qty
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertAlmostEqual(e.position.quantity, 10.0)

    def test_fixed_notional_with_zero_price_after_priming(self):
        # Prime _last_price > 0, then submit zero-ref-price entry — should
        # use _last_price, not divide-by-zero.
        e = _engine(value=300.0, mode=SizingMode.FIXED_NOTIONAL)
        e.on_intent(_intent("cancel", None, ref_price=150.0))  # prime
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=0.0))
        self.assertAlmostEqual(e.position.quantity, 2.0)  # 300 / 150


# ----------------------------------------------------------------------
# Unrealized PnL

class TestPaperEngineUnrealizedPnL(unittest.TestCase):

    def test_unrealized_pnl_long(self):
        e = _engine(value=2.0)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertAlmostEqual(e.unrealized_pnl(110.0), 20.0)
        self.assertAlmostEqual(e.unrealized_pnl(95.0), -10.0)

    def test_unrealized_pnl_short(self):
        e = _engine(value=2.0)
        e.on_intent(_intent("entry", OrderSide.SELL, ref_price=100.0))
        self.assertAlmostEqual(e.unrealized_pnl(90.0), 20.0)
        self.assertAlmostEqual(e.unrealized_pnl(105.0), -10.0)

    def test_unrealized_pnl_flat_is_zero(self):
        e = _engine()
        self.assertEqual(e.unrealized_pnl(100.0), 0.0)


# ----------------------------------------------------------------------
# update_sizing / reset / callback safety

class TestPaperEngineMisc(unittest.TestCase):

    def test_update_sizing_preserves_position(self):
        e = _engine(value=1.0)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        e.update_sizing(SizingConfig(mode=SizingMode.FIXED_QTY,
                                     value=5.0, max_position=10.0))
        # Position untouched.
        self.assertEqual(e.position.side, OrderSide.BUY)
        self.assertAlmostEqual(e.position.quantity, 1.0)
        # Subsequent flip uses the new sizing.
        e.on_intent(_intent("entry", OrderSide.SELL, ref_price=100.0))
        self.assertAlmostEqual(e.position.quantity, 5.0)

    def test_reset_clears_state(self):
        e = _engine(value=1.0)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        e.on_intent(_intent("exit", None, ref_price=110.0))
        self.assertGreater(e.realized_pnl, 0)
        self.assertGreater(len(e.orders), 0)
        e.reset()
        self.assertIsNone(e.position.side)
        self.assertEqual(e.position.quantity, 0.0)
        self.assertEqual(e.orders, [])
        self.assertEqual(e.metrics.intents_received, 0)
        self.assertEqual(e.metrics.realized_pnl, 0.0)
        self.assertEqual(e.balance, 10_000.0)

    def test_order_callback_invoked_once_per_recorded_order(self):
        seen: list[Order] = []
        e = _engine(value=1.0, callback=seen.append)
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))   # 1
        e.on_intent(_intent("exit", None, ref_price=110.0))              # 2
        self.assertEqual(len(seen), 2)
        # Flip records two orders → two more callbacks.
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))   # 3
        e.on_intent(_intent("entry", OrderSide.SELL, ref_price=110.0))  # 4 + 5
        self.assertEqual(len(seen), 5)

    def test_order_callback_exception_is_swallowed(self):
        def boom(_order):
            raise RuntimeError("callback failure")
        e = _engine(value=1.0, callback=boom)
        # Must not raise; order should still land in the deque.
        e.on_intent(_intent("entry", OrderSide.BUY, ref_price=100.0))
        self.assertEqual(len(e.orders), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
