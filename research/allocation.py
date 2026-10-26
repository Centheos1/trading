"""Decision-time allocation for the H6 RORAC experiment.

Tide supplies the retained ES budget. This module only turns a frozen
directional decision into a size. It does not choose the parent signal
or reopen the H5 policy ladder.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.registry import ExperimentRecord
from research.wave import FirstPassageForecast

logger = logging.getLogger(__name__)

SIZERS = ("equal_notional", "volatility_targeted", "rorac")
CONTROLS = ("equal_notional", "volatility_targeted")
CANDIDATE = "rorac"
DEFAULT_SIZER = "volatility_targeted"


@dataclass(frozen=True)
class AllocationCounts:
    """Every sizing adjustment that changed or refused exposure."""

    observations: int
    unavailable_estimates: int
    denominator_floors: int
    gross_caps: int


@dataclass(frozen=True)
class AllocationResult:
    """Sized position and the point-in-time inputs that produced it."""

    position: pd.Series
    diagnostics: pd.DataFrame
    counts: AllocationCounts


def expected_barrier_payoff(
    barrier_width: float,
    target_probability: float,
    stop_probability: float,
    round_trip_cost: float,
) -> float:
    """Direction-specific expected barrier payoff, floored at zero."""
    width = float(barrier_width)
    target = float(target_probability)
    stop = float(stop_probability)
    cost = float(round_trip_cost)
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError("barrier width must be finite and positive")
    for name, probability in (("target", target), ("stop", stop)):
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"{name} probability must be finite and in [0, 1]")
    if not math.isfinite(cost) or cost < 0.0:
        raise ValueError("round-trip cost must be finite and non-negative")
    return max(0.0, width * (target - stop) - cost)


def directional_opportunity(
    barrier_width: pd.Series,
    signal: pd.Series,
    forecast: FirstPassageForecast,
    *,
    round_trip_cost: float,
) -> pd.Series:
    """Use the long payoff for a long signal and the short payoff for a short signal."""
    width = _finite_series(barrier_width, "barrier width")
    if not np.isfinite(width.to_numpy(dtype=float)).all() or (width <= 0.0).any():
        raise ValueError("barrier widths must be finite and positive")
    direction = _signal(signal).reindex(width.index)
    if bool(direction.isna().any()):
        raise ValueError("signal is missing decisions present in the barrier width")
    long_payoff = _vector_payoff(
        width,
        forecast.long_target,
        forecast.long_stop,
        round_trip_cost,
    )
    short_payoff = _vector_payoff(
        width,
        forecast.short_target,
        forecast.short_stop,
        round_trip_cost,
    )
    opportunity = pd.Series(0.0, index=width.index, name="opportunity")
    opportunity.loc[direction > 0.0] = long_payoff.loc[direction > 0.0]
    opportunity.loc[direction < 0.0] = short_payoff.loc[direction < 0.0]
    return opportunity


def rorac_ratio(
    opportunity: pd.Series,
    unit_expected_shortfall: pd.Series,
    *,
    denominator_floor: float,
) -> tuple[pd.Series, pd.Series]:
    """Divide floored opportunity by unit ES, lifting only tiny positive denominators."""
    if not math.isfinite(denominator_floor) or denominator_floor <= 0.0:
        raise ValueError("RORAC denominator floor must be finite and positive")
    payoff = _finite_series(opportunity, "opportunity")
    if (payoff < 0.0).any():
        raise ValueError("RORAC opportunity must be floored at zero")
    risk = _aligned_risk(unit_expected_shortfall, payoff.index)
    unavailable = risk.isna() | ~np.isfinite(risk)
    negative = ~unavailable & (risk < 0.0)
    if bool(negative.any()):
        raise ValueError("unit expected shortfall must be non-negative")
    floors = ~unavailable & (risk < denominator_floor)
    denominator = risk.where(~floors, denominator_floor)
    ratio = pd.Series(np.nan, index=payoff.index, name="rorac")
    available = ~unavailable
    ratio.loc[available] = payoff.loc[available] / denominator.loc[available]
    if int(unavailable.sum()) or int(floors.sum()):
        logger.warning(
            "H6 RORAC inputs: unavailable=%d denominator_floors=%d",
            int(unavailable.sum()),
            int(floors.sum()),
        )
    floor_events = pd.Series(floors, index=payoff.index, name="denominator_floor")
    return ratio, floor_events


def training_rorac_normaliser(ratios: pd.Series, *, quantile: float) -> float:
    """Training-only linear quantile of positive finite RORAC values."""
    if not isinstance(ratios, pd.Series):
        raise ValueError("training RORAC must be a Series")
    if not math.isfinite(quantile) or not 0.0 < quantile < 1.0:
        raise ValueError("RORAC normaliser quantile must be in (0, 1)")
    values = ratios.to_numpy(dtype=float)
    if np.isinf(values).any():
        raise ValueError("training RORAC contains a non-finite value")
    positive = values[np.isfinite(values) & (values > 0.0)]
    if positive.size < 1:
        raise ValueError("training RORAC normaliser needs a positive ratio")
    scale = float(np.quantile(positive, quantile))
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("training RORAC normaliser must be finite and positive")
    return scale


def size_position(
    signal: pd.Series,
    *,
    sizer: str,
    max_gross: float,
    volatility_scale: pd.Series | None = None,
    rorac: pd.Series | None = None,
    normaliser: float | None = None,
    denominator_floor_events: pd.Series | None = None,
) -> AllocationResult:
    """Size one frozen direction with a registered H6 sizer."""
    if sizer not in SIZERS:
        raise ValueError(f"unknown H6 sizer {sizer!r}")
    if not math.isfinite(max_gross) or max_gross <= 0.0:
        raise ValueError("maximum gross exposure must be finite and positive")
    direction = _signal(signal)
    unavailable = pd.Series(False, index=direction.index)
    floors = pd.Series(False, index=direction.index)
    ratio = pd.Series(0.0, index=direction.index, name="rorac")
    if sizer == "equal_notional":
        position = direction * max_gross
    elif sizer == "volatility_targeted":
        if volatility_scale is None:
            raise ValueError("volatility-targeted sizing needs a volatility scale")
        scale = _finite_series(volatility_scale, "volatility scale", allow_nan=True).reindex(
            direction.index
        )
        unavailable = scale.isna() | ~np.isfinite(scale)
        if int(unavailable.sum()):
            logger.warning(
                "H6 volatility-targeted sizing has %d missing scales; positions are zero",
                int(unavailable.sum()),
            )
        position = direction * scale.fillna(0.0).clip(lower=0.0, upper=max_gross)
    else:
        if rorac is None or normaliser is None:
            raise ValueError("RORAC sizing needs a ratio and a training normaliser")
        if not math.isfinite(normaliser) or normaliser <= 0.0:
            raise ValueError("RORAC normaliser must be finite and positive")
        ratio = _finite_series(rorac, "RORAC", allow_nan=True).reindex(direction.index)
        unavailable = ratio.isna() | ~np.isfinite(ratio)
        if denominator_floor_events is not None:
            floors = (
                denominator_floor_events.reindex(direction.index).fillna(False).astype(bool)
            )
        magnitude = (ratio / normaliser).clip(lower=0.0, upper=1.0) * max_gross
        magnitude = magnitude.where(~unavailable, 0.0)
        position = direction * magnitude.fillna(0.0)
        if int(unavailable.sum()):
            logger.warning(
                "H6 RORAC sizing has %d unavailable ratios; positions are zero",
                int(unavailable.sum()),
            )
    position = pd.Series(position.to_numpy(dtype=float), index=direction.index, name=sizer)
    if not np.isfinite(position.to_numpy(dtype=float)).all():
        raise RuntimeError(f"H6 {sizer} produced a non-finite position")
    if bool((position.abs() - max_gross > 1e-12).any()):
        raise RuntimeError("H6 sizer exceeded the gross cap")
    gross_caps = (direction != 0.0) & ~unavailable & (position.abs() >= max_gross - 1e-12)
    if sizer != CANDIDATE:
        gross_caps = pd.Series(False, index=direction.index)
    if int(gross_caps.sum()):
        logger.warning("H6 RORAC gross caps=%d", int(gross_caps.sum()))
    diagnostics = pd.DataFrame(
        {
            "signal": direction,
            "position": position,
            "rorac": ratio.fillna(np.nan),
            "unavailable": unavailable,
            "denominator_floor": floors,
            "gross_cap": gross_caps,
        },
        index=direction.index,
    )
    return AllocationResult(
        position=position,
        diagnostics=diagnostics,
        counts=AllocationCounts(
            observations=len(direction),
            unavailable_estimates=int(unavailable.sum()),
            denominator_floors=int(floors.sum()),
            gross_caps=int(gross_caps.sum()),
        ),
    )


def retained_sizer(record: ExperimentRecord | None) -> str:
    """Keep volatility targeting until H6 itself is sealed and retained."""
    if (
        record is not None
        and record.experiment_id == "EXP-H06"
        and record.status == "Research validated"
        and record.decision == "retain"
        and record.sealed_access
        and record.result == CANDIDATE
    ):
        return CANDIDATE
    return DEFAULT_SIZER


def _signal(signal: pd.Series) -> pd.Series:
    values = _finite_series(signal, "signal")
    if not np.isin(values.to_numpy(dtype=float), (-1.0, 0.0, 1.0)).all():
        raise ValueError("H6 signals must be -1, 0, or 1")
    return pd.Series(values.to_numpy(dtype=float), index=values.index, name="signal")


def _aligned_risk(risk: pd.Series, index: pd.Index) -> pd.Series:
    values = risk.copy()
    values.index = as_utc_index(values.index)
    if values.index.has_duplicates:
        raise ValueError("unit expected shortfall has duplicate timestamps")
    return values.reindex(index)


def _finite_series(values: pd.Series, label: str, *, allow_nan: bool = False) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise ValueError(f"{label} must be a Series")
    index = as_utc_index(values.index)
    series = pd.Series(values.to_numpy(dtype=float), index=index)
    if series.index.has_duplicates:
        raise ValueError(f"{label} has duplicate timestamps")
    numbers = series.to_numpy(dtype=float)
    invalid = np.isinf(numbers) if allow_nan else ~np.isfinite(numbers)
    if bool(np.any(invalid)):
        raise ValueError(f"{label} contains a non-finite value")
    return series


def _vector_payoff(
    barrier_width: pd.Series,
    target_probability: float,
    stop_probability: float,
    round_trip_cost: float,
) -> pd.Series:
    """Same payoff as ``expected_barrier_payoff``, applied to one barrier path."""
    expected_barrier_payoff(1.0, target_probability, stop_probability, round_trip_cost)
    edge = float(target_probability) - float(stop_probability)
    values = np.maximum(
        barrier_width.to_numpy(dtype=float) * edge - float(round_trip_cost),
        0.0,
    )
    return pd.Series(values, index=barrier_width.index)
