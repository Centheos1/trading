"""Lightweight paper execution engine for Ripple intent-driven trading.

Routes ExecutionIntents into simulated fills without a broker connection.
Tracks a single position per symbol with entry price, PnL, and fill history.

Phase 15 additions:
- LIMIT order fill simulation via a price-keyed priority queue.
- Partial-fill simulation when simulated trade volume is insufficient.
- OCO sibling cancellation via shared ``limit_order_id`` cross-reference.
- Timeout cancellation of OPEN orders after ``limit_timeout_ms``.
"""
from __future__ import annotations

import heapq
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from execution.models import (
    ExecutionIntent, ExitType, Order, OrderLifecycle, OrderSide,
    OrderStatus, OrderType, SizingConfig, SizingMode, SuppressionReason,
)

logger = logging.getLogger(__name__)

# Default limit-order timeout (30 seconds in event-time ms).
DEFAULT_LIMIT_TIMEOUT_MS: int = 30_000


@dataclass
class PaperPosition:
    side: Optional[OrderSide] = None
    quantity: float = 0.0
    entry_price: float = 0.0


@dataclass
class PaperMetrics:
    intents_received: int = 0
    entries_filled: int = 0
    exits_filled: int = 0
    suppressed_inventory: int = 0
    suppressed_no_position: int = 0
    realized_pnl: float = 0.0
    limit_timeouts: int = 0
    limit_fallbacks: int = 0


# ---------------------------------------------------------------------------
# Internal LIMIT order queue entry
# ---------------------------------------------------------------------------

@dataclass
class _LimitEntry:
    """One open LIMIT order in the simulated book."""
    order: Order
    # For BUY LIMIT: fill when trade_price <= limit_price
    # For SELL LIMIT: fill when trade_price >= limit_price
    limit_price: float
    placed_ms: int          # event-time when placed
    is_buy: bool

    # heapq uses tuple comparison; we sort by (limit_price, placed_ms)
    def __lt__(self, other: "_LimitEntry") -> bool:
        return (self.limit_price, self.placed_ms) < (other.limit_price, other.placed_ms)


