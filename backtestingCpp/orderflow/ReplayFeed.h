#pragma once

#include <string>
#include <thread>
#include <atomic>
#include <memory>
#include "IDataFeed.h"
#include "TickStore.h"

namespace orderflow {

class ReplayFeed : public IDataFeed {
public:
    explicit ReplayFeed(std::shared_ptr<TickStore> store,
                        double speed_multiplier = 1.0);
    ~ReplayFeed() override;

    void set_time_range(int64_t from_time, int64_t to_time);
    void set_speed(double multiplier) { speed_multiplier_ = multiplier; }

    void subscribe_trades(const std::string& symbol) override;
    void subscribe_depth(const std::string& symbol, int levels = 20) override;

    void set_trade_callback(TradeCallback cb) override { trade_callback_ = std::move(cb); }
    void set_depth_callback(DepthCallback cb) override { depth_callback_ = std::move(cb); }

    void start() override;
    void stop() override;
    bool is_running() const override { return running_.load(); }

    bool is_complete() const { return complete_.load(); }

    void run_sync();

private:
    void replay_thread_func();

    std::shared_ptr<TickStore> store_;
    double speed_multiplier_;

    std::string symbol_;
    int64_t from_time_ = 0;
    int64_t to_time_ = 0;
    bool replay_trades_ = false;
    bool replay_depth_ = false;

    TradeCallback trade_callback_;
    DepthCallback depth_callback_;

    std::thread replay_thread_;
    std::atomic<bool> running_{false};
    std::atomic<bool> complete_{false};
};

} // namespace orderflow
