#pragma once

#include <map>
#include <vector>
#include <mutex>
#include <deque>
#include <cstdint>
#include "Types.h"

namespace orderflow {

struct OrderBookLevel {
    double quantity = 0.0;
    double prev_quantity = 0.0;
    int64_t last_update_ts = 0;
};

struct OrderBookSnapshot {
    int64_t timestamp;
    std::map<double, double, std::greater<double>> bids;
    std::map<double, double> asks;
    double best_bid;
    double best_ask;
    double mid_price;
    double spread;
};

class OrderBook {
public:
    OrderBook() = default;

    void on_depth_update(const DepthUpdate& update);
    void on_trade(const Trade& trade);

    OrderBookSnapshot get_snapshot() const;
    double get_best_bid() const;
    double get_best_ask() const;
    double get_mid_price() const;
    double get_spread() const;

    double get_bid_depth(int levels) const;
    double get_ask_depth(int levels) const;
    double get_imbalance(int levels) const;

    std::vector<std::pair<double, double>> get_bids(int levels) const;
    std::vector<std::pair<double, double>> get_asks(int levels) const;

    struct AbsorptionEvent {
        int64_t timestamp;
        double price;
        double absorbed_volume;
        bool is_bid_absorbing;
    };
    const std::deque<AbsorptionEvent>& get_absorption_events() const { return absorption_events_; }

    struct StackedImbalance {
        int64_t timestamp;
        double start_price;
        double end_price;
        int levels;
        bool is_bid_heavy;
    };
    std::vector<StackedImbalance> detect_stacked_imbalances(double threshold, int min_levels) const;

    int64_t last_update_time() const { return last_update_ts_; }

private:
    void detect_absorption(const Trade& trade);

    mutable std::mutex mutex_;
    std::map<double, OrderBookLevel, std::greater<double>> bids_;
    std::map<double, OrderBookLevel> asks_;
    int64_t last_update_ts_ = 0;
    int64_t last_update_id_ = 0;
    bool initialized_ = false;

    static constexpr double ABSORPTION_VOLUME_THRESHOLD = 5.0;
    static constexpr size_t MAX_ABSORPTION_EVENTS = 1000;
    std::deque<AbsorptionEvent> absorption_events_;

    double cumulative_trade_volume_at_best_ = 0.0;
    double last_best_bid_ = 0.0;
    double last_best_ask_ = 0.0;
};

} // namespace orderflow
