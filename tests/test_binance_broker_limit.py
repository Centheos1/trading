"""Phase 15 — BinanceBroker LIMIT / OCO / cancel_order tests.

Acceptance criteria from implementation_plan.md §Phase 15:
  - place_order(LIMIT, price=X) sends price=X in Binance request body (mock API).
  - cancel_order calls Binance cancel endpoint.
  - place_oco sends LIMIT + STOP_MARKET pair.

All tests use a mock client so no network is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.binance_broker import BinanceBroker
from execution.models import (
    OrderLifecycle,
    OrderSide,
    OrderStatus,
    OrderType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client(order_result: dict | None = None, cancel_ok: bool = True):
    """Build a mock Binance AsyncClient with preset responses."""
    client = MagicMock()
    client.futures_create_order = AsyncMock(
        return_value=order_result or {
            "orderId": "12345",
            "status": "FILLED",
            "avgPrice": "50000.0",
            "executedQty": "0.001",
        }
    )
    client.futures_cancel_order = AsyncMock(return_value={"orderId": "12345"})
    client.futures_exchange_info = AsyncMock(return_value={"symbols": []})
    return client


def _broker_with_client(client) -> BinanceBroker:
    broker = BinanceBroker.__new__(BinanceBroker)
    broker._api_key = ""
    broker._api_secret = ""
    broker._testnet = True
    broker._client = client
    broker._exchange_info = {}
    return broker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlaceOrderLimit(unittest.IsolatedAsyncioTestCase):
    """place_order(LIMIT, price=X) sends price + timeInForce=GTC."""

    async def test_limit_order_sends_price_and_gtc(self):
        client = _mock_client(order_result={
            "orderId": "99",
            "status": "NEW",
            "avgPrice": "0.0",
            "executedQty": "0.0",
        })
        broker = _broker_with_client(client)

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=50_000.0)

        call_kwargs = client.futures_create_order.call_args.kwargs
        self.assertEqual(call_kwargs["type"], "LIMIT")
        self.assertIn("price", call_kwargs)
        self.assertEqual(call_kwargs["price"], "50000.0")
        self.assertEqual(call_kwargs["timeInForce"], "GTC")

    async def test_limit_order_lifecycle_is_open_when_not_immediately_filled(self):
        client = _mock_client(order_result={
            "orderId": "99",
            "status": "NEW",
            "avgPrice": "0.0",
            "executedQty": "0.0",
        })
        # Suppress polling
        client.futures_get_order = AsyncMock(return_value={
            "status": "NEW", "avgPrice": "0.0", "executedQty": "0.0"
        })
        broker = _broker_with_client(client)

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=50_000.0)

        self.assertEqual(order.order_type, OrderType.LIMIT)
        self.assertEqual(order.price, 50_000.0)
        self.assertIn(order.lifecycle, (OrderLifecycle.OPEN, OrderLifecycle.PARTIAL))

    async def test_limit_order_filled_lifecycle(self):
        client = _mock_client(order_result={
            "orderId": "99",
            "status": "FILLED",
            "avgPrice": "50000.0",
            "executedQty": "0.001",
        })
        broker = _broker_with_client(client)

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=50_000.0)

        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.lifecycle, OrderLifecycle.FILLED)

    async def test_limit_with_zero_price_falls_back_to_market(self):
        client = _mock_client()
        broker = _broker_with_client(client)

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=0.0)

        call_kwargs = client.futures_create_order.call_args.kwargs
        # Should have fallen back to MARKET type
        self.assertEqual(call_kwargs["type"], "MARKET")
        self.assertNotIn("price", call_kwargs)
        self.assertEqual(order.order_type, OrderType.MARKET)

    async def test_market_order_sends_no_price(self):
        client = _mock_client()
        broker = _broker_with_client(client)

        await broker.place_order("BTCUSDT", OrderSide.BUY, 0.001, OrderType.MARKET)

        call_kwargs = client.futures_create_order.call_args.kwargs
        self.assertEqual(call_kwargs["type"], "MARKET")
        self.assertNotIn("price", call_kwargs)
        self.assertNotIn("timeInForce", call_kwargs)


class TestCancelOrder(unittest.IsolatedAsyncioTestCase):
    """cancel_order calls Binance cancel endpoint."""

    async def test_cancel_order_calls_binance(self):
        client = _mock_client()
        broker = _broker_with_client(client)

        result = await broker.cancel_order("BTCUSDT", "12345")

        client.futures_cancel_order.assert_called_once_with(
            symbol="BTCUSDT", orderId=12345)
        self.assertTrue(result)

    async def test_cancel_order_returns_false_when_not_connected(self):
        broker = BinanceBroker.__new__(BinanceBroker)
        broker._client = None
        broker._exchange_info = {}
        broker._testnet = True

        result = await broker.cancel_order("BTCUSDT", "12345")
        self.assertFalse(result)

    async def test_cancel_order_returns_false_on_exception(self):
        client = _mock_client()
        client.futures_cancel_order = AsyncMock(side_effect=Exception("API error"))
        broker = _broker_with_client(client)

        result = await broker.cancel_order("BTCUSDT", "12345")
        self.assertFalse(result)


class TestPlaceOCO(unittest.IsolatedAsyncioTestCase):
    """place_oco sends LIMIT target + STOP_MARKET stop-loss pair."""

    async def test_place_oco_calls_create_order_twice(self):
        call_count = [0]
        responses = [
            {"orderId": "101", "status": "NEW", "avgPrice": "0", "executedQty": "0"},
            {"orderId": "102", "status": "NEW", "avgPrice": "0", "executedQty": "0"},
        ]

        async def _create_order(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx]

        client = MagicMock()
        client.futures_create_order = _create_order
        broker = _broker_with_client(client)

        target, stop = await broker.place_oco(
            "BTCUSDT", OrderSide.BUY, 0.001,
            target_price=51_000.0, stop_price=49_000.0)

        self.assertEqual(call_count[0], 2)
        self.assertEqual(target.price, 51_000.0)
        self.assertEqual(target.order_type, OrderType.LIMIT)
        # stop should reference target and vice versa
        self.assertEqual(target.limit_order_id, "102")
        self.assertEqual(stop.limit_order_id, "101")

    async def test_place_oco_target_filled_sibling_references_set(self):
        responses = iter([
            {"orderId": "T1", "status": "FILLED", "avgPrice": "51000", "executedQty": "0.001"},
            {"orderId": "S1", "status": "NEW", "avgPrice": "0", "executedQty": "0"},
        ])

        async def _create(**kwargs):
            return next(responses)

        client = MagicMock()
        client.futures_create_order = _create
        broker = _broker_with_client(client)

        target, stop = await broker.place_oco(
            "BTCUSDT", OrderSide.BUY, 0.001,
            target_price=51_000.0, stop_price=49_000.0)

        self.assertEqual(target.broker_order_id, "T1")
        self.assertEqual(stop.broker_order_id, "S1")
        # Cross-references must be set
        self.assertEqual(target.limit_order_id, "S1")
        self.assertEqual(stop.limit_order_id, "T1")


class TestSyncLifecycle(unittest.TestCase):
    """_sync_lifecycle keeps lifecycle in step with status."""

    def test_sync_filled(self):
        from execution.models import Order, OrderStatus, OrderLifecycle
        order = Order(status=OrderStatus.FILLED)
        BinanceBroker._sync_lifecycle(order)
        self.assertEqual(order.lifecycle, OrderLifecycle.FILLED)

    def test_sync_partial(self):
        from execution.models import Order, OrderStatus, OrderLifecycle
        order = Order(status=OrderStatus.PARTIALLY_FILLED)
        BinanceBroker._sync_lifecycle(order)
        self.assertEqual(order.lifecycle, OrderLifecycle.PARTIAL)

    def test_sync_cancelled(self):
        from execution.models import Order, OrderStatus, OrderLifecycle
        order = Order(status=OrderStatus.CANCELLED)
        BinanceBroker._sync_lifecycle(order)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)

    def test_sync_rejected_maps_to_cancelled_lifecycle(self):
        """REJECTED status must resolve to CANCELLED lifecycle — not left as OPEN."""
        from execution.models import Order, OrderStatus, OrderLifecycle
        order = Order(
            order_type=OrderType.LIMIT,
            lifecycle=OrderLifecycle.OPEN,
            status=OrderStatus.REJECTED,
        )
        BinanceBroker._sync_lifecycle(order)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)


class TestRejectionLifecycle(unittest.IsolatedAsyncioTestCase):
    """Rejected LIMIT orders must come back with lifecycle=CANCELLED, not OPEN."""

    async def test_not_connected_limit_order_lifecycle_is_cancelled(self):
        broker = BinanceBroker.__new__(BinanceBroker)
        broker._client = None
        broker._exchange_info = {}
        broker._testnet = True

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=50_000.0)

        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)

    async def test_qty_rounds_to_zero_limit_order_lifecycle_is_cancelled(self):
        """When quantity < step_size (e.g. sub-minimum BTC qty) the order is rejected."""
        client = _mock_client()
        broker = _broker_with_client(client)
        # Force step_size = 0.001 so that qty=0.0009 rounds to 0
        broker._exchange_info = {
            "BTCUSDT": {
                "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}]
            }
        }

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.0009, OrderType.LIMIT, price=50_000.0)

        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)

    async def test_api_exception_limit_order_lifecycle_is_cancelled(self):
        client = _mock_client()
        client.futures_create_order = AsyncMock(side_effect=Exception("network error"))
        broker = _broker_with_client(client)

        order = await broker.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, OrderType.LIMIT, price=50_000.0)

        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.lifecycle, OrderLifecycle.CANCELLED)

    async def test_oco_stop_leg_not_connected_lifecycle_is_cancelled(self):
        """OCO stop leg rejected when not connected must have lifecycle=CANCELLED."""
        broker = BinanceBroker.__new__(BinanceBroker)
        broker._client = None
        broker._exchange_info = {}
        broker._testnet = True

        target, stop = await broker.place_oco(
            "BTCUSDT", OrderSide.BUY, 0.001,
            target_price=51_000.0, stop_price=49_000.0)

        self.assertEqual(target.status, OrderStatus.REJECTED)
        self.assertEqual(target.lifecycle, OrderLifecycle.CANCELLED)
        self.assertEqual(stop.status, OrderStatus.REJECTED)
        self.assertEqual(stop.lifecycle, OrderLifecycle.CANCELLED)


if __name__ == "__main__":
    unittest.main()
