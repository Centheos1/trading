"""
Wave feature computations from L1 OHLCV bars — strategy.md §8.5.

Supplies all inputs the WaveEngine needs for its regime state machine (§8.8):

  η  — trend efficiency       §8.5.4   rolling |ΣΔP| / Σ|ΔP|
  D  — dispersion proxy       §8.5.2   V1 single-symbol: rolling std(log_returns)
                                        normalised to a plausible cross-sectional range
  AR — absorption ratio proxy §8.5.3   V1 single-symbol: vol-of-vol ratio normalised [0,1]
  VWAP — rolling VWAP         §8.5.5   typical_price × volume / Σvolume
  structure_high/low          §8.5.5   rolling max/min of close

V1 notes
────────
True cross-sectional dispersion (§8.5.2) and absorption ratio (§8.5.3) require
N-asset return panels.  V1 works in single-symbol mode and injects proxies that
have the same qualitative behaviour (high when regimes are unstable).  The
WaveEngine's `set_dispersion()` and `set_absorption_ratio()` injection points
accept these proxies unchanged.

Multi-factor PCA and residual dislocation are V3 per the strategy roadmap.

All computations are pure functions for deterministic replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _rolling_std(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=max(2, window // 4)).std()


def _rolling_mean(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=max(1, window // 4)).mean()


# ────────────────────────────────────────────────────────────────────────
# §8.5.4  Trend efficiency η
# ────────────────────────────────────────────────────────────────────────

def rolling_trend_efficiency(close: pd.Series, *, window: int) -> pd.Series:
    """Rolling η = |ΣΔP| / Σ|ΔP| over a window of close prices.

    η → 1  when price moves in one direction (strong trend / breakout)
    η → 0  when price oscillates (mean-reversion environment)

    Parameters
    ----------
    close : pd.Series  Close prices.
    window : int       Number of bars in the rolling window.
    """
    if len(close) < 2:
        return pd.Series(np.zeros(len(close)), index=close.index)

    diff = close.diff()

    def _eta(arr: np.ndarray) -> float:
        gross = np.abs(arr).sum()
        if gross == 0.0:
            return 0.0
        return min(abs(arr.sum()) / gross, 1.0)

    return diff.rolling(window, min_periods=max(2, window // 4)).apply(
        _eta, raw=True
    ).fillna(0.0)


# ────────────────────────────────────────────────────────────────────────
# §8.5.2  Dispersion proxy (V1 single-symbol)
# ────────────────────────────────────────────────────────────────────────

def rolling_dispersion_proxy(
    close: pd.Series,
    *,
    window: int,
    scale: float = 0.1,
) -> pd.Series:
    """V1 single-symbol proxy for cross-sectional dispersion D.

    True dispersion = sqrt(mean((r_i - r_bar)²)) across N assets.
    In single-symbol mode we approximate it as:

        D_proxy_t = rolling_std(log_return_t, window)

    This captures "how wildly are bar-level returns varying?"  When the crypto
    market is in a risk-off/dislocated regime individual assets have large
    idiosyncratic swings → higher intra-bar vol → higher proxy.

    The result is normalised by ``scale`` (annualised vol equiv ≈ 10% for scale=0.1)
    so the value sits in the same [0, ~1] range the WaveEngine expects.

    Parameters
    ----------
    window : int    Rolling window in bars.
    scale  : float  Normalisation divisor (roughly one annualised vol unit).
    """
    log_ret = np.log(close / close.shift(1))
    raw = _rolling_std(log_ret, window)
    # Normalise: divide by scale so ~10% bar-vol gives proxy ≈ 1
    return (raw / scale).clip(upper=2.0).fillna(0.0)


# ────────────────────────────────────────────────────────────────────────
# §8.5.3  Absorption ratio proxy (V1 single-symbol)
# ────────────────────────────────────────────────────────────────────────

def rolling_ar_proxy(
    close: pd.Series,
    *,
    window: int,
    fast_window: int | None = None,
) -> pd.Series:
    """V1 single-symbol proxy for the correlation absorption ratio AR.

    True AR = Σλ_i(1..k) / Σλ_i(all), where λ are eigenvalues of the
    rolling pairwise correlation matrix across N assets.

    High AR → one dominant factor drives most variance → correlated market.
    Low AR  → idiosyncratic dispersion → breakdown / instability likely.

    V1 proxy: vol-of-vol ratio using two windows.

        fast_vol  = std(log_ret, fast_window)
        slow_vol  = std(log_ret, window)
        ar_proxy  = clip( fast_vol / slow_vol, 0, 1 )

    When recent vol (fast) > long-run vol (slow) we are in an unusual state
    → higher absorption → higher BREAKDOWN probability, matching AR semantics.

    Parameters
    ----------
    window      : int          Slow/long window in bars.
    fast_window : int | None   Fast window; defaults to window // 4.
    """
    fast = fast_window or max(2, window // 4)
    log_ret = np.log(close / close.shift(1))
    fast_std = _rolling_std(log_ret, fast)
    slow_std = _rolling_std(log_ret, window)
    # ar_proxy: recent vol / long-run vol, clipped to [0, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = fast_std / slow_std.replace(0.0, np.nan)
    return ratio.clip(lower=0.0, upper=1.0).fillna(0.5)


# ────────────────────────────────────────────────────────────────────────
# §8.5.5  VWAP and structural levels
# ────────────────────────────────────────────────────────────────────────

def rolling_vwap(
    df: pd.DataFrame,
    *,
    window: int,
) -> pd.Series:
    """Rolling VWAP over ``window`` bars.

    VWAP_t = Σ( typical_price_i × volume_i ) / Σ volume_i
           where typical_price = (high + low + close) / 3
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0.0, np.nan)
    tp_vol = (tp * vol).rolling(window, min_periods=1).sum()
    vol_sum = vol.rolling(window, min_periods=1).sum()
    return (tp_vol / vol_sum).fillna(df["close"])


