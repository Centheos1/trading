"""
Tide-only backtester.

Per AGENT_STRATEGY_RULES §5.1 Tide does not trigger trades — it produces
``(bias, risk_multiplier, vol_regime, ES budget)``.  To validate Tide in
isolation (the standard ablation before Wave/Ripple are layered on), we
run it as a *macro overlay strategy*.

Two sizing modes:
  USD mode (default):
    target_position_$ = bias × risk_multiplier × max_position_usd
    target_units      = target_position_$ / close

  BASE mode (e.g. 1 BTC):
    target_units      = bias × risk_multiplier × max_position_base
    PnL is directly in "BTC price move × held units" — a clean proxy for
    the edge from 1 BTC of directional exposure.

Position changes incur a configurable taker fee.  PnL is mark-to-market
on close-to-close moves.

Timeframe handling:
  ``timeframe`` and ``bar_seconds`` are kept in sync automatically.
  Always set ``timeframe`` via ``with_timeframe()`` so both fields update.

Determinism: identical (params, ohlcv) → identical outputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from schemas import TideBias, TideConfig, VolRegime
from tide.tide_engine import TideEngine
from tide.tide_features import (
    TideFeatureParams,
    build_tide_feature_frame,
)

logger = logging.getLogger(__name__)


class SizingMode(str, Enum):
    USD = "usd"            # max_position_usd / close → units
    BASE = "base"          # max_position_base units (e.g. 1 BTC)


# ────────────────────────────────────────────────────────────────────────
# Parameter container
# ────────────────────────────────────────────────────────────────────────

_TF_SECONDS: dict[str, float] = {
    "1m": 60.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
    "1h": 3600.0, "4h": 14400.0, "12h": 43200.0, "1d": 86400.0,
}


@dataclass
class TideStrategyParams:
    """All tunable parameters for a Tide-overlay backtest run.

    Groups
    ------
    feature pipeline:
        windows (vol_window, eta_window, lsi_window), regime/bias thresholds
    tide engine:
        risk multiplier table, LSI penalty, ES budget
    portfolio:
        sizing mode, initial capital, fees, leverage, rebalance threshold
    metadata:
        symbol, exchange, timeframe (auto-syncs bar_seconds)

    Sizing modes
    ------------
    SizingMode.USD (default):
        target_units = bias × risk_mult × max_position_usd / close
    SizingMode.BASE (e.g. 1 BTC):
        target_units = bias × risk_mult × max_position_base
        PnL is in price-move × held-units; set max_position_base=1.0 for
        "1 BTC equivalent" analysis.
    """
    # ── Feature pipeline ──────────────────────────────────────────────
    vol_window: int = 60
    eta_window: int = 60
    lsi_window: int = 240
    bar_seconds: float = 3600.0           # auto-set by with_timeframe()
    vol_regime_thresholds: tuple[float, float, float] = (0.4, 0.8, 1.5)
    eta_threshold: float = 0.3
    bias_deadband: float = 0.0
    lsi_weights: tuple[float, float, float] = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)

    # ── Tide engine ───────────────────────────────────────────────────
    risk_mult_by_regime: tuple[float, float, float, float] = (1.0, 0.8, 0.5, 0.0)
    lsi_reduce_threshold: float = 1.5
    lsi_reduce_slope: float = 0.2
    es_budget_global: float = 1000.0
    update_interval_ms: int = 60_000

    # ── Portfolio / sizing ────────────────────────────────────────────
    sizing_mode: SizingMode = SizingMode.USD
    initial_capital: float = 10_000.0
    max_position_usd: float = 10_000.0
    max_position_base: float = 1.0        # used when sizing_mode == BASE
    leverage: float = 1.0
    taker_fee_bps: float = 4.0
    maker_fee_bps: float = 2.0
    use_taker_fees: bool = True
    rebalance_threshold: float = 0.0

    # ── Run metadata ──────────────────────────────────────────────────
    symbol: str = "BTCUSDT"
    exchange: str = "binance"
    timeframe: str = "1h"

    # ── Accuracy analysis ─────────────────────────────────────────────
    accuracy_horizons: tuple[int, ...] = (1, 2, 4, 8, 12, 24)

    def with_timeframe(self, tf: str) -> "TideStrategyParams":
        """Return a copy with timeframe and bar_seconds updated together."""
        if tf not in _TF_SECONDS:
            raise ValueError(f"unknown timeframe '{tf}'")
        from dataclasses import replace
        return replace(self, timeframe=tf, bar_seconds=_TF_SECONDS[tf])

    def to_tide_config(self) -> TideConfig:
        return TideConfig(
            update_interval_ms=int(self.update_interval_ms),
            es_budget_global=float(self.es_budget_global),
            max_position_usd=float(self.max_position_usd),
            vol_regime_thresholds=list(self.vol_regime_thresholds),
            lsi_weights=list(self.lsi_weights),
            risk_mult_by_regime=list(self.risk_mult_by_regime),
            lsi_reduce_threshold=float(self.lsi_reduce_threshold),
            lsi_reduce_slope=float(self.lsi_reduce_slope),
        )

    def to_feature_params(self) -> TideFeatureParams:
        return TideFeatureParams(
            vol_window=int(self.vol_window),
            eta_window=int(self.eta_window),
            lsi_window=int(self.lsi_window),
            bar_seconds=float(self.bar_seconds),
            vol_regime_thresholds=tuple(self.vol_regime_thresholds),
            eta_threshold=float(self.eta_threshold),
            bias_deadband=float(self.bias_deadband),
            lsi_weights=tuple(self.lsi_weights),
        )


# ────────────────────────────────────────────────────────────────────────
# Backtest result container
# ────────────────────────────────────────────────────────────────────────

@dataclass
class TideBacktestResult:
    """Full output of a single Tide backtest run.

    Attributes
    ----------
    params : TideStrategyParams
        The exact parameter set used.
    bars : pd.DataFrame
        Per-bar state — one row per OHLCV bar.  Columns:
            close, log_return, realized_vol, eta, net_move, lsi,
            vol_regime (str), bias (str), risk_mult, target_units,
            position, fee, gross_pnl, net_pnl, equity, drawdown
    trades : pd.DataFrame
        One row per position change (rebalance / flip / flatten).  Columns:
            timestamp, prior_units, new_units, delta_units, price,
            notional, fee, prior_bias, new_bias, prior_regime, new_regime
    initial_capital : float
    final_equity : float
    """
    params: TideStrategyParams
    bars: pd.DataFrame
    trades: pd.DataFrame
    initial_capital: float
    final_equity: float

    @property
    def equity_curve(self) -> pd.Series:
        return self.bars["equity"]

    @property
    def returns(self) -> pd.Series:
        """Bar-level net returns (fraction of starting equity per bar)."""
        return self.bars["bar_return"]

    @property
    def drawdown(self) -> pd.Series:
        return self.bars["drawdown"]

    @property
    def num_trades(self) -> int:
        return int(len(self.trades))


# ────────────────────────────────────────────────────────────────────────
# Backtester
# ────────────────────────────────────────────────────────────────────────

_BIAS_SIGN: dict[TideBias, int] = {
    TideBias.LONG: 1,
    TideBias.SHORT: -1,
    TideBias.NEUTRAL: 0,
}


class TideBacktester:
    """Bar-by-bar event-time backtester driving the TideEngine.

    Lifecycle per bar:
      1. Update Tide engine inputs (vol_regime, lsi, bias) from features.
      2. Engine.update(ts) — applies cadence gate, recomputes risk_mult.
      3. Compute target position from snapshot.
      4. Apply rebalance (with fee) if delta exceeds rebalance_threshold.
      5. Mark-to-market on the bar's close-to-close move.
      6. Record state.
    """

    def __init__(self, params: TideStrategyParams):
        self.params = params

    # ── Public API ────────────────────────────────────────────────

    def run(self, ohlcv: pd.DataFrame) -> TideBacktestResult:
        """Run the backtest on an OHLCV DataFrame.

        ``ohlcv`` must be indexed by ``DatetimeIndex`` (UTC) and contain
        ``open, high, low, close, volume`` columns.  Bar width must match
        ``params.bar_seconds`` — caller is responsible for resampling.
        """
        self._validate_input(ohlcv)
        feats = build_tide_feature_frame(ohlcv, self.params.to_feature_params())
        return self._run_loop(feats)

    # ── Internals ─────────────────────────────────────────────────

    def _validate_input(self, ohlcv: pd.DataFrame) -> None:
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(ohlcv.columns)
        if missing:
            raise ValueError(f"ohlcv missing columns: {missing}")
        if len(ohlcv) == 0:
            raise ValueError("ohlcv is empty")
        if not isinstance(ohlcv.index, pd.DatetimeIndex):
            raise ValueError("ohlcv.index must be a DatetimeIndex")
        if not ohlcv.index.is_monotonic_increasing:
            raise ValueError("ohlcv.index must be monotonically increasing")

    def _run_loop(self, feats: pd.DataFrame) -> TideBacktestResult:
        p = self.params
        engine = TideEngine(p.to_tide_config())

        n = len(feats)
        ts_ms = (feats.index.astype("int64") // 1_000_000).to_numpy()
        close = feats["close"].to_numpy(dtype=np.float64)
        bias_arr = feats["bias"].to_numpy()
        regime_arr = feats["vol_regime"].to_numpy()
        lsi_arr = feats["lsi"].fillna(0.0).to_numpy(dtype=np.float64)
        rv_arr = feats["realized_vol"].fillna(0.0).to_numpy(dtype=np.float64)

        # Pre-allocate output columns.
        position = np.zeros(n, dtype=np.float64)        # signed units
        target_units = np.zeros(n, dtype=np.float64)
        risk_mult = np.zeros(n, dtype=np.float64)
        fee = np.zeros(n, dtype=np.float64)
        gross_pnl = np.zeros(n, dtype=np.float64)
        net_pnl = np.zeros(n, dtype=np.float64)
        equity = np.zeros(n, dtype=np.float64)
        bar_return = np.zeros(n, dtype=np.float64)
        published_bias = np.empty(n, dtype=object)
        published_regime = np.empty(n, dtype=object)

        cur_units = 0.0
        cur_equity = float(p.initial_capital)
        fee_rate = (p.taker_fee_bps if p.use_taker_fees else p.maker_fee_bps) / 10_000.0

        trade_records: list[dict] = []

        for i in range(n):
            ts = int(ts_ms[i])
            px = float(close[i])

            # ── Drive the engine (deterministic) ──────────────────
            bias_t = bias_arr[i]
            regime_t = regime_arr[i]
            if not isinstance(bias_t, TideBias):
                bias_t = TideBias.NEUTRAL
            if not isinstance(regime_t, VolRegime):
                regime_t = VolRegime.NORMAL
            engine.set_bias(bias_t)
            engine.set_vol_regime(regime_t)
            engine.set_liquidity_stress(float(lsi_arr[i]))
            engine.set_realized_vol(float(rv_arr[i]))
            snap = engine.update(ts)

            risk_mult[i] = snap.risk_multiplier
            published_bias[i] = snap.bias.value
            published_regime[i] = snap.vol_regime.value

            # ── Target position from Tide snapshot ────────────────
            sign = _BIAS_SIGN.get(snap.bias, 0)
            if p.sizing_mode == SizingMode.BASE:
                # BASE mode: fixed units of the base asset (e.g. 1 BTC).
                # risk_mult scales the fraction of max_position_base used.
                tgt = sign * snap.risk_multiplier * p.max_position_base * p.leverage
            else:
                # USD mode: notional ÷ price.
                tgt_notional = (
                    sign * snap.risk_multiplier * snap.max_position_usd * p.leverage
                )
                tgt = tgt_notional / px if px > 0.0 else 0.0
            target_units[i] = tgt

            # ── Rebalance with fee ────────────────────────────────
            delta = tgt - cur_units
            if p.sizing_mode == SizingMode.BASE:
                min_delta_units = abs(p.rebalance_threshold) * p.max_position_base
            else:
                min_delta_units = (
                    abs(p.rebalance_threshold) * cur_equity / px if px > 0.0 else 0.0
                )
            if abs(delta) > min_delta_units and abs(delta) > 0.0:
                bar_fee = abs(delta) * px * fee_rate
                cur_units = tgt
                fee[i] = bar_fee
                trade_records.append({
                    "timestamp": feats.index[i],
                    "prior_units": cur_units - delta,
                    "new_units": cur_units,
                    "delta_units": delta,
                    "price": px,
                    "notional": abs(delta) * px,
                    "fee": bar_fee,
                    "bias": snap.bias.value,
                    "regime": snap.vol_regime.value,
                })

            # ── Mark-to-market on next bar's close ────────────────
            # PnL accrues on the held position over [close_t, close_{t+1}].
            if i + 1 < n:
                next_px = float(close[i + 1])
                gross = cur_units * (next_px - px)
            else:
                gross = 0.0
            gross_pnl[i] = gross
            net_pnl[i] = gross - fee[i]
            cur_equity += net_pnl[i]
            equity[i] = cur_equity
            position[i] = cur_units

        # Bar return = net_pnl / equity_at_start_of_bar.  Use prior-bar
        # equity for index t (start of bar), seed t=0 with initial.
        prior_equity = np.empty(n, dtype=np.float64)
        prior_equity[0] = p.initial_capital
        if n > 1:
            prior_equity[1:] = equity[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            bar_return = np.where(
                prior_equity > 0.0, net_pnl / prior_equity, 0.0
            )

        # Running max + drawdown (as fraction of running peak equity).
        running_max = np.maximum.accumulate(equity)
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown = np.where(
                running_max > 0.0, (equity - running_max) / running_max, 0.0
            )

        bars = feats[
            ["close", "log_return", "realized_vol", "eta", "net_move", "lsi"]
        ].copy()
        bars["vol_regime"] = published_regime
        bars["bias"] = published_bias
        bars["risk_mult"] = risk_mult
        bars["target_units"] = target_units
        bars["position"] = position
        bars["fee"] = fee
        bars["gross_pnl"] = gross_pnl
        bars["net_pnl"] = net_pnl
        bars["bar_return"] = bar_return
        bars["equity"] = equity
        bars["drawdown"] = drawdown

        if trade_records:
            trades = pd.DataFrame(trade_records).set_index("timestamp")
        else:
            trades = pd.DataFrame(
                columns=[
                    "prior_units", "new_units", "delta_units", "price",
                    "notional", "fee", "bias", "regime",
                ]
            )

        return TideBacktestResult(
            params=p,
            bars=bars,
            trades=trades,
            initial_capital=float(p.initial_capital),
            final_equity=float(cur_equity),
        )


# ────────────────────────────────────────────────────────────────────────
# Convenience wrapper for HDF5-stored bars
# ────────────────────────────────────────────────────────────────────────

def load_ohlcv(
    exchange: str,
    symbol: str,
    *,
    timeframe: str = "1m",
    from_time: Optional[int] = None,
    to_time: Optional[int] = None,
) -> pd.DataFrame:
    """Load OHLCV for backtesting and resample to ``timeframe``.

    Reads from the OHLCV store selected by the ``DATA_STORE`` environment
    variable (``local_parquet`` by default, ``s3`` on EC2).  Falls back to
    the legacy ``data/{exchange}.h5`` HDF5 file when the requested
    symbol/timeframe is not present in the Parquet store.

    Returns the canonical ``open/high/low/close/volume`` DataFrame indexed
    by ``DatetimeIndex`` (UTC, tz-naive — matches existing convention).
    """
    from utils import TF_EQUIV, resample_timeframe

    if timeframe not in TF_EQUIV:
        raise ValueError(f"unknown timeframe '{timeframe}'")

    if from_time is None:
        from_time = 0
    if to_time is None:
        to_time = int(2**63 - 1)

    df: Optional[pd.DataFrame] = None

    try:
        from ohlcv_store import get_ohlcv_store

        store = get_ohlcv_store()
        # Native 1m fetch — resampling to the requested timeframe happens below
        candidate = store.read(
            exchange, symbol, "1m", from_ts=from_time, to_ts=to_time
        )
        if not candidate.empty:
            df = candidate
    except Exception:  # noqa: BLE001
        df = None

    if df is None or df.empty:
        from database import Hdf5Client

        try:
            client = Hdf5Client(exchange)
            df = client.get_data(symbol, from_time, to_time)
        except (KeyError, OSError):
            df = None

    if df is None or df.empty:
        raise RuntimeError(
            f"No data for {exchange}/{symbol} in [{from_time}, {to_time}]"
        )
    df = resample_timeframe(df, timeframe)
    df = df.dropna(subset=["close"])
    return df


def timeframe_to_seconds(timeframe: str) -> float:
    """Convert a TF_EQUIV key to bar width in seconds."""
    units = {
        "1m": 60.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
        "1h": 3600.0, "4h": 14400.0, "12h": 43200.0, "1d": 86400.0,
    }
    if timeframe not in units:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    return units[timeframe]
