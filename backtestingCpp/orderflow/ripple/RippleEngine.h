#pragma once

#include <memory>
#include "RippleTypes.h"
#include "RippleConfig.h"
#include "RippleContext.h"
#include "WallDetector.h"
#include "IRippleFeatureEngine.h"
#include "IRippleEvidenceEngine.h"
#include "IRippleStateInference.h"
#include "ITriggerDecisionEngine.h"
#include "RippleStateTracker.h"
#include "TradeLifecycleEngine.h"
#include "LiquidityMapEngine.h"
#include "RiskEngine.h"
#include "../OrderBook.h"
#include "../TradeFlow.h"
#include "../VolumeProfile.h"
#include "../CumulativeVolumeDelta.h"
#include "../Schemas.h"

namespace orderflow::ripple {

class RippleDiagnostics;  // forward

/// Top-level Ripple facade.
///
/// Owns the full pipeline: observation → features → evidence → inference →
/// trigger.  Each stage is held behind an abstract interface so backends
/// can be swapped independently:
///
///   OrderBook/Trade events
///       │
///       ▼
///   RippleContext   (rolling observation store)
///       │
///       ▼
///   WallDetector    (wall detection — concrete, not swappable yet)
///       │
///       ├──► IRippleFeatureEngine   [swappable: set_feature_engine()]
///       │         │
///       │         ▼
///       ├──► IRippleEvidenceEngine  [swappable: set_evidence_engine()]
///       │         │
///       │         ▼
///       ├──► IRippleStateInference  [swappable: set_inference_backend()]
///       │         │  (wrapped by RippleStateTracker for dwell/dedup)
///       │         ▼
///       └──► ITriggerDecisionEngine [swappable: set_trigger_engine()]
///                 │
///                 ▼
///            RippleDecision → callback / diagnostics
///
/// Default construction creates the concrete rule-based backends
/// (RippleFeatureEngine, RippleEvidenceEngine, ScoreBasedInference,
/// TriggerDecisionEngine).  Swap any layer at runtime without affecting
/// the rest of the pipeline.
class RippleEngine {
public:
    explicit RippleEngine(const RippleConfig& cfg = {});
    ~RippleEngine();

    RippleEngine(const RippleEngine&) = delete;
    RippleEngine& operator=(const RippleEngine&) = delete;

    // --- event ingestion ---
    void on_trade(const Trade& trade, const OrderBook& book);
    void on_depth(const DepthUpdate& update, const OrderBook& book);

    void reset();

    // --- callbacks ---
    void set_decision_callback(RippleDecisionCallback cb);
    void set_inventory(const InventorySnapshot& inv);
    void set_diagnostics(std::shared_ptr<RippleDiagnostics> diag);

    // --- higher-layer context ---
    void set_tide_context(const TideContext& tide) { tide_ = tide; }
    void set_wave_context(const WaveContext& wave) { wave_ = wave; }

    // --- Phase 5: Wave regime and permissions ---
    void set_wave_snapshot(const WaveSnapshot& snap) { wave_snap_ = snap; wave_snap_set_ = true; }
    const WaveSnapshot& wave_snapshot() const { return wave_snap_; }
    bool has_wave_snapshot() const { return wave_snap_set_; }

    // --- Phase 2: trade lifecycle ---
    void on_fill(const FillEvent& fill);
    const TradeLifecycleEngine& lifecycle() const { return lifecycle_; }
    TradeStateSnapshot get_trade_state() const { return lifecycle_.get_snapshot(); }
    LifecycleState get_lifecycle_state() const { return lifecycle_.get_lifecycle_state(); }
    bool has_active_trade() const { return lifecycle_.is_active(); }

    // --- Phase 3: liquidity map and VP/CVD ---
    void set_volume_profile(orderflow::VolumeProfile* vp) { vp_ = vp; }
    void set_cvd(orderflow::CumulativeVolumeDelta* cvd)   { cvd_ = cvd; }
    const LiquidityMapEngine& liquidity_map_engine() const { return liq_map_; }
    const LiquidityMapSnapshot& liquidity_map() const { return liq_map_.snapshot(); }

