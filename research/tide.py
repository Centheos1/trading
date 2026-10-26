"""Causal entry-time risk constraints for the H5 Tide experiment.

This module is deliberately separate from the frozen V1 ``tide`` package.
It applies only a risk-authorisation overlay to an already-frozen H4 parent
position; it does not originate trades or choose an opportunity allocator.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

from research.clock import as_utc_index

logger = logging.getLogger(__name__)

POLICIES = (
    "static_budget",
    "volatility_scaled_budget",
    "stress_scaled_budget",
)


@dataclass(frozen=True)
class RiskCalibration:
    """Training-only loss and volatility scales for one H4 horizon."""

    expected_shortfall: float
    reference_volatility: float
    observations: int


@dataclass(frozen=True)
class ConstraintCounts:
    """Every reason the entry-time overlay changed or refused exposure."""

    observations: int
    es_binds: int
    margin_binds: int
    unavailable_risk_estimates: int
    liquidation_events: int


@dataclass(frozen=True)
class ConstraintResult:
    """Constrained position path and auditable point-in-time diagnostics."""

    position: pd.Series
    diagnostics: pd.DataFrame
    counts: ConstraintCounts


def historical_expected_shortfall(
    returns: pd.Series | np.ndarray,
    confidence: float,
) -> float:
    """Positive historical loss ES at a confidence in ``(0.5, 1)``."""
    if not 0.5 < confidence < 1.0 or not math.isfinite(confidence):
        raise ValueError(f"ES confidence must be finite and in (0.5, 1), got {confidence}")
    values = np.asarray(returns, dtype=float)
    if values.ndim != 1:
        raise ValueError("ES returns must be one-dimensional")
    if not np.isfinite(values).all():
        raise ValueError("ES returns contain a non-finite value")
    if values.size < 2:
        raise ValueError(f"ES needs at least two returns, got {values.size}")
    losses = -values
    value_at_risk = float(np.quantile(losses, confidence))
    tail = losses[losses >= value_at_risk]
    if tail.size == 0:
        raise RuntimeError("ES tail is empty after a finite quantile")
    expected_shortfall = float(np.mean(tail))
    if not math.isfinite(expected_shortfall):
        raise RuntimeError("ES estimate is non-finite")
    return max(expected_shortfall, 0.0)


def fit_risk_calibration(
    horizon_returns: pd.Series,
    minute_returns: pd.Series,
    *,
    confidence: float,
) -> RiskCalibration:
    """Fit H5 risk scales from one origin's training sample only."""
    if horizon_returns.empty or minute_returns.empty:
        raise ValueError("risk calibration needs horizon and minute returns")
    expected_shortfall = historical_expected_shortfall(horizon_returns, confidence)
    minute_values = minute_returns.to_numpy(dtype=float)
    if not np.isfinite(minute_values).all() or len(minute_values) < 2:
        raise ValueError("risk calibration minute returns must be finite with at least two rows")
    reference_volatility = float(np.std(minute_values, ddof=1))
    if not math.isfinite(reference_volatility) or reference_volatility <= 0.0:
        raise ValueError("risk calibration reference volatility must be finite and positive")
    if expected_shortfall <= 0.0:
        raise ValueError("risk calibration expected shortfall must be positive")
    return RiskCalibration(
        expected_shortfall=expected_shortfall,
        reference_volatility=reference_volatility,
        observations=int(len(horizon_returns)),
    )


def causal_volatility_ratio(
    minute_returns: pd.Series,
    decision_index: pd.Index,
    *,
    lookback_minutes: int,
    reference_volatility: float,
    ratio_floor: float,
    ratio_cap: float,
) -> pd.Series:
    """Trailing volatility known at each close, relative to the training scale."""
    if lookback_minutes < 2:
        raise ValueError("volatility lookback must cover at least two completed returns")
    if not math.isfinite(reference_volatility) or reference_volatility <= 0.0:
        raise ValueError("reference volatility must be finite and positive")
    if not 0.0 < ratio_floor <= ratio_cap:
        raise ValueError("volatility ratio bounds must satisfy 0 < floor <= cap")
    source_index = as_utc_index(minute_returns.index)
    values = pd.Series(minute_returns.to_numpy(dtype=float), index=source_index).sort_index()
    if values.index.has_duplicates or not np.isfinite(values.to_numpy()).all():
        raise ValueError("minute returns must have unique timestamps and finite values")
    # A t→t+1 return is known at the t+1 close, never at its decision timestamp t.
    known = pd.Series(values.to_numpy(), index=values.index + pd.Timedelta(minutes=1))
    volatility = known.rolling(
        lookback_minutes,
        min_periods=lookback_minutes,
    ).std(ddof=1)
    span = known.index.to_series().diff(lookback_minutes - 1)
    volatility = volatility.where(span.eq(pd.Timedelta(minutes=lookback_minutes - 1)))
    ratio = volatility / reference_volatility
    ratio = ratio.clip(lower=ratio_floor, upper=ratio_cap)
    decisions = as_utc_index(decision_index)
    result = ratio.reindex(decisions)
    result.name = "volatility_ratio"
    return result


