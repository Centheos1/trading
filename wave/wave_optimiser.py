"""
NSGA-II parameter optimisation for the Wave overlay strategy.

Mirrors tide_optimiser.py in structure but operates over WaveStrategyParams.

Supports two modes:
  isolated  — Wave alone; TideBias is pinned to NEUTRAL each bar
  stacked   — a fixed TideBacktestResult is passed in; Tide signals flow
              into the Wave backtester on each evaluation

Objectives (maximise Sharpe, maximise Calmar, minimise MaxDD):
    (sharpe, calmar, -|max_drawdown|)

All Parameters including timeframe are optimisable.

Calibration assumption (Phase 9 — backtest hardening)
─────────────────────────────────────────────────────
Feature thresholds (η, dispersion, AR) are calibrated against the *bar-count*
distributions produced by the chosen timeframe.  The default ranges in
``WaveOptimiserConfig.param_space`` were tuned on hourly bar data; if you
search over the ``timeframe`` axis without setting
``WaveOptimiserConfig.rescale_windows = True`` then a candidate that swaps
``timeframe`` to ``5m`` will see windows that are ~12x narrower in
wall-clock terms (e.g. ``eta_window=24`` becomes 2h instead of 24h), which
shifts every feature's distribution and breaks threshold calibration.

Two ways to handle this:

1. Set ``WaveOptimiserConfig.rescale_windows = True`` (recommended when
   ``timeframe`` is in the search space).  The optimiser will scale
   ``eta_window``, ``vwap_window``, ``structure_window``, ``disp_window``,
   ``ar_window`` by the bar-seconds ratio when applying a new timeframe so
   the wall-clock coverage stays constant.
2. Pin ``timeframe`` to a single value and let the GA search the windows
   directly.  This is what older runs implicitly did.
"""

from __future__ import annotations

import logging
import random
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from wave.wave_backtest import WaveBacktester, WaveStrategyParams
from wave.wave_metrics import compute_wave_report

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Search-space definition (reuse ParamSpec from tide_optimiser)
# ────────────────────────────────────────────────────────────────────────

@dataclass
class ParamSpec:
    """A single tunable parameter and its sampling spec.

    kind:
        "int"    — uniform int in [low, high]
        "float"  — uniform float in [low, high], rounded to ``decimals``
        "choice" — uniform draw from ``choices`` list
    """
    name: str
    kind: str
    low: float = 0.0
    high: float = 0.0
    decimals: int = 4
    choices: List = field(default_factory=list)


@dataclass
class WaveOptimiserConfig:
    """Search space + GA hyperparameters for Wave optimisation."""

    population_size: int = 20
    generations: int = 12
    crossover_rate: float = 0.8
    mutation_rate: float = 0.3
    seed: int = 42

    # Maximum bars used during GA evaluation (0 = unlimited).
    # Capping to a recent window dramatically speeds up GA iterations at fine
    # timeframes (5m has 170k bars over 2 yrs; 20 000 bars ≈ 10 weeks, fast
    # enough to distinguish good from bad parameters without full-history cost).
    # Final top-N reports are always generated on the complete dataset.
    max_opt_bars: int = 20_000

    param_space: List[ParamSpec] = field(default_factory=lambda: [
        # Feature windows
        ParamSpec("eta_window", "int", 6, 60),
        ParamSpec("vwap_window", "int", 8, 72),
        ParamSpec("structure_window", "int", 8, 72),
        ParamSpec("disp_window", "int", 8, 60),
        ParamSpec("ar_window", "int", 12, 120),
        ParamSpec("disp_scale", "float", 0.01, 0.15, 3),
        # Regime thresholds — ranges cover typical feature distributions on
        # hourly bar data (η p50≈0.15, p90≈0.41; D p50≈0.08, p95≈0.19;
        # AR p50≈0.90, p25≈0.65)
        ParamSpec("eta_mr_threshold", "float", 0.05, 0.30, 2),
        ParamSpec("eta_bo_threshold", "float", 0.25, 0.70, 2),
        ParamSpec("eta_neutral_threshold", "float", 0.10, 0.45, 2),
        ParamSpec("dispersion_threshold", "float", 0.05, 0.30, 3),
        ParamSpec("dispersion_critical", "float", 0.15, 0.50, 3),
        ParamSpec("ar_critical", "float", 0.75, 0.99, 2),
        ParamSpec("ar_recover", "float", 0.40, 0.85, 2),
        ParamSpec("reduced_size_fraction", "float", 0.1, 0.9, 2),
        ParamSpec("rebalance_threshold", "float", 0.0, 0.05, 4),
        # Wave operates at 5s–minute cadence; prefer short timeframes.
        ParamSpec("timeframe", "choice", choices=["5m", "15m", "30m", "1h"]),
    ])

    multi_timeframe: bool = True

    # Phase 9 — backtest hardening.
    # When True, ``with_timeframe`` rescales feature-window lengths to keep
    # wall-clock coverage constant when ``timeframe`` is mutated.  See module
    # docstring for the calibration rationale.  Default False preserves the
    # behaviour of pre-Phase-9 runs.
    rescale_windows: bool = False