def rolling_structure_high(close: pd.Series, *, window: int) -> pd.Series:
    """Rolling maximum of close over ``window`` bars (session high proxy)."""
    return close.rolling(window, min_periods=1).max()


def rolling_structure_low(close: pd.Series, *, window: int) -> pd.Series:
    """Rolling minimum of close over ``window`` bars (session low proxy)."""
    return close.rolling(window, min_periods=1).min()


def distance_vwap(close: pd.Series, vwap: pd.Series) -> pd.Series:
    """§8.5.5 — δ_vwap = (P − VWAP) / P."""
    with np.errstate(divide="ignore", invalid="ignore"):
        d = (close - vwap) / close.replace(0.0, np.nan)
    return d.fillna(0.0)


# ────────────────────────────────────────────────────────────────────────
# Feature params
# ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WaveFeatureParams:
    """All tunable parameters for Wave feature computation.

    These feed into WaveConfig thresholds via the WaveEngine injection API.
    """
    eta_window: int = 30            # bars for rolling trend efficiency
    vwap_window: int = 24           # bars for rolling VWAP
    structure_window: int = 24      # bars for rolling high/low (structure levels)
    disp_window: int = 30           # bars for dispersion proxy
    ar_window: int = 60             # bars (slow) for AR proxy
    disp_scale: float = 0.05        # normalisation for dispersion proxy


# ────────────────────────────────────────────────────────────────────────
# Convenience builder
# ────────────────────────────────────────────────────────────────────────

def build_wave_feature_frame(
    df: pd.DataFrame,
    params: WaveFeatureParams | None = None,
) -> pd.DataFrame:
    """Compute all Wave features from an OHLCV bar DataFrame.

    Parameters
    ----------
    df     : pd.DataFrame  OHLCV with DatetimeIndex, columns open/high/low/close/volume.
    params : WaveFeatureParams | None  Defaults used if None.

    Returns
    -------
    pd.DataFrame with columns:
        close, log_return,
        eta             (trend efficiency)
        dispersion      (V1 proxy)
        ar              (absorption ratio proxy)
        vwap            (rolling VWAP)
        structure_high  (rolling high)
        structure_low   (rolling low)
        distance_vwap   (δ_vwap)
    """
    p = params or WaveFeatureParams()
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"build_wave_feature_frame: missing columns {missing}")

    close = df["close"].astype(float)

    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["log_return"] = np.log(close / close.shift(1)).fillna(0.0)
    out["eta"] = rolling_trend_efficiency(close, window=p.eta_window)
    out["dispersion"] = rolling_dispersion_proxy(
        close, window=p.disp_window, scale=p.disp_scale
    )
    out["ar"] = rolling_ar_proxy(close, window=p.ar_window)
    vwap = rolling_vwap(df, window=p.vwap_window)
    out["vwap"] = vwap
    out["structure_high"] = rolling_structure_high(close, window=p.structure_window)
    out["structure_low"] = rolling_structure_low(close, window=p.structure_window)
    out["distance_vwap"] = distance_vwap(close, vwap)

    return out
