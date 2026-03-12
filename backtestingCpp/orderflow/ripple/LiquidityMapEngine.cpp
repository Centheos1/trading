#include "LiquidityMapEngine.h"
#include <algorithm>
#include <cmath>
#include <numeric>
#include <unordered_map>

namespace orderflow::ripple {

LiquidityMapEngine::LiquidityMapEngine(const LiquidityMapConfig& cfg)
    : cfg_(cfg) {
    work_.reserve(static_cast<size_t>(cfg_.max_levels * 2));
}

void LiquidityMapEngine::reset() {
    snap_ = {};
    work_.clear();
}

// ---------------------------------------------------------------------------
//  update()
// ---------------------------------------------------------------------------

void LiquidityMapEngine::update(const LiquidityMapInput& input) {
    work_.clear();
    snap_.timestamp = input.timestamp;

    collect_wall_levels(input);
    collect_profile_levels(input);
    collect_structural_anchors(input);
    collect_flow_into_levels(input);
    compute_scores(input);
    detect_voids(input);
    finalize(input);
}

// ---------------------------------------------------------------------------
//  Level collection
// ---------------------------------------------------------------------------

void LiquidityMapEngine::collect_wall_levels(const LiquidityMapInput& input) {
    if (!input.walls || !input.metrics) return;
    const auto& walls = *input.walls;
    const auto& mets  = *input.metrics;

    for (size_t i = 0; i < walls.size() && i < mets.size(); ++i) {
        const auto& w = walls[i];
        const auto& m = mets[i];
        if (!m.is_active) continue;
        if (m.relative_size < cfg_.wall_min_quality) continue;

        LiquidityLevel lv;
        lv.price        = w.price;
        lv.depth        = w.current_qty;
        lv.wall_quality = m.relative_size;
        lv.type         = (w.side == WallSide::BID) ? LevelType::WALL_BID
                                                     : LevelType::WALL_ASK;
        work_.push_back(lv);
    }
}

void LiquidityMapEngine::collect_profile_levels(const LiquidityMapInput& input) {
    if (!input.profile || input.profile->empty()) return;
    const auto& prof = *input.profile;

    // Compute median volume for HVN/LVN classification
    std::vector<double> volumes;
    volumes.reserve(prof.size());
    for (const auto& node : prof)
        if (node.volume > 0.0) volumes.push_back(node.volume);

    if (volumes.empty()) return;
    std::sort(volumes.begin(), volumes.end());
    double median_vol = volumes[volumes.size() / 2];

    double hvn_thresh = median_vol * cfg_.hvn_volume_ratio;
    double lvn_thresh = median_vol * cfg_.lvn_volume_ratio;

    for (const auto& node : prof) {
        if (node.volume <= 0.0) continue;

        LevelType lt;
        if (std::abs(node.price - input.poc_price) < 1e-9)
            lt = LevelType::POC;
        else if (node.volume >= hvn_thresh)
            lt = LevelType::HVN;
        else if (node.volume <= lvn_thresh)
            lt = LevelType::LVN;
        else
            continue;  // mid-range nodes are not significant

        LiquidityLevel lv;
        lv.price          = node.price;
        lv.profile_volume = node.volume;
        lv.type           = lt;
        work_.push_back(lv);
    }
}

void LiquidityMapEngine::collect_structural_anchors(const LiquidityMapInput& input) {
    if (input.vwap > 0.0) {
        LiquidityLevel lv;
        lv.price = input.vwap;
        lv.type  = LevelType::VWAP;
        work_.push_back(lv);
    }
}

void LiquidityMapEngine::collect_flow_into_levels(const LiquidityMapInput& input) {
    if (!input.trades_begin || input.trades_begin == input.trades_end) return;

    // Bucket trades by price (rounded to nearest tick = 0.01) and compute net flow
    std::unordered_map<int64_t, double> flow_by_price;
    for (auto it = input.trades_begin; it != input.trades_end; ++it) {
        int64_t bucket = static_cast<int64_t>(std::round(it->price * 100.0));
        double signed_qty = it->is_buyer_maker ? -it->quantity : it->quantity;
        flow_by_price[bucket] += signed_qty;
    }

    // Merge flow into existing levels (match by price bucket)
    for (auto& lv : work_) {
        int64_t bucket = static_cast<int64_t>(std::round(lv.price * 100.0));
        auto fit = flow_by_price.find(bucket);
        if (fit != flow_by_price.end()) {
            lv.net_flow = fit->second;
            flow_by_price.erase(fit);
        }
    }
}

// ---------------------------------------------------------------------------
//  Scoring
// ---------------------------------------------------------------------------

void LiquidityMapEngine::compute_scores(const LiquidityMapInput& input) {
    if (work_.empty()) return;

    // Collect normalization ranges
    double max_depth  = 0.0, max_volume = 0.0, max_quality = 0.0, max_refill = 0.0;
    for (const auto& lv : work_) {
        max_depth   = std::max(max_depth,   lv.depth);
        max_volume  = std::max(max_volume,  lv.profile_volume);
        max_quality = std::max(max_quality, lv.wall_quality);
    }
    if (max_depth   < 1e-15) max_depth   = 1.0;
    if (max_volume  < 1e-15) max_volume  = 1.0;
    if (max_quality < 1e-15) max_quality = 1.0;

    double sigma = std::max(input.sigma_P, 1e-9);

    for (auto& lv : work_) {
        // --- hold_score: only meaningful for wall levels ---
        if (lv.type == LevelType::WALL_BID || lv.type == LevelType::WALL_ASK) {
            double nq = lv.wall_quality / max_quality;
            double nd = lv.depth / max_depth;
            double ni = std::clamp(input.imbalance * 0.5 + 0.5, 0.0, 1.0);
            // refill rate not directly available; use 0 for V1
            lv.hold_score = cfg_.hold_w_quality   * nq
                          + cfg_.hold_w_depth     * nd
                          + cfg_.hold_w_imbalance * ni
                          + cfg_.hold_w_refill    * 0.0;
            lv.hold_score = std::clamp(lv.hold_score, 0.0, 1.0);
        }

        // --- dest_score (§10.3) ---
        bool is_structural = (lv.type == LevelType::VWAP ||
                              lv.type == LevelType::POC);
        double nd = (max_depth  > 1e-15) ? lv.depth / max_depth   : 0.0;
        double nv = (max_volume > 1e-15) ? lv.profile_volume / max_volume : 0.0;
        double vwap_prox = 1.0 / (1.0 + std::abs(lv.price - input.vwap) / sigma);
        double struct_flag = is_structural ? 1.0 : 0.0;

        lv.dest_score = cfg_.dest_w_depth          * nd
                      + cfg_.dest_w_volume         * nv
                      + cfg_.dest_w_vwap_proximity * vwap_prox
                      + cfg_.dest_w_structural     * struct_flag;
        lv.dest_score = std::clamp(lv.dest_score, 0.0, 1.0);
    }
}

// ---------------------------------------------------------------------------
//  Void corridors
// ---------------------------------------------------------------------------

void LiquidityMapEngine::detect_voids(const LiquidityMapInput& input) {
    snap_.void_corridors.clear();
    if (work_.size() < 2) return;

    double sigma = std::max(input.sigma_P, 1e-9);
    double min_gap = cfg_.void_min_gap_sigma * sigma;

    // Sort working levels by price for gap detection
    std::sort(work_.begin(), work_.end(),
              [](const LiquidityLevel& a, const LiquidityLevel& b) {
                  return a.price < b.price;
              });

    for (size_t i = 1; i < work_.size(); ++i) {
        double gap = work_[i].price - work_[i - 1].price;
        if (gap >= min_gap) {
            snap_.void_corridors.push_back({work_[i - 1].price, work_[i].price});
        }
    }
}

// ---------------------------------------------------------------------------
//  Finalize
// ---------------------------------------------------------------------------

void LiquidityMapEngine::finalize(const LiquidityMapInput& input) {
    // Sort by dest_score descending, then trim to max_levels
    std::sort(work_.begin(), work_.end(),
              [](const LiquidityLevel& a, const LiquidityLevel& b) {
                  return a.dest_score > b.dest_score;
              });

    size_t keep = std::min(work_.size(),
                           static_cast<size_t>(cfg_.max_levels));
    snap_.levels.assign(work_.begin(), work_.begin() + keep);

    // Summary fields
    snap_.poc  = input.poc_price;
    snap_.vwap = input.vwap;

    snap_.nearest_bid_wall = 0.0;
    snap_.nearest_ask_wall = 0.0;
    double best_bid_dist = 1e18, best_ask_dist = 1e18;

    for (const auto& lv : snap_.levels) {
        if (lv.type == LevelType::WALL_BID) {
            double d = std::abs(input.mid_price - lv.price);
            if (d < best_bid_dist) { best_bid_dist = d; snap_.nearest_bid_wall = lv.price; }
        } else if (lv.type == LevelType::WALL_ASK) {
            double d = std::abs(lv.price - input.mid_price);
            if (d < best_ask_dist) { best_ask_dist = d; snap_.nearest_ask_wall = lv.price; }
        }
    }
}

// ---------------------------------------------------------------------------
//  Queries
// ---------------------------------------------------------------------------

double LiquidityMapEngine::get_hold_score(double price) const {
    const auto* lv = find_nearest(price);
    return lv ? lv->hold_score : 0.0;
}

double LiquidityMapEngine::get_dest_score(double price) const {
    const auto* lv = find_nearest(price);
    return lv ? lv->dest_score : 0.0;
}

std::vector<LiquidityLevel> LiquidityMapEngine::get_destinations_in_direction(
    TradeSide side, double from_price) const {

    std::vector<LiquidityLevel> result;
    double ss = (side == TradeSide::LONG) ? 1.0 : -1.0;

    for (const auto& lv : snap_.levels) {
        if ((lv.price - from_price) * ss > 0.0)
            result.push_back(lv);
    }

    std::sort(result.begin(), result.end(),
              [](const LiquidityLevel& a, const LiquidityLevel& b) {
                  return a.dest_score > b.dest_score;
              });
    return result;
}

const LiquidityLevel* LiquidityMapEngine::find_nearest(double price) const {
    const LiquidityLevel* best = nullptr;
    double best_dist = 1e18;
    for (const auto& lv : snap_.levels) {
        double d = std::abs(lv.price - price);
        if (d < best_dist) { best_dist = d; best = &lv; }
    }
    return (best_dist < 1e-6) ? best : nullptr;
}

} // namespace orderflow::ripple
