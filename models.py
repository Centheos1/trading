from typing import Dict, List, Union
from datetime import datetime, timezone
from dataclasses import dataclass


class BacktestResult:

    def __repr__(self):
        return f"""{'-' * 80}
Symbol = {self.symbol}
Strategy = {self.strategy}
Timeframe = {self.tf}
Time = {self.from_time} -> {self.to_time}
Parameters = {self.parameters}
PNL = {round(self.pnl, 2)}
Num Trades = {self.num_trades}
Sharpe Ratio = {round(self.sharpe_ratio, 2)}
CAGR = {round(self.cagr, 2)}
Max. Drawdown = {round(self.max_dd, 5)}
Rank = {self.rank}
Crowding Distance = {round(self.crowding_distance, 5)}
Order = {self.order}
"""

    def __init__(self):
        self.symbol: str = ''
        self.strategy: str = ''
        self.tf: str = ''
        self.from_time: str = ''
        self.to_time: str = ''
        self.pnl: float = 0.0
        self.max_dd: float = 0.0
        self.num_trades: int = 0
        self.cagr = 0.0
        self.sharpe_ratio = 0.0
        self.parameters: Dict = dict()
        self.dominated_by: int = 0
        self.dominates: List[int] = []
        self.rank: int = 0
        self.crowding_distance: float = 0.0
        self.order: float = -float("inf")
        self.written_at: str = datetime.now(timezone.utc).isoformat()

    def reset_results(self):
        self.dominated_by = 0
        self.dominates.clear()
        self.rank = 0
        self.crowding_distance = 0.0

    def h5_serialise(self) -> Dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "tf": self.tf,
            "from_time": self.from_time,
            "to_time": self.to_time,
            "pnl": self.pnl,
            "max_dd": self.max_dd,
            "num_trade": self.num_trades,
            "cagr": self.cagr,
            "sharpe_ratio": self.sharpe_ratio,
            "parameters": self.parameters,
            "dominated_by": self.dominated_by,
            "dominates": self.dominates,
            "rank": self.rank,
            "crowding_distance": self.crowding_distance,
            "order": self.order,
            "written_at": self.written_at
        }


@dataclass
class OandaSymbolTag:
    type: str = ''
    name: str = ''

    def __repr__(self):
        return f"""
Type: {self.type}
Name: {self.name}
"""

    def h5_serialise(self) -> Dict:
        return {
            "type": self.type,
            "name": self.name
        }


@dataclass
class OandaSymbolFinancingDaysOfWeek:
    dayOfWeek: str = ''
    day: int = -99
    daysCharged: int = -99

    def __post_init__(self):
        self.day = self._day_to_index.get(self.dayOfWeek, -99)

    _day_to_index = {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4,
        "SATURDAY": 5,
        "SUNDAY": 6,
    }

    def __repr__(self):
        return f"""
Day of Week: {self.dayOfWeek}
Day: {self.day}
Days Charged: {self.daysCharged}
"""

    def h5_serialise(self) -> Dict:
        return {
            "dayOfWeek": self.dayOfWeek,
            "day": self.day,
            "daysCharged": self.daysCharged
        }


@dataclass
class OandaSymbolFinancing:
    longRate: float = -float('inf')
    shortRate: float = -float('inf')
    financingDaysOfWeek: List[OandaSymbolFinancingDaysOfWeek] = None

    def __post_init__(self):
        self.longRate = float(self.longRate) if self.longRate != -float("inf") else self.longRate
        self.shortRate = float(self.shortRate) if self.shortRate != -float("inf") else self.shortRate
        self.financingDaysOfWeek = [OandaSymbolFinancingDaysOfWeek(**d) if isinstance(d, dict) else d for d in self.financingDaysOfWeek] if self.financingDaysOfWeek else None

    def __repr__(self):
        return f"""
Long Rate: {self.longRate}
Short Rate: {self.shortRate}
Financing Days of Week: {self.financingDaysOfWeek}
"""

    def h5_serialise(self) -> Dict:
        return {
            "longRate": self.longRate,
            "shortRate": self.shortRate,
            "financingDaysOfWeek": [d.h5_serialise() for d in self.financingDaysOfWeek] if self.financingDaysOfWeek else None
        }


@dataclass
class OandaGuaranteedStopLossOrderLevelRestriction:
    volume: int = -99
    priceRange:  float = -float('inf')

    def __post_init__(self):
        self.volume = int(self.volume)
        self.priceRange = float(self.priceRange) if self.priceRange !=  -float('inf') else self.priceRange

    def __repr__(self):
        return f"""
Volume: {self.volume}
Price Range: {self.priceRange}
"""

    def h5_serialise(self) -> Dict:
        return {
            "volume": self.volume,
            "priceRange": self.priceRange
        }


