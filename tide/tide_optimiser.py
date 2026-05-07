"""
NSGA-II parameter optimisation for the Tide overlay strategy.

This is a Tide-specific wrapper around the existing NSGA-II machinery in
``optimiser.py``.  We do not reuse ``optimiser.Nsga2`` directly because:

  - It is hard-coded to the legacy strategies (`obv`, `sma`, ...) and to
    the C++ orderflow engine.
  - It does not understand multi-element parameters (Tide has 3-vector
    vol thresholds and 4-vector risk multipliers).
  - It encodes objectives `(cagr, sharpe)` only; we want a 3-objective
    Pareto front: maximize Sharpe + Calmar, minimize MaxDD.

The implementation here is self-contained so the legacy optimiser is not
touched.  Determinism: ``random.Random(seed)`` is the only RNG, and the
backtester itself is deterministic, so identical seeds produce identical
Pareto fronts.

Default parameter space is anchored on §27.1 defaults with engineering
guardrails (no zero windows, monotone vol thresholds, sum-to-1 weights).
"""

from __future__ import annotations

import logging
import random
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from tide.tide_backtest import TideBacktester, TideStrategyParams
from tide.tide_metrics import compute_report

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Search space definition
# ────────────────────────────────────────────────────────────────────────

@dataclass
class ParamSpec:
    """A single tunable parameter and its sampling spec.

    ``kind`` selects the sampler:
        - "int":     uniform int in [low, high]
        - "float":   uniform float in [low, high], rounded to ``decimals``
        - "frac3":   3-vector summing to 1 (for ``lsi_weights``)
        - "vol3":    3 ascending floats (for ``vol_regime_thresholds``)
        - "rmult4":  4 floats in [0, 1] descending (LOW≥NORMAL≥HIGH≥CRISIS)
        - "choice":  uniform draw from ``choices`` list

    For the "choice" kind, set ``choices`` to the list of allowed values;
    ``low``, ``high``, and ``decimals`` are ignored.
    """
    name: str
    kind: str
    low: float = 0.0
    high: float = 0.0
    decimals: int = 4
    choices: List = field(default_factory=list)  # used for kind=="choice"


@dataclass
class TideOptimiserConfig:
    """Search space + GA hyperparameters for Tide optimisation."""

    population_size: int = 20
    generations: int = 12
    crossover_rate: float = 0.8
    mutation_rate: float = 0.3
    seed: int = 42

    # Default search space — sized to be useful but cheap.  Override per
    # study by passing a custom ``param_space``.
    param_space: List[ParamSpec] = field(default_factory=lambda: [
        ParamSpec("vol_window", "int", 20, 240),
        ParamSpec("eta_window", "int", 20, 240),
        ParamSpec("lsi_window", "int", 60, 720),
        ParamSpec("eta_threshold", "float", 0.05, 0.7, 2),
        ParamSpec("bias_deadband", "float", 0.0, 50.0, 1),
        ParamSpec("vol_regime_thresholds", "vol3"),
        ParamSpec("lsi_reduce_threshold", "float", 0.5, 4.0, 2),
        ParamSpec("lsi_reduce_slope", "float", 0.0, 0.5, 3),
        ParamSpec("risk_mult_by_regime", "rmult4"),
        ParamSpec("rebalance_threshold", "float", 0.0, 0.05, 4),
        # Time horizon — sweep 1h / 4h / 12h / 1d in a single optimisation run.
        ParamSpec("timeframe", "choice", choices=["1h", "4h", "12h", "1d"]),
    ])

    # When True (default) and any ParamSpec has kind="choice" with name
    # "timeframe", the optimiser caches the 1m data once and resamples per
    # individual.  If the input data is already at the target resolution,
    # set this to False.
    multi_timeframe: bool = True


# ────────────────────────────────────────────────────────────────────────
# Sampling and mutation helpers (all driven by an injected RNG → seedable)
# ────────────────────────────────────────────────────────────────────────

