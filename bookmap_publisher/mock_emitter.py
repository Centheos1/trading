"""Standalone mock event emitter for the Bookmap publisher.

Runs without the C++ engine, broker, or Redis. Useful for:

  * developing the Java add-on against a known event stream
  * smoke-testing the WebSocket transport in CI / Docker
  * demoing the visualisation pipeline end-to-end

Usage::

    python -m bookmap_publisher.mock_emitter
    python -m bookmap_publisher.mock_emitter --symbol ETHUSDT --interval 0.5

The mock emits each of the five MVP event types (ENTRY, EXIT, METRIC,
POSITION, HEALTH) in a deterministic loop. The WebSocket schema is
identical to the live publisher path, so the Java add-on cannot tell
mock from real apart from the data content.
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from typing import Optional

from bookmap_publisher import event_builder
from bookmap_publisher.publisher import BookmapPublisher
from bookmap_publisher.ws_server import DEFAULT_HOST, DEFAULT_PORT

logger = logging.getLogger("bookmap_publisher.mock_emitter")


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit mock Bookmap events on ws://localhost:8765",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol tag for events")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between event bursts (default 1.0)",
    )
    parser.add_argument(
        "--base-price",
        type=float,
        default=67250.0,
        help="Anchor price for ENTRY/EXIT events",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for reproducible demos",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


class _MockState:
    """Local state machine driving the mock event sequence."""

    REGIMES = ("Absorption", "Breakout", "Drift", "Compression")
    HEALTH_VALUES = ("CONNECTED", "DEGRADED", "CONNECTED")

    def __init__(self, base_price: float) -> None:
        self.base_price = base_price
        self.position_side = ""
        self.position_qty = 0.0
        self.entry_price = 0.0
        self.session_pnl = 0.0
        self.trade_count = 0
        self.tick = 0

    def step_price(self) -> float:
        drift = random.gauss(0.0, 5.0)
        self.base_price = max(1.0, self.base_price + drift)
        return self.base_price


def _build_entry(state: _MockState, symbol: str) -> dict:
    side = random.choice(("BUY", "SELL"))
    qty = round(random.uniform(0.05, 0.5), 4)
    price = round(state.step_price(), 2)
    state.position_side = side
    state.position_qty = qty
    state.entry_price = price
    event = event_builder._empty_event("ENTRY", symbol)
    event["price"] = price
    event["side"] = side
    event["qty"] = qty
    event["label"] = f"{'Long' if side == 'BUY' else 'Short'} entry (mock)"
    return event


def _build_exit(state: _MockState, symbol: str) -> dict:
    side = state.position_side or random.choice(("BUY", "SELL"))
    qty = state.position_qty or round(random.uniform(0.05, 0.5), 4)
    price = round(state.step_price(), 2)
    sign = 1.0 if side == "BUY" else -1.0
    pnl = (price - state.entry_price) * qty * sign if state.entry_price else 0.0
    state.session_pnl += pnl
    state.trade_count += 1
    state.position_side = ""
    state.position_qty = 0.0
    state.entry_price = 0.0
    event = event_builder._empty_event("EXIT", symbol)
    event["price"] = price
    event["side"] = side
    event["qty"] = qty
    event["label"] = f"Exit PnL={pnl:+.2f} (mock)"
    return event


def _build_metric(state: _MockState, symbol: str) -> dict:
    return event_builder.custom_metric(
        symbol,
        name="PnL",
        value=f"{state.session_pnl:+.2f}",
        label=f"trades={state.trade_count}",
    )


def _build_regime(state: _MockState, symbol: str) -> dict:
    return event_builder.custom_metric(
        symbol,
        name="Regime",
        value=random.choice(state.REGIMES),
    )


def _build_position(state: _MockState, symbol: str) -> dict:
    event = event_builder._empty_event("POSITION", symbol)
    if state.position_qty <= 0 or not state.position_side:
        event["label"] = "Flat"
        return event
    event["side"] = state.position_side
    event["qty"] = state.position_qty
    event["price"] = round(state.entry_price, 2)
    event["label"] = f"{state.position_side} {state.position_qty:.4f}"
    return event


def _build_health(state: _MockState, symbol: str) -> dict:
    value = state.HEALTH_VALUES[state.tick % len(state.HEALTH_VALUES)]
    event = event_builder._empty_event("HEALTH", symbol)
    event["name"] = "Connection"
    event["value"] = value
    event["label"] = value
    return event


def _emit_sequence(publisher: BookmapPublisher, state: _MockState, symbol: str) -> None:
    """Emit one tick of the mock sequence.

    Each tick fires HEALTH + METRIC + POSITION + a rotating Regime
    metric. Every fourth tick fires an ENTRY; every eighth fires an
    EXIT.
    """
    publisher.publish_raw(_build_health(state, symbol))
    publisher.publish_raw(_build_regime(state, symbol))
    publisher.publish_raw(_build_metric(state, symbol))
    publisher.publish_raw(_build_position(state, symbol))

    if state.tick > 0 and state.tick % 4 == 0 and not state.position_side:
        publisher.publish_raw(_build_entry(state, symbol))
    if state.tick > 0 and state.tick % 8 == 0 and state.position_side:
        publisher.publish_raw(_build_exit(state, symbol))


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.seed is not None:
        random.seed(args.seed)

    publisher = BookmapPublisher(
        host=args.host,
        port=args.port,
        symbol_default=args.symbol,
    )
    if not publisher.start():
        logger.error("Mock emitter could not start the WS server")
        return 1

    symbol = args.symbol.upper()
    state = _MockState(base_price=args.base_price)
    stop_flag = {"stop": False}

    def _handle_signal(_signum, _frame) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Bookmap mock emitter publishing to ws://%s:%d (symbol=%s, interval=%.2fs)",
        args.host,
        args.port,
        symbol,
        args.interval,
    )
    try:
        while not stop_flag["stop"]:
            _emit_sequence(publisher, state, symbol)
            state.tick += 1
            time.sleep(args.interval)
    finally:
        publisher.stop()
    logger.info("Bookmap mock emitter stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