@dataclass
class OandaSymbolDetails:
    name: str = '' # symbol
    type: str = ''
    displayName: str = ''
    pipLocation: int = -99
    displayPrecision: int = -99
    tradeUnitsPrecision: int = -99
    minimumTradeSize: float = -float('inf')
    maximumTrailingStopDistance: float = -float('inf')
    minimumTrailingStopDistance: float = -float('inf')
    maximumPositionSize: int = -99
    maximumOrderUnits: int = -99
    marginRate: float = -float('inf')
    leverage: int = 1
    guaranteedStopLossOrderMode: str = ''
    minimumGuaranteedStopLossDistance: float = -float('inf')
    guaranteedStopLossOrderExecutionPremium: float = -float('inf')
    guaranteedStopLossOrderLevelRestriction: Union[OandaGuaranteedStopLossOrderLevelRestriction, Dict] = None
    tags: List[Union[OandaSymbolTag, Dict]] = None
    financing: Union[OandaSymbolFinancing, Dict] = None

    def __post_init__(self):
        self.minimumTradeSize = float(self.minimumTradeSize)
        self.maximumTrailingStopDistance = float(self.maximumTrailingStopDistance)
        self.maximumTrailingStopDistance = float(self.maximumTrailingStopDistance)
        self.minimumTrailingStopDistance = float(self.minimumTrailingStopDistance)
        self.maximumPositionSize = int(self.maximumPositionSize)
        self.maximumOrderUnits = int(self.maximumOrderUnits)
        self.minimumGuaranteedStopLossDistance = float(self.minimumGuaranteedStopLossDistance) if self.minimumGuaranteedStopLossDistance != -float('inf') else self.minimumGuaranteedStopLossDistance
        self.guaranteedStopLossOrderExecutionPremium = float(self.guaranteedStopLossOrderExecutionPremium) if self.guaranteedStopLossOrderExecutionPremium != -float('inf') else self.guaranteedStopLossOrderExecutionPremium
        self.guaranteedStopLossOrderLevelRestriction = OandaGuaranteedStopLossOrderLevelRestriction(**self.guaranteedStopLossOrderLevelRestriction) if isinstance(self.guaranteedStopLossOrderLevelRestriction, Dict) else self.guaranteedStopLossOrderLevelRestriction
        self.marginRate = float(self.marginRate)
        self.leverage = int(1 / self.marginRate) if self.marginRate != -float('inf') else self.leverage
        self.tags = [OandaSymbolTag(**t) if isinstance(t, dict) else t for t in self.tags] if self.tags else None
        self.financing = OandaSymbolFinancing(**self.financing) if isinstance(self.financing, dict) else self.financing

    def __repr__(self):
        return f"""{'-' * 80}
Name: {self.name}
Type: {self.type}
Display Name: {self.displayName}
Pip Location: {self.pipLocation}
Display Precision: {self.displayPrecision}
Trade Unit Precision: {self.tradeUnitsPrecision}
Minimum Trade Size: {self.minimumTradeSize}
Maximum Trailing Stop Distance: {self.maximumTrailingStopDistance}
Minimum Trailing Stop Distance: {self.minimumTrailingStopDistance}
Maximum Position Size: {self.maximumPositionSize}
Maximum Order Units: {self.maximumOrderUnits}
Margin Rate: {self.marginRate}
Leverage: {self.leverage}
Guaranteed Stop Loss Order Mode: {self.guaranteedStopLossOrderMode}
Tags: {self.tags}
Financing: {self.financing}
"""

    def h5_serialise(self) -> Dict:
        return {
            "name": self.name,
            "type": self.type,
            "displayName": self.displayName,
            "pipLocation": self.pipLocation,
            "displayPrecision": self.displayPrecision,
            "tradeUnitsPrecision": self.tradeUnitsPrecision,
            "minimumTradeSize": self.minimumTradeSize,
            "maximumTrailingStopDistance": self.maximumTrailingStopDistance,
            "minimumTrailingStopDistance": self.minimumTrailingStopDistance,
            "maximumPositionSize": self.maximumPositionSize,
            "maximumOrderUnits": self.maximumOrderUnits,
            "marginRate": self.marginRate,
            "leverage": self.leverage,
            "guaranteedStopLossOrderMode": self.guaranteedStopLossOrderMode,
            "tags": [t.h5_serialise() for t in self.tags] if self.tags else None,
            "financing": self.financing.h5_serialise() if self.financing else None
        }
