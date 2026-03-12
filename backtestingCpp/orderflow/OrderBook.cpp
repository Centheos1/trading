#include "OrderBook.h"
#include <algorithm>
#include <cmath>

namespace orderflow {

void OrderBook::on_depth_update(const DepthUpdate& update) {
    std::lock_guard<std::mutex> lock(mutex_);

    if (update.is_snapshot) {
        bids_.clear();
        asks_.clear();
    }

    if (!initialized_ && !update.is_snapshot) {
        return;
    }

    for (const auto& level : update.bids) {
        if (level.quantity == 0.0) {
            bids_.erase(level.price);
        } else {
            auto it = bids_.find(level.price);
            double prev = (it != bids_.end()) ? it->second.quantity : 0.0;
            bids_[level.price] = {level.quantity, prev, update.timestamp};
        }
    }

    for (const auto& level : update.asks) {
        if (level.quantity == 0.0) {
            asks_.erase(level.price);
        } else {
            auto it = asks_.find(level.price);
            double prev = (it != asks_.end()) ? it->second.quantity : 0.0;
            asks_[level.price] = {level.quantity, prev, update.timestamp};
        }
    }

    last_update_ts_ = update.timestamp;
    last_update_id_ = update.final_update_id;

    if (update.is_snapshot) {
        initialized_ = true;
    }
}

void OrderBook::on_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);
    detect_absorption(trade);
}

void OrderBook::detect_absorption(const Trade& trade) {
    if (!initialized_) return;

    double best_bid = get_best_bid();
    double best_ask = get_best_ask();

    if (best_bid != last_best_bid_ || best_ask != last_best_ask_) {
        cumulative_trade_volume_at_best_ = 0.0;
        last_best_bid_ = best_bid;
        last_best_ask_ = best_ask;
    }

    cumulative_trade_volume_at_best_ += trade.quantity;

    if (trade.is_sell_aggressor() && std::abs(trade.price - best_bid) < 1e-10) {
        auto it = bids_.find(trade.price);
        if (it != bids_.end() && it->second.quantity > 0) {
            double ratio = cumulative_trade_volume_at_best_ / it->second.quantity;
            if (ratio > ABSORPTION_VOLUME_THRESHOLD) {
                AbsorptionEvent evt{trade.timestamp, trade.price,
                                    cumulative_trade_volume_at_best_, true};
                absorption_events_.push_back(evt);
                if (absorption_events_.size() > MAX_ABSORPTION_EVENTS) {
                    absorption_events_.pop_front();
                }
            }
        }
    }

    if (trade.is_buy_aggressor() && std::abs(trade.price - best_ask) < 1e-10) {
        auto it = asks_.find(trade.price);
        if (it != asks_.end() && it->second.quantity > 0) {
            double ratio = cumulative_trade_volume_at_best_ / it->second.quantity;
            if (ratio > ABSORPTION_VOLUME_THRESHOLD) {
                AbsorptionEvent evt{trade.timestamp, trade.price,
                                    cumulative_trade_volume_at_best_, false};
                absorption_events_.push_back(evt);
                if (absorption_events_.size() > MAX_ABSORPTION_EVENTS) {
                    absorption_events_.pop_front();
                }
            }
        }
    }
}

OrderBookSnapshot OrderBook::get_snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    OrderBookSnapshot snap;
    snap.timestamp = last_update_ts_;

    for (const auto& [price, level] : bids_) {
        snap.bids[price] = level.quantity;
    }
    for (const auto& [price, level] : asks_) {
        snap.asks[price] = level.quantity;
    }

    snap.best_bid = get_best_bid();
    snap.best_ask = get_best_ask();
    snap.mid_price = (snap.best_bid + snap.best_ask) / 2.0;
    snap.spread = snap.best_ask - snap.best_bid;
    return snap;
}

double OrderBook::get_best_bid() const {
    if (bids_.empty()) return 0.0;
    return bids_.begin()->first;
}

