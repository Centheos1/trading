#pragma once

#include <deque>
#include <vector>
#include <mutex>
#include <cstdint>
#include "Types.h"

namespace orderflow {

struct TradeCluster {
    int64_t start_ts;
    int64_t end_ts;
    double price;
    double total_quantity;
    int trade_count;
    bool is_buy;
};

class TradeFlow {
public:
    explicit TradeFlow(double large_trade_threshold = 10.0,
                       int64_t cluster_window_ms = 500);

    void on_trade(const Trade& trade);

    double get_delta() const;
    double get_buy_volume() const;
    double get_sell_volume() const;
    double get_total_volume() const;

    double get_delta_window(int64_t window_ms) const;
    double get_buy_volume_window(int64_t window_ms) const;
    double get_sell_volume_window(int64_t window_ms) const;

    const std::deque<Trade>& get_recent_trades() const { return trades_; }
    const std::deque<TradeCluster>& get_large_trades() const { return large_trades_; }

    void reset();

    void set_large_trade_threshold(double threshold) { large_trade_threshold_ = threshold; }
    void set_max_trades(size_t max_trades) { max_trades_ = max_trades; }

private:
    void detect_large_trade(const Trade& trade);
    void detect_iceberg(const Trade& trade);

    mutable std::mutex mutex_;
    std::deque<Trade> trades_;
    std::deque<TradeCluster> large_trades_;

    double cumulative_buy_volume_ = 0.0;
    double cumulative_sell_volume_ = 0.0;

    double large_trade_threshold_;
    int64_t cluster_window_ms_;
    size_t max_trades_ = 100000;
    static constexpr size_t MAX_LARGE_TRADES = 5000;

    struct IcebergState {
        double price = 0.0;
        double total_volume = 0.0;
        int refill_count = 0;
        int64_t first_ts = 0;
        bool is_bid = false;
    };
    IcebergState iceberg_state_;
};

} // namespace orderflow