# ────────────────────────────────────────────────────────────────────────
# Sampling and mutation helpers
# ────────────────────────────────────────────────────────────────────────

def _sample_one(spec: ParamSpec, rng: random.Random):
    if spec.kind == "int":
        return rng.randint(int(spec.low), int(spec.high))
    if spec.kind == "float":
        return round(rng.uniform(spec.low, spec.high), spec.decimals)
    if spec.kind == "choice":
        if not spec.choices:
            raise ValueError(f"ParamSpec '{spec.name}' has empty choices")
        return rng.choice(spec.choices)
    raise ValueError(f"unknown kind '{spec.kind}'")


def _mutate_one(spec: ParamSpec, current, rng: random.Random):
    if spec.kind == "int":
        delta = int(rng.uniform(-1, 1) * max(1, (spec.high - spec.low) * 0.25))
        return max(int(spec.low), min(int(spec.high), int(current) + delta))
    if spec.kind == "float":
        delta = rng.uniform(-1, 1) * (spec.high - spec.low) * 0.25
        v = max(spec.low, min(spec.high, float(current) + delta))
        return round(v, spec.decimals)
    if spec.kind == "choice":
        return rng.choice(spec.choices) if spec.choices else current
    raise ValueError(f"unknown kind '{spec.kind}'")


# Sensible accuracy horizons (in bar counts) per timeframe so each horizon
# covers Wave's "minutes to hours" range regardless of bar size.
_HORIZONS_BY_TF: dict[str, tuple[int, ...]] = {
    "1m":  (1, 5, 15, 30, 60, 240),   # 1m, 5m, 15m, 30m, 1h, 4h
    "5m":  (1, 3, 6, 12, 24, 48),     # 5m, 15m, 30m, 1h, 2h, 4h
    "15m": (1, 2, 4, 8, 16, 32),      # 15m, 30m, 1h, 2h, 4h, 8h
    "30m": (1, 2, 4, 8, 16, 24),      # 30m, 1h, 2h, 4h, 8h, 12h
    "1h":  (1, 2, 4, 8, 12, 24),      # 1h, 2h, 4h, 8h, 12h, 1d
    "4h":  (1, 2, 3, 6, 12, 18),      # 4h, 8h, 12h, 1d, 2d, 3d
}


def _apply(
    params: WaveStrategyParams,
    name: str,
    value,
    *,
    rescale_windows: bool = False,
) -> WaveStrategyParams:
    """Apply a single GA parameter assignment, returning a new params copy.

    Phase 9: ``rescale_windows`` is forwarded to
    :py:meth:`WaveStrategyParams.with_timeframe` when ``name == "timeframe"``
    so window lengths track the new bar-clock and threshold calibration
    stays valid across timeframes.
    """
    if name == "timeframe":
        tf = str(value)
        new = params.with_timeframe(tf, rescale_windows=rescale_windows)
        # Also update accuracy horizons to match the new timeframe's scale
        horizons = _HORIZONS_BY_TF.get(tf, params.accuracy_horizons)
        from dataclasses import replace as _replace
        return _replace(new, accuracy_horizons=horizons)
    new = deepcopy(params)
    setattr(new, name, value)
    return new


# ────────────────────────────────────────────────────────────────────────
# Individual + Pareto sorting
# ────────────────────────────────────────────────────────────────────────

@dataclass
class WaveIndividual:
    params: WaveStrategyParams
    sharpe: float = 0.0
    calmar: float = 0.0
    max_dd: float = 0.0
    cagr: float = 0.0
    num_trades: int = 0
    final_equity: float = 0.0
    rank: int = -1
    crowding_distance: float = 0.0

    def objectives(self) -> Tuple[float, float, float]:
        return (self.sharpe, self.calmar, -abs(self.max_dd))

    def dominates(self, other: "WaveIndividual") -> bool:
        a = self.objectives()
        b = other.objectives()
        return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def _non_dominated_sort(pop: List[WaveIndividual]) -> List[List[WaveIndividual]]:
    fronts: List[List[WaveIndividual]] = [[]]
    n = [0] * len(pop)
    S: List[List[int]] = [[] for _ in pop]
    for pi, p in enumerate(pop):
        for qi, q in enumerate(pop):
            if pi == qi:
                continue
            if p.dominates(q):
                S[pi].append(qi)
            elif q.dominates(p):
                n[pi] += 1
        if n[pi] == 0:
            p.rank = 0
            fronts[0].append(p)
    i = 0
    while fronts[i]:
        nxt: List[WaveIndividual] = []
        for p in fronts[i]:
            pi = pop.index(p)
            for qi in S[pi]:
                n[qi] -= 1
                if n[qi] == 0:
                    pop[qi].rank = i + 1
                    nxt.append(pop[qi])
        i += 1
        fronts.append(nxt)
    return [f for f in fronts if f]