double OrderBook::get_best_ask() const {
    if (asks_.empty()) return 0.0;
    return asks_.begin()->first;
}

double OrderBook::get_mid_price() const {
    return (get_best_bid() + get_best_ask()) / 2.0;
}

double OrderBook::get_spread() const {
    return get_best_ask() - get_best_bid();
}

double OrderBook::get_bid_depth(int levels) const {
    double total = 0.0;
    int count = 0;
    for (const auto& [price, level] : bids_) {
        if (count >= levels) break;
        total += level.quantity;
        count++;
    }
    return total;
}

double OrderBook::get_ask_depth(int levels) const {
    double total = 0.0;
    int count = 0;
    for (const auto& [price, level] : asks_) {
        if (count >= levels) break;
        total += level.quantity;
        count++;
    }
    return total;
}

double OrderBook::get_imbalance(int levels) const {
    double bid_depth = get_bid_depth(levels);
    double ask_depth = get_ask_depth(levels);
    double total = bid_depth + ask_depth;
    if (total == 0.0) return 0.0;
    return (bid_depth - ask_depth) / total;
}

std::vector<std::pair<double, double>> OrderBook::get_bids(int levels) const {
    std::vector<std::pair<double, double>> result;
    int count = 0;
    for (const auto& [price, level] : bids_) {
        if (count >= levels) break;
        result.emplace_back(price, level.quantity);
        count++;
    }
    return result;
}

std::vector<std::pair<double, double>> OrderBook::get_asks(int levels) const {
    std::vector<std::pair<double, double>> result;
    int count = 0;
    for (const auto& [price, level] : asks_) {
        if (count >= levels) break;
        result.emplace_back(price, level.quantity);
        count++;
    }
    return result;
}

std::vector<OrderBook::StackedImbalance>
OrderBook::detect_stacked_imbalances(double threshold, int min_levels) const {
    std::vector<StackedImbalance> results;

    auto bid_it = bids_.begin();
    auto ask_it = asks_.begin();

    int consecutive_bid_heavy = 0;
    int consecutive_ask_heavy = 0;
    double bid_start_price = 0.0;
    double ask_start_price = 0.0;

    size_t max_levels = std::min(bids_.size(), asks_.size());
    for (size_t i = 0; i < max_levels; ++i) {
        double bid_qty = bid_it->second.quantity;
        double ask_qty = ask_it->second.quantity;
        double total = bid_qty + ask_qty;

        if (total > 0.0) {
            double ratio = bid_qty / (ask_qty > 0.0 ? ask_qty : 1e-10);

            if (ratio > threshold) {
                if (consecutive_bid_heavy == 0) bid_start_price = bid_it->first;
                consecutive_bid_heavy++;
                consecutive_ask_heavy = 0;
            } else if (1.0 / ratio > threshold) {
                if (consecutive_ask_heavy == 0) ask_start_price = ask_it->first;
                consecutive_ask_heavy++;
                consecutive_bid_heavy = 0;
            } else {
                if (consecutive_bid_heavy >= min_levels) {
                    results.push_back({last_update_ts_, bid_start_price,
                                       bid_it->first, consecutive_bid_heavy, true});
                }
                if (consecutive_ask_heavy >= min_levels) {
                    results.push_back({last_update_ts_, ask_start_price,
                                       ask_it->first, consecutive_ask_heavy, false});
                }
                consecutive_bid_heavy = 0;
                consecutive_ask_heavy = 0;
            }
        }
        ++bid_it;
        ++ask_it;
    }

    if (consecutive_bid_heavy >= min_levels) {
        results.push_back({last_update_ts_, bid_start_price,
                           bid_it->first, consecutive_bid_heavy, true});
    }
    if (consecutive_ask_heavy >= min_levels) {
        results.push_back({last_update_ts_, ask_start_price,
                           ask_it->first, consecutive_ask_heavy, false});
    }

    return results;
}

} // namespace orderflow
