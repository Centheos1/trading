"""
Wave-layer backtester — strategy.md §8.

Supports two modes
──────────────────
isolated (default)
    Wave runs standalone.  TideBias is pinned to NEUTRAL every bar.
    Use for validating Wave regime classification in isolation.

stacked
    A completed TideBacktestResult is passed in.  Tide's published bias is
    consumed bar-by-bar so Wave operates exactly as it would in production.

Position sizing (Wave-overlay strategy)
────────────────────────────────────────
Wave does not trigger trades directly — it classifies regimes and publishes
PermissionSets for Ripple.  However, to measure Wave's *economic* contribution
we run a "permission-adjusted" overlay:

    target_units_t = tide_risk_mult_t × wave_size_fraction_t × max_position × direction_t

where:
    tide_risk_mult_t = 1.0 in isolated mode, or Tide's risk_multiplier in stacked mode
    wave_size_fraction_t:
        MEAN_REVERSION  → reduced_size_fraction   (smaller, mean-rev trades)
        BREAKOUT        → 1.0                      (full size trend-following)
        BREAKDOWN       → 0.0                      (all trades off)
        NEUTRAL         → reduced_size_fraction    (conservative default)
    direction_t: driven by Tide bias (stacked) or fixed NEUTRAL (isolated = 0)

In isolated mode with TideBias=NEUTRAL the wave_size_fraction proxy is the
only signal, so direction is derived from the regime:
    BREAKOUT  → sign from whether close is above VWAP (+1) or below (−1)
    MEAN_REVERSION → opposite sign (fade)
    BREAKDOWN / NEUTRAL → 0

This gives a clean PnL signal for regime accuracy assessment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from schemas import TideBias, WaveRegime, WaveConfig
from wave.wave_engine import WaveEngine
from wave.wave_features import WaveFeatureParams, build_wave_feature_frame

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Timeframe helpers (mirrors tide_backtest.py)
# ────────────────────────────────────────────────────────────────────────

_TF_SECONDS: dict[str, float] = {
    "1m": 60.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
    "1h": 3600.0, "4h": 14400.0, "12h": 43200.0, "1d": 86400.0,
}


class SizingMode(str, Enum):
    USD = "usd"
    BASE = "base"


# ────────────────────────────────────────────────────────────────────────
# Parameter container
# ────────────────────────────────────────────────────────────────────────

@dataclass
class WaveStrategyParams:
    """All tunable parameters for a Wave-overlay backtest run.

    Feature pipeline
    ─────────────────
    eta_window, vwap_window, structure_window, disp_window, ar_window,
    disp_scale — forwarded to WaveFeatureParams.

    Wave engine
    ────────────
    eta_mr_threshold, eta_bo_threshold, eta_neutral_threshold,
    dispersion_threshold, dispersion_critical, ar_critical, ar_recover,
    reduced_size_fraction — forwarded to WaveConfig.

    Portfolio / sizing (same semantics as TideStrategyParams)
    ──────────────────
    sizing_mode, initial_capital, max_position_usd, max_position_base,
    leverage, taker_fee_bps, maker_fee_bps, use_taker_fees,
    rebalance_threshold.

    Metadata
    ─────────
    symbol, exchange, timeframe → bar_seconds.
    mode: "isolated" | "stacked".
    """
    # ── Feature pipeline ─────────────────────────────────────────────────
    # Defaults target 5-minute bars (strategy.md §5.1: Wave horizon =
    # "minutes to hours", update cadence = "seconds to minutes",
    # minimum update_interval_ms = 5000).
    # 5m bars give a practical OHLCV approximation of the 5s cadence.
    # At 5m bars, eta_window=24 → 2h rolling look-back (Wave territory).
    eta_window: int = 24           # 24 × 5min = 2h trend-efficiency window
    vwap_window: int = 24          # 2h rolling VWAP
    structure_window: int = 24     # 2h rolling high/low structure
    disp_window: int = 24          # 2h dispersion proxy
    ar_window: int = 48            # 4h absorption-ratio slow window
    disp_scale: float = 0.05

    # ── Wave engine thresholds ────────────────────────────────────────────
    # Reasonable starting point; the optimiser tunes per timeframe.
    eta_mr_threshold: float = 0.15    # below p25 → genuinely choppy
    eta_bo_threshold: float = 0.40    # near p85 → strong directional move
    eta_neutral_threshold: float = 0.28
    dispersion_threshold: float = 0.12
    dispersion_critical: float = 0.22
    ar_critical: float = 0.95
    ar_recover: float = 0.65
    reduced_size_fraction: float = 0.5
    update_interval_ms: int = 0       # 0 = update every bar

    # ── Portfolio / sizing ────────────────────────────────────────────────
    sizing_mode: SizingMode = SizingMode.BASE
    initial_capital: float = 10_000.0
    max_position_usd: float = 10_000.0
    max_position_base: float = 1.0
    leverage: float = 1.0
    taker_fee_bps: float = 4.0
    maker_fee_bps: float = 2.0
    use_taker_fees: bool = True
    rebalance_threshold: float = 0.0

    # ── Backtest hardening (Phase 9) ──────────────────────────────────────
    # Equity floor as a fraction of initial_capital.  When > 0 and equity
    # drops to or below initial_capital * liquidation_equity_frac the
    # backtester forces position to 0 and stops rebalancing for the rest of
    # the run (mirrors a real margin-call / liquidation event).  Default 0.0
    # disables the floor and preserves V1 behaviour.
    liquidation_equity_frac: float = 0.0
    # Linear slippage in basis points charged on |delta| * px alongside fees.
    # Use ~1-2 bps for liquid majors as a starting point.  Default 0.0.
    slippage_bps: float = 0.0
    # Quadratic / market-impact term: extra bps per unit of |delta| relative
    # to max_position_base.  Default 0.0 disables the term.
    slippage_per_unit_bps: float = 0.0

    # ── Run metadata ──────────────────────────────────────────────────────
    symbol: str = "BTCUSDT"
    exchange: str = "binance"
    # Wave's minimum update interval is 5 000 ms; 5m bars are the closest
    # practical OHLCV resolution.  Use 1m bars for even finer resolution.
    timeframe: str = "5m"
    bar_seconds: float = 300.0
    mode: str = "isolated"          # "isolated" | "stacked"

    # ── Accuracy horizons ─────────────────────────────────────────────────
    # Expressed in *bar counts*.  At the default 5m timeframe these map to:
    #   h=1 →  5min   h=3 → 15min   h=6 → 30min
    #   h=12 → 1h     h=24 → 2h     h=48 → 4h
    # Use horizon_labels() to get human-readable strings.
    accuracy_horizons: tuple[int, ...] = (1, 3, 6, 12, 24, 48)

    def with_timeframe(
        self,
        tf: str,
        *,
        rescale_windows: bool = False,
    ) -> "WaveStrategyParams":
        """Return a copy with the timeframe (and bar_seconds) swapped.

        When ``rescale_windows`` is True, the feature-window lengths
        (``eta_window``, ``vwap_window``, ``structure_window``,
        ``disp_window``, ``ar_window``) are rescaled so their wall-clock
        coverage stays constant.  Without this flag a parameter set tuned
        for 1h bars sees windows ~12x narrower (in wall-clock terms) when
        applied at 5m, which produces materially different feature
        distributions and breaks the threshold calibration.

        Default ``rescale_windows=False`` preserves V1 behaviour for
        existing tests / pre-existing optimised parameter sets.
        """
        if tf not in _TF_SECONDS:
            raise ValueError(f"unknown timeframe '{tf}'")
        new_bar_s = _TF_SECONDS[tf]
        if not rescale_windows or self.bar_seconds == new_bar_s:
            return replace(self, timeframe=tf, bar_seconds=new_bar_s)
        # Rescale by the wall-clock ratio.  E.g. 1h -> 5m: 3600/300 = 12x.
        ratio = self.bar_seconds / new_bar_s
        return replace(
            self,
            timeframe=tf,
            bar_seconds=new_bar_s,
            eta_window=max(4, int(round(self.eta_window * ratio))),
            vwap_window=max(4, int(round(self.vwap_window * ratio))),
            structure_window=max(4, int(round(self.structure_window * ratio))),
            disp_window=max(4, int(round(self.disp_window * ratio))),
            ar_window=max(4, int(round(self.ar_window * ratio))),
        )

    def horizon_labels(self) -> list[str]:
        """Return human-readable time labels for each accuracy horizon."""
        bar_min = self.bar_seconds / 60.0
        labels = []
        for h in self.accuracy_horizons:
            mins = h * bar_min
            if mins < 60:
                labels.append(f"{int(round(mins))}min")
            elif mins < 1440:
                hrs = mins / 60.0
                labels.append(f"{hrs:.0f}h" if hrs == int(hrs) else f"{hrs:.1f}h")
            else:
                days = mins / 1440.0
                labels.append(f"{days:.1f}d")
        return labels

    def to_feature_params(self) -> WaveFeatureParams:
        return WaveFeatureParams(
            eta_window=self.eta_window,
            vwap_window=self.vwap_window,
            structure_window=self.structure_window,
            disp_window=self.disp_window,
            ar_window=self.ar_window,
            disp_scale=self.disp_scale,
        )

    def to_wave_config(self) -> WaveConfig:
        return WaveConfig(
            update_interval_ms=self.update_interval_ms,
            eta_mr_threshold=self.eta_mr_threshold,
            eta_bo_threshold=self.eta_bo_threshold,
            eta_neutral_threshold=self.eta_neutral_threshold,
            dispersion_threshold=self.dispersion_threshold,
            dispersion_critical=self.dispersion_critical,
            ar_critical=self.ar_critical,
            ar_recover=self.ar_recover,
            reduced_size_fraction=self.reduced_size_fraction,
        )


# ────────────────────────────────────────────────────────────────────────
# Result container
# ────────────────────────────────────────────────────────────────────────

@dataclass
class WaveBacktestResult:
    """Full output of a single Wave backtest run.

    bars columns:
        close, log_return,
        eta, dispersion, ar, vwap, distance_vwap,
        wave_regime (str),
        tide_bias (str),
        wave_size_fraction,
        target_units, position, fee,
        gross_pnl, net_pnl, equity, drawdown, bar_return
    trades columns:
        timestamp, prior_units, new_units, delta_units, price,
        notional, fee, wave_regime, tide_bias
    """
    params: WaveStrategyParams
    bars: pd.DataFrame
    trades: pd.DataFrame
    initial_capital: float
    final_equity: float
    mode: str
    # Bar index at which the liquidation floor was breached, or None if
    # liquidation never triggered.  Always None when
    # params.liquidation_equity_frac == 0.
    liquidated_at_bar: Optional[int] = None

    @property
    def equity_curve(self) -> pd.Series:
        return self.bars["equity"]

    @property
    def returns(self) -> pd.Series:
        return self.bars["bar_return"]

    @property
    def drawdown(self) -> pd.Series:
        return self.bars["drawdown"]

    @property
    def num_trades(self) -> int:
        return len(self.trades)


# ────────────────────────────────────────────────────────────────────────
# Regime → size fraction
# ────────────────────────────────────────────────────────────────────────

def _wave_size_fraction(regime: WaveRegime, reduced: float) -> float:
    """Translate Wave regime to a scalar position size fraction [0, 1]."""
    if regime == WaveRegime.BREAKOUT:
        return 1.0
    if regime in (WaveRegime.MEAN_REVERSION, WaveRegime.NEUTRAL):
        return reduced
    # BREAKDOWN
    return 0.0


def _isolated_direction(
    regime: WaveRegime,
    close: float,
    vwap: float,
) -> int:
    """In isolated mode (TideBias=NEUTRAL) derive a directional sign from regime.

    BREAKOUT  → above VWAP → LONG (+1), below VWAP → SHORT (−1)
    MEAN_REVERSION → fade: above VWAP → SHORT (−1), below VWAP → LONG (+1)
    BREAKDOWN / NEUTRAL → flat (0)
    """
    if regime == WaveRegime.BREAKOUT:
        return 1 if close >= vwap else -1
    if regime == WaveRegime.MEAN_REVERSION:
        return -1 if close >= vwap else 1
    return 0


# ────────────────────────────────────────────────────────────────────────
# Backtester
# ────────────────────────────────────────────────────────────────────────

class WaveBacktester:
    """Bar-by-bar Wave-layer backtester.

    Supports isolated and stacked modes (see module docstring).

    Parameters
    ----------
    params : WaveStrategyParams
    tide_result : TideBacktestResult | None
        Required when params.mode == "stacked".
    """

    def __init__(
        self,
        params: WaveStrategyParams,
        tide_result=None,
    ):
        self.params = params
        self.tide_result = tide_result
        if params.mode == "stacked" and tide_result is None:
            raise ValueError(
                "WaveBacktester: mode='stacked' requires tide_result"
            )

    # ── Public API ─────────────────────────────────────────────────────

    def run(self, ohlcv: pd.DataFrame) -> WaveBacktestResult:
        """Run the Wave backtest on an OHLCV DataFrame."""
        self._validate(ohlcv)
        feats = build_wave_feature_frame(ohlcv, self.params.to_feature_params())
        return self._run_loop(feats, ohlcv)

    # ── Internals ──────────────────────────────────────────────────────

    def _validate(self, df: pd.DataFrame) -> None:
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"ohlcv missing columns: {missing}")
        if len(df) == 0:
            raise ValueError("ohlcv is empty")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("ohlcv index must be DatetimeIndex")
        if not df.index.is_monotonic_increasing:
            raise ValueError("ohlcv index must be monotonically increasing")
        if self.params.mode == "stacked" and self.tide_result is not None:
            # Verify time-range overlap; exact timestamp alignment is not required
            # because _run_loop uses merge_asof to forward-fill Tide signals.
            tide_start = self.tide_result.bars.index.min()
            tide_end = self.tide_result.bars.index.max()
            wave_start = df.index.min()
            wave_end = df.index.max()
            if wave_start > tide_end or wave_end < tide_start:
                logger.warning(
                    "WaveBacktester: Wave bars (%s → %s) and Tide bars (%s → %s) "
                    "do not overlap; all bars will use NEUTRAL bias",
                    wave_start, wave_end, tide_start, tide_end,
                )

    def _run_loop(
        self, feats: pd.DataFrame, ohlcv: pd.DataFrame
    ) -> WaveBacktestResult:
        p = self.params
        cfg = p.to_wave_config()
        engine = WaveEngine(cfg)

        # Stacked mode: forward-fill Tide signals onto Wave bars using merge_asof.
        # This handles any resolution difference between Tide (1h) and Wave (5m etc.)
        # by assigning each Wave bar the most recent Tide signal published at or
        # before that bar's timestamp.
        tide_bias_pre: np.ndarray | None = None
        tide_rmult_pre: np.ndarray | None = None
        if p.mode == "stacked" and self.tide_result is not None:
            tb = self.tide_result.bars[["bias", "risk_mult"]].copy()
            tb = tb.sort_index()
            # Build DataFrames with a shared "ts" key for merge_asof.
            # feats.index may have any name; normalise it to "ts".
            wave_ts = feats.index.rename("ts").to_frame(index=False)
            tide_df = tb.copy()
            tide_df.index.name = "ts"
            tide_df = tide_df.reset_index()
            merged = pd.merge_asof(
                wave_ts.sort_values("ts"),
                tide_df.sort_values("ts"),
                on="ts",
                direction="backward",   # most recent Tide bar at or before Wave bar
            ).set_index("ts").reindex(feats.index)
            tide_bias_pre = merged["bias"].fillna("NEUTRAL").to_numpy(dtype=object)
            tide_rmult_pre = merged["risk_mult"].fillna(1.0).to_numpy(dtype=np.float64)

        # Set the engine's internal price deque window to cover eta_window bars
        # so that bar-resolution feeds accumulate properly (bars can be hours apart).
        bar_ms = int(p.bar_seconds * 1000)
        engine.set_price_window_ms(bar_ms * p.eta_window * 2)

        n = len(feats)
        ts_ms = (feats.index.astype("int64") // 1_000_000).to_numpy()
        close_arr = feats["close"].to_numpy(dtype=np.float64)
        eta_arr = feats["eta"].to_numpy(dtype=np.float64)
        disp_arr = feats["dispersion"].to_numpy(dtype=np.float64)
        ar_arr = feats["ar"].to_numpy(dtype=np.float64)
        vwap_arr = feats["vwap"].to_numpy(dtype=np.float64)
        sh_arr = feats["structure_high"].to_numpy(dtype=np.float64)
        sl_arr = feats["structure_low"].to_numpy(dtype=np.float64)

        # Output arrays
        wave_regime_arr = np.empty(n, dtype=object)
        tide_bias_arr = np.empty(n, dtype=object)
        size_frac_arr = np.zeros(n, dtype=np.float64)
        target_units = np.zeros(n, dtype=np.float64)
        position = np.zeros(n, dtype=np.float64)
        fee_arr = np.zeros(n, dtype=np.float64)
        gross_pnl = np.zeros(n, dtype=np.float64)
        net_pnl = np.zeros(n, dtype=np.float64)
        equity_arr = np.zeros(n, dtype=np.float64)
        bar_return = np.zeros(n, dtype=np.float64)

        cur_units = 0.0
        cur_equity = float(p.initial_capital)
        fee_rate = (p.taker_fee_bps if p.use_taker_fees else p.maker_fee_bps) / 10_000.0
        trade_records: list[dict] = []
        # Phase 9: backtest hardening — liquidation floor and slippage.
        liq_threshold = (
            float(p.initial_capital) * float(p.liquidation_equity_frac)
            if p.liquidation_equity_frac > 0.0 else None
        )
        liquidated_at: Optional[int] = None
        slip_bps = float(p.slippage_bps)
        slip_per_unit_bps = float(p.slippage_per_unit_bps)
        # Avoid div-by-zero in market-impact term; default to 1.0 if a
        # non-base sizing config left max_position_base at 0.
        max_pos_base_safe = float(p.max_position_base) if p.max_position_base > 0 else 1.0

        for i in range(n):
            ts = int(ts_ms[i])
            px = float(close_arr[i])
            # ── Tide bias (stacked) or NEUTRAL (isolated) ──────────────
            if p.mode == "stacked" and tide_bias_pre is not None:
                try:
                    tide_bias = TideBias(tide_bias_pre[i])
                except (ValueError, KeyError):
                    tide_bias = TideBias.NEUTRAL
                tide_rmult = float(tide_rmult_pre[i])
            else:
                tide_bias = TideBias.NEUTRAL
                tide_rmult = 1.0

            # ── Drive WaveEngine ───────────────────────────────────────
            engine.on_price(px, ts)
            engine.set_dispersion(float(disp_arr[i]))
            engine.set_absorption_ratio(float(ar_arr[i]))
            engine.set_session_structure(
                vwap=float(vwap_arr[i]),
                high=float(sh_arr[i]),
                low=float(sl_arr[i]),
            )
            snap = engine.update(ts, tide_bias)
            if snap is None:
                # Cadence gate fired — reuse last known regime
                regime = WaveRegime(wave_regime_arr[i - 1]) if i > 0 else WaveRegime.NEUTRAL
            else:
                regime = snap.regime

            wave_regime_arr[i] = regime.value
            tide_bias_arr[i] = tide_bias.value

            # ── Position sizing ────────────────────────────────────────
            sfrac = _wave_size_fraction(regime, p.reduced_size_fraction)
            size_frac_arr[i] = sfrac

            if p.mode == "stacked":
                # Tide provides direction (bias → sign)
                _BIAS_SIGN = {TideBias.LONG: 1, TideBias.SHORT: -1, TideBias.NEUTRAL: 0}
                direction = _BIAS_SIGN.get(tide_bias, 0)
            else:
                direction = _isolated_direction(regime, px, float(vwap_arr[i]))

            if p.sizing_mode == SizingMode.BASE:
                tgt = direction * tide_rmult * sfrac * p.max_position_base * p.leverage
            else:
                tgt_notional = (
                    direction * tide_rmult * sfrac * p.max_position_usd * p.leverage
                )
                tgt = tgt_notional / px if px > 0.0 else 0.0

            # Once liquidated, force position to 0 for the rest of the run.
            if liquidated_at is not None:
                tgt = 0.0
            target_units[i] = tgt

            # ── Rebalance ─────────────────────────────────────────────
            delta = tgt - cur_units
            if p.sizing_mode == SizingMode.BASE:
                min_delta = abs(p.rebalance_threshold) * p.max_position_base
            else:
                min_delta = abs(p.rebalance_threshold) * cur_equity / px if px > 0.0 else 0.0

            # After liquidation we still allow the single forced flatten
            # (delta = -cur_units) to clear residual exposure, but never
            # take new positions.
            if abs(delta) > min_delta and abs(delta) > 0.0:
                bar_fee = abs(delta) * px * fee_rate
                # Phase 9 slippage: linear bps on notional plus an optional
                # quadratic market-impact term proportional to |delta| /
                # max_position_base.
                if slip_bps > 0.0 or slip_per_unit_bps > 0.0:
                    impact_bps = slip_bps + slip_per_unit_bps * (
                        abs(delta) / max_pos_base_safe
                    )
                    bar_fee += abs(delta) * px * impact_bps / 10_000.0
                old_units = cur_units
                cur_units = tgt
                fee_arr[i] = bar_fee
                trade_records.append({
                    "timestamp": feats.index[i],
                    "prior_units": old_units,
                    "new_units": cur_units,
                    "delta_units": delta,
                    "price": px,
                    "notional": abs(delta) * px,
                    "fee": bar_fee,
                    "wave_regime": regime.value,
                    "tide_bias": tide_bias.value,
                })

            position[i] = cur_units

            # ── Mark-to-market ────────────────────────────────────────
            if i + 1 < n:
                next_px = float(close_arr[i + 1])
                gross = cur_units * (next_px - px)
            else:
                gross = 0.0
            gross_pnl[i] = gross
            net_pnl[i] = gross - fee_arr[i]
            cur_equity += net_pnl[i]
            equity_arr[i] = cur_equity
            bar_return[i] = net_pnl[i] / p.initial_capital

            # ── Liquidation check ──────────────────────────────────────
            # Trigger once the running equity drops to or below the floor.
            # This sets a flag; on the *next* bar `tgt` is forced to 0 so
            # the position is flattened with one final rebalance (which
            # still pays fees / slippage — realistic for a liquidation).
            if liq_threshold is not None and liquidated_at is None:
                if cur_equity <= liq_threshold:
                    liquidated_at = i

        # ── Drawdown ──────────────────────────────────────────────────
        equity_s = pd.Series(equity_arr, index=feats.index)
        running_max = equity_s.cummax()
        drawdown_s = (equity_s - running_max) / running_max.replace(0.0, np.nan)
        drawdown_s = drawdown_s.fillna(0.0)

        bars = feats.copy()
        bars["wave_regime"] = wave_regime_arr
        bars["tide_bias"] = tide_bias_arr
        bars["wave_size_fraction"] = size_frac_arr
        bars["target_units"] = target_units
        bars["position"] = position
        bars["fee"] = fee_arr
        bars["gross_pnl"] = gross_pnl
        bars["net_pnl"] = net_pnl
        bars["equity"] = equity_arr
        bars["bar_return"] = bar_return
        bars["drawdown"] = drawdown_s

        trades_df = pd.DataFrame(trade_records)
        if trades_df.empty:
            trades_df = pd.DataFrame(columns=[
                "timestamp", "prior_units", "new_units", "delta_units",
                "price", "notional", "fee", "wave_regime", "tide_bias",
            ])

        return WaveBacktestResult(
            params=p,
            bars=bars,
            trades=trades_df,
            initial_capital=p.initial_capital,
            final_equity=float(cur_equity),
            mode=p.mode,
            liquidated_at_bar=liquidated_at,
        )
