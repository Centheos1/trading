#pragma once

#include <deque>
#include <cmath>
#include <optional>
#include "RippleTypes.h"
#include "RippleConfig.h"
#include "../OrderBook.h"
#include "../TradeFlow.h"

namespace orderflow::ripple {

/// Rolling context object that holds all recent market observations and
/// the latest pipeline outputs.  Every downstream component reads from
/// this — it is the single source of truth for the current tick.
///
/// Determinism guarantee: all timestamps come from the event stream, never
/// from wall-clock time.  Replay produces identical context state.
class RippleContext {
public:
    explicit RippleContext(const RippleConfig& cfg);

    void reset();

    // ---------- ingest (called by RippleEngine) ----------

    void on_depth(const OrderBook& book, Timestamp ts);
    void on_trade(const Trade& trade);

    // ---------- observations (pure reads) ----------

    OrderBookView    snapshot_book() const;
    TradeWindow      trade_window(Duration window_ms) const;

    double           microprice() const;
    double           microprice_at(Duration ms_ago) const;
    double           rolling_avg_spread() const;
    double           short_horizon_volatility() const;
    double           top_of_book_qty_stddev(WallSide side) const;

    Timestamp        now() const { return now_; }

    // ---------- pipeline state (written by each stage) ----------

    void set_last_wall(const WallCandidate& w)         { last_wall_ = w; }
    void set_last_wall(std::nullopt_t)                  { last_wall_.reset(); }
    void set_last_features(const RippleFeatures& f)     { last_features_ = f; }
    void set_last_evidence(const RippleEvidence& e)     { last_evidence_ = e; }
    void set_last_inference(const RippleInferenceResult& r) { last_inference_ = r; }
    void set_last_decision(const RippleDecision& d)     { last_decision_ = d; }

    const std::optional<WallCandidate>&       last_wall()      const { return last_wall_; }
    const std::optional<RippleFeatures>&      last_features()  const { return last_features_; }
    const std::optional<RippleEvidence>&      last_evidence()  const { return last_evidence_; }
    const std::optional<RippleInferenceResult>& last_inference() const { return last_inference_; }
    const std::optional<RippleDecision>&      last_decision()  const { return last_decision_; }

    // ---------- replay metadata ----------

    const ReplayMeta& replay_meta() const { return meta_; }
    uint64_t trade_event_seq() const { return meta_.trade_event_count; }
    uint64_t depth_event_seq() const { return meta_.depth_event_count; }

private:
    struct MicroSnap {
        Timestamp ts;
        double    microprice;
        double    spread;
        double    best_bid_qty;
        double    best_ask_qty;
    };

    void prune();

    const RippleConfig& cfg_;
    Timestamp now_ = 0;

    // Rolling history
    std::deque<Trade>     trades_;
    std::deque<MicroSnap> micro_snaps_;
    OrderBookView         last_book_;

    // Latest pipeline outputs (set by each stage, read by next stages + diagnostics)
    std::optional<WallCandidate>       last_wall_;
    std::optional<RippleFeatures>      last_features_;
    std::optional<RippleEvidence>      last_evidence_;
    std::optional<RippleInferenceResult> last_inference_;
    std::optional<RippleDecision>      last_decision_;

    // Replay / timing counters
    ReplayMeta meta_;
};

} // namespace orderflow::ripple
