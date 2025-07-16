#include "GradientPsar.h"
#include "../Database.h"
#include "../Utils.h"
#include "../Metrics.h"

using namespace std;

// Symbol = BTC_USD
// Strategy = gpsar
// Timeframe = 1d
// Time = 1970-01-01 00:00:00 -> 2025-07-13 03:40:11.930000
// Parameters = {'initial_acc': 0.01, 'acc_increment': 0.01, 'max_acc': 1, 'gradient_threshold': 52.95, 'gradient_period': 3}
// PNL = 18.14
// Num Trades = 44
// Sharpe Ratio = 1.82
// CAGR = 8.93
// Max. Drawdown = 66.8415
// Rank = 0
// Crowding Distance = inf

GradientPsar::GradientPsar(char *exchange_c, char *symbol_c, char *timeframe_c, long long from_time, long long to_time)
{
    exchange = exchange_c;
    symbol = symbol_c;
    timeframe = timeframe_c;

    Database db(exchange);
    int array_size = 0;
    double **res = db.get_data(symbol, exchange, array_size);
    db.close_file();

    std::tie(ts, open, high, low, close, volume) = rearrange_candles(res, timeframe, from_time, to_time, array_size);
}

void GradientPsar::execute_backtest(double initial_acc, double acc_increment, double max_acc, double gradient_threshold, int gradient_period)
{
    pnl = 0.0;
    max_dd = 0.0;
    num_trades = 0;
    std::vector<double> returns;

    double max_pnl = 0.0;
    int current_position = 0;
    double entry_price = 0.0;

    int trend[2] = {0, 0};
    double sar[2] = {0.0, 0.0};
    double ep[2] = {0.0, 0.0};
    double af[2] = {0.0, 0.0};

    double temp_sar = 0.0;
    std::vector<double> sar_history;

    // Initial trend setup
    trend[0] = close[1] > close[0] ? 1 : -1;
    sar[0] = trend[0] > 0 ? high[0] : low[0];
    ep[0] = trend[0] > 0 ? high[1] : low[1];
    af[0] = initial_acc;
    sar_history.push_back(sar[0]);

    for (int i = 2; i < ts.size(); i++)
    {
        // Calculate new SAR
        temp_sar = sar[0] + af[0] * (ep[0] - sar[0]);

        if (trend[0] < 0)
        {
            if (trend[0] <= -2)
                temp_sar = max(temp_sar, max(high[i - 1], high[i - 2]));
            trend[1] = temp_sar < high[i] ? 1 : trend[0] - 1;
        }
        else
        {
            if (trend[0] >= 2)
                temp_sar = min(temp_sar, min(low[i - 1], low[i - 2]));
            trend[1] = temp_sar > low[i] ? -1 : trend[0] + 1;
        }

        // Update EP
        if (trend[1] < 0)
            ep[1] = trend[1] != -1 ? min(low[i], ep[0]) : low[i];
        else
            ep[1] = trend[1] != 1 ? max(high[i], ep[0]) : high[i];

        // Update AF
        if (abs(trend[1]) == 1)
        {
            sar[1] = ep[0];
            af[1] = initial_acc;
        }
        else
        {
            sar[1] = temp_sar;
            af[1] = (ep[1] == ep[0]) ? af[0] : min(max_acc, af[0] + acc_increment);
        }

        // Store SAR for gradient calculation
        sar_history.push_back(sar[1]);
        if (sar_history.size() > gradient_period)
            sar_history.erase(sar_history.begin());

        // Compute angle from gradient
        double angle = 0.0;
        if (sar_history.size() >= gradient_period)
        {
            double delta_sar = sar_history.back() - sar_history[sar_history.size() - gradient_period];
            double gradient = delta_sar / (gradient_period - 1);
            angle = atan(gradient) * 180.0 / M_PI;
        }

        bool valid_gradient = fabs(angle) >= gradient_threshold;

        // === Entry Logic (Standard PSAR) ===

        // Long Entry (trend flip up)
        if (trend[1] == 1 && trend[0] < 0 && current_position == 0)
        {
            current_position = 1;
            entry_price = close[i];
        }

        // Short Entry (trend flip down)
        else if (trend[1] == -1 && trend[0] > 0 && current_position == 0)
        {
            current_position = -1;
            entry_price = close[i];
        }

        // === Exit Logic (Require Gradient Confirmation) ===

        // Exit Long on strong reversal down
        else if (current_position == 1 && trend[1] == -1 && valid_gradient)
        {
            double pnl_temp = (close[i] / entry_price - 1) * 100.0;
            pnl += pnl_temp;
            max_pnl = max(max_pnl, pnl);
            max_dd = max(max_dd, max_pnl - pnl);
            num_trades++;
            current_position = 0;
            returns.push_back(pnl);
        }

        // Exit Short on strong reversal up
        else if (current_position == -1 && trend[1] == 1 && valid_gradient)
        {
            double pnl_temp = (entry_price / close[i] - 1) * 100.0;
            pnl += pnl_temp;
            max_pnl = max(max_pnl, pnl);
            max_dd = max(max_dd, max_pnl - pnl);
            num_trades++;
            current_position = 0;
            returns.push_back(pnl);
        }

        // Update state
        trend[0] = trend[1];
        sar[0] = sar[1];
        ep[0] = ep[1];
        af[0] = af[1];
    }

    long long from_time = ts[0];
    long long to_time = ts.back();
    sharpe_ratio = compute_sharpe_ratio(returns);
    // cagr = compute_cagr_from_percent(pnl, from_time, to_time);
    cagr = compute_cagr_from_returns(returns, from_time, to_time, 10000.0);
    double years = compute_years_from_ms(from_time, to_time);
    // printf("Sharpe: %f | CAGR: %f | %lld -> %lld Years: %f\n", sharpe_ratio, cagr, from_time, to_time, years);

}

extern "C"
{
    GradientPsar *GradientPsar_new(char *exchange, char *symbol, char *timeframe, long long from_time, long long to_time)
    {
        return new GradientPsar(exchange, symbol, timeframe, from_time, to_time);
    }

    void GradientPsar_execute_backtest(GradientPsar *gradient_psar, double initial_acc, double acc_increment, double max_acc, double gradient_threshold, int gradient_period)
    {
        return gradient_psar->execute_backtest(initial_acc, acc_increment, max_acc, gradient_threshold, gradient_period);
    }

    double GradientPsar_get_pnl(GradientPsar *gradient_psar) { return gradient_psar->pnl; }
    double GradientPsar_get_max_dd(GradientPsar *gradient_psar) { return gradient_psar->max_dd; }
    int GradientPsar_get_num_trades(GradientPsar *gradient_psar) { return gradient_psar->num_trades; }
    double GradientPsar_get_sharpe_ratio(GradientPsar *gradient_psar) { return gradient_psar->sharpe_ratio; }
    double GradientPsar_get_cagr(GradientPsar *gradient_psar) { return gradient_psar->cagr; }
}