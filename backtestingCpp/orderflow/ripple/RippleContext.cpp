#include "RippleContext.h"
#include <algorithm>
#include <numeric>
#include <cmath>

namespace orderflow::ripple {

RippleContext::RippleContext(const RippleConfig& cfg) : cfg_(cfg) {
    meta_.is_replay = cfg.replay_mode;
}

void RippleContext::reset() {
    now_ = 0;
    trades_.clear();
    micro_snaps_.clear();
    last_book_ = {};
    last_wall_.reset();
    last_features_.reset();
    last_evidence_.reset();
    last_inference_.reset();
    last_decision_.reset();
    meta_ = {};
    meta_.is_replay = cfg_.replay_mode;
}

// ---------- ingest ----------

void RippleContext::on_depth(const OrderBook& book, Timestamp ts) {
    now_ = ts;

    meta_.depth_event_count++;
    if (meta_.first_event_ts == 0) meta_.first_event_ts = ts;
    meta_.last_event_ts = ts;

    last_book_.timestamp = ts;
    last_book_.best_bid  = book.get_best_bid();
    last_book_.best_ask  = book.get_best_ask();
    last_book_.mid_price = book.get_mid_price();
    last_book_.spread    = book.get_spread();

    last_book_.bids = book.get_bids(cfg_.book_depth_levels);
    last_book_.asks = book.get_asks(cfg_.book_depth_levels);

    double bb_qty = 0.0, ba_qty = 0.0;
    if (!last_book_.bids.empty()) bb_qty = last_book_.bids[0].second;
    if (!last_book_.asks.empty()) ba_qty = last_book_.asks[0].second;
    double total_tob = bb_qty + ba_qty;

    double mp = last_book_.mid_price;
    if (total_tob > 0 && last_book_.best_bid > 0 && last_book_.best_ask > 0)
        mp = (last_book_.best_bid * ba_qty + last_book_.best_ask * bb_qty) / total_tob;

    micro_snaps_.push_back({ts, mp, last_book_.spread, bb_qty, ba_qty});

    prune();
}

void RippleContext::on_trade(const Trade& trade) {
    if (trade.timestamp > now_) now_ = trade.timestamp;

    meta_.trade_event_count++;
    if (meta_.first_event_ts == 0) meta_.first_event_ts = trade.timestamp;
    meta_.last_event_ts = trade.timestamp;

    trades_.push_back(trade);
    if (trades_.size() > cfg_.max_context_trades) trades_.pop_front();
}

// ---------- observations ----------

OrderBookView RippleContext::snapshot_book() const {
    return last_book_;
}

TradeWindow RippleContext::trade_window(Duration window_ms) const {
    TradeWindow tw;
    Timestamp cutoff = now_ - window_ms;

    auto it = std::lower_bound(trades_.begin(), trades_.end(), cutoff,
        [](const Trade& t, Timestamp c) { return t.timestamp < c; });

    size_t offset = static_cast<size_t>(std::distance(trades_.begin(), it));
    size_t count  = trades_.size() - offset;
    if (count > 0)
        tw.recent = std::span<const Trade>(&trades_[offset], count);

    tw.window_start = cutoff;
    tw.window_end   = now_;

    for (auto& t : tw.recent) {
        if (t.is_buy_aggressor()) {
            tw.buy_volume += t.quantity;
            tw.buy_count++;
        } else {
            tw.sell_volume += t.quantity;
            tw.sell_count++;
        }
    }
    tw.net_delta = tw.buy_volume - tw.sell_volume;
    return tw;
}

double RippleContext::microprice() const {
    if (micro_snaps_.empty()) return last_book_.mid_price;
    return micro_snaps_.back().microprice;
}

double RippleContext::microprice_at(Duration ms_ago) const {
    if (micro_snaps_.empty()) return last_book_.mid_price;
    Timestamp target = now_ - ms_ago;
    for (auto it = micro_snaps_.rbegin(); it != micro_snaps_.rend(); ++it) {
        if (it->ts <= target) return it->microprice;
    }
    return micro_snaps_.front().microprice;
}

double RippleContext::rolling_avg_spread() const {
    if (micro_snaps_.empty()) return last_book_.spread;
    Timestamp cutoff = now_ - cfg_.volatility_window_ms;
    double sum = 0.0;
    int count = 0;
    for (auto it = micro_snaps_.rbegin(); it != micro_snaps_.rend(); ++it) {
        if (it->ts < cutoff) break;
        sum += it->spread;
        count++;
    }
    return count > 0 ? sum / count : last_book_.spread;
}

double RippleContext::short_horizon_volatility() const {
    if (micro_snaps_.size() < 3) return 0.0;
    Timestamp cutoff = now_ - cfg_.volatility_window_ms;

    // Welford's online algorithm — no heap allocation
    double mean = 0.0, m2 = 0.0;
    size_t n = 0;
    for (size_t i = micro_snaps_.size() - 1; i > 0; --i) {
        if (micro_snaps_[i].ts < cutoff) break;
        double delta_price = micro_snaps_[i].microprice - micro_snaps_[i - 1].microprice;
        ++n;
        double d1 = delta_price - mean;
        mean += d1 / static_cast<double>(n);
        double d2 = delta_price - mean;
        m2 += d1 * d2;
    }
    if (n < 2) return 0.0;
    return std::sqrt(m2 / static_cast<double>(n));
}

double RippleContext::top_of_book_qty_stddev(WallSide side) const {
    if (micro_snaps_.size() < 3) return 0.0;
    Timestamp cutoff = now_ - cfg_.feature_window_ms;

    // Welford's online algorithm — no heap allocation
    double mean = 0.0, m2 = 0.0;
    size_t n = 0;
    for (auto it = micro_snaps_.rbegin(); it != micro_snaps_.rend(); ++it) {
        if (it->ts < cutoff) break;
        double val = (side == WallSide::BID) ? it->best_bid_qty : it->best_ask_qty;
        ++n;
        double d1 = val - mean;
        mean += d1 / static_cast<double>(n);
        double d2 = val - mean;
        m2 += d1 * d2;
    }
    if (n < 2 || mean <= 0.0) return 0.0;
    return std::sqrt(m2 / static_cast<double>(n)) / mean;
}

// ---------- internals ----------

void RippleContext::prune() {
    Timestamp keep_from = now_ - std::max({cfg_.feature_window_ms,
                                            cfg_.volatility_window_ms,
                                            cfg_.microprice_lookback_ms}) * 2;
    while (!micro_snaps_.empty() && micro_snaps_.front().ts < keep_from)
        micro_snaps_.pop_front();
    while (micro_snaps_.size() > cfg_.max_context_snaps)
        micro_snaps_.pop_front();

    while (!trades_.empty() && trades_.front().timestamp < keep_from)
        trades_.pop_front();
}

} // namespace orderflow::ripple
