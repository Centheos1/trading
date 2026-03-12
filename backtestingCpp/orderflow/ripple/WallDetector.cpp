#include "WallDetector.h"
#include <algorithm>
#include <cmath>
#include <numeric>

namespace orderflow::ripple {

WallDetector::WallDetector(const RippleConfig& cfg) : cfg_(cfg) {}

void WallDetector::reset() {
    id_counter_ = 0;
    wall_map_.clear();
    active_walls_.clear();
    active_metrics_.clear();
    primary_bid_m_ = nullptr;
    primary_ask_m_ = nullptr;
    cached_baselines_.clear();
    cached_ranks_.clear();
    price_to_id_.clear();
    cooldown_map_.clear();
}

uint64_t WallDetector::next_id() { return ++id_counter_; }

int64_t WallDetector::price_to_tick(double price) const {
    return static_cast<int64_t>(std::round(price / cfg_.tick_size));
}

// ---------------------------------------------------------------
//  Baseline: median of visible levels on one side, excluding
//  the candidate itself.  Falls back to 1.0 for empty books.
// ---------------------------------------------------------------

double WallDetector::compute_baseline(
    const std::vector<std::pair<double, double>>& levels,
    double exclude_price, double tick_size) {

    std::vector<double> qtys;
    qtys.reserve(levels.size());
    for (auto& [p, q] : levels) {
        if (std::abs(p - exclude_price) < tick_size * 0.5) continue;
        if (q > 0.0) qtys.push_back(q);
    }
    if (qtys.empty()) return 1.0;

    std::sort(qtys.begin(), qtys.end());
    size_t n = qtys.size();
    double median = (n % 2 == 0)
        ? (qtys[n / 2 - 1] + qtys[n / 2]) / 2.0
        : qtys[n / 2];
    return std::max(median, 1e-9);
}

// ---------------------------------------------------------------
//  Rank: 1 = largest quantity on this side
// ---------------------------------------------------------------

std::vector<int> WallDetector::compute_ranks(
    const std::vector<std::pair<double, double>>& levels) {

    std::vector<size_t> indices(levels.size());
    std::iota(indices.begin(), indices.end(), 0);
    std::sort(indices.begin(), indices.end(), [&](size_t a, size_t b) {
        return levels[a].second > levels[b].second;
    });
    std::vector<int> ranks(levels.size(), 0);
    for (size_t r = 0; r < indices.size(); ++r)
        ranks[indices[r]] = static_cast<int>(r + 1);
    return ranks;
}

// ---------------------------------------------------------------
//  Main entry — called every depth update
// ---------------------------------------------------------------

void WallDetector::on_depth(const RippleContext& ctx) {
    auto book = ctx.snapshot_book();
    Timestamp ts = ctx.now();

    cached_best_bid_ = book.best_bid;
    cached_best_ask_ = book.best_ask;
    cached_mid_      = book.mid_price;
    cached_baselines_.clear();
    cached_ranks_.clear();

    for (auto& [id, w] : wall_map_) w.last_seen = 0;

    scan_side(WallSide::BID, book.bids, book.best_bid, book.mid_price, ts);
    scan_side(WallSide::ASK, book.asks, book.best_ask, book.mid_price, ts);

    prune_stale(ts);
    elect_primaries(ts);
}

// ---------------------------------------------------------------
//  Scan one side of the book
// ---------------------------------------------------------------

void WallDetector::scan_side(
    WallSide side,
    const std::vector<std::pair<double, double>>& levels,
    double best_price, double mid_price, Timestamp ts) {

    if (levels.size() < 2) return;  // need ≥2 levels for a meaningful baseline

    auto ranks = compute_ranks(levels);

    for (size_t i = 0; i < levels.size(); ++i) {
        double price = levels[i].first;
        double qty   = levels[i].second;
        if (qty <= 0.0) continue;

        double baseline = compute_baseline(levels, price, cfg_.tick_size);
        double rel = qty / baseline;
        double dist_mid = std::abs(mid_price - price) / cfg_.tick_size;

        SidePrice sp{side, price_to_tick(price)};
        auto pit = price_to_id_.find(sp);
        bool already_tracked = pit != price_to_id_.end() &&
                               wall_map_.count(pit->second);

        if (already_tracked) {
            // Always update existing walls so lifecycle can transition
            // (e.g. WITHDRAWN / FAILED) even if they drop below threshold
            auto& wall = wall_map_[pit->second];
            update_tracked(wall, qty, ts);
            wall.last_seen = ts;
            cached_baselines_[wall.wall_id] = baseline;
            cached_ranks_[wall.wall_id] = ranks[i];
        } else {
            if (rel < cfg_.wall_min_relative_size) continue;
            if (dist_mid > cfg_.wall_max_distance_ticks) continue;

            auto cit = cooldown_map_.find(sp);
            if (cit != cooldown_map_.end() &&
                (ts - cit->second) < cfg_.wall_dedup_cooldown_ms)
                continue;

            uint64_t id = next_id();
            WallCandidate w;
            w.wall_id      = id;
            w.side         = side;
            w.price        = price;
            w.initial_qty  = qty;
            w.current_qty  = qty;
            w.prev_qty     = qty;
            w.peak_qty     = qty;
            w.first_seen   = ts;
            w.last_seen    = ts;
            w.lifecycle    = WallLifecycle::FORMING;
            w.update_count = 1;
            wall_map_[id]    = w;
            price_to_id_[sp] = id;

            cached_baselines_[id] = baseline;
            cached_ranks_[id]     = ranks[i];
        }
    }
}

// ---------------------------------------------------------------
//  Update lifecycle of an existing wall (depth-only)
// ---------------------------------------------------------------

void WallDetector::update_tracked(WallCandidate& wall, double new_qty,
                                   Timestamp ts) {
    wall.prev_qty = wall.current_qty;
    wall.current_qty = new_qty;
    wall.update_count++;
    if (new_qty > wall.peak_qty) wall.peak_qty = new_qty;

    double delta = wall.prev_qty - new_qty;  // positive = shrinking

    // Use first_seen-based elapsed for rate computation (last_seen may be
    // zeroed for stale detection within the same on_depth call).
    double dt_sec = (wall.update_count > 1)
        ? std::max(0.001, (ts - wall.first_seen)
                          / (1000.0 * (wall.update_count - 1)))
        : 0.001;

    constexpr double EMA_ALPHA = 0.3;

    if (delta > 0.0) {
        double rate = delta / dt_sec;
        wall.depletion_rate = EMA_ALPHA * rate + (1.0 - EMA_ALPHA) * wall.depletion_rate;
    } else if (delta < 0.0) {
        double rate = (-delta) / dt_sec;
        wall.refill_rate = EMA_ALPHA * rate + (1.0 - EMA_ALPHA) * wall.refill_rate;
        wall.refill_count++;
    }

    Duration age_ms = ts - wall.first_seen;
    double qty_ratio = wall.initial_qty > 0.0 ? new_qty / wall.initial_qty : 0.0;
    double single_tick_drop_pct = (delta > 0.0 && wall.peak_qty > 0.0)
        ? delta / wall.peak_qty : 0.0;

    // WITHDRAWN takes priority: abrupt single-tick loss of >50% of peak
    if (single_tick_drop_pct > 0.50) {
        wall.lifecycle = WallLifecycle::WITHDRAWN;
    } else if (qty_ratio < cfg_.wall_break_threshold_pct) {
        wall.lifecycle = WallLifecycle::FAILED;
    } else if (wall.refill_rate > wall.depletion_rate && delta < 0.0) {
        wall.lifecycle = WallLifecycle::REFILLING;
    } else if (wall.depletion_rate > 0.0 && qty_ratio < 0.7) {
        wall.lifecycle = WallLifecycle::DEPLETING;
    } else if (age_ms >= cfg_.wall_min_age_ms) {
        wall.lifecycle = WallLifecycle::ACTIVE;
    }
}

// ---------------------------------------------------------------
//  Build WallMetrics from a candidate + book context
// ---------------------------------------------------------------

WallMetrics WallDetector::build_metrics(const WallCandidate& wall,
                                         double baseline,
                                         double best_price,
                                         double mid_price,
                                         int rank,
                                         Timestamp ts) const {
    WallMetrics m;
    m.wall_id        = wall.wall_id;
    m.side           = wall.side;
    m.price          = wall.price;
    m.absolute_size  = wall.current_qty;
    m.baseline_depth = baseline;
    m.relative_size  = baseline > 0.0 ? wall.current_qty / baseline : 0.0;

    m.distance_from_mid_ticks  = std::abs(mid_price - wall.price) / cfg_.tick_size;
    m.distance_from_best_ticks = std::abs(best_price - wall.price) / cfg_.tick_size;

    m.first_seen_ts    = wall.first_seen;
    m.persistence_sec  = std::max(0.0, (ts - wall.first_seen) / 1000.0);
    m.growth_rate      = wall.refill_rate;
    m.depletion_rate   = wall.depletion_rate;
    m.depletion_pct    = wall.peak_qty > 0.0
        ? (wall.peak_qty - wall.current_qty) / wall.peak_qty : 0.0;

    m.local_rank          = rank;
    m.is_active           = (wall.lifecycle == WallLifecycle::ACTIVE ||
                             wall.lifecycle == WallLifecycle::DEPLETING ||
                             wall.lifecycle == WallLifecycle::REFILLING);
    m.same_price_persists = (wall.update_count >= 2);
    m.disappeared         = false;
    m.withdrawn           = (wall.lifecycle == WallLifecycle::WITHDRAWN);
    m.lifecycle           = wall.lifecycle;
    return m;
}

// ---------------------------------------------------------------
//  Elect primary walls and build published vectors
// ---------------------------------------------------------------

void WallDetector::elect_primaries(Timestamp ts) {
    active_walls_.clear();
    active_metrics_.clear();
    primary_bid_   = nullptr;
    primary_ask_   = nullptr;
    primary_bid_m_ = nullptr;
    primary_ask_m_ = nullptr;

    for (auto& [id, w] : wall_map_) {
        if (w.lifecycle == WallLifecycle::FAILED ||
            w.lifecycle == WallLifecycle::WITHDRAWN)
            continue;
        if (w.last_seen == 0) continue;

        double baseline = cached_baselines_.count(w.wall_id)
            ? cached_baselines_[w.wall_id] : 1.0;
        double best = (w.side == WallSide::BID)
            ? cached_best_bid_ : cached_best_ask_;
        int rank = cached_ranks_.count(w.wall_id)
            ? cached_ranks_[w.wall_id] : 0;

        active_walls_.push_back(w);
        active_metrics_.push_back(
            build_metrics(w, baseline, best, cached_mid_, rank, ts));
    }

    double best_bid_score = -1.0, best_ask_score = -1.0;
    size_t best_bid_idx = 0, best_ask_idx = 0;

    for (size_t i = 0; i < active_walls_.size(); ++i) {
        auto& w = active_walls_[i];
        double score = w.current_qty *
                       (1.0 + active_metrics_[i].persistence_sec);
        if (w.side == WallSide::BID && score > best_bid_score) {
            best_bid_score = score;
            best_bid_idx = i;
        }
        if (w.side == WallSide::ASK && score > best_ask_score) {
            best_ask_score = score;
            best_ask_idx = i;
        }
    }

    if (best_bid_score >= 0.0) {
        primary_bid_   = &active_walls_[best_bid_idx];
        primary_bid_m_ = &active_metrics_[best_bid_idx];
    }
    if (best_ask_score >= 0.0) {
        primary_ask_   = &active_walls_[best_ask_idx];
        primary_ask_m_ = &active_metrics_[best_ask_idx];
    }
}

// ---------------------------------------------------------------
//  Prune walls not seen this tick or terminal
// ---------------------------------------------------------------

void WallDetector::prune_stale(Timestamp ts) {
    std::vector<uint64_t> to_remove;
    for (auto& [id, w] : wall_map_) {
        bool stale     = (w.last_seen == 0);
        bool failed    = (w.lifecycle == WallLifecycle::FAILED);
        bool withdrawn = (w.lifecycle == WallLifecycle::WITHDRAWN);
        if (stale || failed || withdrawn) {
            SidePrice sp{w.side, price_to_tick(w.price)};
            cooldown_map_[sp] = ts;
            price_to_id_.erase(sp);
            to_remove.push_back(id);
        }
    }
    for (auto id : to_remove) wall_map_.erase(id);

    // Prune expired cooldowns (2x dedup cooldown)
    Duration max_age = cfg_.wall_dedup_cooldown_ms * 2;
    for (auto it = cooldown_map_.begin(); it != cooldown_map_.end(); ) {
        if (ts - it->second > max_age)
            it = cooldown_map_.erase(it);
        else
            ++it;
    }
}

} // namespace orderflow::ripple
