"""Phase 14E — pin ``num_trades`` as a first-class NSGA-II Pareto axis.

The legacy optimiser at the project root (``optimiser.py``) used to
ignore ``BacktestResult.num_trades`` in both ``crowding_distance`` and
``non_dominated_sorting`` (two ``# TODO add num_trades`` markers).
This suite verifies that the field now participates in both NSGA-II
operators, so the optimiser can distinguish parameter sets that earn
the same PnL with very different trade counts.

The tests bypass ``Nsga2.__init__`` so they do not require a live H5
database or the compiled C++ engine. They exercise the two
operators directly with synthetic ``BacktestResult`` instances.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import BacktestResult  # noqa: E402
from optimiser import Nsga2  # noqa: E402


def _make_result(*, pnl: float = 0.0, max_dd: float = 0.0,
                 sharpe_ratio: float = 0.0, cagr: float = 0.0,
                 num_trades: int = 0) -> BacktestResult:
    """Construct a ``BacktestResult`` with the four NSGA-II axes
    populated and everything else left at the dataclass default."""
    b = BacktestResult()
    b.pnl = pnl
    b.max_dd = max_dd
    b.sharpe_ratio = sharpe_ratio
    b.cagr = cagr
    b.num_trades = num_trades
    return b


class TestCrowdingDistanceNumTrades(unittest.TestCase):
    """``crowding_distance`` must iterate ``num_trades`` as a fourth
    objective (Phase 14E). Pre-fix it iterated only
    ``[pnl, max_dd, sharpe_ratio]`` — a parameter set that produced
    20 trades vs 1 trade with identical other metrics had the same
    crowding distance, so neither would be preferred during selection."""

    def _nsga(self) -> Nsga2:
        # Bypass __init__ to avoid the H5 / C++ dependency chain.
        return Nsga2.__new__(Nsga2)

    def test_crowding_distance_includes_num_trades_axis(self):
        """When ``num_trades`` is the only varying axis, the middle
        individual receives a finite non-zero crowding distance from
        the ``num_trades`` dimension. Pre-14E this would have been
        zero (the axis was not iterated)."""
        nsga = self._nsga()
        # Three individuals identical on every axis except num_trades.
        population = [
            _make_result(num_trades=5),
            _make_result(num_trades=10),
            _make_result(num_trades=20),
        ]
        ranked = nsga.crowding_distance(population)
        middle = next(b for b in ranked if b.num_trades == 10)
        # Endpoints get +inf; middle gets a finite, positive distance
        # solely from the num_trades dimension since pnl/max_dd/sharpe
        # all have diff=0 → distance contribution 0.
        self.assertNotEqual(middle.crowding_distance, float("inf"))
        self.assertGreater(middle.crowding_distance, 0.0)

    def test_crowding_distance_endpoints_marked_infinite(self):
        """NSGA-II convention: extremes of every objective axis sit
        at the boundary of the front and receive +inf crowding
        distance. Adding ``num_trades`` does not change this — the
        first and last sorted individuals still get +inf."""
        nsga = self._nsga()
        population = [
            _make_result(num_trades=1),
            _make_result(num_trades=50),
            _make_result(num_trades=100),
        ]
        ranked = nsga.crowding_distance(population)
        extremes = [b for b in ranked if b.crowding_distance == float("inf")]
        # ranked is sorted by the last objective (num_trades).
        # Both extremes (lowest + highest num_trades) get +inf.
        self.assertEqual(len(extremes), 2)


class TestDominanceNumTrades(unittest.TestCase):
    """``non_dominated_sorting`` must include ``num_trades`` in the
    Pareto dominance check (Phase 14E)."""

    def _nsga(self) -> Nsga2:
        return Nsga2.__new__(Nsga2)

    def test_higher_num_trades_dominates_when_other_axes_equal(self):
        """A strictly higher ``num_trades`` with equal ``cagr`` and
        ``sharpe_ratio`` produces Pareto dominance — assigning the
        winner to front 0 and the loser to front 1. Pre-14E both
        would sit on front 0 (incomparable on ``num_trades``).

        Note: ``non_dominated_sorting`` decrements ``dominated_by``
        as later fronts are built, so we assert on ``rank`` and
        ``dominates`` instead of the raw counter."""
        nsga = self._nsga()
        winner = _make_result(cagr=0.10, sharpe_ratio=1.0, num_trades=20)
        loser = _make_result(cagr=0.10, sharpe_ratio=1.0, num_trades=5)
        population = {1: winner, 2: loser}
        fronts = nsga.non_dominated_sorting(population)
        # Winner: front 0, dominates the loser explicitly.
        self.assertEqual(winner.rank, 0)
        self.assertIn(winner, fronts[0])
        self.assertIn(2, winner.dominates)
        # Loser: front 1, dominated by exactly one other individual.
        self.assertEqual(loser.rank, 1)
        self.assertIn(loser, fronts[1])
        self.assertNotIn(1, loser.dominates)

    def test_dominance_still_requires_no_axis_worse(self):
        """Phase 14E preserves the strict Pareto invariant: an
        individual that is better on one axis but worse on another
        does NOT dominate. Critical for the new 3-axis check —
        higher ``num_trades`` cannot rescue a worse ``cagr``. Both
        individuals stay on front 0 because they are mutually
        incomparable."""
        nsga = self._nsga()
        a = _make_result(cagr=0.10, sharpe_ratio=1.0, num_trades=5)
        b = _make_result(cagr=0.05, sharpe_ratio=1.0, num_trades=20)
        population = {1: a, 2: b}
        fronts = nsga.non_dominated_sorting(population)
        # Neither dominates the other.
        self.assertNotIn(2, a.dominates)
        self.assertNotIn(1, b.dominates)
        # Both sit on front 0.
        self.assertEqual(a.rank, 0)
        self.assertEqual(b.rank, 0)
        self.assertEqual(len(fronts), 1)


if __name__ == "__main__":
    unittest.main()