    // --- Phase 4: risk engine ---
    RiskEngine&       risk_engine()       { return risk_; }
    const RiskEngine& risk_engine() const { return risk_; }
    void set_risk_budget(double es_budget, double max_position_usd, double risk_multiplier);
    void set_realized_vol(double vol);

    // --- backend swapping ---
    void set_feature_engine(std::unique_ptr<IRippleFeatureEngine> engine);
    void set_evidence_engine(std::unique_ptr<IRippleEvidenceEngine> engine);
    void set_inference_backend(std::unique_ptr<IRippleStateInference> backend);
    void set_trigger_engine(std::unique_ptr<ITriggerDecisionEngine> engine);

    // --- read accessors ---
    RippleState               current_state()    const;
    const RippleConfig&       config()           const { return cfg_; }
    const RippleContext&      context()          const { return ctx_; }
    const WallDetector&       wall_detector()    const { return wall_det_; }

    const RippleFeatures*     last_features()    const { return &last_features_; }
    const RippleEvidence*     last_evidence()    const { return &last_evidence_; }
    const RippleInferenceResult& last_inference() const { return last_inference_; }
    const RippleDecision&     last_decision()    const { return last_decision_; }

    RippleDiagnostics* diagnostics() const { return diagnostics_.get(); }

    uint64_t pipeline_runs() const { return pipeline_runs_; }
    uint64_t trade_count()   const { return trade_count_; }
    uint64_t depth_count()   const { return depth_count_; }

    // --- Phase 6: aggregate PnL from lifecycle ---
    double   cumulative_pnl()      const { return lifecycle_.cumulative_pnl(); }
    double   lifecycle_peak_equity()   const { return lifecycle_.peak_equity(); }
    double   lifecycle_max_drawdown()  const { return lifecycle_.max_drawdown_value(); }
    uint64_t completed_trades()    const { return lifecycle_.completed_trades(); }

private:
    void run_pipeline(Timestamp ts);
    void ensure_diagnostics();

    RippleConfig           cfg_;
    RippleContext          ctx_;
    WallDetector           wall_det_;

    std::unique_ptr<IRippleFeatureEngine>    feat_engine_;
    std::unique_ptr<IRippleEvidenceEngine>   ev_engine_;
    RippleStateTracker                       state_tracker_;
    std::unique_ptr<ITriggerDecisionEngine>  trigger_engine_;
    TradeLifecycleEngine                     lifecycle_;
    LiquidityMapEngine                       liq_map_;
    RiskEngine                               risk_;

    InventorySnapshot      inventory_;
    RippleDecisionCallback callback_;

    RippleFeatures         last_features_;
    RippleEvidence         last_evidence_;
    RippleInferenceResult  last_inference_;
    RippleDecision         last_decision_;

    double                 last_trade_price_      = 0.0;
    Timestamp              last_pipeline_ts_      = 0;
    uint64_t               pipeline_runs_         = 0;
    uint64_t               trade_count_           = 0;
    uint64_t               depth_count_           = 0;
    size_t                 trades_since_pipeline_ = 0;

    std::shared_ptr<RippleDiagnostics> diagnostics_;

    TideContext tide_;
    WaveContext wave_;
    WaveSnapshot wave_snap_ = DefaultWaveSnapshot::make();
    bool         wave_snap_set_ = false;

    // Phase 3: optional external engine references (non-owning)
    orderflow::VolumeProfile*          vp_  = nullptr;
    orderflow::CumulativeVolumeDelta*  cvd_ = nullptr;
    double session_vwap_     = 0.0;
    double session_volume_   = 0.0;
    double session_pv_       = 0.0;   // price × volume accumulator
    double session_high_     = 0.0;
    double session_low_      = 1e18;
};

} // namespace orderflow::ripple
