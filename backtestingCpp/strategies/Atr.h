#include <string>
#include <vector>


class Atr
{
    public:
        Atr(char* exchange_c, char* symbol_c, char* timeframe_c, long long from_time, long long to_time);
        void execute_backtest(int period, double atr_multiplier);

        std::string exchange;
        std::string symbol;
        std::string timeframe;

        std::vector<double> ts, open, high, low, close, volume, spread;
        // std::vector<double> ts, open, high, low, close, volume;

        double pnl = 0.0;
        double max_dd;
        int num_trades = 0;
        double sharpe_ratio = 0.0;
        double cagr = 0.0;
    
    private:
        double calculate_true_range(int i);
        double calculate_atr(int i, int period);
};