#include <algorithm>
#include <cmath>
#include <iostream>

#include "Atr.h"
#include "../Database.h"
#include "../Utils.h"
#include "../Metrics.h"



using namespace std;

Atr::Atr(char* exchange_c, char* symbol_c, char* timeframe_c, long long from_time, long long to_time)
{
    exchange = exchange_c;
    symbol = symbol_c;
    timeframe = timeframe_c;

    Database db(exchange);
    int array_size = 0;
    double **res = db.get_data(symbol, exchange, array_size);

    std::tie(ts, open, high, low, close, volume) = rearrange_candles(res, timeframe, from_time, to_time, array_size);
}

double Atr::calculate_true_range(int i)
{
    if (i == 0) return high[i] - low[i];

    double tr_1 = high[i] - low[i];
    double tr_2 = abs(high[i] - close[i-1]);
    double tr_3 = abs(low[i] - close[i-1]);

    return max({tr_1, tr_2, tr_3});
}

double Atr::calculate_atr(int end_index, int period)
{
    if (end_index < period - 1) return 0.0;

    double sum_tr = 0.0;
    for (int i = end_index - period + 1; i <= end_index; ++i)
    {
        sum_tr += calculate_true_range(i);
    }

    return sum_tr / period;
}

void Atr::execute_backtest(int period, double atr_multiplier)
{
    bool verbose = false;
    
    pnl = 0.0;
    max_dd = 0.0;
    num_trades = 0;
    std::vector<double> returns;

    double max_pnl = 0.0;

    int current_position = 0;
    double entry_price = 0.0;
    double peak_equity = 0.0;
    double equity = 0.0;

    for (size_t i = static_cast<size_t>(period); i < close.size(); ++i)
    {
        double atr = calculate_atr(i, static_cast<int>(period));
        if (atr == 0.0) continue;

        double buy_threshold = close[i - 1] + atr_multiplier * atr;
        double sell_threshold = close[i - 1] - atr_multiplier * atr;

        if (current_position == 0)
        {
            if (close[i] > buy_threshold) {
                // Enter long
                entry_price = close[i];
                current_position = 1;
                num_trades++;
                if (verbose) {
                    cout << "Enter Long " << entry_price << " | num_trades: " << num_trades << endl;
                }
            } else if (close[i] < sell_threshold) {
                // Enter short
                entry_price = close[i];
                current_position = -1;
                num_trades++;
                if (verbose) {
                    cout << "Enter Short " << entry_price << " | num_trades: " << num_trades << endl;
                }
            }
        }
        else if (current_position == 1)
        {
            if (close[i] < sell_threshold) {
                // Close long
                double trade_pnl = (entry_price / close[i] - 1) * 100;
                pnl += trade_pnl;
                equity += trade_pnl;
                peak_equity = max(peak_equity, equity);
                max_dd = max(max_dd, peak_equity - equity);
                current_position = 0;
                returns.push_back(pnl);

                if (verbose) {
                    cout << "Close Long " << entry_price << " | " << close[i] << " trade_pnl: " << trade_pnl << " pnl: " << pnl << " equity: " << equity << endl;
                }
            }
        }
        else if (current_position == -1)
        {
            if (close[i] > buy_threshold) {
                // Close short
                double trade_pnl = (close[i] / entry_price - 1) * 100;
                pnl += trade_pnl;
                equity += trade_pnl;
                peak_equity = max(peak_equity, equity);
                max_dd = max(max_dd, peak_equity - equity);
                current_position = 0;
                returns.push_back(pnl);

                if (verbose) {
                    // printf("Missing %i candle(s) from %f\n", missing_candles, current_ts);
                    cout << "Close Short " << entry_price << " | " << close[i] << " trade_pnl: " << trade_pnl << " pnl: " << pnl << " equity: " << equity << endl;;
                }
            }
        }
    }

    peak_equity = max(peak_equity, equity);

    if (verbose) cout << "equity: " << equity << endl;
    if (verbose) cout << "peak_equity: " << peak_equity << endl;

    // Exit final position
    // if (current_position == 1) {
    //     // Close long
    //     double trade_pnl = close[i] - entry_price;
    //     pnl += trade_pnl;
    //     equity += trade_pnl;
    //     max_dd = max(max_dd, peak_equity - equity);
    // } else if (current_position == -1) {
    //     // Close short
    //     double trade_pnl = entry_price - close[i];
    //     pnl += trade_pnl;
    //     equity += trade_pnl;
    //     max_dd = max(max_dd, peak_equity - equity);
    // }

    long long from_time = ts[0];
    long long to_time = ts.back();
    sharpe_ratio = compute_sharpe_ratio(returns);
    // cagr = compute_cagr_from_percent(pnl, from_time, to_time);
    cagr = compute_cagr_from_returns(returns, from_time, to_time, 10000.0);

}

extern "C"
{
    Atr *Atr_new(char *exchange, char *symbol, char *timeframe, long long from_time, long long to_time)
    {
        return new Atr(exchange, symbol, timeframe, from_time, to_time);
    }

    void Atr_execute_backtest(Atr *atr, int period, double atr_multiplier)
    {
        return atr->execute_backtest(period, atr_multiplier);
    }

    double Atr_get_pnl(Atr *atr) { return atr->pnl; }
    double Atr_get_max_dd(Atr *atr) { return atr->max_dd; }
    int Atr_get_num_trades(Atr *atr) { return atr->num_trades; }
    double Atr_get_sharpe_ratio(Atr *atr) { return atr->sharpe_ratio; }
    double Atr_get_cagr(Atr *atr) { return atr->cagr; }

}