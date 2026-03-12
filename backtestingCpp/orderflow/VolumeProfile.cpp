#include "VolumeProfile.h"
#include <algorithm>
#include <cmath>
#include <numeric>

namespace orderflow {

VolumeProfile::VolumeProfile(double tick_size)
    : tick_size_(tick_size), window_ms_(0) {}

double VolumeProfile::round_to_tick(double price) const {
    return std::round(price / tick_size_) * tick_size_;
}

void VolumeProfile::set_window(int64_t window_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    window_ms_ = window_ms;
}

int64_t VolumeProfile::get_window() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return window_ms_;
}

void VolumeProfile::trim_expired(int64_t current_time) {
    if (window_ms_ <= 0) return;
    int64_t cutoff = current_time - window_ms_;
    while (!trades_.empty() && trades_.front().timestamp < cutoff) {
        const auto& old = trades_.front();
        auto it = levels_.find(old.rounded_price);
        if (it != levels_.end()) {
            auto& lv = it->second;
            lv.total_volume -= old.quantity;
            if (old.is_buy)
                lv.buy_volume -= old.quantity;
            else
                lv.sell_volume -= old.quantity;
            if (lv.total_volume <= 1e-12)
                levels_.erase(it);
        }
        trades_.pop_front();
    }
    if (!trades_.empty())
        start_time_ = trades_.front().timestamp;
}

void VolumeProfile::on_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);

    double rounded = round_to_tick(trade.price);
    bool is_buy = trade.is_buy_aggressor();

    auto& level = levels_[rounded];
    level.total_volume += trade.quantity;
    if (is_buy) {
        level.buy_volume += trade.quantity;
    } else {
        level.sell_volume += trade.quantity;
    }

    trades_.push_back({trade.timestamp, rounded, trade.quantity, is_buy});

    if (start_time_ == 0) start_time_ = trade.timestamp;
    end_time_ = trade.timestamp;

    trim_expired(trade.timestamp);
}

void VolumeProfile::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    levels_.clear();
    trades_.clear();
    start_time_ = 0;
    end_time_ = 0;
}

double VolumeProfile::get_poc_price() const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (levels_.empty()) return 0.0;

    double max_vol = 0.0;
    double poc = 0.0;
    for (const auto& [price, level] : levels_) {
        if (level.total_volume > max_vol) {
            max_vol = level.total_volume;
            poc = price;
        }
    }
    return poc;
}

double VolumeProfile::get_volume_at_price(double price) const {
    std::lock_guard<std::mutex> lock(mutex_);
    double rounded = round_to_tick(price);
    auto it = levels_.find(rounded);
    if (it == levels_.end()) return 0.0;
    return it->second.total_volume;
}

ValueArea VolumeProfile::compute_value_area(double pct) const {
    std::lock_guard<std::mutex> lock(mutex_);
    ValueArea va{};
    if (levels_.empty()) return va;

    double total = 0.0;
    double max_vol = 0.0;

    for (const auto& [price, level] : levels_) {
        total += level.total_volume;
        if (level.total_volume > max_vol) {
            max_vol = level.total_volume;
            va.poc_price = price;
        }
    }
    va.poc_volume = max_vol;
    va.total_volume = total;

    double target = total * pct;
    double accumulated = max_vol;

    auto poc_it = levels_.find(va.poc_price);
    auto upper = poc_it;
    auto lower = poc_it;

    va.vah = va.poc_price;
    va.val = va.poc_price;

    while (accumulated < target) {
        auto next_upper = std::next(upper);
        auto prev_lower = (lower == levels_.begin()) ? levels_.end() : std::prev(lower);

        double upper_vol = (next_upper != levels_.end()) ? next_upper->second.total_volume : 0.0;
        double lower_vol = (prev_lower != levels_.end()) ? prev_lower->second.total_volume : 0.0;

        if (upper_vol >= lower_vol && next_upper != levels_.end()) {
            accumulated += upper_vol;
            upper = next_upper;
            va.vah = upper->first;
        } else if (prev_lower != levels_.end()) {
            accumulated += lower_vol;
            lower = prev_lower;
            va.val = lower->first;
        } else {
            break;
        }
    }

    return va;
}

std::vector<VolumeNode> VolumeProfile::get_profile() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<VolumeNode> result;
    result.reserve(levels_.size());
    for (const auto& [price, level] : levels_) {
        result.push_back({price, level.total_volume,
                          level.buy_volume, level.sell_volume});
    }
    return result;
}

std::vector<double> VolumeProfile::get_naked_pocs(
    const std::vector<ValueArea>& historical_vas,
    double current_price) const {
    std::vector<double> naked;
    for (const auto& va : historical_vas) {
        bool is_naked = true;
        for (const auto& [price, level] : levels_) {
            if (std::abs(price - va.poc_price) < tick_size_) {
                is_naked = false;
                break;
            }
        }
        if (is_naked) {
            naked.push_back(va.poc_price);
        }
    }
    std::sort(naked.begin(), naked.end(),
              [current_price](double a, double b) {
                  return std::abs(a - current_price) < std::abs(b - current_price);
              });
    return naked;
}

double VolumeProfile::get_total_volume() const {
    std::lock_guard<std::mutex> lock(mutex_);
    double total = 0.0;
    for (const auto& [price, level] : levels_) {
        total += level.total_volume;
    }
    return total;
}

} // namespace orderflow
