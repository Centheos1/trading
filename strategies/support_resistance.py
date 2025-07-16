import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf

from metrics import compute_sharpe_ratio, compute_cagr_from_returns

pd.set_option("display.max_columns", None)
pd.set_option("display.max_rows", 100)
pd.set_option("display.width", 1000)

# 1d
# Parameters = {'min_points': 2, 'min_diff_points': 2, 'rounding_nb': 131.93, 'take_profit': 40, 'stop_loss': 1.02}
# PNL = 104.23
# Max. Drawdown = 12.0571
# Rank = 0
# Crowding Distance = inf

# Parameters = {'min_points': 2, 'min_diff_points': 2, 'rounding_nb': 249.19, 'take_profit': 40, 'stop_loss': 1}

def backtest(df_original: pd.DataFrame, min_points: int, min_diff_points: int, rounding_nb: float,
             take_profit: float, stop_loss: float):

    df = df_original.copy()
    df.ffill(inplace=True)

    candle_length = df.iloc[1].name - df.iloc[0].name

    SUPPORTS = "supports"
    RESISTANCES = "resistances"
    BROKEN_IN_LAST_THRESHOLD = 3
    LAST_H_L_MAX = 10

    pnl = 0
    max_pnl = 0
    num_trades = 0
    max_drawdown = 0
    returns = list()
    trade_side = 0
    entry_price = None

    df["rounded_high"] = round( df["high"] / rounding_nb ) * rounding_nb
    df["rounded_low"] = round( df["low"] / rounding_nb ) * rounding_nb

    price_groups = {SUPPORTS: dict(), RESISTANCES: dict()}
    last_h_l = {SUPPORTS: [], RESISTANCES: []}
    resistances_supports = {SUPPORTS: [], RESISTANCES: []}
    # For visualisation
    levels = {SUPPORTS: [], RESISTANCES: []}

    # Numpy Arrays
    highs = np.array(df["high"])
    lows = np.array(df["low"])
    rounded_highs = np.array(df["rounded_high"])
    rounded_lows = np.array(df["rounded_low"])
    closes = np.array(df["close"])
    times = np.array(df.index)

    row = {
        "high": highs,
        "low": lows,
        "rounded_high": rounded_highs,
        "rounded_low": rounded_lows,
        "close": closes
    }

    # for index, row in df.iterrows():
    for i in range(len(highs)):

        index = times[i]

        for side in [RESISTANCES,SUPPORTS]:

            h_l = "high" if side == RESISTANCES else "low"

            if row["rounded_" + h_l][i] in price_groups[side]:

                grp = price_groups[side][ row["rounded_" + h_l][i] ]

                broken_in_last = 0

                if grp["start_time"] is None:
                    for c in last_h_l[side]:
                        if c > row[h_l][i] and side == RESISTANCES:
                            broken_in_last += 1
                        elif c < row[h_l][i] and side == SUPPORTS:
                            broken_in_last += 1

                    if broken_in_last < BROKEN_IN_LAST_THRESHOLD:
                        grp["start_time"] = index

                if (
                    broken_in_last < BROKEN_IN_LAST_THRESHOLD
                    and ( grp["last"] is None
                    or index >= grp["last"] + min_diff_points * candle_length )
                ):

                    grp["prices"].append(row[h_l][i])

                    # Check breakout has pasted y candles
                    if len(grp["prices"]) >= min_points:
                        extreme_price = max(grp["prices"]) if side == RESISTANCES else min(grp["prices"])
                        levels[side].append([ (grp["start_time"], extreme_price), (index, extreme_price) ])
                        resistances_supports[side].append({ "price": extreme_price, "broken": False })

                    grp["last"] = index

            else:
                broken_in_last = 0

                for c in last_h_l[side]:
                    if c > row[h_l][i] and side == RESISTANCES:
                        broken_in_last += 1
                    elif c < row[h_l][i] and side == SUPPORTS:
                        broken_in_last += 1

                if broken_in_last < BROKEN_IN_LAST_THRESHOLD:
                    price_groups[side][ row["rounded_" + h_l][i] ] = { "prices": [ row[h_l][i] ], "start_time": index, "last": index }

            # Check whether price groups are still valid or not
            for key, value in price_groups[side].items():
                if len(value["prices"]) > 0:
                    # Resistance Breakout
                    if side == RESISTANCES and row[h_l][i] > max(value["prices"]):
                        value["prices"].clear()
                        value["start_time"] = None
                        value["last"] = None
                    # Support Breakout
                    if side == SUPPORTS and row[h_l][i] < min(value["prices"]):
                        value["prices"].clear()
                        value["start_time"] = None
                        value["last"] = None

            last_h_l[side].append(row[h_l][i])
            if len(last_h_l[side]) > LAST_H_L_MAX:
                last_h_l[side].pop(0)

            # Check new trade
            for sup_res in resistances_supports[side]:
                entry_condition = row["close"][i] > sup_res["price"] if side == RESISTANCES else row["close"][i] < sup_res["price"]

                if entry_condition and not sup_res["broken"]:
                    sup_res["broken"] = True
                    if trade_side == 0:
                        entry_price = row["close"][i]
                        trade_side = 1 if side == RESISTANCES else -1

            # Check PNL
            if trade_side == 1:
                if row["close"][i] >= entry_price * (1 + take_profit / 100) or row["close"][i] <= entry_price * (1 - stop_loss / 100):
                    pnl += (row["close"][i] / entry_price - 1) * 100
                    trade_side = 0
                    returns.append(float((row["close"][i] / entry_price - 1) * 100))
                    entry_price = None
                    num_trades += 1
            elif trade_side == -1:
                if row["close"][i] <= entry_price * (1 - take_profit / 100) or row["close"][i] >= entry_price * (1 + stop_loss / 100):
                    pnl += (entry_price / row["close"][i] - 1) * 100
                    trade_side = 0
                    returns.append(float((row["close"][i] / entry_price - 1) * 100))
                    entry_price = None
                    num_trades += 1

            max_pnl = max(max_pnl, pnl)
            max_drawdown = max(max_drawdown, max_pnl - pnl)

    # mpf.plot(df, type="candle", style="charles", alines=dict(alines=levels[RESISTANCES] + levels[SUPPORTS]), title='Support & Resistance Strategy Chart')
    # plt.show()

    sharpe_ratio = compute_sharpe_ratio(returns)
    cagr = compute_cagr_from_returns(returns, df.index[0], df.index[-1])

    return pnl, max_drawdown, num_trades, sharpe_ratio, cagr


