#include "RippleFeatureEngine.h"
#include <algorithm>
#include <cmath>
#include <numeric>

namespace orderflow::ripple {

RippleFeatureEngine::RippleFeatureEngine(const RippleConfig& cfg) : cfg_(cfg) {}

RippleFeatures RippleFeatureEngine::compute(const RippleContext& ctx,
                                             const WallCandidate& wall,
                                             const WallMetrics* metrics) const {
    RippleFeatures f{};
    auto book = ctx.snapshot_book();
    auto tw = ctx.trade_window(cfg_.feature_window_ms);

    // ---------------------------------------------------------------
    //  1. relative_wall_size
    //     Wall qty normalised by local depth baseline.
    //     Prefer WallMetrics median-based baseline from Stage 2;
    //     fall back to mean-of-top-5 same-side levels.
    // ---------------------------------------------------------------
    if (metrics && metrics->baseline_depth > 0.0) {
        f.relative_wall_size = wall.current_qty / metrics->baseline_depth;
    } else {
        const auto& levels = (wall.side == WallSide::BID) ? book.bids : book.asks;
        double sum = 0.0;
        int n = std::min(5, static_cast<int>(levels.size()));
        for (int i = 0; i < n; ++i) sum += levels[i].second;
        double avg = n > 0 ? sum / n : 1.0;
        f.relative_wall_size = avg > 0.0 ? wall.current_qty / avg : 0.0;
    }

    // ---------------------------------------------------------------
    //  2. wall_persistence_sec
    //     Seconds the wall has been tracked without disappearing.
    //     Prefer WallMetrics if available.
    // ---------------------------------------------------------------
    f.wall_persistence_sec = metrics
        ? metrics->persistence_sec
        : std::max(0.0, (ctx.now() - wall.first_seen) / 1000.0);

    // ---------------------------------------------------------------
    //  3. wall_depletion_rate  (EMA qty/sec when shrinking)
    // ---------------------------------------------------------------
    f.wall_depletion_rate = metrics
        ? metrics->depletion_rate
        : wall.depletion_rate;

    // ---------------------------------------------------------------
    //  4. wall_refill_rate  (EMA qty/sec when growing)
    // ---------------------------------------------------------------
    f.wall_refill_rate = metrics
        ? metrics->growth_rate
        : wall.refill_rate;

    // ---------------------------------------------------------------
    //  5. wall_cancel_rate
    //     Estimate of qty/sec lost to cancellation rather than fills.
    //     Derived from wall qty shrinkage not explained by aggressive
    //     trade volume hitting the wall's price level.
    // ---------------------------------------------------------------
    f.wall_cancel_rate = compute_cancel_rate(wall, tw, cfg_.tick_size);

    // ---------------------------------------------------------------
    //  6. distance_to_wall_ticks
    //     How far the wall is from the current mid price, in ticks.
    //     Prefer WallMetrics (already computed from exact book state).
    // ---------------------------------------------------------------
    if (metrics) {
        f.distance_to_wall_ticks = metrics->distance_from_mid_ticks;
    } else if (cfg_.tick_size > 0.0) {
        f.distance_to_wall_ticks = std::abs(book.mid_price - wall.price) / cfg_.tick_size;
    }

    // ---------------------------------------------------------------
    //  7. queue_stability
    //     Coefficient of variation of top-of-book qty on the wall's side
    //     over the feature window.  Low = stable queue, high = churn.
    // ---------------------------------------------------------------
    f.queue_stability = ctx.top_of_book_qty_stddev(wall.side);

    // ---------------------------------------------------------------
    //  8. aggressive_flow_imbalance  [-1, +1]
    //     Signed: positive = net buy aggression, negative = net sell.
    //     Uses trade volume, not count.
    // ---------------------------------------------------------------
    double total_vol = tw.buy_volume + tw.sell_volume;
    if (total_vol > 0.0)
        f.aggressive_flow_imbalance =
            std::clamp((tw.buy_volume - tw.sell_volume) / total_vol, -1.0, 1.0);

    // ---------------------------------------------------------------
    //  9. trade_count_imbalance  [-1, +1]
    //     Same direction as flow imbalance but uses counts.
    //     Divergence between volume imbalance and count imbalance
    //     hints at size-driven vs crowd-driven pressure.
    // ---------------------------------------------------------------
    double total_count = tw.buy_count + tw.sell_count;
    if (total_count > 0.0)
        f.trade_count_imbalance =
            std::clamp((tw.buy_count - tw.sell_count) / total_count, -1.0, 1.0);

    // ---------------------------------------------------------------
    //  10. microprice_drift  (ticks/sec)
    //      Rate of microprice displacement over the lookback window.
    //      Positive = upward drift.
    // ---------------------------------------------------------------
    double mp_now  = ctx.microprice();
    double mp_past = ctx.microprice_at(cfg_.microprice_lookback_ms);
    double dt_sec  = cfg_.microprice_lookback_ms / 1000.0;
    if (cfg_.tick_size > 0.0 && dt_sec > 0.0)
        f.microprice_drift = (mp_now - mp_past) / cfg_.tick_size / dt_sec;

    // ---------------------------------------------------------------
    //  11. top1_imbalance  [-1, +1]
    //      Best-bid qty vs best-ask qty.
    // ---------------------------------------------------------------
    double bb_qty = !book.bids.empty() ? book.bids[0].second : 0.0;
    double ba_qty = !book.asks.empty() ? book.asks[0].second : 0.0;
    double tob_sum = bb_qty + ba_qty;
    if (tob_sum > 0.0)
        f.top1_imbalance = std::clamp((bb_qty - ba_qty) / tob_sum, -1.0, 1.0);

    // ---------------------------------------------------------------
    //  12. top5_imbalance  [-1, +1]
    //      Aggregate bid depth (top 5) vs aggregate ask depth (top 5).
    // ---------------------------------------------------------------
    double bid5 = 0.0, ask5 = 0.0;
    for (int i = 0; i < std::min(5, static_cast<int>(book.bids.size())); ++i)
        bid5 += book.bids[i].second;
    for (int i = 0; i < std::min(5, static_cast<int>(book.asks.size())); ++i)
        ask5 += book.asks[i].second;
    double top5_sum = bid5 + ask5;
    if (top5_sum > 0.0)
        f.top5_imbalance = std::clamp((bid5 - ask5) / top5_sum, -1.0, 1.0);

    // ---------------------------------------------------------------
    //  13. impact_per_unit_volume
    //      Microprice displacement per unit of aggressive volume over
    //      the feature window.  Measures price response to flow.
    //      Normalised to ticks.
    // ---------------------------------------------------------------
    f.impact_per_unit_volume = compute_impact(ctx, tw);

    // ---------------------------------------------------------------
    //  14. spread_shock
    //      Current spread relative to rolling average spread.
    //      > 1 means wider than normal; signals instability.
    // ---------------------------------------------------------------
    double avg_spread = ctx.rolling_avg_spread();
    f.spread_shock = (avg_spread > 0.0) ? book.spread / avg_spread : 1.0;

    // ---------------------------------------------------------------
    //  15. short_horizon_volatility
    //      Standard deviation of microprice changes within the
    //      volatility window.  Absolute price units.
    // ---------------------------------------------------------------
    f.short_horizon_volatility = ctx.short_horizon_volatility();

    // ---------------------------------------------------------------
    //  16. time_since_wall_formed_sec
    //      Identical clock source as wall_persistence_sec but kept
    //      as a separate slot so downstream consumers can distinguish
    //      wall age from inferred persistence (future extension).
    // ---------------------------------------------------------------
    f.time_since_wall_formed_sec =
        std::max(0.0, (ctx.now() - wall.first_seen) / 1000.0);

    return f;
}

// ---------------------------------------------------------------
//  Cancel rate: qty/sec lost to cancellation (not fills).
//
//  Method: compute total wall depletion (peak → current), then
//  subtract the aggressive trade volume that hit the wall's price
//  level within the feature window.  The residual is "unexplained"
//  loss, likely cancelled/spoofed.
// ---------------------------------------------------------------

double RippleFeatureEngine::compute_cancel_rate(
    const WallCandidate& wall,
    const TradeWindow& tw,
    double tick_size) const {

    double total_depletion = std::max(0.0, wall.peak_qty - wall.current_qty);
    if (total_depletion <= 0.0) return 0.0;

    // Sum aggressive volume at the wall's price level within the window.
    // For bid walls, sell aggressors hit the wall; for ask walls, buy aggressors.
    double agg_vol_at_wall = 0.0;
    for (auto& t : tw.recent) {
        bool price_match = tick_size > 0.0
            ? std::abs(t.price - wall.price) < tick_size * 0.5
            : t.price == wall.price;
        if (!price_match) continue;
        if (wall.side == WallSide::BID && t.is_sell_aggressor())
            agg_vol_at_wall += t.quantity;
        else if (wall.side == WallSide::ASK && t.is_buy_aggressor())
            agg_vol_at_wall += t.quantity;
    }

    double unexplained = std::max(0.0, total_depletion - agg_vol_at_wall);
    double elapsed_sec = std::max(0.001,
        (tw.window_end - wall.first_seen) / 1000.0);

    return unexplained / elapsed_sec;
}

// ---------------------------------------------------------------
//  Impact: microprice displacement per unit aggressive volume.
//  Uses the context's microprice at the start and end of the
//  feature window so the metric reflects actual price response
//  rather than first/last trade price.
// ---------------------------------------------------------------

double RippleFeatureEngine::compute_impact(
    const RippleContext& ctx,
    const TradeWindow& tw) const {

    double total_vol = tw.buy_volume + tw.sell_volume;
    if (total_vol <= 0.0) return 0.0;

    double mp_now   = ctx.microprice();
    double mp_start = ctx.microprice_at(cfg_.feature_window_ms);
    double price_move = std::abs(mp_now - mp_start);

    if (cfg_.tick_size > 0.0)
        price_move /= cfg_.tick_size;  // normalise to ticks

    return price_move / total_vol;
}

} // namespace orderflow::ripple
