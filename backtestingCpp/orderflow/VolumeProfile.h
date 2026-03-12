#pragma once

#include <map>
#include <deque>
#include <vector>
#include <mutex>
#include <cstdint>
#include "Types.h"

namespace orderflow {

struct ValueArea {
    double poc_price;
    double poc_volume;
    double vah;
    double val;
    double total_volume;
};

class VolumeProfile {
public:
    explicit VolumeProfile(double tick_size = 0.01);

    void on_trade(const Trade& trade);
    void reset();

    void set_tick_size(double tick_size) { tick_size_ = tick_size; }
    double get_tick_size() const { return tick_size_; }

    void set_window(int64_t window_ms);
    int64_t get_window() const;

    ValueArea compute_value_area(double pct = 0.70) const;
    double get_poc_price() const;
    double get_volume_at_price(double price) const;

    std::vector<VolumeNode> get_profile() const;
    std::vector<double> get_naked_pocs(const std::vector<ValueArea>& historical_vas,
                                        double current_price) const;

    double get_total_volume() const;
    int64_t get_start_time() const { return start_time_; }
    int64_t get_end_time() const { return end_time_; }

private:
    double round_to_tick(double price) const;
    void trim_expired(int64_t current_time);

    mutable std::mutex mutex_;
    double tick_size_;
    int64_t window_ms_ = 0;

    struct PriceLevel {
        double total_volume = 0.0;
        double buy_volume = 0.0;
        double sell_volume = 0.0;
    };
    std::map<double, PriceLevel> levels_;

    struct StoredTrade {
        int64_t timestamp;
        double rounded_price;
        double quantity;
        bool is_buy;
    };
    std::deque<StoredTrade> trades_;

    int64_t start_time_ = 0;
    int64_t end_time_ = 0;
};

} // namespace orderflow
