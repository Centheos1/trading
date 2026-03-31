"""Shared market/strategy state consumed by all views.

MarketState is a thin coordination object that holds *references* to the
canonical data structures (candle buckets, trade slices, strategy snapshot,
signals, etc.).  It does NOT duplicate heavy data — views read directly from
the same deques/dicts that the ingestion pipeline writes.

The timer-tick in MainWindow updates the scalar fields (chart_now, best_bid,
best_ask, strategy_snapshot, etc.) once per tick.  Views that are currently
active read whatever they need; inactive views skip rendering entirely.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Optional

from execution.models import StrategyUIState

if TYPE_CHECKING:
    from ui.heatmap_widget import TimeBucket, TradeSlice
    from execution.models import SignalEntry


class MarketState:
    """Shared model consumed by Order Flow, Candle Chart, and Strategy views.

    All mutable fields are written by MainWindow's timer tick and read by
    the active view's paint/update cycle.  This is single-threaded (Qt
    main-thread only), so no locking is required.
    """

    __slots__ = (
        'candles',
        'trade_slices',
        'chart_now',
        'visible_window_ms',
        'bucket_duration_ms',
        'best_bid',
        'best_ask',
        'strategy_snapshot',
        'strategy_ui_state',
        'signals',
    )

    def __init__(
        self,
        candles: deque | None = None,
        trade_slices: dict | None = None,
        bucket_duration_ms: int = 60_000,
        visible_window_ms: int = 60_000,
    ):
        self.candles: deque = candles if candles is not None else deque(maxlen=200)
        self.trade_slices: dict = trade_slices if trade_slices is not None else {}
        self.chart_now: int = 0
        self.visible_window_ms: int = visible_window_ms
        self.bucket_duration_ms: int = bucket_duration_ms
        self.best_bid: float = 0.0
        self.best_ask: float = 0.0
        self.strategy_snapshot = None
        self.strategy_ui_state: StrategyUIState = StrategyUIState.DISARMED
        self.signals: deque = deque(maxlen=2000)

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask

    @property
    def has_candles(self) -> bool:
        return len(self.candles) > 0

    @property
    def has_strategy(self) -> bool:
        return self.strategy_snapshot is not None
