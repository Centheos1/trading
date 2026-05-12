from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from execution.models import AccountInfo, Order, OrderSide, OrderType, Position


class BrokerInterface(ABC):
    """Abstract broker connection. Implement per exchange/broker."""

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        ...

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float = 0.0,
    ) -> Order:
        """Place an order. For LIMIT orders pass a positive ``price``."""
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, broker_order_id: str) -> bool:
        """Cancel an open order by its broker-assigned ID."""
        ...

    @abstractmethod
    async def place_oco(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        target_price: float,
        stop_price: float,
    ) -> Tuple[Order, Order]:
        """Place an OCO pair (LIMIT target + STOP_MARKET stop-loss).

        Returns ``(target_order, stop_order)``. When either leg fills,
        the caller is responsible for cancelling the sibling via
        :meth:`cancel_order`.
        """
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> List[Order]:
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        ...

    @abstractmethod
    async def close_position(self, symbol: str) -> Optional[Order]:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...
