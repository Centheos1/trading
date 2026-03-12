#include "FootprintChart.h"
#include <algorithm>
#include <cmath>

namespace orderflow {

FootprintChart::FootprintChart(int64_t bar_duration_ms, double tick_size)
    : bar_duration_ms_(bar_duration_ms), tick_size_(tick_size) {}

double FootprintChart::round_to_tick(double price) const {
    return std::round(price / tick_size_) * tick_size_;
}

void FootprintChart::on_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);

    if (bars_.empty()) {
        start_new_bar(trade);
        return;
    }

    auto& current = bars_.back();
    if (trade.timestamp >= current.open_time + bar_duration_ms_) {
        current.close_time = current.open_time + bar_duration_ms_;
        start_new_bar(trade);
        return;
    }

    double rounded = round_to_tick(trade.price);
    auto& cell = current.cells[rounded];

    if (trade.is_buy_aggressor()) {
        cell.ask_volume += trade.quantity;
    } else {
        cell.bid_volume += trade.quantity;
    }

    current.high = std::max(current.high, trade.price);
    current.low = std::min(current.low, trade.price);
    current.close = trade.price;
    current.close_time = trade.timestamp;
}

void FootprintChart::start_new_bar(const Trade& trade) {
    int64_t bar_open = (trade.timestamp / bar_duration_ms_) * bar_duration_ms_;
    FootprintBar bar;
    bar.open_time = bar_open;
    bar.close_time = trade.timestamp;
    bar.open = trade.price;
    bar.high = trade.price;
    bar.low = trade.price;
    bar.close = trade.price;

    double rounded = round_to_tick(trade.price);
    auto& cell = bar.cells[rounded];
    if (trade.is_buy_aggressor()) {
        cell.ask_volume += trade.quantity;
    } else {
        cell.bid_volume += trade.quantity;
    }

    bars_.push_back(std::move(bar));
    if (bars_.size() > MAX_BARS) {
        bars_.erase(bars_.begin());
    }
}

void FootprintChart::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    bars_.clear();
}

const FootprintBar& FootprintChart::get_current_bar() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return bars_.back();
}

std::vector<FootprintBar> FootprintChart::get_bars(int count) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (count <= 0 || bars_.empty()) return {};
    int start = std::max(0, (int)bars_.size() - count);
    return {bars_.begin() + start, bars_.end()};
}

size_t FootprintChart::bar_count() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return bars_.size();
}

} // namespace orderflow
