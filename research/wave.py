"""Causal probability objects and frozen decision rules for H4 Wave research."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.distributions import GaussianModel, StudentTModel, fit_gaussian, fit_student_t

logger = logging.getLogger(__name__)

_OUTCOMES = (-1, 0, 1)
_REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class BarrierBuild:
    """First-passage outcomes and every reason a decision was unavailable."""

    labels: pd.DataFrame
    gap_exclusions: int
    volatility_exclusions: int
    endpoint_exclusions: int
    intrabar_ties: int


@dataclass(frozen=True)
class FirstPassageForecast:
    """Smoothed target, stop, and expiry probabilities for both directions."""

    long_target: float
    long_stop: float
    long_expiry: float
    short_target: float
    short_stop: float
    short_expiry: float
    observations: int


@dataclass(frozen=True)
class ProbabilityScore:
    """Binary probability calibration for one declared event."""

    observations: int
    probability: float
    event_rate: float
    brier_score: float
    log_loss: float


def normalise_ohlc(bars: pd.DataFrame) -> pd.DataFrame:
    """Validate sorted UTC OHLC bars without silently repairing them."""
    missing = set(_REQUIRED_BAR_COLUMNS) - set(bars.columns)
    if missing:
        raise ValueError(f"Wave bars are missing OHLC columns: {sorted(missing)}")
    if bars.empty:
        raise ValueError("Wave bars are empty")
    frame = bars.loc[:, _REQUIRED_BAR_COLUMNS].copy()
    frame.index = as_utc_index(frame.index)
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("Wave bars contain duplicate timestamps")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise ValueError("Wave bars contain non-positive or non-finite OHLC values")
    if (
        (frame["high"] < frame[["open", "close"]].max(axis=1)).any()
        or (frame["low"] > frame[["open", "close"]].min(axis=1)).any()
        or (frame["high"] < frame["low"]).any()
    ):
        raise ValueError("Wave bars contain inconsistent OHLC values")
    return frame


def trailing_log_volatility(close: pd.Series, lookback_minutes: int) -> pd.Series:
    """Sample volatility of exact one-minute returns known at each close."""
    if lookback_minutes < 2:
        raise ValueError("barrier volatility lookback must cover at least two minutes")
    index = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=index).sort_index()
    previous = values.shift(1)
    exact = values.index.to_series().diff().eq(pd.Timedelta(minutes=1))
    returns = np.log(values / previous).where(exact)
    volatility = returns.rolling(lookback_minutes, min_periods=lookback_minutes).std(ddof=1)
    span = values.index.to_series().diff(lookback_minutes)
    volatility = volatility.where(span.eq(pd.Timedelta(minutes=lookback_minutes)))
    volatility.name = "trailing_log_volatility"
    return volatility


def build_barrier_labels(
    bars: pd.DataFrame,
    horizon_minutes: int,
    *,
    volatility_lookback_minutes: int,
    barrier_sigma: float,
    intrabar_rule: str,
) -> BarrierBuild:
    """Build long and short target-before-stop outcomes from future OHLC bars.

    Outcomes are ``1`` for target first, ``-1`` for stop first, and ``0`` for
    expiry. If both levels trade in one minute, the registered conservative
    rule assigns the stop. A missing minute before resolution invalidates and
    counts the decision instead of converting it into a shorter path.
    """
    if horizon_minutes < 1:
        raise ValueError(f"barrier horizon must be positive, got {horizon_minutes}")
    if barrier_sigma <= 0.0 or not math.isfinite(barrier_sigma):
        raise ValueError(f"barrier sigma must be finite and positive, got {barrier_sigma}")
    if intrabar_rule != "stop_first":
        raise ValueError(f"H4 intrabar rule must be stop_first, got {intrabar_rule!r}")
    frame = normalise_ohlc(bars)
    index = frame.index
    volatility = trailing_log_volatility(frame["close"], volatility_lookback_minutes)
    width = volatility * math.sqrt(horizon_minutes) * barrier_sigma
    endpoint = index + pd.Timedelta(minutes=horizon_minutes)
    endpoint_available = endpoint <= index[-1]
    vol_available = np.isfinite(width.to_numpy(dtype=float)) & (width.to_numpy(dtype=float) > 0.0)
    valid = endpoint_available & vol_available
    active_long = valid.copy()
    active_short = valid.copy()
    long_outcome = np.zeros(len(frame), dtype=np.int8)
    short_outcome = np.zeros(len(frame), dtype=np.int8)
    ties = 0
    gap_exclusions = 0
    entry = frame["close"].to_numpy(dtype=float)
    upper = entry * np.exp(width.fillna(0.0).to_numpy(dtype=float))
    lower = entry * np.exp(-width.fillna(0.0).to_numpy(dtype=float))
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)

    for minute in range(1, horizon_minutes + 1):
        if not bool(active_long.any() or active_short.any()):
            break
        future_high = np.full(len(frame), np.nan)
        future_low = np.full(len(frame), np.nan)
        exact = (index[minute:] - index[:-minute]) == pd.Timedelta(minutes=minute)
        positions = np.flatnonzero(exact)
        future_high[positions] = high[positions + minute]
        future_low[positions] = low[positions + minute]
        missing = (active_long | active_short) & (
            ~np.isfinite(future_high) | ~np.isfinite(future_low)
        )
        if bool(missing.any()):
            gap_exclusions += int(missing.sum())
            valid[missing] = False
            active_long[missing] = False
            active_short[missing] = False

        long_target = active_long & (future_high >= upper)
        long_stop = active_long & (future_low <= lower)
        short_target = active_short & (future_low <= lower)
        short_stop = active_short & (future_high >= upper)
        both = (long_target & long_stop) | (short_target & short_stop)
        ties += int(both.sum())

        long_resolved = long_target | long_stop
        short_resolved = short_target | short_stop
        long_outcome[long_target & ~long_stop] = 1
        long_outcome[long_stop] = -1
        short_outcome[short_target & ~short_stop] = 1
        short_outcome[short_stop] = -1
        active_long &= ~long_resolved
        active_short &= ~short_resolved

    labels = pd.DataFrame(
        {
            "long_outcome": long_outcome[valid],
            "short_outcome": short_outcome[valid],
            "barrier_log_width": width.to_numpy(dtype=float)[valid],
        },
        index=index[valid],
    )
    labels.index.name = "decision_time"
    volatility_exclusions = int((endpoint_available & ~vol_available).sum())
    endpoint_exclusions = int((~endpoint_available).sum())
    if gap_exclusions or volatility_exclusions or endpoint_exclusions or ties:
        logger.warning(
            "H4 %dm barrier labels exclusions: gaps=%d volatility=%d endpoints=%d "
            "stop_first_ties=%d",
            horizon_minutes,
            gap_exclusions,
            volatility_exclusions,
            endpoint_exclusions,
            ties,
        )
    if labels.empty:
        raise RuntimeError(f"H4 {horizon_minutes}-minute barrier build kept no labels")
    return BarrierBuild(
        labels=labels,
        gap_exclusions=gap_exclusions,
        volatility_exclusions=volatility_exclusions,
        endpoint_exclusions=endpoint_exclusions,
        intrabar_ties=ties,
    )


def select_label_window(
    labels: pd.DataFrame,
    horizon_minutes: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Select decisions whose complete horizon is known inside ``[start, end)``."""
    if labels.empty:
        raise ValueError("label window cannot select from an empty frame")
    index = as_utc_index(labels.index)
    start_utc = pd.Timestamp(start).tz_convert("UTC")
    end_utc = pd.Timestamp(end).tz_convert("UTC")
    known = index + pd.Timedelta(minutes=horizon_minutes)
    selected = (index >= start_utc) & (known < end_utc)
    return labels.loc[selected].copy()


