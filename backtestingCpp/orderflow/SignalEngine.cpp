#include "SignalEngine.h"
#include <algorithm>
#include <cmath>

namespace orderflow {

SignalEngine::SignalEngine(OrderBook& order_book,
                           TradeFlow& trade_flow,
                           VolumeProfile& volume_profile,
                           CumulativeVolumeDelta& cvd,
                           FootprintChart& footprint)
    : order_book_(order_book),
      trade_flow_(trade_flow),
      volume_profile_(volume_profile),
      cvd_(cvd),
      footprint_(footprint) {}

void SignalEngine::on_trade(const Trade& trade) {
    evaluate_signals(trade);
}

void SignalEngine::on_depth_update(const DepthUpdate& update) {
    if (update.timestamp - last_signal_ts_ < MIN_SIGNAL_INTERVAL_MS) return;
    check_stacked_imbalances();
}

void SignalEngine::evaluate_signals(const Trade& trade) {
    if (trade.timestamp - last_signal_ts_ < MIN_SIGNAL_INTERVAL_MS) return;

    check_absorption(trade);
    check_delta_divergence();
    check_exhaustion();
    check_poc_rejection(trade);
}

void SignalEngine::check_absorption(const Trade& trade) {
    const auto& events = order_book_.get_absorption_events();
    if (events.empty()) return;

    int64_t cutoff = trade.timestamp - params_.absorption_window_ms;
    int buy_absorptions = 0;
    int sell_absorptions = 0;

    for (auto it = events.rbegin(); it != events.rend(); ++it) {
        if (it->timestamp < cutoff) break;
        if (it->is_bid_absorbing) buy_absorptions++;
        else sell_absorptions++;
    }

    if (buy_absorptions >= 2) {
        double strength = std::min(1.0, buy_absorptions / 5.0);
        if (strength >= params_.signal_strength_min) {
            emit_signal({trade.timestamp, SignalType::ABSORPTION_BUY,
                         trade.price, strength,
                         "Bid absorption: " + std::to_string(buy_absorptions) + " events"});
        }
    }

    if (sell_absorptions >= 2) {
        double strength = std::min(1.0, sell_absorptions / 5.0);
        if (strength >= params_.signal_strength_min) {
            emit_signal({trade.timestamp, SignalType::ABSORPTION_SELL,
                         trade.price, strength,
                         "Ask absorption: " + std::to_string(sell_absorptions) + " events"});
        }
    }
}

void SignalEngine::check_stacked_imbalances() {
    auto imbalances = order_book_.detect_stacked_imbalances(
        params_.imbalance_threshold, params_.stacked_imbalance_levels);

    if (imbalances.empty()) return;

    double mid = order_book_.get_mid_price();
    if (mid <= 0.0) return;

    bool emitted = false;
    for (const auto& imb : imbalances) {
        if (emitted) break;

        double imb_mid = (imb.start_price + imb.end_price) / 2.0;
        double distance = std::abs(imb_mid - mid) / mid;
        if (distance > 0.005) continue;

        double strength = std::min(1.0, (double)imb.levels / (params_.stacked_imbalance_levels * 2));
        if (strength < params_.signal_strength_min) continue;

        SignalType type = imb.is_bid_heavy
                              ? SignalType::STACKED_IMBALANCE_BUY
                              : SignalType::STACKED_IMBALANCE_SELL;

        emit_signal({imb.timestamp, type, mid, strength,
                     "Stacked imbalance: " + std::to_string(imb.levels) + " levels"});
        emitted = true;
    }
}

void SignalEngine::check_delta_divergence() {
    auto div = cvd_.detect_divergence(params_.cvd_divergence_lookback);
    if (!div.detected) return;

    double strength = std::min(1.0, std::abs(div.delta_change) /
                                        (std::abs(div.price_change) + 1e-10));
    strength = std::min(strength, 1.0);
    if (strength < params_.signal_strength_min) return;

    double price = order_book_.get_mid_price();
    if (price <= 0.0) return;

    SignalType type = div.is_bearish
                          ? SignalType::DELTA_DIVERGENCE_BEAR
                          : SignalType::DELTA_DIVERGENCE_BULL;

    emit_signal({div.end_ts, type, price, strength,
                 "Delta divergence: price " +
                     std::string(div.is_bearish ? "up" : "down") +
                     " / delta " +
                     std::string(div.is_bearish ? "down" : "up")});
}

void SignalEngine::check_exhaustion() {
    auto exh = cvd_.detect_exhaustion(params_.exhaustion_lookback_bars,
                                       params_.exhaustion_price_threshold);
    if (!exh.detected) return;

    double strength = std::min(1.0, exh.consecutive_declining / 5.0);
    if (strength < params_.signal_strength_min) return;

    double price = order_book_.get_mid_price();
    if (price <= 0.0) return;

    SignalType type = exh.is_bearish
                          ? SignalType::EXHAUSTION_SELL
                          : SignalType::EXHAUSTION_BUY;

    auto history = cvd_.get_history();
    int64_t ts = history.empty() ? 0 : history.back().timestamp;

    emit_signal({ts, type, price, strength,
                 "Exhaustion: " + std::to_string(exh.consecutive_declining) +
                     " declining bars"});
}

void SignalEngine::check_poc_rejection(const Trade& trade) {
    double poc = volume_profile_.get_poc_price();
    if (poc == 0.0) return;

    double distance = std::abs(trade.price - poc) / poc;
    if (distance > params_.poc_rejection_distance) return;

    double delta_short = trade_flow_.get_delta_window(params_.poc_rejection_window_ms);

    if (delta_short > 0 && trade.price > poc) {
        double strength = std::min(1.0, std::abs(delta_short) / 100.0);
        if (strength >= params_.signal_strength_min) {
            emit_signal({trade.timestamp, SignalType::POC_REJECTION_BUY,
                         trade.price, strength,
                         "POC rejection buy: delta=" + std::to_string(delta_short)});
        }
    } else if (delta_short < 0 && trade.price < poc) {
        double strength = std::min(1.0, std::abs(delta_short) / 100.0);
        if (strength >= params_.signal_strength_min) {
            emit_signal({trade.timestamp, SignalType::POC_REJECTION_SELL,
                         trade.price, strength,
                         "POC rejection sell: delta=" + std::to_string(delta_short)});
        }
    }
}

void SignalEngine::emit_signal(const Signal& signal) {
    if (signal.price <= 0.0 || !std::isfinite(signal.price)) return;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        signals_.push_back(signal);
        if (signals_.size() > MAX_SIGNALS) {
            signals_.pop_front();
        }
        last_signal_ts_ = signal.timestamp;
    }

