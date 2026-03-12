#include "OrderFlowEngine.h"

namespace orderflow {

OrderFlowEngine::OrderFlowEngine(const EngineConfig& config)
    : config_(config),
      trade_flow_(config.large_trade_threshold, config.cluster_window_ms),
      volume_profile_(config.tick_size),
      footprint_(config.footprint_bar_ms, config.tick_size),
      signal_engine_(order_book_, trade_flow_, volume_profile_, cvd_, footprint_),
      ripple_(config.ripple) {
    signal_engine_.set_params(config.signal_params);
}

OrderFlowEngine::~OrderFlowEngine() {
    stop();
}

void OrderFlowEngine::set_data_feed(std::shared_ptr<IDataFeed> feed) {
    data_feed_ = std::move(feed);
}

void OrderFlowEngine::start(const std::string& symbol) {
    if (running_.load()) return;
    running_ = true;

    if (data_feed_) {
        data_feed_->set_trade_callback([this](const Trade& trade) {
            process_trade(trade);
        });
        data_feed_->set_depth_callback([this](const DepthUpdate& update) {
            process_depth(update);
        });
        data_feed_->subscribe_trades(symbol);
        data_feed_->subscribe_depth(symbol);
        data_feed_->start();
    }
}

void OrderFlowEngine::stop() {
    if (!running_.load()) return;
    running_ = false;

    if (data_feed_) {
        data_feed_->stop();
    }
}

void OrderFlowEngine::set_signal_callback(SignalCallback cb) {
    signal_engine_.set_signal_callback(std::move(cb));
}

void OrderFlowEngine::set_ripple_callback(ripple::RippleDecisionCallback cb) {
    ripple_.set_decision_callback(std::move(cb));
}

void OrderFlowEngine::reset_ripple() {
    ripple_.reset();
}

void OrderFlowEngine::set_tick_store(std::shared_ptr<TickStore> store, const std::string& symbol) {
    tick_store_ = std::move(store);
    store_symbol_ = symbol;
}

void OrderFlowEngine::set_config(const EngineConfig& config) {
    config_ = config;
    volume_profile_.set_tick_size(config.tick_size);
    trade_flow_.set_large_trade_threshold(config.large_trade_threshold);
    footprint_.set_bar_duration(config.footprint_bar_ms);
    footprint_.set_tick_size(config.tick_size);
    signal_engine_.set_params(config.signal_params);
}

void OrderFlowEngine::process_trade(const Trade& trade) {
    std::lock_guard<std::mutex> lock(process_mutex_);
    order_book_.on_trade(trade);
    trade_flow_.on_trade(trade);
    volume_profile_.on_trade(trade);
    cvd_.on_trade(trade);
    footprint_.on_trade(trade);
    signal_engine_.on_trade(trade);
    ripple_.on_trade(trade, order_book_);

    if (tick_store_) {
        tick_store_->store_trade(store_symbol_, trade);
    }
}

void OrderFlowEngine::process_depth(const DepthUpdate& update) {
    std::lock_guard<std::mutex> lock(process_mutex_);
    order_book_.on_depth_update(update);
    signal_engine_.on_depth_update(update);
    ripple_.on_depth(update, order_book_);

    if (tick_store_) {
        if (update.is_snapshot) {
            tick_store_->store_depth_snapshot(store_symbol_, update);
        } else {
            tick_store_->store_depth_update(store_symbol_, update);
        }
    }
}

} // namespace orderflow