def _sample_one(spec: ParamSpec, rng: random.Random):
    if spec.kind == "int":
        return rng.randint(int(spec.low), int(spec.high))
    if spec.kind == "float":
        return round(rng.uniform(spec.low, spec.high), spec.decimals)
    if spec.kind == "choice":
        if not spec.choices:
            raise ValueError(f"ParamSpec '{spec.name}' kind='choice' has empty choices")
        return rng.choice(spec.choices)
    if spec.kind == "frac3":
        a, b, c = rng.random(), rng.random(), rng.random()
        s = a + b + c
        if s == 0:
            return (1 / 3, 1 / 3, 1 / 3)
        return (round(a / s, 4), round(b / s, 4), round(1 - a / s - b / s, 4))
    if spec.kind == "vol3":
        # ascending 3-vector from a reasonable range for ann. crypto vol
        a = round(rng.uniform(0.1, 0.6), 2)
        b = round(rng.uniform(a + 0.05, max(a + 0.05, 1.2)), 2)
        c = round(rng.uniform(b + 0.05, max(b + 0.05, 2.5)), 2)
        return (a, b, c)
    if spec.kind == "rmult4":
        a = round(rng.uniform(0.7, 1.0), 2)   # LOW
        b = round(rng.uniform(0.4, a), 2)     # NORMAL
        c = round(rng.uniform(0.0, b), 2)     # HIGH
        d = round(rng.uniform(0.0, max(c, 0.0)), 2)  # CRISIS
        return (a, b, c, d)
    raise ValueError(f"unknown kind '{spec.kind}'")


def _mutate_one(spec: ParamSpec, current, rng: random.Random):
    """Random ±50% perturbation, clamped to spec bounds and structural
    invariants.  Each ``spec.kind`` keeps the appropriate constraints."""
    if spec.kind == "int":
        delta = int(rng.uniform(-1, 1) * max(1, (spec.high - spec.low) * 0.25))
        v = max(int(spec.low), min(int(spec.high), int(current) + delta))
        return v
    if spec.kind == "float":
        delta = rng.uniform(-1, 1) * (spec.high - spec.low) * 0.25
        v = max(spec.low, min(spec.high, float(current) + delta))
        return round(v, spec.decimals)
    if spec.kind == "choice":
        if not spec.choices:
            return current
        return rng.choice(spec.choices)
    if spec.kind == "frac3":
        a, b, c = current
        a = max(0.0, a + rng.uniform(-0.1, 0.1))
        b = max(0.0, b + rng.uniform(-0.1, 0.1))
        c = max(0.0, c + rng.uniform(-0.1, 0.1))
        s = a + b + c
        if s == 0:
            return (1 / 3, 1 / 3, 1 / 3)
        return (round(a / s, 4), round(b / s, 4), round(1 - a / s - b / s, 4))
    if spec.kind == "vol3":
        a, b, c = current
        a = max(0.05, a + rng.uniform(-0.1, 0.1))
        b = max(a + 0.05, b + rng.uniform(-0.1, 0.1))
        c = max(b + 0.05, c + rng.uniform(-0.1, 0.1))
        return (round(a, 2), round(b, 2), round(c, 2))
    if spec.kind == "rmult4":
        a, b, c, d = current
        a = max(0.5, min(1.0, a + rng.uniform(-0.1, 0.1)))
        b = max(0.0, min(a, b + rng.uniform(-0.1, 0.1)))
        c = max(0.0, min(b, c + rng.uniform(-0.1, 0.1)))
        d = max(0.0, min(max(c, 0.0), d + rng.uniform(-0.1, 0.1)))
        return (round(a, 2), round(b, 2), round(c, 2), round(d, 2))
    raise ValueError(f"unknown kind '{spec.kind}'")


def _apply(params: TideStrategyParams, name: str, value) -> TideStrategyParams:
    if name == "timeframe":
        return params.with_timeframe(str(value))
    new = deepcopy(params)
    setattr(new, name, value)
    return new


# ────────────────────────────────────────────────────────────────────────
# Individual + Pareto sorting
# ────────────────────────────────────────────────────────────────────────