    process_signal_for_position(signal);

    if (signal_callback_) {
        signal_callback_(signal);
    }
}

void SignalEngine::process_signal_for_position(const Signal& signal) {
    int dir = signal.direction();
    if (dir == 0) return;

    double fill_price = (dir == 1)
        ? order_book_.get_best_ask()
        : order_book_.get_best_bid();
    if (fill_price <= 0.0) fill_price = signal.price;
    if (fill_price <= 0.0) return;

    if (dir == 1 && current_position_ <= 0) {
        if (current_position_ == -1 && entry_price_ > 0.0) {
            double trade_pnl = (entry_price_ / fill_price - 1.0) * 100.0;
            if (std::isfinite(trade_pnl)) {
                pnl_ += trade_pnl;
                max_pnl_ = std::max(max_pnl_, pnl_);
                max_drawdown_ = std::max(max_drawdown_, max_pnl_ - pnl_);
                num_trades_++;
                returns_.push_back(pnl_);
            }
        }
        current_position_ = 1;
        entry_price_ = fill_price;
    } else if (dir == -1 && current_position_ >= 0) {
        if (current_position_ == 1 && entry_price_ > 0.0) {
            double trade_pnl = (fill_price / entry_price_ - 1.0) * 100.0;
            if (std::isfinite(trade_pnl)) {
                pnl_ += trade_pnl;
                max_pnl_ = std::max(max_pnl_, pnl_);
                max_drawdown_ = std::max(max_drawdown_, max_pnl_ - pnl_);
                num_trades_++;
                returns_.push_back(pnl_);
            }
        }
        current_position_ = -1;
        entry_price_ = fill_price;
    }
}

std::vector<Signal> SignalEngine::get_signals() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return {signals_.begin(), signals_.end()};
}

std::vector<Signal> SignalEngine::get_signals_since(int64_t timestamp) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<Signal> result;
    for (const auto& s : signals_) {
        if (s.timestamp >= timestamp) result.push_back(s);
    }
    return result;
}

void SignalEngine::clear_signals() {
    std::lock_guard<std::mutex> lock(mutex_);
    signals_.clear();
}

std::vector<double> SignalEngine::get_returns() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return returns_;
}

} // namespace orderflow