def _crowding_distance(front: List[WaveIndividual]) -> None:
    if len(front) <= 2:
        for ind in front:
            ind.crowding_distance = float("inf")
        return
    for ind in front:
        ind.crowding_distance = 0.0
    for k in range(3):
        s = sorted(front, key=lambda x: x.objectives()[k])
        s[0].crowding_distance = float("inf")
        s[-1].crowding_distance = float("inf")
        f_min, f_max = s[0].objectives()[k], s[-1].objectives()[k]
        denom = (f_max - f_min) if f_max > f_min else 1.0
        for j in range(1, len(s) - 1):
            s[j].crowding_distance += (s[j + 1].objectives()[k] - s[j - 1].objectives()[k]) / denom


def _tournament(pop: List[WaveIndividual], rng: random.Random) -> WaveIndividual:
    a, b = rng.sample(pop, 2)
    if a.rank != b.rank:
        return a if a.rank < b.rank else b
    return a if a.crowding_distance > b.crowding_distance else b


# ────────────────────────────────────────────────────────────────────────
# Optimiser
# ────────────────────────────────────────────────────────────────────────

class WaveOptimiser:
    """Multi-objective Wave parameter search (NSGA-II).

    Parameters
    ----------
    base_params  : WaveStrategyParams  Seed parameter set.
    ohlcv_1m     : pd.DataFrame        1-minute OHLCV; resampled per individual
                                        when multi_timeframe=True.  If data is
                                        already at the target resolution, pass it
                                        directly and set multi_timeframe=False.
    config       : WaveOptimiserConfig
    tide_result  : TideBacktestResult | None
                   Required when base_params.mode == "stacked".
    """

    def __init__(
        self,
        base_params: WaveStrategyParams,
        ohlcv_1m: pd.DataFrame,
        config: Optional[WaveOptimiserConfig] = None,
        tide_result=None,
    ):
        self.base_params = base_params
        self.ohlcv_1m = ohlcv_1m
        self.cfg = config or WaveOptimiserConfig()
        self.tide_result = tide_result
        self._rng = random.Random(self.cfg.seed)
        # Cache of resampled OHLCV keyed by timeframe string
        self._tf_cache: Dict[str, pd.DataFrame] = {}

    # ── Public API ─────────────────────────────────────────────────────

    def run(self) -> List[WaveIndividual]:
        """Run NSGA-II; return the Pareto-optimal front."""
        pop = self._init_population()
        for gen in range(self.cfg.generations):
            offspring = self._make_offspring(pop)
            combined = pop + offspring
            for ind in combined:
                if ind.rank < 0:
                    self._evaluate(ind)
            fronts = _non_dominated_sort(combined)
            for front in fronts:
                _crowding_distance(front)
            new_pop: List[WaveIndividual] = []
            for front in fronts:
                if len(new_pop) + len(front) <= self.cfg.population_size:
                    new_pop.extend(front)
                else:
                    remaining = self.cfg.population_size - len(new_pop)
                    s = sorted(front, key=lambda x: -x.crowding_distance)
                    new_pop.extend(s[:remaining])
                    break
            pop = new_pop
            best = max(pop, key=lambda x: x.sharpe)
            logger.info(
                "Gen %d/%d  best sharpe=%.3f  calmar=%.3f  dd=%.2f%%  tf=%s",
                gen + 1, self.cfg.generations,
                best.sharpe, best.calmar, abs(best.max_dd) * 100,
                best.params.timeframe,
            )
        final_fronts = _non_dominated_sort(pop)
        pareto = final_fronts[0] if final_fronts else pop
        return sorted(pareto, key=lambda x: -x.sharpe)

    def format_pareto_front(self, front: List[WaveIndividual]) -> str:
        lines = [
            f"{'Rank':<5} {'Sharpe':>7} {'Calmar':>8} {'MaxDD':>8} "
            f"{'CAGR':>7} {'TF':>5} {'Mode':>9} {'η_mr':>5} {'η_bo':>5}"
        ]
        lines.append("-" * 78)
        for i, ind in enumerate(front):
            p = ind.params
            lines.append(
                f"{i + 1:<5} {ind.sharpe:>7.3f} {ind.calmar:>8.3f} "
                f"{abs(ind.max_dd) * 100:>7.1f}% {ind.cagr * 100:>6.1f}% "
                f"{p.timeframe:>5} {p.mode:>9} {p.eta_mr_threshold:>5.2f} "
                f"{p.eta_bo_threshold:>5.2f}"
            )
        return "\n".join(lines)

    # ── Internals ──────────────────────────────────────────────────────

    def _resample(self, timeframe: str) -> pd.DataFrame:
        """Resample ohlcv_1m to ``timeframe`` and cache the full result."""
        if timeframe in self._tf_cache:
            return self._tf_cache[timeframe]
        if timeframe == "1m":
            self._tf_cache["1m"] = self.ohlcv_1m
            return self.ohlcv_1m
        rule_map = {
            "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h", "4h": "4h", "12h": "12h", "1d": "1D",
        }
        rule = rule_map.get(timeframe, timeframe)
        resampled = self.ohlcv_1m.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        self._tf_cache[timeframe] = resampled
        return resampled

    def _get_data(self, timeframe: str) -> pd.DataFrame:
        """Return the (optionally capped) dataset used during GA evaluation."""
        if not self.cfg.multi_timeframe:
            full = self.ohlcv_1m
        else:
            full = self._resample(timeframe)
        if self.cfg.max_opt_bars > 0 and len(full) > self.cfg.max_opt_bars:
            return full.iloc[-self.cfg.max_opt_bars:]
        return full

    def _get_full_data(self, timeframe: str) -> pd.DataFrame:
        """Return the complete (uncapped) dataset for final report generation."""
        if not self.cfg.multi_timeframe:
            return self.ohlcv_1m
        return self._resample(timeframe)

    def _init_population(self) -> List[WaveIndividual]:
        pop = []
        for _ in range(self.cfg.population_size):
            params = deepcopy(self.base_params)
            for spec in self.cfg.param_space:
                params = _apply(
                    params, spec.name, _sample_one(spec, self._rng),
                    rescale_windows=self.cfg.rescale_windows,
                )
            ind = WaveIndividual(params=params)
            self._evaluate(ind)
            pop.append(ind)
        return pop

    def _evaluate(self, ind: WaveIndividual) -> None:
        params = deepcopy(ind.params)
        # Disable accuracy computation for speed during GA
        params = deepcopy(ind.params)
        from dataclasses import replace
        params = replace(params, accuracy_horizons=())

        try:
            data = self._get_data(params.timeframe)
            if len(data) < 50:
                ind.sharpe = -99.0
                ind.calmar = -99.0
                ind.max_dd = -1.0
                ind.rank = 0
                return

            backtester = WaveBacktester(params, tide_result=self.tide_result)
            result = backtester.run(data)
            report = compute_wave_report(result, compute_accuracy=False)

            ind.sharpe = report.sharpe if not _bad(report.sharpe) else -99.0
            ind.calmar = report.calmar if not _bad(report.calmar) else -99.0
            ind.max_dd = report.max_drawdown
            ind.cagr = report.cagr
            ind.num_trades = report.num_trades
            ind.final_equity = report.final_equity
        except Exception as exc:
            logger.debug("Wave evaluation error: %s", exc)
            ind.sharpe = -99.0
            ind.calmar = -99.0
            ind.max_dd = -1.0
        finally:
            ind.rank = 0  # will be re-ranked in non-dominated sort

    def _make_offspring(self, pop: List[WaveIndividual]) -> List[WaveIndividual]:
        offspring = []
        while len(offspring) < len(pop):
            p1 = _tournament(pop, self._rng)
            p2 = _tournament(pop, self._rng)
            child_params = self._crossover(p1.params, p2.params)
            child_params = self._mutate(child_params)
            child = WaveIndividual(params=child_params)
            offspring.append(child)
        return offspring

    def _crossover(
        self,
        a: WaveStrategyParams,
        b: WaveStrategyParams,
    ) -> WaveStrategyParams:
        if self._rng.random() > self.cfg.crossover_rate:
            return deepcopy(a)
        params = deepcopy(a)
        for spec in self.cfg.param_space:
            if self._rng.random() < 0.5:
                params = _apply(
                    params, spec.name, getattr(b, spec.name),
                    rescale_windows=self.cfg.rescale_windows,
                )
        return params

    def _mutate(self, params: WaveStrategyParams) -> WaveStrategyParams:
        for spec in self.cfg.param_space:
            if self._rng.random() < self.cfg.mutation_rate:
                params = _apply(
                    params, spec.name,
                    _mutate_one(spec, getattr(params, spec.name), self._rng),
                    rescale_windows=self.cfg.rescale_windows,
                )
        return params


def _bad(v: float) -> bool:
    import math
    return not math.isfinite(v) or v < -90.0
