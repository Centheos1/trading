"""
Tide feature computations from L1 OHLCV bars.

Implements the deterministic V1 Tide features from strategy.md §7.4:
  - Realized volatility               (§7.4.1)
  - Directional efficiency ratio η    (§7.4.2)
  - Liquidity Stress Index proxy      (§7.4.3, L1-only proxy)
  - Volatility regime classification  (§7.4 + §27.1 thresholds)
  - Bias derivation from net return + η

Tide MUST NOT depend on L2 data (AGENT_STRATEGY_RULES §5.1).  All features
here use only OHLCV bars.  The LSI proxy substitutes spread/depth/impact
inputs (which Tide does not see) with three OHLCV-derived stress signals
(bar range vs. baseline, vol-of-vol, volume z-score) — they share the same
qualitative property (z-scored, positive when stressed) and feed the same
§7.4.7 risk-multiplier reduction logic.

All computations are pure functions over arrays / DataFrames so they are
trivially testable and replay-deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from schemas import TideBias, VolRegime


# Continuous-market annualization factor: minutes per crypto trading year.
MINUTES_PER_YEAR_CONTINUOUS: float = 365.0 * 24.0 * 60.0


# ────────────────────────────────────────────────────────────────────────
# §7.4.1  Realized volatility
# ────────────────────────────────────────────────────────────────────────

def realized_volatility(
    log_returns: Sequence[float] | np.ndarray | pd.Series,
    *,
    bar_seconds: float,
    annualization_seconds: float = 365.0 * 24.0 * 3600.0,
) -> float:
    """Annualized realized volatility from a window of log returns.

    σ̂_realized = sqrt( (1/N) Σ rᵢ² ) · sqrt(T / Δt)

    Parameters
    ----------
    log_returns : iterable of float
        Log returns sampled at ``bar_seconds`` intervals.
    bar_seconds : float
        Bar width in seconds (Δt).
    annualization_seconds : float
        Seconds in the annualization horizon (T).  Default = continuous
        crypto year (365 d × 24 h × 3600 s).

    Returns
    -------
    float
        Annualized realized volatility.  ``0.0`` if input is empty.
    """
    arr = np.asarray(log_returns, dtype=np.float64)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0 or bar_seconds <= 0.0:
        return 0.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    ann = float(np.sqrt(annualization_seconds / bar_seconds))
    return rms * ann


def rolling_realized_volatility(
    close: pd.Series,
    *,
    window: int,
    bar_seconds: float,
    annualization_seconds: float = 365.0 * 24.0 * 3600.0,
) -> pd.Series:
    """Rolling annualized realized vol indexed identically to ``close``.

    Uses log returns; values for the first ``window`` bars are NaN.
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    log_rets = np.log(close).diff()
    sq = log_rets ** 2
    rms = np.sqrt(sq.rolling(window=window, min_periods=window).mean())
    ann = float(np.sqrt(annualization_seconds / bar_seconds))
    return rms * ann


# ────────────────────────────────────────────────────────────────────────
# §7.4.2  Directional efficiency ratio η  +  signed net move
# ────────────────────────────────────────────────────────────────────────

def directional_efficiency(prices: Sequence[float] | np.ndarray | pd.Series) -> float:
    """η = |Σ ΔPᵢ| / Σ |ΔPᵢ|, with η ∈ [0, 1].

    Returns 0.0 on insufficient data or zero gross movement.
    """
    arr = np.asarray(prices, dtype=np.float64)
    if arr.size < 2:
        return 0.0
    diffs = np.diff(arr)
    gross = float(np.sum(np.abs(diffs)))
    if gross == 0.0:
        return 0.0
    return float(abs(np.sum(diffs))) / gross


def signed_net_move(prices: Sequence[float] | np.ndarray | pd.Series) -> float:
    """Net cumulative price change Σ ΔPᵢ over the window (sign carries direction)."""
    arr = np.asarray(prices, dtype=np.float64)
    if arr.size < 2:
        return 0.0
    return float(arr[-1] - arr[0])


def rolling_directional_efficiency(close: pd.Series, *, window: int) -> pd.Series:
    """Rolling η, NaN for the first ``window-1`` bars."""
    if window < 2:
        raise ValueError("window must be >= 2")
    diff = close.diff()
    abs_sum = diff.abs().rolling(window=window, min_periods=window).sum()
    net_sum = diff.rolling(window=window, min_periods=window).sum()
    eta = (net_sum.abs() / abs_sum).where(abs_sum > 0.0, 0.0)
    eta = eta.where(abs_sum.notna(), np.nan)
    return eta.clip(0.0, 1.0)


def rolling_signed_net_move(close: pd.Series, *, window: int) -> pd.Series:
    """Rolling Σ ΔPᵢ; NaN for first ``window-1`` bars."""
    if window < 2:
        raise ValueError("window must be >= 2")
    return close.diff().rolling(window=window, min_periods=window).sum()


