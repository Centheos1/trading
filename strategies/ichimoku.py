import pandas as pd
import numpy as np

from metrics import compute_sharpe_ratio, compute_cagr_from_returns

# import matplotlib.pyplot as plt
# import mplfinance as mpf

pd.set_option("display.max_columns", None)
pd.set_option("display.max_rows", 100)
pd.set_option("display.width", 1000)

# 15m
# Parameters = {'kijun_period': 6, 'tenkan_period': 2}
# PNL = 0.35
# Max. Drawdown = 0.135
# Rank = 0
# Crowding Distance = inf


def backtest(df_original: pd.DataFrame, tenkan_period: int, kijun_period: int):

    df = df_original.copy()

    # Tenkan Sen (fast-moving): short-term signal line
    # Mean highs/lows over x periods

    df["rolling_min_tenkan"] = df["low"].rolling(window=tenkan_period).min()
    df["rolling_max_tenkan"] = df["high"].rolling(window=tenkan_period).max()

    df["tenkan_sen"] = ( df["rolling_max_tenkan"] + df["rolling_min_tenkan"] ) / 2

    df.drop(["rolling_min_tenkan", "rolling_max_tenkan"], axis=1, inplace=True)

    # Kijun Sen: (slow-moving): long term signal line
    # Mean highs/lows over Y periods

    df["rolling_min_kijun"] = df["low"].rolling(window=kijun_period).min()
    df["rolling_max_kijun"] = df["high"].rolling(window=kijun_period).max()

    df["kijun_sen"] = ( df["rolling_max_kijun"] + df["rolling_min_kijun"] ) / 2

    df.drop(["rolling_min_kijun", "rolling_max_kijun"], axis=1, inplace=True)

    # Senkou span A
    # Mean between Tenkan & Kijun projected Y periods ahead
    df["senkou_span_a"] = ( ( df["tenkan_sen"] + df["kijun_sen"] ) / 2 ).shift(kijun_period)

    # Senkou span B
    # Mean highs/lows over projected Y*2 periods ahead
    df["rolling_min_senkou"] = df["low"].rolling(window=kijun_period * 2).min()
    df["rolling_max_senkou"] = df["high"].rolling(window=kijun_period * 2).max()

    df["senkou_span_b"] = ( ( df["rolling_max_senkou"] + df["rolling_min_senkou"] ) / 2 ).shift(kijun_period)

    df.drop(["rolling_min_senkou", "rolling_max_senkou"], axis=1, inplace=True)

    # Chikou span: confirmation line
    # Close price compared with Y periods back
    df["chikou_span"] = df["close"].shift(kijun_period)

    df.dropna(inplace=True)

    # Signal
    df["tenkan_minus_kijun"] = df["tenkan_sen"] - df["kijun_sen"]
    df["prev_tenkan_minus_kijun"] = df["tenkan_minus_kijun"].shift(1)

    df["signal"] = np.where((df["tenkan_minus_kijun"] > 0) &
                            (df["prev_tenkan_minus_kijun"] < 0) &
                            (df["close"] > df["senkou_span_a"]) &
                            (df["close"] > df["senkou_span_b"]) &
                            (df["close"] > df["chikou_span"]), 1,

                            np.where((df["tenkan_minus_kijun"] < 0) &
                                     (df["prev_tenkan_minus_kijun"] > 0) &
                                     (df["close"] < df["senkou_span_a"]) &
                                     (df["close"] < df["senkou_span_b"]) &
                                     (df["close"] < df["chikou_span"]), -1, np.nan))

    df = df[df["signal"] != 0].copy()

    df["pnl"] = df["close"].pct_change(fill_method=None) * df["signal"].shift(1)

    # Plot the candlestick chart with Ichimoku components
    # ichimoku_lines = [
    #     mpf.make_addplot(df['tenkan_sen'], color='blue'),
    #     mpf.make_addplot(df['kijun_sen'], color='red'),
    #     mpf.make_addplot(df['senkou_span_a'], color='green'),
    #     mpf.make_addplot(df['senkou_span_b'], color='orange'),
    #     mpf.make_addplot(df['chikou_span'], color='purple')
    # ]
    # mpf.plot(df, type='candle', addplot=ichimoku_lines, volume=True, style='charles', title='Ichimoku Strategy Chart')

    # print(df.head())

    pnl = df["pnl"].sum()
    df["cum_pnl"] = df["pnl"].cumsum()
    df["max_cum_pnl"] = df["cum_pnl"].cummax()
    df["drawdown"] = df["max_cum_pnl"] - df["cum_pnl"]
    max_drawdown = df["drawdown"].max()
    num_trades = df["signal"].diff().abs().sum()

    if len(df):
        sharpe_ratio = compute_sharpe_ratio(df["pnl"])
        cagr = compute_cagr_from_returns(pnl, df.index[0], df.index[-1])
    else:
        sharpe_ratio = 0.0
        cagr = 0.0

    return pnl, max_drawdown, num_trades, sharpe_ratio, cagr

