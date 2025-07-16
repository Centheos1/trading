from typing import Dict, List
from datetime import datetime


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
        self.written_at: str = datetime.utcnow().isoformat()

    def reset_results(self):
        self.dominated_by = 0
        self.dominates.clear()
        self.rank = 0
        self.crowding_distance = 0.0

    def h5_serialise(self):
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