# ────────────────────────────────────────────────────────────────────────
# §7.4.3  Liquidity Stress Index — L1-only proxy for Tide
# ────────────────────────────────────────────────────────────────────────
#
# The strategy doc defines LSI_tide on (spread, depth, impact).  Tide is
# explicitly forbidden from consuming L2 data (§5.1), so the canonical
# inputs are not directly available at this layer.  We construct a
# functionally-equivalent proxy from OHLCV alone:
#
#   stress₁ = z((H − L) / C)              — intrabar range stress
#   stress₂ = z(rolling_std(realized_vol)) — vol-of-vol (regime instability)
#   stress₃ = z(volume)                    — volume z-score (panic/illiquidity)
#
# Each component is z-scored against a rolling baseline so positive values
# mean "stressed".  The composite uses ``TideConfig.lsi_weights`` exactly
# as the §7.4.3 formula prescribes.  Default weights sum to 1.0.
#
# This proxy is plugged into the same §7.4.7 risk-multiplier logic, so the
# Tide engine code path is unchanged — only the input source differs.
# ────────────────────────────────────────────────────────────────────────

def _zscore(series: pd.Series, *, window: int) -> pd.Series:
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    z = (series - mean) / std
    return z.where(std > 0.0, 0.0)


def liquidity_stress_proxy(
    df: pd.DataFrame,
    *,
    window: int,
    weights: Sequence[float] = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
    bar_seconds: float,
    realized_vol_window: int = 60,
) -> pd.Series:
    """Compute the Tide Liquidity Stress Index (§7.4.3) proxy from OHLCV.

    Required columns: ``high``, ``low``, ``close``, ``volume``.
    Returns a Series aligned to ``df.index``; NaN until enough warmup.
    """
    required = {"high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"liquidity_stress_proxy: missing columns {missing}")
    if len(weights) != 3:
        raise ValueError("weights must have length 3")
    if window < 2:
        raise ValueError("window must be >= 2")

    rng_norm = (df["high"] - df["low"]) / df["close"].replace(0.0, np.nan)
    rng_norm = rng_norm.fillna(0.0)
    z_range = _zscore(rng_norm, window=window)

    rv = rolling_realized_volatility(
        df["close"], window=realized_vol_window, bar_seconds=bar_seconds
    )
    vol_of_vol = rv.rolling(window=window, min_periods=window).std(ddof=0)
    z_vov = _zscore(vol_of_vol.ffill(), window=window)

    z_volume = _zscore(df["volume"].astype(float), window=window)

    w1, w2, w3 = (float(w) for w in weights)
    lsi = w1 * z_range + w2 * z_vov + w3 * z_volume
    return lsi


# ────────────────────────────────────────────────────────────────────────
# Volatility regime classification  (§7.4, §27.1)
# ────────────────────────────────────────────────────────────────────────

def classify_vol_regime(
    realized_vol: float,
    thresholds: Sequence[float] = (0.4, 0.8, 1.5),
) -> VolRegime:
    """Map an annualized realized vol to a discrete regime.

    Thresholds are ``[LOW/NORMAL, NORMAL/HIGH, HIGH/CRISIS]`` (ascending).
    Defaults match ``TideConfig.vol_regime_thresholds``.
    """
    if len(thresholds) != 3:
        raise ValueError("thresholds must have length 3")
    t_lo, t_mid, t_hi = (float(x) for x in thresholds)
    if not (t_lo <= t_mid <= t_hi):
        raise ValueError("thresholds must be ascending")
    if not np.isfinite(realized_vol) or realized_vol < 0.0:
        return VolRegime.NORMAL
    if realized_vol < t_lo:
        return VolRegime.LOW
    if realized_vol < t_mid:
        return VolRegime.NORMAL
    if realized_vol < t_hi:
        return VolRegime.HIGH
    return VolRegime.CRISIS


def classify_vol_regime_series(
    realized_vol: pd.Series,
    thresholds: Sequence[float] = (0.4, 0.8, 1.5),
) -> pd.Series:
    """Vectorized vol-regime classification over a Series.

    Returns a Series of ``VolRegime`` values aligned to input index.  NaN
    inputs map to ``VolRegime.NORMAL`` to keep the strategy operable
    during warmup (matches DEFAULT_TIDE_SNAPSHOT in schemas.py).
    """
    if len(thresholds) != 3:
        raise ValueError("thresholds must have length 3")
    t_lo, t_mid, t_hi = (float(x) for x in thresholds)
    out = np.empty(len(realized_vol), dtype=object)
    vals = realized_vol.to_numpy()
    for i, v in enumerate(vals):
        if not np.isfinite(v) or v < 0.0:
            out[i] = VolRegime.NORMAL
        elif v < t_lo:
            out[i] = VolRegime.LOW
        elif v < t_mid:
            out[i] = VolRegime.NORMAL
        elif v < t_hi:
            out[i] = VolRegime.HIGH
        else:
            out[i] = VolRegime.CRISIS
    return pd.Series(out, index=realized_vol.index, name="vol_regime")


# ────────────────────────────────────────────────────────────────────────
# Bias derivation
# ────────────────────────────────────────────────────────────────────────
#
# The strategy doc lists candidate Tide bias features (crypto beta return,
# funding, OI change) but does not specify a single deterministic V1 rule
# for choosing LONG / SHORT / NEUTRAL.  We implement the most natural
# rule consistent with the spec:
#
#   bias = LONG  if η > eta_threshold AND net_move > 0
#   bias = SHORT if η > eta_threshold AND net_move < 0
#   bias = NEUTRAL otherwise
#
# Rationale: η > θ marks "trending" conditions; the sign of the net move
# carries direction.  Below θ the move is mean-reverting / unclear, so
# Tide should hold no directional opinion.  Both inputs come from the
# rolling η window already needed for §7.4.2.
# ────────────────────────────────────────────────────────────────────────

def derive_bias(
    eta: float,
    net_move: float,
    *,
    eta_threshold: float = 0.3,
    deadband: float = 0.0,
) -> TideBias:
    """Derive a TideBias from directional efficiency + net move.

    Parameters
    ----------
    eta : float
        Directional efficiency ratio η ∈ [0, 1] over the bias window.
    net_move : float
        Signed net move over the same window (price units).
    eta_threshold : float
        Minimum η required to express a directional view.  Below this,
        bias is NEUTRAL.
    deadband : float
        Minimum |net_move| required to call a side.  Below this, bias is
        NEUTRAL even if η is high.  Default 0 = sign-only.
    """
    if not np.isfinite(eta) or eta < eta_threshold:
        return TideBias.NEUTRAL
    if not np.isfinite(net_move) or abs(net_move) <= deadband:
        return TideBias.NEUTRAL
    return TideBias.LONG if net_move > 0.0 else TideBias.SHORT


def derive_bias_series(
    eta: pd.Series,
    net_move: pd.Series,
    *,
    eta_threshold: float = 0.3,
    deadband: float = 0.0,
) -> pd.Series:
    """Vectorized bias derivation."""
    out = np.full(len(eta), TideBias.NEUTRAL, dtype=object)
    eta_v = eta.to_numpy()
    nm_v = net_move.to_numpy()
    for i in range(len(out)):
        e, n = eta_v[i], nm_v[i]
        if not np.isfinite(e) or e < eta_threshold:
            continue
        if not np.isfinite(n) or abs(n) <= deadband:
            continue
        out[i] = TideBias.LONG if n > 0.0 else TideBias.SHORT
    return pd.Series(out, index=eta.index, name="bias")


# ────────────────────────────────────────────────────────────────────────
# Convenience: build the full Tide feature frame in one pass
# ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TideFeatureParams:
    """Parameters for the Tide feature pipeline."""
    vol_window: int = 60                 # bars used for realized vol
    eta_window: int = 60                 # bars used for η + net move
    lsi_window: int = 240                # bars used for LSI z-scoring baseline
    bar_seconds: float = 60.0            # bar width (default = 1m)
    vol_regime_thresholds: tuple[float, float, float] = (0.4, 0.8, 1.5)
    eta_threshold: float = 0.3
    bias_deadband: float = 0.0
    lsi_weights: tuple[float, float, float] = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    annualization_seconds: float = 365.0 * 24.0 * 3600.0


def build_tide_feature_frame(
    df: pd.DataFrame,
    params: TideFeatureParams | None = None,
) -> pd.DataFrame:
    """Compute every per-bar Tide feature and return them as a DataFrame.

    Input
    -----
    df : pd.DataFrame
        Indexed by ``DatetimeIndex`` (or any monotonically increasing
        timestamp index).  Required columns: ``open, high, low, close,
        volume``.

    Output
    ------
    pd.DataFrame with columns:
        ``log_return, realized_vol, eta, net_move, vol_regime, bias, lsi``
    Plus the original price columns retained for downstream use.
    """
    p = params or TideFeatureParams()

    if not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
        raise ValueError(
            "build_tide_feature_frame: df must contain open/high/low/close/volume"
        )
    if len(df) < max(p.vol_window, p.eta_window, p.lsi_window) + 2:
        # Allow but warn: downstream NaNs will dominate.
        pass

    out = df[["open", "high", "low", "close", "volume"]].copy()
    out["log_return"] = np.log(out["close"]).diff()
    out["realized_vol"] = rolling_realized_volatility(
        out["close"],
        window=p.vol_window,
        bar_seconds=p.bar_seconds,
        annualization_seconds=p.annualization_seconds,
    )
    out["eta"] = rolling_directional_efficiency(out["close"], window=p.eta_window)
    out["net_move"] = rolling_signed_net_move(out["close"], window=p.eta_window)
    out["vol_regime"] = classify_vol_regime_series(
        out["realized_vol"], thresholds=p.vol_regime_thresholds
    )
    out["bias"] = derive_bias_series(
        out["eta"],
        out["net_move"],
        eta_threshold=p.eta_threshold,
        deadband=p.bias_deadband,
    )
    out["lsi"] = liquidity_stress_proxy(
        out,
        window=p.lsi_window,
        weights=p.lsi_weights,
        bar_seconds=p.bar_seconds,
        realized_vol_window=p.vol_window,
    )
    return out
