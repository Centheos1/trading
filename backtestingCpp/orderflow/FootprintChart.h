#pragma once

#include <map>
#include <vector>
#include <mutex>
#include <cstdint>
#include "Types.h"

namespace orderflow {

struct FootprintBar {
    int64_t open_time;
    int64_t close_time;
    double open;
    double high;
    double low;
    double close;
    std::map<double, FootprintCell> cells;

    double total_delta() const {
        double d = 0.0;
        for (const auto& [price, cell] : cells) {
            d += cell.ask_volume - cell.bid_volume;
        }
        return d;
    }

    double total_volume() const {
        double v = 0.0;
        for (const auto& [price, cell] : cells) {
            v += cell.bid_volume + cell.ask_volume;
        }
        return v;
    }

    struct ImbalanceLevel {
        double price;
        double ratio;
        bool is_ask_dominant;
    };

    std::vector<ImbalanceLevel> get_imbalances(double threshold) const {
        std::vector<ImbalanceLevel> result;
        for (const auto& [price, cell] : cells) {
            double total = cell.bid_volume + cell.ask_volume;
            if (total < 1e-10) continue;
            double ratio = cell.ask_volume / (cell.bid_volume > 0 ? cell.bid_volume : 1e-10);
            if (ratio > threshold) {
                result.push_back({price, ratio, true});
            } else if (1.0 / ratio > threshold) {
                result.push_back({price, 1.0 / ratio, false});
            }
        }
        return result;
    }
};

class FootprintChart {
public:
    explicit FootprintChart(int64_t bar_duration_ms = 60000, double tick_size = 0.01);

    void on_trade(const Trade& trade);
    void reset();

    const FootprintBar& get_current_bar() const;
    std::vector<FootprintBar> get_bars(int count) const;
    size_t bar_count() const;

    void set_bar_duration(int64_t duration_ms) { bar_duration_ms_ = duration_ms; }
    void set_tick_size(double tick_size) { tick_size_ = tick_size; }

private:
    double round_to_tick(double price) const;
    void start_new_bar(const Trade& trade);

    mutable std::mutex mutex_;
    int64_t bar_duration_ms_;
    double tick_size_;
    std::vector<FootprintBar> bars_;
    static constexpr size_t MAX_BARS = 10000;
};

} // namespace orderflow
