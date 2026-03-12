#pragma once

#include <vector>
#include <deque>
#include <mutex>
#include <cstdint>
#include "Types.h"
#include "OrderBook.h"
#include "TradeFlow.h"
#include "VolumeProfile.h"
#include "CumulativeVolumeDelta.h"
#include "FootprintChart.h"

namespace orderflow {

struct SignalParams {
    double imbalance_threshold = 3.0;
    int stacked_imbalance_levels = 3;

    double absorption_volume_ratio = 5.0;
    int64_t absorption_window_ms = 5000;

    int cvd_divergence_lookback = 100;

    int exhaustion_lookback_bars = 5;
    double exhaustion_price_threshold = 0.001;

    double poc_rejection_distance = 0.001;
    int64_t poc_rejection_window_ms = 60000;

    double signal_strength_min = 0.3;
};

class SignalEngine {
public:
    SignalEngine(OrderBook& order_book,
                 TradeFlow& trade_flow,
                 VolumeProfile& volume_profile,
                 CumulativeVolumeDelta& cvd,
                 FootprintChart& footprint);

    void set_params(const SignalParams& params) { params_ = params; }
    SignalParams get_params() const { return params_; }

    void on_trade(const Trade& trade);
    void on_depth_update(const DepthUpdate& update);

    std::vector<Signal> get_signals() const;
    std::vector<Signal> get_signals_since(int64_t timestamp) const;
    void clear_signals();

    void set_signal_callback(SignalCallback cb) { signal_callback_ = cb; }

    int get_position() const { return current_position_; }
    double get_pnl() const { return pnl_; }
    double get_max_drawdown() const { return max_drawdown_; }
    int get_num_trades() const { return num_trades_; }
    std::vector<double> get_returns() const;

private:
    void evaluate_signals(const Trade& trade);
    void check_absorption(const Trade& trade);
    void check_stacked_imbalances();
    void check_delta_divergence();
    void check_exhaustion();
    void check_poc_rejection(const Trade& trade);

    void emit_signal(const Signal& signal);
    void process_signal_for_position(const Signal& signal);

    OrderBook& order_book_;
    TradeFlow& trade_flow_;
    VolumeProfile& volume_profile_;
    CumulativeVolumeDelta& cvd_;
    FootprintChart& footprint_;

    SignalParams params_;
    SignalCallback signal_callback_;

    mutable std::mutex mutex_;
    std::deque<Signal> signals_;
    static constexpr size_t MAX_SIGNALS = 10000;

    int current_position_ = 0;
    double entry_price_ = 0.0;
    double pnl_ = 0.0;
    double max_pnl_ = 0.0;
    double max_drawdown_ = 0.0;
    int num_trades_ = 0;
    std::vector<double> returns_;

    int64_t last_signal_ts_ = 0;
    static constexpr int64_t MIN_SIGNAL_INTERVAL_MS = 1000;
};

} // namespace orderflow