class PaperEngine:
    """Simulates fills from Ripple ExecutionIntents.

    Runs synchronously on the caller's thread (no broker, no asyncio).

    Phase 15 LIMIT simulation contract
    ------------------------------------
    • On each incoming trade tick (``on_trade``), all open LIMIT orders are
      checked for crossing:
        - BUY LIMIT at ``price``: fills when ``trade_price <= price``.
        - SELL LIMIT at ``price``: fills when ``trade_price >= price``.
    • Partial fills: if the simulated trade ``qty`` is less than the residual
      order quantity, only ``trade.qty`` is credited and the order remains
      ``PARTIAL`` on the book.
    • Timeout: orders older than ``limit_timeout_ms`` (event-time) are
      cancelled on the next ``on_trade`` tick.
    • OCO: when two orders share the same ``limit_order_id`` cross-reference,
      filling one immediately cancels the other.
    """

    def __init__(
        self,
        symbol: str,
        sizing: SizingConfig,
        order_callback: Optional[Callable[[Order], None]] = None,
        limit_timeout_ms: int = DEFAULT_LIMIT_TIMEOUT_MS,
        use_limit_orders: bool = False,
    ) -> None:
        self._symbol = symbol.upper()
        self._sizing = sizing
        self._order_callback = order_callback
        self._limit_timeout_ms = limit_timeout_ms
        # Phase 15 — opt-in flag. False → V1 MARKET-only behaviour identical
        # to pre-Phase 15. True → route bounce/scale-in entries to LIMIT and
        # simulate partial fills, OCO, and timeout.
        self._use_limit_orders = use_limit_orders
        self._position = PaperPosition()
        self._orders: Deque[Order] = deque(maxlen=500)
        self._metrics = PaperMetrics()
        self._balance = 10_000.0
        self._last_price = 0.0

        # Phase 15 — open LIMIT orders keyed by order.id
        self._open_limits: Dict[str, _LimitEntry] = {}
        # Heap for efficient expiry scanning (not used for cross checking —
        # the dict is small enough for a linear scan on each trade tick).
        self._limit_heap: List[_LimitEntry] = []

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def position(self) -> PaperPosition:
        return self._position

    @property
    def orders(self) -> List[Order]:
        return list(self._orders)

    @property
    def metrics(self) -> PaperMetrics:
        return self._metrics

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def realized_pnl(self) -> float:
        return self._metrics.realized_pnl

    def unrealized_pnl(self, current_price: float) -> float:
        if self._position.side is None or self._position.quantity <= 0:
            return 0.0
        if self._position.side == OrderSide.BUY:
            return (current_price - self._position.entry_price) * self._position.quantity
        else:
            return (self._position.entry_price - current_price) * self._position.quantity

    def update_sizing(self, sizing: SizingConfig) -> None:
        self._sizing = sizing

    # ------------------------------------------------------------------
    # Intent handling
    # ------------------------------------------------------------------

    def on_intent(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        """Process an execution intent. Returns None if filled, or a
        SuppressionReason if the intent was suppressed."""
        self._metrics.intents_received += 1

        if intent.reference_price > 0:
            self._last_price = intent.reference_price

        if intent.intent_type == "entry":
            return self._handle_entry(intent)
        elif intent.intent_type == "exit":
            return self._handle_exit(intent)
        elif intent.intent_type == "cancel":
            logger.info("Paper: cancel passive — cancelling all open LIMIT orders")
            self._cancel_all_limits()
            return None
        elif intent.intent_type == "rearm":
            logger.info("Paper: rearm — eligibility reset")
            return None
        else:
            return None

    def on_trade(self, trade_price: float, trade_qty: float, now_ms: int) -> None:
        """Phase 15 — advance the LIMIT order book with an incoming trade tick.

        Must be called by the caller on every trade event so that LIMIT fills,
        partial fills, and timeouts are simulated faithfully.

        ``now_ms`` must be event-time milliseconds (never wall-clock).
        """
        self._last_price = trade_price
        self._expire_limits(now_ms)
        self._try_fill_limits(trade_price, trade_qty, now_ms)

    # ------------------------------------------------------------------
    # Entry / exit handlers
    # ------------------------------------------------------------------

    def _handle_entry(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        if intent.side is None:
            return SuppressionReason.INVENTORY

        if (self._position.side == intent.side and
                self._position.quantity > 0):
            self._metrics.suppressed_inventory += 1
            return SuppressionReason.INVENTORY

        if (self._position.side is not None and
                self._position.side != intent.side and
                self._position.quantity > 0):
            self._close_position(intent, f"CLOSE_{intent.action}")

        price = intent.reference_price if intent.reference_price > 0 else self._last_price
        if price <= 0:
            return SuppressionReason.INVENTORY

        qty = self._compute_quantity(price)
        if qty <= 0:
            return SuppressionReason.INVENTORY

        # Phase 15 — route LIMIT vs MARKET based on urgency / action
        use_limit = self._should_use_limit(intent, price)

        if use_limit:
            self._place_limit_order(intent, intent.side, qty, price)
            return None

        # MARKET fill — immediate
        self._position = PaperPosition(
            side=intent.side, quantity=qty, entry_price=price)
        self._metrics.entries_filled += 1

        order = Order(
            symbol=self._symbol,
            side=intent.side,
            quantity=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=price,
            fill_quantity=qty,
            timestamp=intent.timestamp / 1000.0,
            signal_type=intent.action,
            ripple_reason=intent.reason,
            lifecycle=OrderLifecycle.FILLED,
        )
        self._record_order(order)
        return None

    def _handle_exit(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        if self._position.side is None or self._position.quantity <= 0:
            self._metrics.suppressed_no_position += 1
            return SuppressionReason.INVENTORY

        exit_type = intent.exit_type
        use_limit = (
            self._use_limit_orders
            and exit_type in (ExitType.TARGET, ExitType.EXHAUSTION)
            and intent.reference_price > 0
        )

        if use_limit:
            close_side = (OrderSide.SELL if self._position.side == OrderSide.BUY
                          else OrderSide.BUY)
            qty = self._position.quantity
            self._place_limit_order(intent, close_side, qty, intent.reference_price)
        else:
            self._close_position(intent, intent.action)

        self._metrics.exits_filled += 1
        return None

    def _close_position(self, intent: ExecutionIntent, signal_type: str) -> None:
        price = intent.reference_price if intent.reference_price > 0 else self._last_price
        if price <= 0:
            return

        qty = self._position.quantity
        if self._position.side == OrderSide.BUY:
            pnl = (price - self._position.entry_price) * qty
            close_side = OrderSide.SELL
        else:
            pnl = (self._position.entry_price - price) * qty
            close_side = OrderSide.BUY

        self._metrics.realized_pnl += pnl
        self._balance += pnl

        order = Order(
            symbol=self._symbol,
            side=close_side,
            quantity=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=price,
            fill_quantity=qty,
            timestamp=intent.timestamp / 1000.0,
            signal_type=f"CLOSE_{signal_type}",
            ripple_reason=intent.reason,
            lifecycle=OrderLifecycle.FILLED,
        )
        self._record_order(order)
        self._position = PaperPosition()

    # ------------------------------------------------------------------
    # Phase 15 — LIMIT order mechanics
    # ------------------------------------------------------------------

    def _should_use_limit(self, intent: ExecutionIntent, price: float) -> bool:
        """Return True when a LIMIT order should be used for this entry intent.

        Returns False when ``use_limit_orders=False`` (default) to preserve
        byte-identical V1 MARKET-fill behaviour. Set ``use_limit_orders=True``
        at construction to enable Phase 15 LIMIT routing.
        """
        if not self._use_limit_orders:
            return False
        if intent.urgency == "IMMEDIATE":
            return False
        # Bounce entries use LIMIT; breakout entries use MARKET
        action = intent.action or ""
        if "BOUNCE" in action and "ENTER" in action:
            return price > 0
        return False

    def _place_limit_order(
        self,
        intent: ExecutionIntent,
        side: OrderSide,
        qty: float,
        limit_price: float,
    ) -> Order:
        """Register a LIMIT order in the open-orders book."""
        order = Order(
            symbol=self._symbol,
            side=side,
            quantity=qty,
            order_type=OrderType.LIMIT,
            status=OrderStatus.SUBMITTED,
            price=limit_price,
            fill_quantity=0.0,
            timestamp=intent.timestamp / 1000.0,
            signal_type=intent.action,
            ripple_reason=intent.reason,
            lifecycle=OrderLifecycle.OPEN,
            placed_ms=intent.timestamp,
        )
        entry = _LimitEntry(
            order=order,
            limit_price=limit_price,
            placed_ms=intent.timestamp,
            is_buy=(side == OrderSide.BUY),
        )
        self._open_limits[order.id] = entry
        heapq.heappush(self._limit_heap, entry)
        self._record_order(order)
        logger.debug("Paper LIMIT placed: %s %s @ %.2f qty=%.6f",
                     side.value, self._symbol, limit_price, qty)
        return order

    def _try_fill_limits(self, trade_price: float, trade_qty: float, now_ms: int) -> None:
        """Check all open LIMIT orders for fill conditions against an incoming trade."""
        if not self._open_limits:
            return

        filled_ids: List[str] = []
        for oid, entry in list(self._open_limits.items()):
            order = entry.order
            crosses = (
                (entry.is_buy and trade_price <= entry.limit_price) or
                (not entry.is_buy and trade_price >= entry.limit_price)
            )
            if not crosses:
                continue

            residual = order.quantity - order.fill_quantity
            fill_qty = min(residual, trade_qty)
            order.fill_quantity += fill_qty
            order.fill_price = entry.limit_price  # fill at limit price

            if order.fill_quantity >= order.quantity - 1e-10:
                order.fill_quantity = order.quantity
                order.status = OrderStatus.FILLED
                order.lifecycle = OrderLifecycle.FILLED
                filled_ids.append(oid)
                self._apply_fill(order)
                logger.debug("Paper LIMIT FILLED: %s @ %.2f qty=%.6f",
                             order.signal_type, order.fill_price, order.fill_quantity)
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
                order.lifecycle = OrderLifecycle.PARTIAL
                self._apply_partial_fill(order, fill_qty)
                logger.debug("Paper LIMIT PARTIAL: %s @ %.2f qty=%.6f/%.6f",
                             order.signal_type, order.fill_price,
                             order.fill_quantity, order.quantity)

        for oid in filled_ids:
            self._remove_limit(oid, cancel_sibling=True)

    def _expire_limits(self, now_ms: int) -> None:
        """Cancel OPEN/PARTIAL LIMIT orders that have exceeded ``limit_timeout_ms``."""
        expired: List[str] = []
        for oid, entry in list(self._open_limits.items()):
            age_ms = now_ms - entry.placed_ms
            if age_ms > self._limit_timeout_ms:
                expired.append(oid)

        for oid in expired:
            entry = self._open_limits.get(oid)
            if entry:
                order = entry.order
                order.status = OrderStatus.CANCELLED
                order.lifecycle = OrderLifecycle.CANCELLED
                self._metrics.limit_timeouts += 1
                logger.info("Paper LIMIT TIMEOUT: %s order_id=%s age_ms=%d",
                            order.signal_type, order.id,
                            now_ms - entry.placed_ms)
            self._remove_limit(oid, cancel_sibling=False)

    def _apply_fill(self, order: Order) -> None:
        """Update position / PnL on a fully-filled LIMIT order."""
        if order.side in (OrderSide.BUY,):
            if self._position.side is None or self._position.quantity == 0:
                self._position = PaperPosition(
                    side=order.side,
                    quantity=order.fill_quantity,
                    entry_price=order.fill_price,
                )
                self._metrics.entries_filled += 1
            else:
                self._realize_close(order)
        else:
            self._realize_close(order)

    def _apply_partial_fill(self, order: Order, fill_qty: float) -> None:
        """Update position on a partial LIMIT fill."""
        if order.side == OrderSide.BUY:
            if self._position.side is None or self._position.quantity == 0:
                # Open new partial position
                self._position = PaperPosition(
                    side=order.side,
                    quantity=fill_qty,
                    entry_price=order.fill_price,
                )
            else:
                # Scale into existing long
                old_qty = self._position.quantity
                old_price = self._position.entry_price
                new_qty = old_qty + fill_qty
                self._position.entry_price = (
                    (old_price * old_qty + order.fill_price * fill_qty) / new_qty
                )
                self._position.quantity = new_qty
        else:
            # Partial exit — reduce position
            close_qty = min(fill_qty, self._position.quantity)
            if self._position.side == OrderSide.BUY:
                pnl = (order.fill_price - self._position.entry_price) * close_qty
            else:
                pnl = (self._position.entry_price - order.fill_price) * close_qty
            self._metrics.realized_pnl += pnl
            self._balance += pnl
            self._position.quantity -= close_qty
            if self._position.quantity <= 1e-10:
                self._position = PaperPosition()

    def _realize_close(self, order: Order) -> None:
        """Realize PnL for a fully-filled exit LIMIT order."""
        qty = order.fill_quantity
        if self._position.side == OrderSide.BUY:
            pnl = (order.fill_price - self._position.entry_price) * qty
        else:
            pnl = (self._position.entry_price - order.fill_price) * qty
        self._metrics.realized_pnl += pnl
        self._balance += pnl
        self._position = PaperPosition()
        self._metrics.exits_filled += 1

    def _remove_limit(self, order_id: str, cancel_sibling: bool) -> None:
        """Remove a LIMIT order from the open-orders book.

        If ``cancel_sibling`` is True and the removed order has a
        ``limit_order_id`` set, cancel the sibling order (OCO semantics).
        """
        entry = self._open_limits.pop(order_id, None)
        if entry is None:
            return
        if cancel_sibling:
            sibling_id = entry.order.limit_order_id
            if sibling_id and sibling_id in self._open_limits:
                sib_entry = self._open_limits[sibling_id]
                sib_entry.order.status = OrderStatus.CANCELLED
                sib_entry.order.lifecycle = OrderLifecycle.CANCELLED
                logger.info("Paper OCO sibling cancelled: %s", sibling_id)
                self._open_limits.pop(sibling_id, None)

    def _cancel_all_limits(self) -> None:
        for entry in list(self._open_limits.values()):
            entry.order.status = OrderStatus.CANCELLED
            entry.order.lifecycle = OrderLifecycle.CANCELLED
        self._open_limits.clear()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _compute_quantity(self, price: float) -> float:
        mode = self._sizing.mode
        val = self._sizing.value

        if mode == SizingMode.FIXED_QTY:
            return val
        elif mode == SizingMode.FIXED_NOTIONAL:
            return val / price if price > 0 else 0.0
        elif mode == SizingMode.PCT_BALANCE:
            return (self._balance * val / 100.0) / price if price > 0 else 0.0
        return 0.0

    def _record_order(self, order: Order) -> None:
        self._orders.append(order)
        if self._order_callback:
            try:
                self._order_callback(order)
            except Exception:
                pass

    def reset(self) -> None:
        self._position = PaperPosition()
        self._orders.clear()
        self._metrics = PaperMetrics()
        self._balance = 10_000.0
        self._open_limits.clear()
        self._limit_heap.clear()