@dataclass
class TideIndividual:
    """One candidate parameter set evaluated against the data."""
    params: TideStrategyParams
    sharpe: float = 0.0
    calmar: float = 0.0
    max_dd: float = 0.0           # negative number; closer to 0 is better
    cagr: float = 0.0
    num_trades: int = 0
    final_equity: float = 0.0
    rank: int = -1
    crowding_distance: float = 0.0

    def objectives(self) -> Tuple[float, float, float]:
        """Return objectives in *maximization* form."""
        return (self.sharpe, self.calmar, -abs(self.max_dd))

    def dominates(self, other: "TideIndividual") -> bool:
        a = self.objectives()
        b = other.objectives()
        return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def _non_dominated_sort(pop: List[TideIndividual]) -> List[List[TideIndividual]]:
    """Standard NSGA-II fast non-dominated sort."""
    fronts: List[List[TideIndividual]] = [[]]
    n = [0] * len(pop)
    S: List[List[int]] = [[] for _ in pop]
    for p_idx, p in enumerate(pop):
        for q_idx, q in enumerate(pop):
            if p_idx == q_idx:
                continue
            if p.dominates(q):
                S[p_idx].append(q_idx)
            elif q.dominates(p):
                n[p_idx] += 1
        if n[p_idx] == 0:
            p.rank = 0
            fronts[0].append(p)
    i = 0
    while fronts[i]:
        next_front: List[TideIndividual] = []
        for p in fronts[i]:
            p_idx = pop.index(p)
            for q_idx in S[p_idx]:
                n[q_idx] -= 1
                if n[q_idx] == 0:
                    pop[q_idx].rank = i + 1
                    next_front.append(pop[q_idx])
        i += 1
        fronts.append(next_front)
    return [f for f in fronts if f]


def _crowding_distance(front: List[TideIndividual]) -> None:
    if len(front) <= 2:
        for ind in front:
            ind.crowding_distance = float("inf")
        return
    for ind in front:
        ind.crowding_distance = 0.0
    for k in range(3):  # three objectives
        front_sorted = sorted(front, key=lambda x: x.objectives()[k])
        front_sorted[0].crowding_distance = float("inf")
        front_sorted[-1].crowding_distance = float("inf")
        f_min = front_sorted[0].objectives()[k]
        f_max = front_sorted[-1].objectives()[k]
        denom = f_max - f_min if f_max > f_min else 1.0
        for j in range(1, len(front_sorted) - 1):
            front_sorted[j].crowding_distance += (
                (front_sorted[j + 1].objectives()[k]
                 - front_sorted[j - 1].objectives()[k]) / denom
            )


def _tournament(pop: List[TideIndividual], rng: random.Random) -> TideIndividual:
    a, b = rng.sample(pop, 2)
    if a.rank != b.rank:
        return a if a.rank < b.rank else b
    return a if a.crowding_distance > b.crowding_distance else b


# ────────────────────────────────────────────────────────────────────────
# Optimiser
# ────────────────────────────────────────────────────────────────────────

