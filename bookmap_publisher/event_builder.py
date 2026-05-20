"""Pure functions that map engine objects to Bookmap event dicts.

These builders own the JSON contract between Python and the Java
add-on. They MUST match the schema documented in
``docs/BOOKMAP_INTEGRATION.md`` and consumed by
``bookmap-addon/.../model/StrategyEvent.java``.

The MVP intentionally fills several fields with placeholder values
marked ``# TODO: full mapping``. Each TODO is the explicit unit of
work for a follow-up phase — replacing the placeholder with a real
projection of strategy / engine state. Until then the Java add-on
displays the placeholder, which is enough to validate the IPC path
end-to-end.

Canonical schema (all events)::

    {
        "type":       "ENTRY" | "EXIT" | "METRIC" | "POSITION" | "HEALTH",
        "symbol":     str,
        "timestamp":  int (epoch ms),
        "price":      float,
        "side":       "BUY" | "SELL" | "",
        "qty":        float,
        "label":      str,    # human-readable summary
        "name":       str,    # metric/regime field name (METRIC only)
        "value":      str     # metric/regime field value (METRIC only)
    }
"""

from __future__ import annotations

import time
from typing import Any, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


def _side_str(side: Any) -> str:
    """Render an ``OrderSide`` (or string) as 'BUY' / 'SELL' / ''."""
    if side is None:
        return ""
    value = getattr(side, "value", None)
    if isinstance(value, str):
        return value
    if isinstance(side, str):
        return side
    name = getattr(side, "name", None)
    return name if isinstance(name, str) else ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _empty_event(event_type: str, symbol: str) -> dict:
    """Construct a fully-populated event dict with default placeholders.

    All schema fields are present in every event so the Java POJO never
    sees a missing key. Per-builder updates overwrite only the fields
    that have a meaningful projection.
    """
    return {
        "type": event_type,
        "symbol": symbol,
        "timestamp": _now_ms(),
        "price": 0.0,
        "side": "",
        "qty": 0.0,
        "label": "",
        "name": "",
        "value": "",
    }


# --------------------------------------------------------------------- ENTRY


def entry_from_intent(intent: Any, symbol: str) -> dict:
    """Build an ENTRY event from an ``ExecutionIntent``.

    Called immediately after ``exec_mgr.on_intent(intent)`` succeeds for
    an entry-type intent. ``intent.timestamp`` is event-time milliseconds
    from the Ripple decision; ``intent.reference_price`` is the engine's
    chosen entry reference (limit price for bounce, microprice for
    breakout).
    """
    event = _empty_event("ENTRY", symbol)
    event["timestamp"] = int(getattr(intent, "timestamp", 0) or _now_ms())
    event["price"] = _safe_float(getattr(intent, "reference_price", 0.0))
    event["side"] = _side_str(getattr(intent, "side", None))
    # TODO: full mapping — populate ``qty`` from the sized order. The
    # ExecutionManager sizes the order after the intent is published, so
    # the MVP exposes ``reference_price`` only.
    event["qty"] = 0.0
    action = str(getattr(intent, "action", "") or "")
    reason = str(getattr(intent, "reason", "") or "")
    event["label"] = action or "ENTRY"
    if reason:
        event["label"] = f"{event['label']} ({reason})"
    return event


# ---------------------------------------------------------------------- EXIT


def exit_from_intent(
    intent: Any,
    exec_mgr: Any,
    symbol: str,
) -> dict:
    """Build an EXIT event from an ``ExecutionIntent``.

    Uses ``exec_mgr.current_qty`` (still set when the exit is dispatched)
    for the qty field. Realised PnL is exposed via ``label`` once the
    fill is confirmed by ``ExecutionManager`` — for the MVP we publish
    the intent-side view immediately and let a future iteration emit a
    second EXIT event keyed to the broker fill.
    """
    event = _empty_event("EXIT", symbol)
    event["timestamp"] = int(getattr(intent, "timestamp", 0) or _now_ms())
    event["price"] = _safe_float(getattr(intent, "reference_price", 0.0))
    current_side = getattr(exec_mgr, "current_side", None)
    event["side"] = _side_str(current_side)
    event["qty"] = _safe_float(getattr(exec_mgr, "current_qty", 0.0))
    action = str(getattr(intent, "action", "") or "")
    exit_type = getattr(intent, "exit_type", None)
    exit_type_name = getattr(exit_type, "name", "") if exit_type else ""
    parts = [p for p in (action or "EXIT", exit_type_name) if p]
    event["label"] = " / ".join(parts)
    return event


# -------------------------------------------------------------------- METRIC


def metric_from_exec_mgr(exec_mgr: Any, symbol: str) -> dict:
    """Build a METRIC event from current ``ExecutionManager`` state.

    The MVP emits PnL as the headline metric. Future iterations should
    rotate through metric names (Regime, Risk Budget %, Tide Bias, …)
    by emitting multiple METRIC events with different ``name`` values.
    """
    event = _empty_event("METRIC", symbol)
    pnl = _safe_float(getattr(exec_mgr, "session_realized_pnl", 0.0))
    trade_count = int(getattr(exec_mgr, "session_trade_count", 0) or 0)
    event["name"] = "PnL"
    event["value"] = f"{pnl:+.4f}"
    event["label"] = f"trades={trade_count}"
    # TODO: full mapping — emit additional METRIC events for Regime,
    # Risk Budget %, Tide Bias, and Wave state. These read off
    # ``ofe_engine.get_strategy_snapshot()`` which is not threaded into
    # the publisher in the MVP.
    return event


# ------------------------------------------------------------------ POSITION


def position_from_exec_mgr(exec_mgr: Any, symbol: str) -> dict:
    """Build a POSITION event from current ``ExecutionManager`` state."""
    event = _empty_event("POSITION", symbol)
    side = _side_str(getattr(exec_mgr, "current_side", None))
    qty = _safe_float(getattr(exec_mgr, "current_qty", 0.0))
    event["side"] = side
    event["qty"] = qty
    if qty <= 0 or not side:
        event["label"] = "Flat"
    else:
        event["label"] = f"{side} {qty:.6f}"
    return event


# -------------------------------------------------------------------- HEALTH


def health_event(symbol: str, connected: bool) -> dict:
    """Build a HEALTH event indicating publisher connectivity state."""
    event = _empty_event("HEALTH", symbol)
    state = "CONNECTED" if connected else "DISCONNECTED"
    event["name"] = "Connection"
    event["value"] = state
    event["label"] = state
    return event


def custom_metric(
    symbol: str,
    name: str,
    value: str,
    label: Optional[str] = None,
) -> dict:
    """Build a generic METRIC event for arbitrary ``(name, value)`` pairs.

    Used by ``mock_emitter`` and any future caller that wants to surface
    a metric the engine does not yet wire automatically (Regime, Tide
    Bias, …).
    """
    event = _empty_event("METRIC", symbol)
    event["name"] = name
    event["value"] = value
    event["label"] = label if label is not None else f"{name}={value}"
    return event
