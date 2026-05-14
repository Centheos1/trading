"""Tests for Phase 3 / Phase 9D: Inactive UI Cleanup.

Validates:
- D1 (Phase 9D): Tick Size and Imbalance inputs FULLY REMOVED from
  ``MainWindow`` (they were previously hidden; Phase 9D drops them).
  Defaults are hardcoded in ``_on_connect``.
- D1: Sizing controls disabled when strategy is disarmed.
- D2 (Phase 9D): Replay item REMOVED from mode combo (it was
  previously disabled).  ``_mode_combo`` now exposes only ``"Live"``.
- D3: Account panel shows placeholder when not connected.
- D3: Account panel shows content when connected.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import StrategyUIState
from ui.account_panel import AccountPanel

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
# A. Tick Size and Imbalance fully removed (Phase 9D, supersedes D1)
# ====================================================================

def test_tick_size_removed():
    """Phase 9D: ``_tick_size_input`` / ``_tick_size_label`` removed entirely."""
    print("test_tick_size_removed")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(not hasattr(w, "_tick_size_input"),
          "Tick Size input attribute removed")
    check(not hasattr(w, "_tick_size_label"),
          "Tick Size label attribute removed")


def test_imbalance_removed():
    """Phase 9D: ``_imbalance_input`` / ``_imbalance_label`` removed entirely."""
    print("test_imbalance_removed")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(not hasattr(w, "_imbalance_input"),
          "Imbalance input attribute removed")
    check(not hasattr(w, "_imbalance_label"),
          "Imbalance label attribute removed")


# ====================================================================
# B. Sizing controls disabled when disarmed (D1)
# ====================================================================

def test_sizing_disabled_when_disarmed():
    print("test_sizing_disabled_when_disarmed")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(w._strategy_ui_state == StrategyUIState.DISARMED,
          "initial state is DISARMED")
    check(not w._sizing_mode_combo.isEnabled(),
          "sizing mode combo disabled when disarmed")
    check(not w._sizing_value_input.isEnabled(),
          "sizing value input disabled when disarmed")


def test_sizing_enabled_when_armed():
    print("test_sizing_enabled_when_armed")
    from ui.main_window import MainWindow
    w = MainWindow()
    w._set_strategy_state(StrategyUIState.ARMED_WAITING)
    check(w._sizing_mode_combo.isEnabled(),
          "sizing mode combo enabled when armed")
    check(w._sizing_value_input.isEnabled(),
          "sizing value input enabled when armed")

    w._set_strategy_state(StrategyUIState.DISARMED)
    check(not w._sizing_mode_combo.isEnabled(),
          "sizing disabled again after disarm")
    check(not w._sizing_value_input.isEnabled(),
          "sizing value disabled again after disarm")


# ====================================================================
# C. Replay item removed from mode combo (Phase 9D, supersedes D2)
# ====================================================================

def test_replay_item_removed():
    """Phase 9D: ``_mode_combo`` exposes only "Live" — the disabled
    "Replay" entry was removed entirely.  Replay scenarios live in
    the CLI ``backtest`` mode now."""
    print("test_replay_item_removed")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(w._mode_combo.count() == 1,
          f"mode combo has exactly 1 item (got {w._mode_combo.count()})")
    check(w._mode_combo.itemText(0) == "Live",
          f"only item is Live (got '{w._mode_combo.itemText(0)}')")
    check(w._mode_combo.currentText() == "Live",
          f"default selection is Live (got '{w._mode_combo.currentText()}')")


# ====================================================================
# D. Account panel placeholder (D3)
# ====================================================================

def test_account_panel_placeholder_on_init():
    print("test_account_panel_placeholder_on_init")
    p = AccountPanel()
    check(not p._connected, "initial state is not connected")
    check(not p._placeholder.isHidden(), "placeholder visible on init")
    check(p._content.isHidden(), "content hidden on init")
    check("Not connected" in p._placeholder.text(),
          f"placeholder text correct (got '{p._placeholder.text()}')")


def test_account_panel_shows_content_on_update():
    print("test_account_panel_shows_content_on_update")
    p = AccountPanel()
    p.update_account(1000.0, 900.0, [])
    check(p._connected, "connected after update_account")
    check(p._placeholder.isHidden(), "placeholder hidden after connect")
    check(not p._content.isHidden(), "content visible after connect")


def test_account_panel_set_connected():
    print("test_account_panel_set_connected")
    p = AccountPanel()
    p.set_connected(True)
    check(p._connected, "connected is True")
    check(p._placeholder.isHidden(), "placeholder hidden")
    check(not p._content.isHidden(), "content visible")

    p.set_connected(False)
    check(not p._connected, "connected is False")
    check(not p._placeholder.isHidden(), "placeholder visible again")
    check(p._content.isHidden(), "content hidden again")


def test_account_panel_clear_disconnects():
    print("test_account_panel_clear_disconnects")
    p = AccountPanel()
    p.set_connected(True)
    check(p._connected, "connected before clear")
    p.clear()
    check(not p._connected, "disconnected after clear")
    check(not p._placeholder.isHidden(), "placeholder visible after clear")


# ====================================================================
# E. Phase 9D — candle combo removed; QML toolbar drives bucket duration
# ====================================================================

def test_candle_combo_removed():
    """Phase 9D: ``_candle_combo`` removed from MainWindow toolbar.

    The QML ``CandleChartView.qml`` toolbar owns the timeframe ComboBox
    now; it drives ``set_candle_duration_ms`` via the bridge.
    """
    print("test_candle_combo_removed")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(not hasattr(w, "_candle_combo"),
          "_candle_combo attribute removed")
    check(hasattr(w, "set_candle_duration_ms"),
          "set_candle_duration_ms mutator exists")


def test_set_candle_duration_updates_state():
    """Phase 9D: ``set_candle_duration_ms`` mirrors the legacy
    ``_on_candle_changed`` behaviour for QML-driven changes."""
    print("test_set_candle_duration_updates_state")
    from ui.main_window import MainWindow
    w = MainWindow()
    w.set_candle_duration_ms(300_000)
    check(w._candle_duration_ms == 300_000,
          f"_candle_duration_ms updated (got {w._candle_duration_ms})")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_tick_size_removed,
        test_imbalance_removed,
        test_sizing_disabled_when_disarmed,
        test_sizing_enabled_when_armed,
        test_replay_item_removed,
        test_account_panel_placeholder_on_init,
        test_account_panel_shows_content_on_update,
        test_account_panel_set_connected,
        test_account_panel_clear_disconnects,
        test_candle_combo_removed,
        test_set_candle_duration_updates_state,
    ]
    for t in tests:
        t()
    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"UI cleanup tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