class TideOptimiser:
    """Multi-objective Tide parameter search.

    Usage
    -----
    >>> opt = TideOptimiser(base_params, ohlcv, config=TideOptimiserConfig())
    >>> front = opt.run()                       # Pareto-optimal individuals
    >>> for ind in front[:5]: print(ind.sharpe, ind.calmar, ind.max_dd)

    Multi-timeframe optimisation
    ----------------------------
    Include ``ParamSpec("timeframe", "choice", choices=["1h","4h","12h","1d"])``
    in the param space (default config already does this).

    Pass the 1-minute OHLCV frame and set ``config.multi_timeframe=True``
    (default).  The optimiser caches resampled frames keyed by timeframe,
    so each individual sees consistent data at its chosen resolution.

    If your input data is already at a fixed resolution (e.g. 4h), set
    ``config.multi_timeframe=False`` to skip resampling.

    Reproducibility
    ---------------
    Identical ``(base_params, ohlcv, config.seed)`` produces an identical
    Pareto front.  Backtester and engine are deterministic; the only
    stochastic component is the GA itself, which is seeded.
    """

    def __init__(
        self,
        base_params: TideStrategyParams,
        ohlcv: pd.DataFrame,
        *,
        config: TideOptimiserConfig | None = None,
        progress: Optional[Callable[[int, int, List[TideIndividual]], None]] = None,
    ):
        self._base = base_params
        self._data = ohlcv
        self._cfg = config or TideOptimiserConfig()
        self._rng = random.Random(self._cfg.seed)
        self._progress = progress
        # Lazy-populated resampled cache: {timeframe: pd.DataFrame}
        self._tf_cache: Dict[str, pd.DataFrame] = {}

    # ── Public ────────────────────────────────────────────────────

    def run(self) -> List[TideIndividual]:
        """Run the GA and return the final Pareto front (rank 0)."""
        cfg = self._cfg
        pop = self._initial_population()
        self._evaluate(pop)
        fronts = _non_dominated_sort(pop)
        for f in fronts:
            _crowding_distance(f)

        for g in range(cfg.generations):
            children = self._make_children(pop)
            self._evaluate(children)
            combined = pop + children
            fronts = _non_dominated_sort(combined)
            for f in fronts:
                _crowding_distance(f)

            new_pop: List[TideIndividual] = []
            for f in fronts:
                if len(new_pop) + len(f) <= cfg.population_size:
                    new_pop.extend(f)
                else:
                    f_sorted = sorted(
                        f, key=lambda x: x.crowding_distance, reverse=True
                    )
                    new_pop.extend(f_sorted[: cfg.population_size - len(new_pop)])
                    break
            pop = new_pop

            if self._progress:
                self._progress(g + 1, cfg.generations, pop)
            else:
                best = max(pop, key=lambda x: x.sharpe)
                logger.info(
                    "[TideOpt] gen %d/%d  pop=%d  best Sharpe=%.3f Calmar=%.3f "
                    "MDD=%.2f%% CAGR=%.2f%% trades=%d",
                    g + 1, cfg.generations, len(pop),
                    best.sharpe, best.calmar, best.max_dd * 100,
                    best.cagr * 100, best.num_trades,
                )

        fronts = _non_dominated_sort(pop)
        for f in fronts:
            _crowding_distance(f)
        return fronts[0] if fronts else []

    def all_evaluated_individuals(
        self, front: List[TideIndividual], top_k: int | None = None
    ) -> List[TideIndividual]:
        """Convenience: return the front sorted by Sharpe (desc)."""
        ordered = sorted(front, key=lambda x: x.sharpe, reverse=True)
        if top_k is not None:
            ordered = ordered[:top_k]
        return ordered

    # ── Internals ─────────────────────────────────────────────────

    def _initial_population(self) -> List[TideIndividual]:
        out: List[TideIndividual] = []
        # Seed first slot with the user-provided base params (anchor).
        out.append(TideIndividual(params=deepcopy(self._base)))
        while len(out) < self._cfg.population_size:
            params = deepcopy(self._base)
            for spec in self._cfg.param_space:
                params = _apply(params, spec.name, _sample_one(spec, self._rng))
            out.append(TideIndividual(params=params))
        return out

    def _make_children(self, pop: List[TideIndividual]) -> List[TideIndividual]:
        children: List[TideIndividual] = []
        seen: set[str] = set()
        max_attempts = self._cfg.population_size * 10
        attempts = 0
        while len(children) < self._cfg.population_size and attempts < max_attempts:
            attempts += 1
            p1 = _tournament(pop, self._rng)
            p2 = _tournament(pop, self._rng)
            child_params = self._crossover(p1.params, p2.params)
            child_params = self._mutate(child_params)
            key = self._signature(child_params)
            if key in seen:
                continue
            seen.add(key)
            children.append(TideIndividual(params=child_params))
        return children

    def _crossover(
        self, a: TideStrategyParams, b: TideStrategyParams
    ) -> TideStrategyParams:
        new = deepcopy(a)
        for spec in self._cfg.param_space:
            if self._rng.random() < self._cfg.crossover_rate:
                new = _apply(new, spec.name, getattr(b, spec.name))
        return new

    def _mutate(self, params: TideStrategyParams) -> TideStrategyParams:
        new = deepcopy(params)
        for spec in self._cfg.param_space:
            if self._rng.random() < self._cfg.mutation_rate:
                cur = getattr(new, spec.name)
                new = _apply(new, spec.name, _mutate_one(spec, cur, self._rng))
        return new

    @staticmethod
    def _signature(p: TideStrategyParams) -> str:
        return "|".join(f"{k}={getattr(p, k)}" for k in [
            "timeframe", "vol_window", "eta_window", "lsi_window",
            "eta_threshold", "bias_deadband", "vol_regime_thresholds",
            "lsi_reduce_threshold", "lsi_reduce_slope",
            "risk_mult_by_regime", "rebalance_threshold",
        ])

    def _get_data(self, tf: str) -> pd.DataFrame:
        """Return data resampled to `tf`, using a lazy cache."""
        if tf in self._tf_cache:
            return self._tf_cache[tf]
        if not self._cfg.multi_timeframe:
            return self._data
        try:
            from utils import resample_timeframe
            resampled = resample_timeframe(self._data, tf).dropna(subset=["close"])
        except Exception:
            # Fall back to raw data (e.g. already at the right resolution)
            resampled = self._data
        self._tf_cache[tf] = resampled
        return resampled

    def _evaluate(self, pop: List[TideIndividual]) -> None:
        for ind in pop:
            try:
                data = self._get_data(ind.params.timeframe)
                # Disable accuracy in optimiser eval loop for speed.
                from dataclasses import replace
                fast_params = replace(ind.params, accuracy_horizons=())
                bt = TideBacktester(fast_params).run(data)
                rep = compute_report(bt)
                ind.sharpe = float(rep.sharpe)
                ind.calmar = float(rep.calmar)
                ind.max_dd = float(rep.max_drawdown)
                ind.cagr = float(rep.cagr)
                ind.num_trades = int(rep.num_rebalances)
                ind.final_equity = float(rep.final_equity)
            except Exception as exc:
                logger.warning(
                    "TideOptimiser eval failed for params=%s: %s",
                    ind.params, exc
                )
                ind.sharpe = float("-inf")
                ind.calmar = float("-inf")
                ind.max_dd = -1.0
                ind.cagr = float("-inf")
                ind.num_trades = 0
                ind.final_equity = 0.0


