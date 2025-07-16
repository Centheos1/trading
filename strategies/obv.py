import pandas as pd
import numpy as np

from metrics import compute_sharpe_ratio, compute_cagr_from_returns

pd.set_option("display.max_columns", None)
pd.set_option("display.max_rows", 1000)
pd.set_option("display.width", 1000)


# Parameters = {'ma_period': 81}

def backtest(df_original: pd.DataFrame, ma_period: int):

    df = df_original.copy()
    df.ffill(inplace=True)

    df["obv"] = ( np.sign(df["close"].diff()) * df["volume"] ).fillna(0).cumsum()
    df["obv_ma"] = round( df["obv"].rolling(window=ma_period).mean(), 2)

    df["signal"] = np.where( df["obv"] > df["obv_ma"], 1, -1 )
    df["close_change"] = df["close"].pct_change(fill_method=None)
    df["signal_shift"] = df["signal"].shift(1)
    df["pnl"] = df["close"].pct_change(fill_method=None) * df["signal"].shift(1)

    df["cum_pnl"] = df["pnl"].cumsum()
    df["max_cum_pnl"] = df["cum_pnl"].cummax()
    df["drawdown"] = df["max_cum_pnl"] - df["cum_pnl"]

    pnl = df["pnl"].sum()
    max_drawdown = df["drawdown"].max()
    num_trades = df["signal"].diff().abs().sum()

    if len(df):
        sharpe_ratio = compute_sharpe_ratio(df["pnl"])
        cagr = compute_cagr_from_returns(df["pnl"].tolist(), df.index[0], df.index[-1])
    else:
        sharpe_ratio = 0.0
        cagr = 0.0

    # print(f"{df.tail(500)}")

    return pnl, max_drawdown, num_trades, sharpe_ratio, cagr