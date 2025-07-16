from datetime import datetime, timezone
import pandas as pd
import typing
from ctypes import *

TF_EQUIV = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1h", "4h": "4h", "12h": "12h", "1d": "d"}

STRAT_PARAMS = {
    "obv": {
        "ma_period": {"name": "MA Period", "type": int, "min": 2, "max": 200},
    },
    "ichimoku": {
        "kijun_period": {"name": "Kijun Period", "type": int, "min": 2, "max": 200},
        "tenkan_period": {"name": "Tenkan Period", "type": int, "min": 2, "max": 200},
    },
    "sup_res": {
        "min_points": {"name": "Min. Points", "type": int, "min": 2, "max": 20},
        "min_diff_points": {"name": "Min. Difference between Points", "type": int, "min": 2, "max": 100},
        "rounding_nb": {"name": "Rounding Number", "type": float, "min": 10, "max": 500, "decimals": 2},
        "take_profit": {"name": "Take Profit %", "type": float, "min": 1, "max": 100, "decimals": 2},
        "stop_loss": {"name": "Stop Loss %", "type": float, "min": 1, "max": 100, "decimals": 2},
    },
    "sma": {
        "slow_ma": {"name": "Slow MA Period", "type": int, "min": 2, "max": 200},
        "fast_ma": {"name": "Fast MA Period", "type": int, "min": 2, "max": 200},
    },
    "psar": {
        "initial_acc": {"name": "Initial Acceleration", "type": float, "min": 0.01, "max": 0.2, "decimals": 2},
        "acc_increment": {"name": "Acceleration Increment", "type": float, "min": 0.01, "max": 0.3, "decimals": 2},
        "max_acc": {"name": "Max. Acceleration", "type": float, "min": 0.05, "max": 1, "decimals": 2},
    },
    "atr": {
        "period": {"name": "Period", "type": int, "min": 1, "max": 30},
        "atr_multiplier": {"name": "ATR Multiplier", "type": float, "min": 0.1, "max": 5.0, "decimals": 2}
    },
    "gpsar": {
        "initial_acc": {"name": "Initial Acceleration", "type": float, "min": 0.01, "max": 0.2, "decimals": 2},
        "acc_increment": {"name": "Acceleration Increment", "type": float, "min": 0.01, "max": 0.3, "decimals": 2},
        "max_acc": {"name": "Max. Acceleration", "type": float, "min": 0.05, "max": 1, "decimals": 2},
        "gradient_threshold": {"name": "Gradient Threshold","type": float, "min": 15, "max": 800, "decimals": 2},
        "gradient_period": {"name": "Gradient Period", "type": int, "min": 1, "max": 10}
    }
}


def ms_to_dt(ms: int) -> typing.Union[datetime, None]:
    if ms is None:
        return ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def dt_to_ms(dt: typing.Union[str, datetime]) -> typing.Union[int, None]:
    if dt is None:
        return dt
    return int(pd.to_datetime(dt).timestamp() * 1000)


def resample_timeframe(data: pd.DataFrame, tf: str) -> pd.DataFrame:
    return data.resample(TF_EQUIV.get(tf)).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )


def get_library():
    lib = CDLL("backtestingCpp/build/libbacktesting.dylib", winmode=0)

    # SMA
    lib.Sma_new.restype = c_void_p
    lib.Sma_new.argtypes = [c_char_p, c_char_p, c_char_p, c_longlong, c_longlong]

    lib.Sma_execute_backtest.restype = c_void_p
    lib.Sma_execute_backtest.argtypes = [c_void_p, c_int, c_int]

    lib.Sma_get_pnl.restype = c_double
    lib.Sma_get_pnl.argtypes = [c_void_p]

    lib.Sma_get_max_dd.restype = c_double
    lib.Sma_get_max_dd.argtypes = [c_void_p]

    lib.Sma_get_num_trades.restype = c_int
    lib.Sma_get_num_trades.argtypes = [c_void_p]

    lib.Sma_get_sharpe_ratio.restype = c_double
    lib.Sma_get_sharpe_ratio.argtypes = [c_void_p]

    lib.Sma_get_cagr.restype = c_double
    lib.Sma_get_cagr.argtypes = [c_void_p]

    # PSAR
    lib.Psar_new.restype = c_void_p
    lib.Psar_new.argtypes = [c_char_p, c_char_p, c_char_p, c_longlong, c_longlong]

    lib.Psar_execute_backtest.restype = c_void_p
    lib.Psar_execute_backtest.argtypes = [c_void_p, c_double, c_double, c_double]

    lib.Psar_get_pnl.restype = c_double
    lib.Psar_get_pnl.argtypes = [c_void_p]

    lib.Psar_get_max_dd.restype = c_double
    lib.Psar_get_max_dd.argtypes = [c_void_p]

    lib.Psar_get_num_trades.restype = c_int
    lib.Psar_get_num_trades.argtypes = [c_void_p]

    lib.Psar_get_sharpe_ratio.restype = c_double
    lib.Psar_get_sharpe_ratio.argtypes = [c_void_p]

    lib.Psar_get_cagr.restype = c_double
    lib.Psar_get_cagr.argtypes = [c_void_p]

    # ATR
    lib.Atr_new.restype = c_void_p
    lib.Atr_new.argtypes = [c_char_p, c_char_p, c_char_p, c_longlong, c_longlong]

    lib.Atr_execute_backtest.restype = c_void_p
    lib.Atr_execute_backtest.argtypes = [c_void_p, c_int, c_double]

    lib.Atr_get_pnl.restype = c_double
    lib.Atr_get_pnl.argtypes = [c_void_p]

    lib.Atr_get_max_dd.restype = c_double
    lib.Atr_get_max_dd.argtypes = [c_void_p]

    lib.Atr_get_num_trades.restype = c_int
    lib.Atr_get_num_trades.argtypes = [c_void_p]

    lib.Atr_get_sharpe_ratio.restype = c_double
    lib.Atr_get_sharpe_ratio.argtypes = [c_void_p]

    lib.Atr_get_cagr.restype = c_double
    lib.Atr_get_cagr.argtypes = [c_void_p]

    # Gradient PSAR
    lib.GradientPsar_new.restype = c_void_p
    lib.GradientPsar_new.argtypes = [c_char_p, c_char_p, c_char_p, c_longlong, c_longlong]

    lib.GradientPsar_execute_backtest.restype = c_void_p
    lib.GradientPsar_execute_backtest.argtypes = [c_void_p, c_double, c_double, c_double, c_double, c_int]

    lib.GradientPsar_get_pnl.restype = c_double
    lib.GradientPsar_get_pnl.argtypes = [c_void_p]

    lib.GradientPsar_get_max_dd.restype = c_double
    lib.GradientPsar_get_max_dd.argtypes = [c_void_p]

    lib.GradientPsar_get_num_trades.restype = c_int
    lib.GradientPsar_get_num_trades.argtypes = [c_void_p]

    lib.GradientPsar_get_sharpe_ratio.restype = c_double
    lib.GradientPsar_get_sharpe_ratio.argtypes = [c_void_p]

    lib.GradientPsar_get_cagr.restype = c_double
    lib.GradientPsar_get_cagr.argtypes = [c_void_p]

    return lib
