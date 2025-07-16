// #include <iostream>
// #include <hdf5.h>

// int main(int, char **)
// {
//     std::cout << "Hello world\n";
//     hid_t falp = H5Pcreate(H5P_FILE_ACCESS);
// }

#include <iostream>
#include <cstring>

#include "strategies/Sma.h"
#include "strategies/Psar.h"
#include "strategies/Atr.h"
#include "strategies/GradientPsar.h"

int main(int, char **)
{

    printf("###### Start ######\n");
    // std::string symbol = "BTCUSDT";
    // std::string exchange = "binance";
    // std::string symbol = "AU200_AUD";
    std::string symbol = "BTC_USD";
    // std::string symbol = "AUD_USD";
    // std::string symbol = "XAU_AUD";
    std::string exchange = "oanda";
    // std::string timeframe = "5m";
    std::string timeframe = "1d";

    char *symbol_char = strcpy((char *)malloc(symbol.length() + 1), symbol.c_str());
    char *exchange_char = strcpy((char *)malloc(exchange.length() + 1), exchange.c_str());
    char *tf_char = strcpy((char *)malloc(timeframe.length() + 1), timeframe.c_str());

    long long from_time = 0;
    long long to_time = 1735603200000;

    // Sma sma(exchange_char, symbol_char, tf_char, from_time, to_time);
    // int slow_ma = 15;
    // int fast_ma = 8;
    // sma.execute_backtest(slow_ma, fast_ma);
    // printf("SMA: PNL: %f | Max Drawdown: %f | Num Trades: %d | Sharpe: %f | CAGR: %f\n\n", sma.pnl, sma.max_dd, sma.num_trades, sma.sharpe_ratio, sma.cagr);

    // double initial_acc = 0.02;
    // double acc_increment = 0.02; 
    // double max_acc = 0.2;
    // Psar psar(exchange_char, symbol_char, tf_char, from_time, to_time);
    // psar.execute_backtest(0.02, 0.02, 0.2);
    // printf("PSAR: PNL: %f | Max Drawdown: %f | Num Trades: %d | Sharpe: %f | CAGR: %f\n\n", psar.pnl, psar.max_dd, psar.num_trades, psar.sharpe_ratio, psar.cagr);

    // double period = 9;
    // double atr_multiplier = 1.5;
    // Atr atr(exchange_char, symbol_char, tf_char, from_time, to_time);
    // atr.execute_backtest(period, atr_multiplier);
    // printf("ATR: PNL: %f | Max Drawdown: %f | Num Trades: %d | Sharpe: %f | CAGR: %f\n\n", atr.pnl, atr.max_dd, atr.num_trades, atr.sharpe_ratio, atr.cagr);

    double initial_acc = 0.02;
    double acc_increment = 0.02; 
    double max_acc = 0.2;
    double gradient_threshold = 75; // degree of the angle to be stronger than
    int gradient_period = 5; // the max window to measure the angle
    GradientPsar gradient_psar(exchange_char, symbol_char, tf_char, from_time, to_time);
    gradient_psar.execute_backtest(initial_acc, acc_increment, max_acc, gradient_threshold, gradient_period);
    printf("GradientPsar: PNL: %f | Max Drawdown: %f | Num Trades: %d | Sharpe: %f | CAGR: %f\n\n", gradient_psar.pnl, gradient_psar.max_dd, gradient_psar.num_trades, gradient_psar.sharpe_ratio, gradient_psar.cagr);

    printf("###### Finish ######\n");
}
