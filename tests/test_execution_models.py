"""
Phase 12 — Track D: execution/models.py helper coverage.

Pins the surface of `_parse_intent_name`, `ripple_decision_to_entry`,
and `ripple_decision_to_intent`, plus the dataclass defaults that
`PaperEngine` / `ExecutionManager` depend on. No C++ engine, no Qt.
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
    OrderSide,
    SignalCategory,
    SignalEntry,
    SizingConfig,
    SizingMode,
    SuppressionReason,
    _RIPPLE_INTENT_MAP,
    _parse_intent_name,
    ripple_decision_to_entry,
    ripple_decision_to_intent,
)


class _FakeIntent:
    """Stand-in for a C++ pybind11 enum value. ``str(self)`` yields
    ``"RippleIntent.NAME"`` to mirror the real C++ binding format."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f"RippleIntent.{self.name}"


class _FakeDecision:
    """Drop-in for the C++ RippleDecision struct used by the helpers."""

    def __init__(self, intent_name: str, *,
                 timestamp: int = 1_700_000_000_000,
                 reference_price: float = 100.0,
                 invalidation_price: float = 95.0,
                 confidence: float = 0.85,
                 reason: str = "test reason",
                 wall_id: int = 7) -> None:
        self.intent = _FakeIntent(intent_name)
        self.timestamp = timestamp
        self.reference_price = reference_price
        self.invalidation_price = invalidation_price
        self.confidence = confidence
        self.reason = reason
        self.wall_id = wall_id


# ----------------------------------------------------------------------

class TestParseIntentName(unittest.TestCase):

    def test_parse_intent_name_strips_enum_prefix(self):
        d = _FakeDecision("ENTER_BOUNCE_LONG")
        self.assertEqual(_parse_intent_name(d), "ENTER_BOUNCE_LONG")

    def test_parse_intent_name_handles_no_dot(self):
        # Some C++ enum bindings str() without a class prefix.
        class _NoDot:
            intent = "REARM_FOR_NEXT_BOUNCE"
            def __str__(self): return "REARM_FOR_NEXT_BOUNCE"
        # _parse_intent_name reads decision.intent (not decision itself).
        d = type("D", (), {"intent": _NoDot()})()
        self.assertEqual(_parse_intent_name(d), "REARM_FOR_NEXT_BOUNCE")


# ----------------------------------------------------------------------

class TestRippleDecisionToEntry(unittest.TestCase):

    def test_no_action_returns_none(self):
        d = _FakeDecision("NO_ACTION")
        self.assertIsNone(ripple_decision_to_entry(d))

    def test_unknown_intent_name_returns_none(self):
        d = _FakeDecision("FROBNICATE_PASSIVE_LIQUIDITY")
        self.assertIsNone(ripple_decision_to_entry(d))

    def test_every_mapped_intent_round_trips_to_signal_entry(self):
        # For every entry in the map, the helper must:
        #   - return a SignalEntry whose category, signal_type, and side
        #     match the map's tuple
        #   - propagate timestamp, reference_price, confidence, reason
        for intent_name, (cat, display, is_buy) in _RIPPLE_INTENT_MAP.items():
            d = _FakeDecision(intent_name, timestamp=42, reference_price=99.5,
                              confidence=0.42, reason=f"r-{intent_name}")
            entry = ripple_decision_to_entry(d, state_name="STATE_X")
            self.assertIsNotNone(entry, intent_name)
            self.assertEqual(entry.category, cat, intent_name)
            self.assertEqual(entry.signal_type, display, intent_name)
            expected_side = "BUY" if is_buy is True else (
                "SELL" if is_buy is False else "")
            self.assertEqual(entry.side, expected_side, intent_name)
            self.assertEqual(entry.timestamp, 42)
            self.assertAlmostEqual(entry.price, 99.5)
            self.assertAlmostEqual(entry.strength, 0.42)
            self.assertEqual(entry.description, f"r-{intent_name}")
            self.assertEqual(entry.state_summary, "STATE_X")
            self.assertEqual(entry.source, "ripple")

    def test_explicit_intent_name_short_circuits_parse(self):
        # A blank decision.intent + explicit intent_name still resolves.
        d = _FakeDecision("ANYTHING")
        entry = ripple_decision_to_entry(d, intent_name="ENTER_BREAKOUT_LONG")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.signal_type, "ENTRY_BREAKOUT_LONG")
        self.assertEqual(entry.side, "BUY")


