"""Tests for Phase 3: Inactive UI Cleanup.

Validates:
- D1: Tick Size and Imbalance inputs hidden
- D1: Sizing controls disabled when strategy is disarmed
- D2: Replay option greyed out in mode combo
- D3: Account panel shows placeholder when not connected
- D3: Account panel shows content when connected
- Backward compatibility: hidden elements still exist in code
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
# A. Tick Size and Imbalance hidden (D1)
# ====================================================================

def test_tick_size_hidden():
    print("test_tick_size_hidden")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(not w._tick_size_input.isVisible(),
          "Tick Size input is hidden")
    check(not w._tick_size_label.isVisible(),
          "Tick Size label is hidden")
    check(w._tick_size_input.text() == "0.01",
          f"Tick Size still has default value (got {w._tick_size_input.text()})")


def test_imbalance_hidden():
    print("test_imbalance_hidden")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(not w._imbalance_input.isVisible(),
          "Imbalance input is hidden")
    check(not w._imbalance_label.isVisible(),
          "Imbalance label is hidden")
    check(w._imbalance_input.text() == "3.0",
          f"Imbalance still has default value (got {w._imbalance_input.text()})")


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
# C. Replay option greyed out (D2)
# ====================================================================

def test_replay_greyed_out():
    print("test_replay_greyed_out")
    from ui.main_window import MainWindow
    w = MainWindow()
    model = w._mode_combo.model()
    live_item = model.item(0)
    replay_item = model.item(1)

    check(live_item.isEnabled(), "Live option is enabled")
    check(not replay_item.isEnabled(), "Replay option is disabled")
    check(w._mode_combo.currentText() == "Live",
          f"default selection is Live (got {w._mode_combo.currentText()})")


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
# E. Backward compatibility
# ====================================================================

def test_hidden_elements_still_exist():
    print("test_hidden_elements_still_exist")
    from ui.main_window import MainWindow
    w = MainWindow()
    check(hasattr(w, '_tick_size_input'), "tick_size_input still in code")
    check(hasattr(w, '_imbalance_input'), "imbalance_input still in code")
    check(hasattr(w, '_tick_size_label'), "tick_size_label still in code")
    check(hasattr(w, '_imbalance_label'), "imbalance_label still in code")


def test_tick_size_still_readable_for_connect():
    print("test_tick_size_still_readable_for_connect")
    from ui.main_window import MainWindow
    w = MainWindow()
    val = w._tick_size_input.text()
    check(float(val) == 0.01, f"tick_size readable as float (got {val})")
    imb = w._imbalance_input.text()
    check(float(imb) == 3.0, f"imbalance readable as float (got {imb})")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_tick_size_hidden,
        test_imbalance_hidden,
        test_sizing_disabled_when_disarmed,
        test_sizing_enabled_when_armed,
        test_replay_greyed_out,
        test_account_panel_placeholder_on_init,
        test_account_panel_shows_content_on_update,
        test_account_panel_set_connected,
        test_account_panel_clear_disconnects,
        test_hidden_elements_still_exist,
        test_tick_size_still_readable_for_connect,
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
