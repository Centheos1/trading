import sys
import os
import logging
from typing import Tuple, Dict, List

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
except ImportError:
    logger.warning(
        "orderflow_engine C++ module not found. "
        "Build it first: cd backtestingCpp/orderflow && mkdir build && cd build "
        "&& cmake .. && make"
    )
    ofe = None


def _build_config(params: Dict) -> "ofe.EngineConfig":
    config = ofe.EngineConfig()
    config.tick_size = params.get("tick_size", 0.01)

    sp = ofe.SignalParams()
    sp.imbalance_threshold = params.get("imbalance_threshold", 3.0)
    sp.stacked_imbalance_levels = params.get("stacked_imbalance_levels", 3)
    sp.absorption_volume_ratio = params.get("absorption_volume_ratio", 5.0)
    sp.cvd_divergence_lookback = params.get("cvd_divergence_lookback", 100)
    sp.exhaustion_lookback_bars = params.get("exhaustion_lookback_bars", 5)
    sp.signal_strength_min = params.get("signal_strength_min", 0.3)
    config.signal_params = sp

    # Ripple parameters (Phase 6)
    rcfg = config.ripple
    rcfg.tick_size = config.tick_size
    rcfg.paper_fills = params.get("paper_fills", False)
    _RIPPLE_MAP = {
        "wall_min_relative_size": "wall_min_relative_size",
        "absorption_entry": "absorption_entry",
        "exhaustion_entry": "exhaustion_entry",
        "breakout_entry": "breakout_entry",
        "idle_exit_threshold": "idle_exit_threshold",
        "bounce_max_break_risk": "bounce_max_break_risk",
        "feature_window_ms": "feature_window_ms",
    }
    for param_key, attr_name in _RIPPLE_MAP.items():
        if param_key in params:
            setattr(rcfg, attr_name, params[param_key])

    # Lifecycle sub-parameters
    lc = rcfg.lifecycle
    if "confirmation_window_ms" in params:
        lc.confirmation_window_ms = params["confirmation_window_ms"]
    if "max_hold_time_ms" in params:
        lc.max_hold_time_ms = params["max_hold_time_ms"]
    if "trailing_stop_sigma" in params:
        lc.trailing_stop_sigma = params["trailing_stop_sigma"]
    if "target_distance_sigma" in params:
        lc.target_distance_sigma = params["target_distance_sigma"]
    rcfg.lifecycle = lc

    config.ripple = rcfg
    return config


def backtest(exchange: str, symbol: str, from_time: int, to_time: int,
             params: Dict) -> Tuple[float, float, int, float, float]:
    if ofe is None:
        raise RuntimeError(
            "orderflow_engine C++ module not built. "
            "Run: cd backtestingCpp/orderflow && mkdir -p build && cd build "
            "&& cmake .. && make"
        )

    config = _build_config(params)

    # Enable paper fills for Ripple-based metrics
    rcfg = config.ripple
    rcfg.paper_fills = True
    config.ripple = rcfg

    engine = ofe.OrderFlowEngine(config)

    tick_store_path = os.path.join("data", f"{exchange}_ticks.h5")
    if not os.path.exists(tick_store_path):
        raise FileNotFoundError(
            f"Tick data file not found: {tick_store_path}. "
            "Collect tick data first using the data collection mode."
        )

    store = ofe.TickStore(tick_store_path)
    replay = ofe.ReplayFeed(store, 0.0)
    replay.set_time_range(from_time, to_time)

    ofe.connect_feed(engine, replay)
    engine.start(symbol)

    replay.run_sync()

    ripple = engine.get_ripple()
    ripple_trades = ripple.completed_trades()
    ripple_pnl = ripple.cumulative_pnl()
    ripple_dd = ripple.lifecycle_max_drawdown()

    sig_engine = engine.get_signal_engine()
    sig_pnl = sig_engine.get_pnl()
    sig_dd = sig_engine.get_max_drawdown()
    sig_trades = sig_engine.get_num_trades()

    # Use Ripple metrics when trades completed, else fall back to SignalEngine
    if ripple_trades > 0:
        pnl = ripple_pnl
        max_drawdown = ripple_dd
        num_trades = int(ripple_trades)
    else:
        pnl = sig_pnl
        max_drawdown = sig_dd
        num_trades = sig_trades

    returns = sig_engine.get_returns()
    sharpe_ratio = _compute_sharpe(returns)
    cagr = _compute_cagr(returns, from_time, to_time)

    engine.stop()
    store.close()

    return pnl, max_drawdown, num_trades, sharpe_ratio, cagr


def _compute_sharpe(returns: List[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    import numpy as np
    r = np.diff(returns)
    if np.std(r) == 0:
        return 0.0
    return float((np.mean(r) - risk_free) / np.std(r))


def _compute_cagr(returns: List[float], from_time: int, to_time: int,
                  initial: float = 10000.0) -> float:
    if not returns or from_time >= to_time:
        return 0.0

    final_value = initial * (1.0 + returns[-1] / 100.0)
    years = (to_time - from_time) / (365.25 * 24 * 3600 * 1000)
    if years <= 0 or final_value <= 0:
        return 0.0

    return float((final_value / initial) ** (1.0 / years) - 1.0) * 100.0


def run_live(symbol: str, exchange: str = "binance", futures: bool = True,
             params: Dict = None, tick_store_path: str = None):
    if ofe is None:
        raise RuntimeError("orderflow_engine C++ module not built.")

    params = params or {}
    config = _build_config(params)
    engine = ofe.OrderFlowEngine(config)

    feed = ofe.BinanceWsFeed(futures)
    ofe.connect_feed(engine, feed)

    if tick_store_path:
        store = ofe.TickStore(tick_store_path)

        def on_signal(signal):
            logger.info(
                f"SIGNAL: {signal.type_name()} @ {signal.price:.2f} "
                f"strength={signal.strength:.2f} - {signal.description}"
            )

        engine.set_signal_callback(on_signal)

    engine.start(symbol)
    return engine
