#pragma once

#include <memory>
#include <string>
#include <thread>
#include <atomic>
#include <mutex>
#include "Types.h"
#include "IDataFeed.h"
#include "OrderBook.h"
#include "TradeFlow.h"
#include "VolumeProfile.h"
#include "CumulativeVolumeDelta.h"
#include "FootprintChart.h"
#include "SignalEngine.h"
#include "TickStore.h"
#include "ripple/RippleEngine.h"

namespace orderflow {

struct EngineConfig {
    double tick_size = 0.01;
    double large_trade_threshold = 10.0;
    int64_t footprint_bar_ms = 60000;
    int64_t cluster_window_ms = 500;
    SignalParams signal_params;
    ripple::RippleConfig ripple;
};

class OrderFlowEngine {
public:
    explicit OrderFlowEngine(const EngineConfig& config = {});
    ~OrderFlowEngine();

    OrderFlowEngine(const OrderFlowEngine&) = delete;
    OrderFlowEngine& operator=(const OrderFlowEngine&) = delete;

    void set_data_feed(std::shared_ptr<IDataFeed> feed);
    void start(const std::string& symbol);
    void stop();
    bool is_running() const { return running_.load(); }

    OrderBook& get_order_book() { return order_book_; }
    TradeFlow& get_trade_flow() { return trade_flow_; }
    VolumeProfile& get_volume_profile() { return volume_profile_; }
    CumulativeVolumeDelta& get_cvd() { return cvd_; }
    FootprintChart& get_footprint() { return footprint_; }
    SignalEngine& get_signal_engine() { return signal_engine_; }

    const OrderBook& get_order_book() const { return order_book_; }
    const TradeFlow& get_trade_flow() const { return trade_flow_; }
    const VolumeProfile& get_volume_profile() const { return volume_profile_; }
    const CumulativeVolumeDelta& get_cvd() const { return cvd_; }
    const FootprintChart& get_footprint() const { return footprint_; }
    const SignalEngine& get_signal_engine() const { return signal_engine_; }

    void set_signal_callback(SignalCallback cb);
    void set_ripple_callback(ripple::RippleDecisionCallback cb);
    void set_tick_store(std::shared_ptr<TickStore> store, const std::string& symbol);
    void set_config(const EngineConfig& config);
    EngineConfig get_config() const { return config_; }

    ripple::RippleEngine& get_ripple() { return ripple_; }
    const ripple::RippleEngine& get_ripple() const { return ripple_; }

    StrategySnapshot get_strategy_snapshot() const;

    void reset_ripple();

    void process_trade(const Trade& trade);
    void process_depth(const DepthUpdate& update);

private:
    EngineConfig config_;

    OrderBook order_book_;
    TradeFlow trade_flow_;
    VolumeProfile volume_profile_;
    CumulativeVolumeDelta cvd_;
    FootprintChart footprint_;
    SignalEngine signal_engine_;
    ripple::RippleEngine ripple_;

    std::shared_ptr<IDataFeed> data_feed_;
    std::shared_ptr<TickStore> tick_store_;
    std::string store_symbol_;
    std::atomic<bool> running_{false};
    std::mutex process_mutex_;
};

} // namespace orderflow
