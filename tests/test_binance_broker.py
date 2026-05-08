"""
Phase 12 — Track C: BinanceBroker REST/WS-client tests.

Patches `binance.AsyncClient.create` and the futures REST methods used by
the broker so the suite never touches the network. Covers connect/
disconnect, account parsing, place/cancel/poll order paths, position
queries, status mapping, and quantity rounding.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution import binance_broker as bb
from execution.binance_broker import BinanceBroker, FUTURES_TESTNET_BASE
from execution.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


# ----------------------------------------------------------------------
# Helpers

def _exchange_info(*, step_size: str = "0.001", symbol: str = "BTCUSDT"):
    return {
        "symbols": [
            {
                "symbol": symbol,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": step_size,
                     "minQty": "0.001", "maxQty": "1000"},
                ],
            }
        ]
    }


def _make_async_client(**method_overrides) -> MagicMock:
    """Build a stand-in for `binance.AsyncClient`. Every futures_* call
    is an AsyncMock by default; pass overrides to replace specific
    methods or set return values."""
    client = MagicMock()
    client.FUTURES_URL = "https://fapi.binance.com/fapi"
    # Async methods
    client.futures_exchange_info = AsyncMock(return_value=_exchange_info())
    client.futures_account = AsyncMock(return_value={
        "totalWalletBalance": "10000.0",
        "availableBalance": "5000.0",
        "positions": [],
    })
    client.futures_create_order = AsyncMock(return_value={
        "orderId": 123,
        "status": "FILLED",
        "avgPrice": "100.0",
        "executedQty": "0.5",
    })
    client.futures_get_order = AsyncMock(return_value={
        "status": "FILLED", "avgPrice": "100.0", "executedQty": "0.5",
    })
    client.futures_cancel_order = AsyncMock(return_value={"status": "CANCELED"})
    client.futures_get_open_orders = AsyncMock(return_value=[])
    client.futures_position_information = AsyncMock(return_value=[])
    client.close_connection = AsyncMock()
    for name, val in method_overrides.items():
        setattr(client, name, val)
    return client


def _make_broker_no_dotenv() -> BinanceBroker:
    """Construct a BinanceBroker without invoking real `load_dotenv`."""
    with patch.object(bb, "load_dotenv", lambda: None):
        return BinanceBroker()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ----------------------------------------------------------------------

class TestConnectFlow(unittest.TestCase):

    def test_connect_happy_path_populates_exchange_info(self):
        broker = _make_broker_no_dotenv()
        broker._testnet = False
        client = _make_async_client()

        async def fake_create(*a, **k):
            return client

        with patch("binance.AsyncClient.create",
                   new=AsyncMock(side_effect=fake_create)):
            ok = _run(broker.connect())

        self.assertTrue(ok)
        self.assertTrue(broker.is_connected())
        self.assertIn("BTCUSDT", broker._exchange_info)
        self.assertEqual(broker.get_step_size("BTCUSDT"), 0.001)

    def test_connect_failure_returns_false(self):
        broker = _make_broker_no_dotenv()
        with patch("binance.AsyncClient.create",
                   new=AsyncMock(side_effect=RuntimeError("nope"))):
            ok = _run(broker.connect())
        self.assertFalse(ok)
        self.assertFalse(broker.is_connected())
        self.assertIsNone(broker._client)

    def test_connect_testnet_rewrites_futures_url(self):
        broker = _make_broker_no_dotenv()
        broker._testnet = True
        client = _make_async_client()

        async def fake_create(*a, **k):
            return client

        with patch("binance.AsyncClient.create",
                   new=AsyncMock(side_effect=fake_create)):
            _run(broker.connect())

        self.assertEqual(client.FUTURES_URL, FUTURES_TESTNET_BASE + "/fapi")

    def test_disconnect_closes_and_nulls_client(self):
        broker = _make_broker_no_dotenv()
        client = _make_async_client()
        broker._client = client
        _run(broker.disconnect())
        client.close_connection.assert_awaited_once()
        self.assertIsNone(broker._client)


# ----------------------------------------------------------------------

class TestAccountInfo(unittest.TestCase):

    def test_get_account_info_parses_balance_and_filters_zero_positions(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_account=AsyncMock(return_value={
                "totalWalletBalance": "10500.5",
                "availableBalance": "9000.25",
                "positions": [
                    {"symbol": "BTCUSDT", "positionAmt": "0.5",
                     "entryPrice": "100.0", "unrealizedProfit": "5.5"},
                    {"symbol": "ETHUSDT", "positionAmt": "0",  # filtered
                     "entryPrice": "0", "unrealizedProfit": "0"},
                    {"symbol": "SOLUSDT", "positionAmt": "-2.0",  # short
                     "entryPrice": "20.0", "unrealizedProfit": "-1.5"},
                ],
            })
        )
        info = _run(broker.get_account_info())
        self.assertAlmostEqual(info.balance, 10500.5)
        self.assertAlmostEqual(info.available_balance, 9000.25)
        self.assertEqual(len(info.positions), 2)
        sides = {p.symbol: p.side for p in info.positions}
        self.assertEqual(sides["BTCUSDT"], OrderSide.BUY)
        self.assertEqual(sides["SOLUSDT"], OrderSide.SELL)

    def test_get_account_info_safe_float_swallows_malformed_values(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_account=AsyncMock(return_value={
                "totalWalletBalance": "",       # → 0
                "availableBalance": "garbage",  # → 0
                "positions": [],
            })
        )
        info = _run(broker.get_account_info())
        self.assertEqual(info.balance, 0.0)
        self.assertEqual(info.available_balance, 0.0)


# ----------------------------------------------------------------------

class TestPlaceOrder(unittest.TestCase):

    def test_round_quantity_zero_rejects(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client()
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "1.0"}]}
        order = _run(broker.place_order("BTCUSDT", OrderSide.BUY, 0.5))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("rounds to 0", order.error_message)

    def test_happy_path_returns_filled_order(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client()
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        order = _run(broker.place_order("BTCUSDT", OrderSide.BUY, 0.5))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.broker_order_id, "123")
        self.assertAlmostEqual(order.fill_price, 100.0)
        self.assertAlmostEqual(order.fill_quantity, 0.5)

    def test_submitted_with_zero_fill_polls_until_filled(self):
        broker = _make_broker_no_dotenv()
        get_order = AsyncMock(side_effect=[
            {"status": "NEW", "avgPrice": "0", "executedQty": "0"},
            {"status": "FILLED", "avgPrice": "100.5", "executedQty": "0.5"},
        ])
        broker._client = _make_async_client(
            futures_create_order=AsyncMock(return_value={
                "orderId": 7, "status": "NEW",
                "avgPrice": "0", "executedQty": "0",
            }),
            futures_get_order=get_order,
        )
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        # Skip the 0.3s sleep inside _poll_order_fill. _poll_order_fill
        # does a local `import asyncio`, so we patch the function on the
        # already-imported asyncio module directly.
        with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            order = _run(broker.place_order("BTCUSDT", OrderSide.BUY, 0.5))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.fill_price, 100.5)
        self.assertGreaterEqual(get_order.await_count, 2)

    def test_exchange_exception_rejects_order(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_create_order=AsyncMock(side_effect=RuntimeError("server")),
        )
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        order = _run(broker.place_order("BTCUSDT", OrderSide.BUY, 0.5))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("server", order.error_message)


# ----------------------------------------------------------------------

class TestCancelAndQueries(unittest.TestCase):

    def test_cancel_order_returns_true_on_success(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client()
        self.assertTrue(_run(broker.cancel_order("BTCUSDT", "123")))

    def test_cancel_order_returns_false_on_exception(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_cancel_order=AsyncMock(side_effect=RuntimeError("x")),
        )
        self.assertFalse(_run(broker.cancel_order("BTCUSDT", "123")))

    def test_get_open_orders_parses_results(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_get_open_orders=AsyncMock(return_value=[
                {"orderId": 1, "symbol": "BTCUSDT", "side": "BUY",
                 "origQty": "0.25", "type": "MARKET", "status": "NEW"},
            ]),
        )
        orders = _run(broker.get_open_orders("BTCUSDT"))
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, OrderSide.BUY)
        self.assertAlmostEqual(orders[0].quantity, 0.25)

    def test_get_position_filters_zero_amount(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_position_information=AsyncMock(return_value=[
                {"symbol": "BTCUSDT", "positionAmt": "0",
                 "entryPrice": "0", "unRealizedProfit": "0"},
                {"symbol": "BTCUSDT", "positionAmt": "0.3",
                 "entryPrice": "100.0", "unRealizedProfit": "1.5"},
            ]),
        )
        pos = _run(broker.get_position("BTCUSDT"))
        self.assertIsNotNone(pos)
        self.assertEqual(pos.side, OrderSide.BUY)
        self.assertAlmostEqual(pos.quantity, 0.3)


# ----------------------------------------------------------------------

class TestClosePosition(unittest.TestCase):

    def test_close_position_no_op_when_no_position(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client()  # default → no positions
        order = _run(broker.close_position("BTCUSDT"))
        self.assertIsNone(order)

    def test_close_position_flips_long_to_sell_market(self):
        broker = _make_broker_no_dotenv()
        broker._client = _make_async_client(
            futures_position_information=AsyncMock(return_value=[
                {"symbol": "BTCUSDT", "positionAmt": "0.4",
                 "entryPrice": "100.0", "unRealizedProfit": "0"},
            ]),
        )
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        order = _run(broker.close_position("BTCUSDT"))
        self.assertIsNotNone(order)
        # close_position calls place_order(SELL) which goes through the
        # default futures_create_order returning FILLED.
        self.assertEqual(order.side, OrderSide.SELL)
        self.assertEqual(order.status, OrderStatus.FILLED)


# ----------------------------------------------------------------------

class TestRoundingAndStatusMapping(unittest.TestCase):

    def test_round_quantity_step_precision(self):
        broker = _make_broker_no_dotenv()
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        # 0.123456 → floor(0.123456 / 0.001) * 0.001 = 0.123, with 3-dec round
        self.assertAlmostEqual(
            broker._round_quantity("BTCUSDT", 0.123456), 0.123)
        # Step 0.1 → 1 decimal
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.1"}]}
        self.assertAlmostEqual(
            broker._round_quantity("BTCUSDT", 0.789), 0.7)
        # Step 1.0 → 0 decimals
        broker._exchange_info["BTCUSDT"] = {"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "1.0"}]}
        self.assertAlmostEqual(
            broker._round_quantity("BTCUSDT", 7.9), 7.0)

    def test_round_quantity_no_step_falls_back_to_8_decimals(self):
        broker = _make_broker_no_dotenv()
        # No exchange info entry → step=0 → fallback round(qty, 8)
        self.assertAlmostEqual(
            broker._round_quantity("XYZ", 1.123456789), 1.12345679)

    def test_map_status_covers_all_six_statuses(self):
        m = BinanceBroker._map_status
        self.assertEqual(m("NEW"), OrderStatus.SUBMITTED)
        self.assertEqual(m("PARTIALLY_FILLED"), OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(m("FILLED"), OrderStatus.FILLED)
        self.assertEqual(m("CANCELED"), OrderStatus.CANCELLED)
        self.assertEqual(m("REJECTED"), OrderStatus.REJECTED)
        self.assertEqual(m("EXPIRED"), OrderStatus.CANCELLED)
        # Unknown → PENDING fallback
        self.assertEqual(m("FROBNICATED"), OrderStatus.PENDING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
