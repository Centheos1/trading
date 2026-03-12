#include "TradeFlow.h"
#include <algorithm>
#include <cmath>

namespace orderflow {

TradeFlow::TradeFlow(double large_trade_threshold, int64_t cluster_window_ms)
    : large_trade_threshold_(large_trade_threshold),
      cluster_window_ms_(cluster_window_ms) {}

void TradeFlow::on_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);

    trades_.push_back(trade);
    if (trades_.size() > max_trades_) {
        trades_.pop_front();
    }

    if (trade.is_buy_aggressor()) {
        cumulative_buy_volume_ += trade.quantity;
    } else {
        cumulative_sell_volume_ += trade.quantity;
    }

    detect_large_trade(trade);
    detect_iceberg(trade);
}

double TradeFlow::get_delta() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cumulative_buy_volume_ - cumulative_sell_volume_;
}

double TradeFlow::get_buy_volume() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cumulative_buy_volume_;
}

double TradeFlow::get_sell_volume() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cumulative_sell_volume_;
}

double TradeFlow::get_total_volume() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cumulative_buy_volume_ + cumulative_sell_volume_;
}

double TradeFlow::get_delta_window(int64_t window_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (trades_.empty()) return 0.0;

    int64_t cutoff = trades_.back().timestamp - window_ms;
    double buy = 0.0, sell = 0.0;

    for (auto it = trades_.rbegin(); it != trades_.rend(); ++it) {
        if (it->timestamp < cutoff) break;
        if (it->is_buy_aggressor()) buy += it->quantity;
        else sell += it->quantity;
    }
    return buy - sell;
}

double TradeFlow::get_buy_volume_window(int64_t window_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (trades_.empty()) return 0.0;

    int64_t cutoff = trades_.back().timestamp - window_ms;
    double vol = 0.0;

    for (auto it = trades_.rbegin(); it != trades_.rend(); ++it) {
        if (it->timestamp < cutoff) break;
        if (it->is_buy_aggressor()) vol += it->quantity;
    }
    return vol;
}

double TradeFlow::get_sell_volume_window(int64_t window_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (trades_.empty()) return 0.0;

    int64_t cutoff = trades_.back().timestamp - window_ms;
    double vol = 0.0;

    for (auto it = trades_.rbegin(); it != trades_.rend(); ++it) {
        if (it->timestamp < cutoff) break;
        if (it->is_sell_aggressor()) vol += it->quantity;
    }
    return vol;
}

void TradeFlow::detect_large_trade(const Trade& trade) {
    if (trade.quantity >= large_trade_threshold_) {
        TradeCluster cluster{trade.timestamp, trade.timestamp, trade.price,
                             trade.quantity, 1, trade.is_buy_aggressor()};
        large_trades_.push_back(cluster);
        if (large_trades_.size() > MAX_LARGE_TRADES) {
            large_trades_.pop_front();
        }
        return;
    }

    if (!large_trades_.empty()) {
        auto& last = large_trades_.back();
        bool same_side = (trade.is_buy_aggressor() == last.is_buy);
        bool within_window = (trade.timestamp - last.end_ts) < cluster_window_ms_;
        bool same_price_area = std::abs(trade.price - last.price) / last.price < 0.001;

        if (same_side && within_window && same_price_area) {
            last.total_quantity += trade.quantity;
            last.trade_count++;
            last.end_ts = trade.timestamp;
            if (last.total_quantity >= large_trade_threshold_) {
                return;
            }
        }
    }
}

void TradeFlow::detect_iceberg(const Trade& trade) {
    bool same_price = std::abs(trade.price - iceberg_state_.price) < 1e-10;
    bool is_same_side = trade.is_sell_aggressor() == iceberg_state_.is_bid;

    if (same_price && is_same_side) {
        iceberg_state_.total_volume += trade.quantity;
        iceberg_state_.refill_count++;
    } else {
        if (iceberg_state_.refill_count >= 5 &&
            iceberg_state_.total_volume >= large_trade_threshold_ * 2) {
            TradeCluster iceberg{
                iceberg_state_.first_ts, trade.timestamp,
                iceberg_state_.price, iceberg_state_.total_volume,
                iceberg_state_.refill_count, !iceberg_state_.is_bid};
            large_trades_.push_back(iceberg);
            if (large_trades_.size() > MAX_LARGE_TRADES) {
                large_trades_.pop_front();
            }
        }

        iceberg_state_.price = trade.price;
        iceberg_state_.total_volume = trade.quantity;
        iceberg_state_.refill_count = 1;
        iceberg_state_.first_ts = trade.timestamp;
        iceberg_state_.is_bid = trade.is_sell_aggressor();
    }
}

void TradeFlow::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    trades_.clear();
    large_trades_.clear();
    cumulative_buy_volume_ = 0.0;
    cumulative_sell_volume_ = 0.0;
    iceberg_state_ = {};
}

} // namespace orderflow