# ----------------------------------------------------------------------

class TestRippleDecisionToIntent(unittest.TestCase):

    def test_no_action_returns_none(self):
        self.assertIsNone(
            ripple_decision_to_intent(_FakeDecision("NO_ACTION")))

    def test_unknown_intent_returns_none(self):
        self.assertIsNone(
            ripple_decision_to_intent(_FakeDecision("UNKNOWN_INTENT")))

    def test_intent_type_per_category(self):
        cases = {
            "ENTER_BOUNCE_LONG":     ("entry",   OrderSide.BUY),
            "ENTER_BOUNCE_SHORT":    ("entry",   OrderSide.SELL),
            "ENTER_BREAKOUT_LONG":   ("entry",   OrderSide.BUY),
            "ENTER_BREAKOUT_SHORT":  ("entry",   OrderSide.SELL),
            "EXIT_BOUNCE":           ("exit",    None),
            "EXIT_BREAKOUT":         ("exit",    None),
            "CANCEL_PASSIVE_ORDERS": ("cancel",  None),
            "REARM_FOR_NEXT_BOUNCE": ("rearm",   None),
            "PREPARE_BOUNCE_LONG":   ("prepare", OrderSide.BUY),
            "PREPARE_BOUNCE_SHORT":  ("prepare", OrderSide.SELL),
        }
        for name, (expected_type, expected_side) in cases.items():
            d = _FakeDecision(name)
            intent = ripple_decision_to_intent(d, state_name="ST")
            self.assertIsNotNone(intent, name)
            self.assertEqual(intent.intent_type, expected_type, name)
            self.assertEqual(intent.side, expected_side, name)
            self.assertEqual(intent.action, name)

    def test_carryover_fields(self):
        d = _FakeDecision(
            "ENTER_BOUNCE_LONG", timestamp=99, reference_price=123.5,
            invalidation_price=120.0, confidence=0.7, reason="reason",
            wall_id=11)
        i = ripple_decision_to_intent(d, state_name="STATE_Y")
        self.assertEqual(i.timestamp, 99)
        self.assertAlmostEqual(i.reference_price, 123.5)
        self.assertAlmostEqual(i.invalidation_price, 120.0)
        self.assertAlmostEqual(i.confidence, 0.7)
        self.assertEqual(i.reason, "reason")
        self.assertEqual(i.wall_id, 11)
        self.assertEqual(i.state_summary, "STATE_Y")


# ----------------------------------------------------------------------

class TestDataclassDefaults(unittest.TestCase):
    """The downstream code depends on these defaults; pin them so any
    accidental reorder/rename is caught."""

    def test_signal_entry_defaults(self):
        e = SignalEntry()
        self.assertEqual(e.timestamp, 0)
        self.assertEqual(e.signal_type, "")
        self.assertEqual(e.source, "legacy")
        self.assertEqual(e.category, SignalCategory.LEGACY_RAW)
        self.assertEqual(e.suppressed, SuppressionReason.NONE)

    def test_execution_intent_defaults(self):
        i = ExecutionIntent()
        self.assertEqual(i.timestamp, 0)
        self.assertEqual(i.source, "Ripple")
        self.assertEqual(i.action, "")
        self.assertIsNone(i.side)
        self.assertEqual(i.intent_type, "")
        self.assertEqual(i.reference_price, 0.0)
        self.assertEqual(i.wall_id, 0)

    def test_sizing_config_defaults_match_production_usage(self):
        c = SizingConfig()
        self.assertEqual(c.mode, SizingMode.FIXED_QTY)
        self.assertAlmostEqual(c.value, 0.001)
        self.assertAlmostEqual(c.max_position, 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
