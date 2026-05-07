"""Tide layer — macro bias, capital allocation, and risk budgeting."""

from tide.tide_engine import TideEngine
from tide.tide_features import (
    TideFeatureParams,
    build_tide_feature_frame,
    classify_vol_regime,
    classify_vol_regime_series,
    derive_bias,
    derive_bias_series,
    directional_efficiency,
    liquidity_stress_proxy,
    realized_volatility,
    rolling_directional_efficiency,
    rolling_realized_volatility,
    rolling_signed_net_move,
    signed_net_move,
)
from tide.tide_backtest import (
    TideBacktester,
    TideBacktestResult,
    TideStrategyParams,
    load_ohlcv,
    timeframe_to_seconds,
)
from tide.tide_metrics import (
    TidePerformanceReport,
    compute_report,
)
from tide.tide_report import (
    WrittenReport,
    plot_drawdown,
    plot_equity_curve,
    plot_regime_overlay,
    render_markdown,
    render_text,
    write_report,
)
from tide.tide_optimiser import (
    TideIndividual,
    TideOptimiser,
    TideOptimiserConfig,
    format_pareto_front,
)

__all__ = [
    "TideEngine",
    "TideFeatureParams",
    "build_tide_feature_frame",
    "classify_vol_regime",
    "classify_vol_regime_series",
    "derive_bias",
    "derive_bias_series",
    "directional_efficiency",
    "liquidity_stress_proxy",
    "realized_volatility",
    "rolling_directional_efficiency",
    "rolling_realized_volatility",
    "rolling_signed_net_move",
    "signed_net_move",
    "TideBacktester",
    "TideBacktestResult",
    "TideStrategyParams",
    "load_ohlcv",
    "timeframe_to_seconds",
    "TidePerformanceReport",
    "compute_report",
    "WrittenReport",
    "plot_drawdown",
    "plot_equity_curve",
    "plot_regime_overlay",
    "render_markdown",
    "render_text",
    "write_report",
    "TideIndividual",
    "TideOptimiser",
    "TideOptimiserConfig",
    "format_pareto_front",
]
