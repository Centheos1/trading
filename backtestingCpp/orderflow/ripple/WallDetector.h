#pragma once

#include <vector>
#include <unordered_map>
#include <cstdint>
#include "IWallDetector.h"
#include "RippleConfig.h"
#include "RippleContext.h"

namespace orderflow::ripple {

class WallDetector : public IWallDetector {
public:
    explicit WallDetector(const RippleConfig& cfg);

    void reset();

    void on_depth(const RippleContext& ctx) override;

    const std::vector<WallCandidate>& active_walls() const override { return active_walls_; }
    const std::vector<WallMetrics>&   metrics()      const override { return active_metrics_; }

    const WallCandidate* primary_bid_wall()    const override { return primary_bid_; }
    const WallCandidate* primary_ask_wall()    const override { return primary_ask_; }
    const WallMetrics*   primary_bid_metrics() const override { return primary_bid_m_; }
    const WallMetrics*   primary_ask_metrics() const override { return primary_ask_m_; }

private:
    void scan_side(WallSide side,
                   const std::vector<std::pair<double, double>>& levels,
                   double best_price, double mid_price, Timestamp ts);

    static double compute_baseline(const std::vector<std::pair<double, double>>& levels,
                                   double exclude_price, double tick_size);
    static std::vector<int> compute_ranks(
        const std::vector<std::pair<double, double>>& levels);

    void update_tracked(WallCandidate& wall, double new_qty, Timestamp ts);

    WallMetrics build_metrics(const WallCandidate& wall,
                              double baseline, double best_price,
                              double mid_price, int rank,
                              Timestamp ts) const;

    void elect_primaries(Timestamp ts);
    void prune_stale(Timestamp ts);

    int64_t price_to_tick(double price) const;
    uint64_t next_id();

    const RippleConfig& cfg_;
    uint64_t id_counter_ = 0;

    // Tracked walls keyed by wall_id
    std::unordered_map<uint64_t, WallCandidate> wall_map_;

    // Published every tick
    std::vector<WallCandidate> active_walls_;
    std::vector<WallMetrics>   active_metrics_;

    const WallCandidate* primary_bid_ = nullptr;
    const WallCandidate* primary_ask_ = nullptr;
    const WallMetrics*   primary_bid_m_ = nullptr;
    const WallMetrics*   primary_ask_m_ = nullptr;

    // Per-wall cached info from latest scan (populated during scan_side)
    std::unordered_map<uint64_t, double> cached_baselines_;
    std::unordered_map<uint64_t, int>    cached_ranks_;
    double cached_best_bid_ = 0.0;
    double cached_best_ask_ = 0.0;
    double cached_mid_      = 0.0;

    // Price -> wall_id lookup (tick-grid based for tolerance)
    struct SidePrice {
        WallSide side;
        int64_t  tick_key;
        bool operator==(const SidePrice& o) const {
            return side == o.side && tick_key == o.tick_key;
        }
    };
    struct SidePriceHash {
        size_t operator()(const SidePrice& k) const {
            return std::hash<int64_t>{}(k.tick_key) ^
                   (std::hash<int>{}(static_cast<int>(k.side)) << 48);
        }
    };

    std::unordered_map<SidePrice, uint64_t, SidePriceHash>  price_to_id_;
    std::unordered_map<SidePrice, Timestamp, SidePriceHash> cooldown_map_;
};

} // namespace orderflow::ripple