def apply_entry_constraint(
    parent_position: pd.Series,
    volatility_ratio: pd.Series,
    calibration: RiskCalibration,
    *,
    policy: str,
    budget_fraction: float,
    stress_trigger_ratio: float,
    stress_budget_multiplier: float,
    max_gross: float,
) -> ConstraintResult:
    """Apply one registered H5 budget without increasing parent exposure."""
    if policy not in POLICIES:
        raise ValueError(f"unknown H5 budget policy {policy!r}")
    if not 0.0 < budget_fraction <= 1.0:
        raise ValueError("budget fraction must be in (0, 1]")
    if stress_trigger_ratio <= 0.0 or not math.isfinite(stress_trigger_ratio):
        raise ValueError("stress trigger ratio must be finite and positive")
    if not 0.0 < stress_budget_multiplier <= 1.0:
        raise ValueError("stress budget multiplier must be in (0, 1]")
    if max_gross <= 0.0 or not math.isfinite(max_gross):
        raise ValueError("maximum gross exposure must be finite and positive")
    index = as_utc_index(parent_position.index)
    parent = pd.Series(parent_position.to_numpy(dtype=float), index=index, name="parent_position")
    if parent.index.has_duplicates or not np.isfinite(parent.to_numpy()).all():
        raise ValueError("parent positions must have unique timestamps and finite values")
    ratio = volatility_ratio.reindex(index)
    unavailable = ratio.isna() | ~np.isfinite(ratio)
    safe_ratio = ratio.where(~unavailable, 0.0)
    estimated_es = parent.abs() * calibration.expected_shortfall * safe_ratio
    base_budget = calibration.expected_shortfall * budget_fraction
    if policy == "static_budget":
        budget = pd.Series(base_budget, index=index)
    else:
        budget = base_budget * safe_ratio
        if policy == "stress_scaled_budget":
            budget = budget.where(
                safe_ratio < stress_trigger_ratio,
                budget * stress_budget_multiplier,
            )
    multiplier = pd.Series(1.0, index=index)
    positive_risk = estimated_es > 0.0
    multiplier.loc[positive_risk] = np.minimum(
        1.0,
        budget.loc[positive_risk] / estimated_es.loc[positive_risk],
    )
    multiplier.loc[unavailable] = 0.0
    es_binds = (multiplier < 1.0) & (parent != 0.0) & ~unavailable
    constrained = parent * multiplier
    clipped = constrained.clip(lower=-max_gross, upper=max_gross)
    margin_binds = ~np.isclose(clipped.to_numpy(), constrained.to_numpy())
    constrained = pd.Series(clipped, index=index, name=policy)
    if bool((constrained.abs() - parent.abs() > 1e-12).any()):
        raise RuntimeError("H5 constraint increased parent exposure")
    if int(es_binds.sum()) or int(np.sum(margin_binds)) or int(unavailable.sum()):
        logger.warning(
            "H5 %s constraints: es_binds=%d margin_binds=%d unavailable=%d",
            policy,
            int(es_binds.sum()),
            int(np.sum(margin_binds)),
            int(unavailable.sum()),
        )
    diagnostics = pd.DataFrame(
        {
            "parent_position": parent,
            "position": constrained,
            "volatility_ratio": ratio,
            "estimated_es": estimated_es,
            "budget": budget,
            "risk_multiplier": multiplier,
            "es_bind": es_binds,
            "margin_bind": margin_binds,
            "risk_unavailable": unavailable,
        },
        index=index,
    )
    return ConstraintResult(
        position=constrained,
        diagnostics=diagnostics,
        counts=ConstraintCounts(
            observations=len(index),
            es_binds=int(es_binds.sum()),
            margin_binds=int(np.sum(margin_binds)),
            unavailable_risk_estimates=int(unavailable.sum()),
            liquidation_events=0,
        ),
    )


def liquidation_event_count(net_returns: pd.Series) -> int:
    """Count one-bar equity losses that would exhaust a 1× account."""
    values = net_returns.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("liquidation diagnostics need finite net returns")
    return int(np.sum(values <= -1.0))


def one_sided_sign_pvalue(differences: pd.Series | np.ndarray) -> float:
    """Exact one-sided sign-test p-value for positive paired differences."""
    values = np.asarray(differences, dtype=float)
    values = values[np.isfinite(values) & (values != 0.0)]
    count = int(values.size)
    if count == 0:
        return 1.0
    wins = int(np.sum(values > 0.0))
    probability = sum(math.comb(count, k) for k in range(wins, count + 1)) / (2**count)
    return float(min(max(probability, 0.0), 1.0))


def holm_adjust(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-adjust a named finite p-value family."""
    if not pvalues:
        raise ValueError("Holm adjustment needs at least one p-value")
    checked: list[tuple[str, float]] = []
    for name, value in pvalues.items():
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f"invalid p-value for {name}: {value}")
        checked.append((name, number))
    ordered = sorted(checked, key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    size = len(ordered)
    for rank, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (size - rank) * value))
        adjusted[name] = running
    return adjusted


def normal_approximation_pvalue(mean: float, standard_error: float) -> float:
    """One-sided normal diagnostic, kept separate from the primary sign test."""
    if standard_error <= 0.0 or not math.isfinite(standard_error):
        return 1.0
    z_score = mean / standard_error
    return float(1.0 - NormalDist().cdf(z_score))
