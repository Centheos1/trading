#include "CumulativeVolumeDelta.h"
#include <algorithm>
#include <cmath>

namespace orderflow {

void CumulativeVolumeDelta::on_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);

    double delta_change = trade.is_buy_aggressor() ? trade.quantity : -trade.quantity;
    cumulative_delta_ += delta_change;

    history_.push_back({trade.timestamp, cumulative_delta_, trade.price});
    if (history_.size() > max_history_) {
        history_.pop_front();
    }
}

void CumulativeVolumeDelta::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    history_.clear();
    cumulative_delta_ = 0.0;
}

double CumulativeVolumeDelta::get_cvd() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cumulative_delta_;
}

double CumulativeVolumeDelta::get_session_delta() const {
    return get_cvd();
}

std::vector<CVDPoint> CumulativeVolumeDelta::get_history() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return {history_.begin(), history_.end()};
}

std::vector<CVDPoint> CumulativeVolumeDelta::get_history_window(int64_t window_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (history_.empty()) return {};

    int64_t cutoff = history_.back().timestamp - window_ms;
    std::vector<CVDPoint> result;

    for (auto it = history_.rbegin(); it != history_.rend(); ++it) {
        if (it->timestamp < cutoff) break;
        result.push_back(*it);
    }
    std::reverse(result.begin(), result.end());
    return result;
}

CumulativeVolumeDelta::DivergenceResult
CumulativeVolumeDelta::detect_divergence(int lookback_points) const {
    std::lock_guard<std::mutex> lock(mutex_);
    DivergenceResult result{false, false, 0.0, 0.0, 0, 0};

    if ((int)history_.size() < lookback_points || lookback_points < 2) return result;

    auto it_end = history_.rbegin();
    auto it_start = it_end;
    std::advance(it_start, lookback_points - 1);

    double price_change = it_end->price - it_start->price;
    double delta_change = it_end->delta - it_start->delta;

    bool price_up = price_change > 0;
    bool delta_up = delta_change > 0;

    if (price_up != delta_up) {
        result.detected = true;
        result.is_bearish = price_up && !delta_up;
        result.price_change = price_change;
        result.delta_change = delta_change;
        result.start_ts = it_start->timestamp;
        result.end_ts = it_end->timestamp;
    }

    return result;
}

CumulativeVolumeDelta::ExhaustionResult
CumulativeVolumeDelta::detect_exhaustion(int lookback_bars, double price_change_threshold) const {
    std::lock_guard<std::mutex> lock(mutex_);
    ExhaustionResult result{false, false, 0, 0.0};

    if ((int)history_.size() < lookback_bars * 2) return result;

    int step = std::max(1, (int)history_.size() / lookback_bars);
    std::vector<double> bar_deltas;
    std::vector<double> bar_prices;

    for (int i = 0; i < lookback_bars && i * step < (int)history_.size(); ++i) {
        size_t idx = history_.size() - 1 - i * step;
        size_t prev_idx = (i + 1) * step < history_.size()
                              ? history_.size() - 1 - (i + 1) * step
                              : 0;
        bar_deltas.push_back(history_[idx].delta - history_[prev_idx].delta);
        bar_prices.push_back(history_[idx].price);
    }
    std::reverse(bar_deltas.begin(), bar_deltas.end());
    std::reverse(bar_prices.begin(), bar_prices.end());

    if (bar_prices.size() < 3) return result;

    double total_price_change = bar_prices.back() - bar_prices.front();
    bool trending_up = total_price_change > price_change_threshold;
    bool trending_down = total_price_change < -price_change_threshold;

    if (!trending_up && !trending_down) return result;

    int declining_delta = 0;
    for (size_t i = 1; i < bar_deltas.size(); ++i) {
        if (trending_up && bar_deltas[i] < bar_deltas[i - 1]) {
            declining_delta++;
        } else if (trending_down && bar_deltas[i] > bar_deltas[i - 1]) {
            declining_delta++;
        }
    }

    if (declining_delta >= (int)bar_deltas.size() / 2) {
        result.detected = true;
        result.is_bearish = trending_up;
        result.consecutive_declining = declining_delta;
        result.price_at_extreme = trending_up ? *std::max_element(bar_prices.begin(), bar_prices.end())
                                              : *std::min_element(bar_prices.begin(), bar_prices.end());
    }

    return result;
}

} // namespace orderflow
