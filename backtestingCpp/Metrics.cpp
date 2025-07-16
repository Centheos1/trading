#include <cmath>
#include <numeric>
#include <limits>

#include "Metrics.h"

// Sharpe Ratio = (Mean Return - Risk-Free Rate) / Std Dev of Returns
double compute_sharpe_ratio(const std::vector<double>& returns, double risk_free_rate)
{
    if (returns.empty()) return 0.0;

    double mean = std::accumulate(returns.begin(), returns.end(), 0.0) / returns.size();

    double variance = 0.0;
    for (double r : returns)
        variance += (r - mean) * (r - mean);
    variance /= returns.size();

    double std_dev = std::sqrt(variance);
    if (std_dev == 0.0) return 0.0;

    return (mean - risk_free_rate) / std_dev;
}

// CAGR using total % return and years elapsed
double compute_cagr_from_percent(double total_return_percent, long long from_time, long long to_time)
{
    double years = compute_years_from_ms(from_time, to_time);
    if (years <= 0.0)
        return 0.0;

    double multiplier = 1.0 + total_return_percent / 100.0;

    if (multiplier <= 0.0)
        return -1.0; // Represents complete loss or worse

    return std::pow(multiplier, 1.0 / years) - 1.0;
}

double compute_cagr_from_returns(const std::vector<double>& returns, long long from_time, long long to_time, double initial_value)
{
    double years = compute_years_from_ms(from_time, to_time);
    if (years <= 0.0)
        return 0.0;
    double final_value = initial_value;
    for (double r : returns) {
        final_value *= (1 + r / 100);
    }
    if (final_value <= 0) {
        return -1.0;
    }
    
    double cagr = std::pow((final_value / initial_value), (1.0 / years)) - 1.0;
    return cagr;
}

// Convert millisecond timestamps to fractional years
double compute_years_from_ms(long long from_time, long long to_time)
{
    return static_cast<double>(to_time - from_time) / 31536000000.0;  // 365 days
}