def fit_first_passage(
    labels: pd.DataFrame,
    *,
    smoothing: float,
) -> FirstPassageForecast:
    """Fit a deterministic smoothed categorical forecast to training labels."""
    if smoothing <= 0.0 or not math.isfinite(smoothing):
        raise ValueError(f"probability smoothing must be finite and positive, got {smoothing}")
    required = {"long_outcome", "short_outcome"}
    missing = required - set(labels.columns)
    if missing:
        raise ValueError(f"first-passage labels are missing columns: {sorted(missing)}")
    if labels.empty:
        raise ValueError("first-passage fit needs training labels")
    probabilities: dict[str, float] = {}
    for direction in ("long", "short"):
        values = labels[f"{direction}_outcome"].to_numpy(dtype=int)
        unexpected = sorted(set(values) - set(_OUTCOMES))
        if unexpected:
            raise ValueError(f"{direction} outcomes contain invalid values: {unexpected}")
        denominator = len(values) + smoothing * len(_OUTCOMES)
        probabilities[f"{direction}_stop"] = (int(np.sum(values == -1)) + smoothing) / denominator
        probabilities[f"{direction}_expiry"] = (int(np.sum(values == 0)) + smoothing) / denominator
        probabilities[f"{direction}_target"] = (int(np.sum(values == 1)) + smoothing) / denominator
    return FirstPassageForecast(observations=len(labels), **probabilities)