# ────────────────────────────────────────────────────────────────────────
# Pretty-print helper
# ────────────────────────────────────────────────────────────────────────

def format_pareto_front(front: List[TideIndividual], top_k: int = 10) -> str:
    """Pretty-print the top-K Pareto-optimal individuals."""
    if not front:
        return "(empty Pareto front)"
    ordered = sorted(front, key=lambda x: x.sharpe, reverse=True)[:top_k]
    lines = [
        "rank | sharpe | calmar |  max_dd  |   cagr  | rebal |  tf "
        "| vol_w eta_w lsi_w | vol_reg_thr | risk_mult | lsi_thr lsi_slp"
    ]
    for i, ind in enumerate(ordered, 1):
        p = ind.params
        lines.append(
            f"{i:>4} | {ind.sharpe:>6.3f} | {ind.calmar:>6.3f} | "
            f"{ind.max_dd * 100:>7.2f}% | {ind.cagr * 100:>6.2f}% | "
            f"{ind.num_trades:>5} | {p.timeframe:>3} "
            f"| {p.vol_window:>4} {p.eta_window:>4} {p.lsi_window:>4} | "
            f"{tuple(round(x, 2) for x in p.vol_regime_thresholds)!r} | "
            f"{tuple(round(x, 2) for x in p.risk_mult_by_regime)!r} | "
            f"{p.lsi_reduce_threshold:>5.2f}  {p.lsi_reduce_slope:>5.3f}"
        )
    return "\n".join(lines)
