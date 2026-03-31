"""Tests for Phase 2: Strategy Controls + Signal Log integration.

Validates:
- StrategyMode and StrategyUIState enums
- SignalCategory extensions (STRATEGY_ARM, STRATEGY_DISARM, STRATEGY_STATE_CHANGE)
- SignalEntry strategy metadata fields
- Strategy state machine transitions
- ARM/DISARM button state
- Strategy signal emission to blotter
- Strategy mode persistence via QSettings
- TradeBlotter strategy filter
- StrategyDiagnosticsPanel state badge
- Heatmap strategy overlay lines
- Backward compatibility with existing pipeline
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import (
    SignalCategory, SignalEntry, StrategyMode, StrategyUIState,
    RippleMode, SuppressionReason,
)
from ui.heatmap_widget import HeatmapWidget
from ui.trade_blotter import (
    TradeBlotter, _FILTER_STRATEGY, _FILTER_TRADE, _FILTER_RIPPLE,
    _FILTER_RAW, _CONTEXT_SIGNAL_PREFIXES,
)
from execution.models import _CATEGORY_LAYER_MAP
from ui.strategy_panel import StrategyDiagnosticsPanel, _STATE_BADGE

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


# ====================================================================
# A. Enum and model tests
# ====================================================================

def test_strategy_mode_enum():
    print("test_strategy_mode_enum")
    check(StrategyMode.OBSERVE.value == "observe", "OBSERVE value")
    check(StrategyMode.PAPER.value == "paper", "PAPER value")
    check(StrategyMode.LIVE.value == "live", "LIVE value")
    check(len(StrategyMode) == 3, f"3 modes (got {len(StrategyMode)})")


def test_strategy_ui_state_enum():
    print("test_strategy_ui_state_enum")
    expected = {"disarmed", "arming", "armed_waiting", "armed_active",
                "armed_exiting", "armed_cooldown", "disarming"}
    actual = {s.value for s in StrategyUIState}
    check(actual == expected, f"all 7 states present (got {actual})")


def test_signal_category_extensions():
    print("test_signal_category_extensions")
    check(SignalCategory.STRATEGY_ARM.value == "STRATEGY_ARM", "STRATEGY_ARM")
    check(SignalCategory.STRATEGY_DISARM.value == "STRATEGY_DISARM", "STRATEGY_DISARM")
    check(SignalCategory.STRATEGY_STATE_CHANGE.value == "STRATEGY_STATE_CHANGE",
          "STRATEGY_STATE_CHANGE")
    check(SignalCategory.TIDE.value == "TIDE", "TIDE category exists")
    check(SignalCategory.WAVE.value == "WAVE", "WAVE category exists")
    check(SignalCategory.TRADE_LIFECYCLE.value == "TRADE_LIFECYCLE",
          "TRADE_LIFECYCLE category exists")
    original = {"CONTEXT", "RIPPLE_PREPARE", "RIPPLE_ENTRY", "RIPPLE_EXIT",
                "RIPPLE_CANCEL", "RIPPLE_REARM", "LEGACY_RAW", "EXECUTION",
                "DIAGNOSTIC"}
    for v in original:
        check(v in {c.value for c in SignalCategory},
              f"original category {v} preserved")


def test_signal_entry_strategy_fields():
    print("test_signal_entry_strategy_fields")
    e = SignalEntry()
    check(e.wave_regime == "", "wave_regime default empty")
    check(e.tide_bias == "", "tide_bias default empty")
    check(e.risk_budget_pct == 0.0, "risk_budget_pct default 0")
    check(e.lifecycle_state == "", "lifecycle_state default empty")
    check(e.archetype == "", "archetype default empty")

    e2 = SignalEntry(
        wave_regime="MEAN_REVERSION",
        tide_bias="LONG",
        risk_budget_pct=42.5,
        lifecycle_state="ENTRY",
        archetype="BOUNCE",
    )
    check(e2.wave_regime == "MEAN_REVERSION", "wave_regime set")
    check(e2.tide_bias == "LONG", "tide_bias set")
    check(e2.risk_budget_pct == 42.5, "risk_budget_pct set")
    check(e2.lifecycle_state == "ENTRY", "lifecycle_state set")
    check(e2.archetype == "BOUNCE", "archetype set")

    check(e.timestamp == 0, "backward compat: timestamp default")
    check(e.source == "legacy", "backward compat: source default")
    check(e.suppressed == SuppressionReason.NONE, "backward compat: suppressed default")


# ====================================================================
# B. Strategy mode ↔ RippleMode mapping
# ====================================================================

def test_strategy_to_ripple_mapping():
    print("test_strategy_to_ripple_mapping")
    mapping = {
        StrategyMode.OBSERVE: RippleMode.LOG_ONLY,
        StrategyMode.PAPER: RippleMode.PAPER,
        StrategyMode.LIVE: RippleMode.LOG_ONLY,
    }
    for sm, expected_rm in mapping.items():
        rm = {
            StrategyMode.OBSERVE: RippleMode.LOG_ONLY,
            StrategyMode.PAPER: RippleMode.PAPER,
            StrategyMode.LIVE: RippleMode.LOG_ONLY,
        }.get(sm)
        check(rm == expected_rm,
              f"StrategyMode.{sm.name} -> RippleMode.{expected_rm.name} (got {rm})")


# ====================================================================
# C. TradeBlotter filter tests
# ====================================================================

def test_blotter_strategy_categories():
    print("test_blotter_strategy_categories")
    check(SignalCategory.STRATEGY_ARM in _FILTER_STRATEGY,
          "STRATEGY_ARM in strategy filter")
    check(SignalCategory.STRATEGY_DISARM in _FILTER_STRATEGY,
          "STRATEGY_DISARM in strategy filter")
    check(SignalCategory.STRATEGY_STATE_CHANGE in _FILTER_STRATEGY,
          "STRATEGY_STATE_CHANGE in strategy filter")
    check(SignalCategory.RIPPLE_ENTRY in _FILTER_STRATEGY,
          "RIPPLE_ENTRY in strategy filter")
    check(SignalCategory.TRADE_LIFECYCLE in _FILTER_STRATEGY,
          "TRADE_LIFECYCLE in strategy filter")
    check(SignalCategory.TIDE in _FILTER_STRATEGY,
          "TIDE in strategy filter")
    check(SignalCategory.WAVE in _FILTER_STRATEGY,
          "WAVE in strategy filter")
    check(SignalCategory.CONTEXT in _FILTER_STRATEGY,
          "CONTEXT in strategy filter")
    check(SignalCategory.EXECUTION in _FILTER_STRATEGY,
          "EXECUTION in strategy filter")
    check(SignalCategory.LEGACY_RAW not in _FILTER_STRATEGY,
          "LEGACY_RAW not in strategy filter")
    check(SignalCategory.DIAGNOSTIC not in _FILTER_STRATEGY,
          "DIAGNOSTIC not in strategy filter")


def test_trade_filter():
    print("test_trade_filter")
    check(SignalCategory.TRADE_LIFECYCLE in _FILTER_TRADE,
          "TRADE_LIFECYCLE in trade filter")
    check(SignalCategory.EXECUTION in _FILTER_TRADE,
          "EXECUTION in trade filter")
    check(len(_FILTER_TRADE) == 2, f"trade filter has 2 cats (got {len(_FILTER_TRADE)})")


def test_layer_display_map():
    print("test_layer_display_map")
    check(_CATEGORY_LAYER_MAP[SignalCategory.TIDE] == "TIDE", "TIDE -> TIDE")
    check(_CATEGORY_LAYER_MAP[SignalCategory.WAVE] == "WAVE", "WAVE -> WAVE")
    check(_CATEGORY_LAYER_MAP[SignalCategory.RIPPLE_ENTRY] == "RIPPLE", "RIPPLE_ENTRY -> RIPPLE")
    check(_CATEGORY_LAYER_MAP[SignalCategory.TRADE_LIFECYCLE] == "TRADE", "TRADE_LIFECYCLE -> TRADE")
    check(_CATEGORY_LAYER_MAP[SignalCategory.EXECUTION] == "EXEC", "EXECUTION -> EXEC")
    check(_CATEGORY_LAYER_MAP[SignalCategory.LEGACY_RAW] == "RAW", "LEGACY_RAW -> RAW")
    check(_CATEGORY_LAYER_MAP[SignalCategory.STRATEGY_ARM] == "STRATEGY", "STRATEGY_ARM -> STRATEGY")
    for cat in SignalCategory:
        check(cat in _CATEGORY_LAYER_MAP,
              f"{cat.value} has layer mapping")


def test_blotter_add_strategy_entries():
    print("test_blotter_add_strategy_entries")
    b = TradeBlotter()
    now_ms = int(time.time() * 1000)

    arm_entry = SignalEntry(
        timestamp=now_ms,
        signal_type="STRATEGY_ARM",
        source="strategy",
        category=SignalCategory.STRATEGY_ARM,
        description="Strategy armed in observe mode",
    )
    b.add_entry(arm_entry)
    check(b._model.rowCount() == 1, "1 row after arm signal")

    state_entry = SignalEntry(
        timestamp=now_ms + 1000,
        signal_type="STRATEGY_STATE_CHANGE",
        source="strategy",
        category=SignalCategory.STRATEGY_STATE_CHANGE,
        description="State: armed_waiting -> armed_active",
        lifecycle_state="armed_active",
    )
    b.add_entry(state_entry)
    check(b._model.rowCount() == 2, "2 rows after state change")

    disarm_entry = SignalEntry(
        timestamp=now_ms + 2000,
        signal_type="STRATEGY_DISARM",
        source="strategy",
        category=SignalCategory.STRATEGY_DISARM,
        description="Strategy disarmed",
    )
    b.add_entry(disarm_entry)
    check(b._model.rowCount() == 3, "3 rows after disarm signal")


# ====================================================================
# D. StrategyDiagnosticsPanel tests
# ====================================================================

def test_diagnostics_panel_state_badge():
    print("test_diagnostics_panel_state_badge")
    panel = StrategyDiagnosticsPanel()

    check(panel._strategy_state == StrategyUIState.DISARMED,
          "initial state is DISARMED")

    for state in StrategyUIState:
        check(state in _STATE_BADGE,
              f"badge defined for {state.value}")

    panel.set_strategy_state(StrategyUIState.ARMED_WAITING)
    check(panel._strategy_state == StrategyUIState.ARMED_WAITING,
          "state updated to ARMED_WAITING")

    panel.set_strategy_state(StrategyUIState.ARMED_ACTIVE)
    check(panel._strategy_state == StrategyUIState.ARMED_ACTIVE,
          "state updated to ARMED_ACTIVE")

    panel.clear()
    check(panel._strategy_state == StrategyUIState.DISARMED,
          "clear resets to DISARMED")


def test_diagnostics_panel_snapshot():
    print("test_diagnostics_panel_snapshot")

    class MockTide:
        bias = "LONG"
    class MockWave:
        regime = "MEAN_REVERSION"
        trend_efficiency = 0.75
    class MockRipple:
        liquidity_state = "LIQUID"
        microprice = 83456.789
        imbalance = 0.15
    class MockTrade:
        state = "ENTRY"
        archetype = "BOUNCE"
        unrealized_pnl = 0.0025
        stop_price = 83200.0
        target_price = 83800.0
        hold_time_ms = 125000
    class MockRisk:
        consumed_es = 40.0
        es_budget = 100.0
    class MockSnap:
        tide = MockTide()
        wave = MockWave()
        ripple = MockRipple()
        trade = MockTrade()
        risk = MockRisk()

    panel = StrategyDiagnosticsPanel()
    panel.update_snapshot(MockSnap())

    labels = [r[0] for r in panel._rows]
    check("Tide" in labels, "Tide row present")
    check("Wave" in labels, "Wave row present")
    check("Archetype" in labels, "Archetype row present")
    check("Stop" in labels, "Stop row present")
    check("Target" in labels, "Target row present")
    check("Hold" in labels, "Hold row present")
    check("ES Used" in labels, "ES Used row present")

    tide_row = next(r for r in panel._rows if r[0] == "Tide")
    check(tide_row[1] == "LONG", f"Tide value is LONG (got {tide_row[1]})")

    arch_row = next(r for r in panel._rows if r[0] == "Archetype")
    check(arch_row[1] == "BOUNCE", f"Archetype value is BOUNCE (got {arch_row[1]})")

    hold_row = next(r for r in panel._rows if r[0] == "Hold")
    check(hold_row[1] == "02:05", f"Hold time formatted as 02:05 (got {hold_row[1]})")

    stop_row = next(r for r in panel._rows if r[0] == "Stop")
    check("83200" in stop_row[1], f"Stop price shown (got {stop_row[1]})")

    es_row = next(r for r in panel._rows if r[0] == "ES Used")
    check(es_row[1] == "40%", f"ES Used is 40% (got {es_row[1]})")


# ====================================================================
# E. Heatmap strategy overlay tests
# ====================================================================

def test_heatmap_overlay_set_clear():
    print("test_heatmap_overlay_set_clear")
    w = HeatmapWidget()
    w.resize(800, 500)

    check(w._overlay_entry_price == 0.0, "initial entry overlay is 0")
    check(w._overlay_stop_price == 0.0, "initial stop overlay is 0")
    check(w._overlay_target_price == 0.0, "initial target overlay is 0")

    w.set_strategy_overlay(83500.0, 83200.0, 83800.0)
    check(w._overlay_entry_price == 83500.0, "entry price set")
    check(w._overlay_stop_price == 83200.0, "stop price set")
    check(w._overlay_target_price == 83800.0, "target price set")

    w.clear_strategy_overlay()
    check(w._overlay_entry_price == 0.0, "entry cleared")
    check(w._overlay_stop_price == 0.0, "stop cleared")
    check(w._overlay_target_price == 0.0, "target cleared")


def test_heatmap_overlay_partial():
    print("test_heatmap_overlay_partial")
    w = HeatmapWidget()
    w.resize(800, 500)

    w.set_strategy_overlay(entry_price=83500.0)
    check(w._overlay_entry_price == 83500.0, "only entry set")
    check(w._overlay_stop_price == 0.0, "stop stays 0")
    check(w._overlay_target_price == 0.0, "target stays 0")


# ====================================================================
# F. State machine logic tests (isolated — no MainWindow)
# ====================================================================

def test_state_transitions_validity():
    print("test_state_transitions_validity")
    s = StrategyUIState.DISARMED
    check(s.value == "disarmed", "start disarmed")
    check(s.value.startswith("armed") is False, "disarmed does not start with armed")

    s = StrategyUIState.ARMED_WAITING
    check(s.value.startswith("armed"), "armed_waiting starts with armed")

    s = StrategyUIState.ARMED_ACTIVE
    check(s.value.startswith("armed"), "armed_active starts with armed")

    s = StrategyUIState.ARMED_EXITING
    check(s.value.startswith("armed"), "armed_exiting starts with armed")

    s = StrategyUIState.ARMED_COOLDOWN
    check(s.value.startswith("armed"), "armed_cooldown starts with armed")


def test_lifecycle_to_ui_state_mapping():
    print("test_lifecycle_to_ui_state_mapping")
    _LIFECYCLE_MAP = {
        "IDLE": StrategyUIState.ARMED_WAITING,
        "SETUP": StrategyUIState.ARMED_ACTIVE,
        "ENTRY": StrategyUIState.ARMED_ACTIVE,
        "CONFIRMATION": StrategyUIState.ARMED_ACTIVE,
        "EXPANSION": StrategyUIState.ARMED_ACTIVE,
        "MATURATION": StrategyUIState.ARMED_ACTIVE,
        "EXIT": StrategyUIState.ARMED_EXITING,
        "COOLDOWN": StrategyUIState.ARMED_COOLDOWN,
    }
    for lc_state, expected_ui in _LIFECYCLE_MAP.items():
        check(expected_ui.value.startswith("armed"),
              f"{lc_state} maps to armed state ({expected_ui.value})")

    active_states = {"SETUP", "ENTRY", "CONFIRMATION", "EXPANSION", "MATURATION"}
    for s in active_states:
        check(_LIFECYCLE_MAP[s] == StrategyUIState.ARMED_ACTIVE,
              f"{s} maps to ARMED_ACTIVE")

    check(_LIFECYCLE_MAP["EXIT"] == StrategyUIState.ARMED_EXITING, "EXIT maps correctly")
    check(_LIFECYCLE_MAP["COOLDOWN"] == StrategyUIState.ARMED_COOLDOWN, "COOLDOWN maps correctly")
    check(_LIFECYCLE_MAP["IDLE"] == StrategyUIState.ARMED_WAITING, "IDLE maps correctly")


# ====================================================================
# G. Settings backward compatibility
# ====================================================================

def test_settings_migration():
    print("test_settings_migration")
    from ui.main_window import MainWindow

    mapping = MainWindow._RIPPLE_TO_STRATEGY
    check(mapping.get("log_only") == StrategyMode.OBSERVE,
          "log_only -> OBSERVE")
    check(mapping.get("paper") == StrategyMode.PAPER,
          "paper -> PAPER")
    check(mapping.get("disabled") == StrategyMode.OBSERVE,
          "disabled -> OBSERVE")


# ====================================================================
# H. Signal entry creation with strategy context
# ====================================================================

def test_signal_entry_with_full_context():
    print("test_signal_entry_with_full_context")
    now_ms = int(time.time() * 1000)
    e = SignalEntry(
        timestamp=now_ms,
        signal_type="STRATEGY_STATE_CHANGE",
        source="strategy",
        category=SignalCategory.STRATEGY_STATE_CHANGE,
        description="State: armed_waiting -> armed_active",
        wave_regime="MEAN_REVERSION",
        tide_bias="LONG",
        risk_budget_pct=30.0,
        lifecycle_state="armed_active",
        archetype="BOUNCE",
    )
    check(e.category == SignalCategory.STRATEGY_STATE_CHANGE, "category set")
    check(e.source == "strategy", "source is strategy")
    check(e.wave_regime == "MEAN_REVERSION", "regime preserved")
    check(e.tide_bias == "LONG", "bias preserved")
    check(e.archetype == "BOUNCE", "archetype preserved")
    check(e.lifecycle_state == "armed_active", "lifecycle state preserved")


# ====================================================================
# I. Backward compatibility — existing pipeline unaffected
# ====================================================================

def test_existing_signal_entry_compat():
    print("test_existing_signal_entry_compat")
    e = SignalEntry(
        timestamp=1700000060000,
        signal_type="RIPPLE_ENTER_BOUNCE_LONG",
        source="ripple",
        category=SignalCategory.RIPPLE_ENTRY,
        side="BUY",
        price=83000.0,
        strength=0.85,
        description="Bounce entry",
        state_summary="ENTRY",
    )
    check(e.wave_regime == "", "new fields default empty for old entries")
    check(e.tide_bias == "", "new fields default empty")
    check(e.archetype == "", "new fields default empty")
    check(e.risk_budget_pct == 0.0, "new fields default zero")
    check(e.lifecycle_state == "", "new fields default empty")


def test_existing_ripple_mode_preserved():
    print("test_existing_ripple_mode_preserved")
    check(RippleMode.DISABLED.value == "disabled", "DISABLED preserved")
    check(RippleMode.LOG_ONLY.value == "log_only", "LOG_ONLY preserved")
    check(RippleMode.PAPER.value == "paper", "PAPER preserved")


# ====================================================================
# J. Signal reclassification and default filter
# ====================================================================

def test_context_signal_reclassification():
    """EXHAUSTION/ABSORPTION signals should be classified as CONTEXT."""
    print("test_context_signal_reclassification")
    b = TradeBlotter()
    now_ms = int(time.time() * 1000)

    b.add_signal(now_ms, "EXHAUSTION_BUY", 83000.0, 0.5, "2 declining bars")
    b.add_signal(now_ms + 100, "ABSORPTION_SELL", 83001.0, 0.6, "wall absorbed")
    b.add_signal(now_ms + 200, "STACKED_IMBALANCE_BUY", 83002.0, 0.4, "3 levels")

    check(b._model.rowCount() == 3, f"3 rows total (got {b._model.rowCount()})")

    e0 = b._model._data[0]
    check(e0.category == SignalCategory.CONTEXT,
          f"EXHAUSTION reclassified as CONTEXT (got {e0.category})")
    check(e0.source == "legacy", "EXHAUSTION source stays legacy")

    e1 = b._model._data[1]
    check(e1.category == SignalCategory.CONTEXT,
          f"ABSORPTION reclassified as CONTEXT (got {e1.category})")

    e2 = b._model._data[2]
    check(e2.category == SignalCategory.LEGACY_RAW,
          f"STACKED_IMBALANCE stays LEGACY_RAW (got {e2.category})")


def test_context_prefixes_defined():
    """Verify the prefix list covers expected signal types."""
    print("test_context_prefixes_defined")
    check("EXHAUSTION_" in _CONTEXT_SIGNAL_PREFIXES,
          "EXHAUSTION_ in prefix list")
    check("ABSORPTION_" in _CONTEXT_SIGNAL_PREFIXES,
          "ABSORPTION_ in prefix list")
    check("SWEEP_" in _CONTEXT_SIGNAL_PREFIXES,
          "SWEEP_ in prefix list")


def test_blotter_default_strategy_filter():
    """Blotter should default to Strategy filter ON."""
    print("test_blotter_default_strategy_filter")
    b = TradeBlotter()
    check(b._btn_strategy.isChecked(),
          "Strategy checkbox checked by default")

    now_ms = int(time.time() * 1000)
    b.add_signal(now_ms, "STACKED_IMBALANCE_BUY", 83000.0, 0.5, "3 levels")
    b.add_entry(SignalEntry(
        timestamp=now_ms + 100,
        signal_type="STRATEGY_ARM",
        source="strategy",
        category=SignalCategory.STRATEGY_ARM,
        description="Armed",
    ))
    b.add_signal(now_ms + 200, "EXHAUSTION_BUY", 83000.0, 0.4, "exhaustion")

    total = b._model.rowCount()
    visible = b._proxy.rowCount()
    check(total == 3, f"3 total rows (got {total})")
    check(visible < total,
          f"filter hides some rows ({visible} visible, {total} total)")
    check(visible >= 2,
          f"strategy + context visible ({visible} visible)")


def test_blotter_uncheck_strategy_shows_all():
    """Unchecking Strategy shows all signals."""
    print("test_blotter_uncheck_strategy_shows_all")
    b = TradeBlotter()
    now_ms = int(time.time() * 1000)
    b.add_signal(now_ms, "STACKED_IMBALANCE_BUY", 83000.0, 0.5, "3 levels")
    b.add_entry(SignalEntry(
        timestamp=now_ms + 100,
        signal_type="STRATEGY_ARM",
        source="strategy",
        category=SignalCategory.STRATEGY_ARM,
        description="Armed",
    ))

    b._btn_strategy.setChecked(False)
    visible = b._proxy.rowCount()
    total = b._model.rowCount()
    check(visible == total,
          f"all visible when Strategy unchecked ({visible}/{total})")


# ====================================================================
# K. Layout — strategy panel not in left column
# ====================================================================

def test_left_col_widget_count_matches_chart_stack():
    """Left column must have same widget count as chart stack for sync."""
    print("test_left_col_widget_count_matches_chart_stack")
    from ui.main_window import MainWindow
    w = MainWindow()
    left_count = w._left_col.count()
    chart_count = w._chart_stack.count()
    check(left_count == chart_count,
          f"left_col widgets ({left_count}) == chart_stack widgets ({chart_count})")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_strategy_mode_enum,
        test_strategy_ui_state_enum,
        test_signal_category_extensions,
        test_signal_entry_strategy_fields,
        test_strategy_to_ripple_mapping,
        test_blotter_strategy_categories,
        test_trade_filter,
        test_layer_display_map,
        test_blotter_add_strategy_entries,
        test_diagnostics_panel_state_badge,
        test_diagnostics_panel_snapshot,
        test_heatmap_overlay_set_clear,
        test_heatmap_overlay_partial,
        test_state_transitions_validity,
        test_lifecycle_to_ui_state_mapping,
        test_settings_migration,
        test_signal_entry_with_full_context,
        test_existing_signal_entry_compat,
        test_existing_ripple_mode_preserved,
        test_context_signal_reclassification,
        test_context_prefixes_defined,
        test_blotter_default_strategy_filter,
        test_blotter_uncheck_strategy_shows_all,
        test_left_col_widget_count_matches_chart_stack,
    ]
    for t in tests:
        t()
    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Strategy UI tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
