"""Phase 15 — ExecutionManager order-type routing tests.

Acceptance criteria from implementation_plan.md §Phase 15:
  1. Bounce entry (urgency=NORMAL, reference_price valid) → LIMIT at reference_price.
  2. Breakout entry (urgency=IMMEDIATE) → MARKET.
  3. ExitType.TARGET and ExitType.EXHAUSTION → LIMIT.
  4. ExitType.INVALIDATION and ExitType.RISK_BUDGET → MARKET with urgency=IMMEDIATE.
  5. reference_price=NaN or reference_price=0.0 on entry → MARKET + LIMIT_FALLBACK log.
  5b. Exit intent with reference_price=0.0 → MARKET (never blocked).
 12. Wired live: grep gate (verified by test_wired_live below).
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.models import (
    ExecutionIntent,
    ExitType,
    OrderSide,
    OrderType,
)
from execution.execution_manager import ExecutionManager


# ---------------------------------------------------------------------------
# Exercise _resolve_order_type directly (static method — no broker needed)
# ---------------------------------------------------------------------------

def _intent(
    intent_type: str,
    action: str,
    ref_price: float,
    urgency: str = "NORMAL",
    exit_type=None,
    side: OrderSide = OrderSide.BUY,
) -> ExecutionIntent:
    return ExecutionIntent(
        timestamp=1_700_000_000_000,
        source="Ripple",
        action=action,
        side=side,
        intent_type=intent_type,
        reference_price=ref_price,
        confidence=0.9,
        urgency=urgency,
        exit_type=exit_type,
    )


class TestOrderTypeRouting(unittest.TestCase):
    """Direct unit tests of ExecutionManager._resolve_order_type."""

    # AC 1 — bounce entry → LIMIT
    def test_bounce_entry_normal_urgency_valid_price_yields_limit(self):
        intent = _intent("entry", "ENTER_BOUNCE_LONG", ref_price=50_000.0,
                         urgency="NORMAL")
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.LIMIT)
        self.assertAlmostEqual(price, 50_000.0)
        self.assertFalse(fallback)

    def test_bounce_short_entry_normal_urgency_yields_limit(self):
        intent = _intent("entry", "ENTER_BOUNCE_SHORT", ref_price=50_000.0,
                         urgency="NORMAL", side=OrderSide.SELL)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.LIMIT)
        self.assertAlmostEqual(price, 50_000.0)

    # AC 2 — breakout entry → MARKET
    def test_breakout_entry_immediate_yields_market(self):
        intent = _intent("entry", "ENTER_BREAKOUT_LONG", ref_price=50_000.0,
                         urgency="IMMEDIATE")
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertEqual(price, 0.0)
        self.assertFalse(fallback)

    # AC 3 — TARGET exit → LIMIT
    def test_target_exit_yields_limit(self):
        intent = _intent("exit", "EXIT_BOUNCE", ref_price=51_000.0,
                         exit_type=ExitType.TARGET)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.LIMIT)
        self.assertAlmostEqual(price, 51_000.0)
        self.assertFalse(fallback)

    def test_exhaustion_exit_yields_limit(self):
        intent = _intent("exit", "EXIT_BOUNCE", ref_price=51_000.0,
                         exit_type=ExitType.EXHAUSTION)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.LIMIT)

    # AC 4 — INVALIDATION / RISK_BUDGET exits → MARKET
    def test_invalidation_exit_yields_market(self):
        intent = _intent("exit", "EXIT_INVALIDATION", ref_price=49_000.0,
                         urgency="IMMEDIATE", exit_type=ExitType.INVALIDATION)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertFalse(fallback)

    def test_risk_budget_exit_yields_market(self):
        intent = _intent("exit", "EXIT_RISK_BUDGET", ref_price=49_000.0,
                         urgency="IMMEDIATE", exit_type=ExitType.RISK_BUDGET)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)

    def test_time_exit_yields_market(self):
        intent = _intent("exit", "EXIT_TIME", ref_price=50_000.0,
                         exit_type=ExitType.TIME)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)

    # AC 5 — NaN / zero reference_price on entry → MARKET + fallback
    def test_nan_ref_price_on_bounce_entry_falls_back_to_market(self):
        intent = _intent("entry", "ENTER_BOUNCE_LONG", ref_price=float("nan"),
                         urgency="NORMAL")
        with self.assertLogs("execution.execution_manager", level="WARNING") as cm:
            otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertTrue(fallback)
        self.assertTrue(any("LIMIT_FALLBACK" in msg for msg in cm.output))

    def test_zero_ref_price_on_bounce_entry_falls_back_to_market(self):
        intent = _intent("entry", "ENTER_BOUNCE_LONG", ref_price=0.0,
                         urgency="NORMAL")
        with self.assertLogs("execution.execution_manager", level="WARNING") as cm:
            otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertTrue(fallback)
        self.assertTrue(any("LIMIT_FALLBACK" in msg for msg in cm.output))

    def test_inf_ref_price_on_bounce_entry_falls_back_to_market(self):
        intent = _intent("entry", "ENTER_BOUNCE_LONG", ref_price=float("inf"),
                         urgency="NORMAL")
        with self.assertLogs("execution.execution_manager", level="WARNING") as cm:
            otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertTrue(fallback)

    # AC 5b — exit with zero ref price → MARKET (never blocked)
    def test_exit_with_zero_ref_price_falls_back_to_market_not_blocked(self):
        intent = _intent("exit", "EXIT_TARGET", ref_price=0.0,
                         exit_type=ExitType.TARGET)
        with self.assertLogs("execution.execution_manager", level="WARNING") as cm:
            otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertTrue(fallback)
        # Must have logged LIMIT_FALLBACK
        self.assertTrue(any("LIMIT_FALLBACK" in msg for msg in cm.output))

    def test_exit_nan_ref_price_falls_back_to_market(self):
        intent = _intent("exit", "EXIT_TARGET", ref_price=float("nan"),
                         exit_type=ExitType.TARGET)
        with self.assertLogs("execution.execution_manager", level="WARNING"):
            otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)
        self.assertTrue(fallback)

    # Scale-in → LIMIT (NORMAL urgency)
    def test_scale_in_normal_yields_limit(self):
        intent = _intent("entry", "SCALE_IN", ref_price=50_000.0, urgency="NORMAL")
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.LIMIT)

    # Unknown intent type → MARKET default
    def test_unknown_intent_type_yields_market(self):
        intent = _intent("prepare", "PREPARE_BOUNCE_LONG", ref_price=50_000.0)
        otype, price, fallback = ExecutionManager._resolve_order_type(intent)
        self.assertEqual(otype, OrderType.MARKET)


class TestWiredLive(unittest.TestCase):
    """AC 12 — grep gate: ensure place_order with LIMIT is present in production code."""

    def test_limit_routing_present_in_execution_manager(self):
        src = (ROOT / "execution" / "execution_manager.py").read_text()
        self.assertIn("OrderType.LIMIT", src,
                      "execution_manager.py must reference OrderType.LIMIT "
                      "(Phase 15 wiring gate)")

    def test_limit_routing_present_in_paper_engine(self):
        src = (ROOT / "execution" / "paper_engine.py").read_text()
        self.assertIn("OrderType.LIMIT", src,
                      "paper_engine.py must reference OrderType.LIMIT "
                      "(Phase 15 wiring gate)")

    def test_limit_routing_present_in_binance_broker(self):
        src = (ROOT / "execution" / "binance_broker.py").read_text()
        self.assertIn("OrderType.LIMIT", src,
                      "binance_broker.py must reference OrderType.LIMIT "
                      "(Phase 15 wiring gate)")


if __name__ == "__main__":
    unittest.main()