def fit_terminal_density(values: pd.Series, family: str) -> GaussianModel | StudentTModel:
    """Fit the H1-selected unconditional terminal-return family."""
    sample = values.to_numpy(dtype=float)
    if family == "gaussian":
        return fit_gaussian(sample)
    if family == "student_t":
        return fit_student_t(sample)
    raise ValueError(f"H4 needs an H1 gaussian or student_t family, got {family!r}")


def score_probability(probability: float, events: pd.Series | np.ndarray) -> ProbabilityScore:
    """Score one frozen probability with Brier score and binary log loss."""
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability must be finite and in [0, 1], got {probability}")
    values = np.asarray(events, dtype=float)
    if values.ndim != 1 or len(values) < 2:
        raise ValueError("probability scoring needs at least two one-dimensional events")
    if not np.isin(values, (0.0, 1.0)).all():
        raise ValueError("probability events must be binary")
    clipped = float(np.clip(probability, 1e-12, 1.0 - 1e-12))
    brier = float(np.mean((probability - values) ** 2))
    log_loss = float(-np.mean(values * math.log(clipped) + (1.0 - values) * math.log(1.0 - clipped)))
    return ProbabilityScore(
        observations=len(values),
        probability=float(probability),
        event_rate=float(values.mean()),
        brier_score=brier,
        log_loss=log_loss,
    )


def terminal_up_probability(model: GaussianModel | StudentTModel) -> float:
    """Probability that the terminal log return is positive."""
    probability = 1.0 - float(model.cdf(np.asarray([0.0]))[0])
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise RuntimeError(f"terminal model produced invalid up probability {probability}")
    return probability


def first_passage_signals(
    barrier_width: pd.Series,
    forecast: FirstPassageForecast,
    *,
    cost_bps: float,
    round_trip_cost_multiplier: float,
) -> tuple[pd.Series, pd.Series]:
    """Return probability-aware and hard-label directional signals.

    The probability-aware signal may abstain when neither side's expected
    barrier payoff clears the frozen round-trip cost. The hard-label control
    always selects the side with the larger target-minus-stop probability.
    """
    if cost_bps < 0.0 or round_trip_cost_multiplier <= 0.0:
        raise ValueError("cost must be non-negative and its multiplier positive")
    width = barrier_width.astype(float)
    if width.empty or not np.isfinite(width.to_numpy()).all() or (width <= 0.0).any():
        raise ValueError("barrier widths must be finite and positive")
    cost = cost_bps / 10_000.0 * round_trip_cost_multiplier
    long_edge = width * (forecast.long_target - forecast.long_stop) - cost
    short_edge = width * (forecast.short_target - forecast.short_stop) - cost
    candidate = pd.Series(0.0, index=width.index, name="first_passage_probability")
    candidate.loc[(long_edge > short_edge) & (long_edge > 0.0)] = 1.0
    candidate.loc[(short_edge > long_edge) & (short_edge > 0.0)] = -1.0
    hard_direction = (
        1.0
        if forecast.long_target - forecast.long_stop
        >= forecast.short_target - forecast.short_stop
        else -1.0
    )
    hard = pd.Series(hard_direction, index=width.index, name="hard_label_wave")
    return candidate, hard


def terminal_signals(
    index: pd.Index,
    model: GaussianModel | StudentTModel,
    *,
    cost_bps: float,
    round_trip_cost_multiplier: float,
) -> pd.Series:
    """Map the frozen terminal distribution mean to long, short, or NO-TRADE."""
    if cost_bps < 0.0 or round_trip_cost_multiplier <= 0.0:
        raise ValueError("cost must be non-negative and its multiplier positive")
    expected = float(model.mean if isinstance(model, GaussianModel) else model.loc)
    threshold = cost_bps / 10_000.0 * round_trip_cost_multiplier
    signal = 1.0 if expected > threshold else -1.0 if expected < -threshold else 0.0
    return pd.Series(signal, index=as_utc_index(index), name="terminal_return_probability")
