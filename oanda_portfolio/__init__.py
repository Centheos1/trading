"""Oanda mean-variance frontier research and practice-account execution.

The historical walk-forward in :mod:`oanda_portfolio.simulate` never places
orders. Practice orders are sent only by :func:`oanda_portfolio.practice.submit_plan`.
"""

from oanda_portfolio.daily import aggregate_to_daily, load_oanda_daily_panel
from oanda_portfolio.frontier import FrontierConfig, frontier_portfolio
from oanda_portfolio.practice import submit_plan
from oanda_portfolio.simulate import simulate

__all__ = [
    "FrontierConfig",
    "aggregate_to_daily",
    "frontier_portfolio",
    "load_oanda_daily_panel",
    "simulate",
    "submit_plan",
]
