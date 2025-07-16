#ifndef METRICS_H
#define METRICS_H

#include <vector>

double compute_sharpe_ratio(const std::vector<double>& returns, double risk_free_rate = 0.0);
double compute_cagr_from_percent(double total_return_percent, long long from_time, long long to_time);
double compute_cagr_from_returns(const std::vector<double>& returns, long long from_time, long long to_time, double initial_value);
double compute_years_from_ms(long long from_time, long long to_time);

#endif // METRICS_H
