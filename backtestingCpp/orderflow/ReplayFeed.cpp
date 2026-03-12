#include "ReplayFeed.h"
#include <algorithm>
#include <chrono>
#include <thread>

namespace orderflow {

ReplayFeed::ReplayFeed(std::shared_ptr<TickStore> store, double speed_multiplier)
    : store_(std::move(store)), speed_multiplier_(speed_multiplier) {}

ReplayFeed::~ReplayFeed() {
    stop();
}

void ReplayFeed::set_time_range(int64_t from_time, int64_t to_time) {
    from_time_ = from_time;
    to_time_ = to_time;
}

void ReplayFeed::subscribe_trades(const std::string& symbol) {
    symbol_ = symbol;
    replay_trades_ = true;
}

void ReplayFeed::subscribe_depth(const std::string& symbol, int /*levels*/) {
    symbol_ = symbol;
    replay_depth_ = true;
}

void ReplayFeed::start() {
    if (running_.load()) return;
    running_ = true;
    complete_ = false;
    replay_thread_ = std::thread(&ReplayFeed::replay_thread_func, this);
}

void ReplayFeed::stop() {
    running_ = false;
    if (replay_thread_.joinable()) {
        replay_thread_.join();
    }
}

void ReplayFeed::run_sync() {
    running_ = true;
    complete_ = false;
    replay_thread_func();
}

void ReplayFeed::replay_thread_func() {
    std::vector<Trade> trades;
    std::vector<DepthUpdate> depth_snapshots;
    std::vector<DepthUpdate> depth_updates;

    if (replay_trades_) {
        trades = store_->load_trades(symbol_, from_time_, to_time_);
    }
    if (replay_depth_) {
        depth_snapshots = store_->load_depth_snapshots(symbol_, from_time_, to_time_);
        depth_updates = store_->load_depth_updates(symbol_, from_time_, to_time_);
    }

    struct Event {
        int64_t timestamp;
        enum Type { TRADE, DEPTH_SNAPSHOT, DEPTH_UPDATE } type;
        size_t index;
    };

    std::vector<Event> events;
    events.reserve(trades.size() + depth_snapshots.size() + depth_updates.size());

    for (size_t i = 0; i < trades.size(); ++i) {
        events.push_back({trades[i].timestamp, Event::TRADE, i});
    }
    for (size_t i = 0; i < depth_snapshots.size(); ++i) {
        events.push_back({depth_snapshots[i].timestamp, Event::DEPTH_SNAPSHOT, i});
    }
    for (size_t i = 0; i < depth_updates.size(); ++i) {
        events.push_back({depth_updates[i].timestamp, Event::DEPTH_UPDATE, i});
    }

    std::sort(events.begin(), events.end(),
              [](const Event& a, const Event& b) { return a.timestamp < b.timestamp; });

    int64_t last_ts = 0;
    bool real_time = speed_multiplier_ > 0.0;

    for (const auto& event : events) {
        if (!running_.load()) break;

        if (real_time && last_ts > 0) {
            int64_t delay_ms = static_cast<int64_t>(
                (event.timestamp - last_ts) / speed_multiplier_);
            if (delay_ms > 0 && delay_ms < 10000) {
                std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
            }
        }
        last_ts = event.timestamp;

        switch (event.type) {
            case Event::TRADE:
                if (trade_callback_) trade_callback_(trades[event.index]);
                break;
            case Event::DEPTH_SNAPSHOT:
                if (depth_callback_) depth_callback_(depth_snapshots[event.index]);
                break;
            case Event::DEPTH_UPDATE:
                if (depth_callback_) depth_callback_(depth_updates[event.index]);
                break;
        }
    }

    complete_ = true;
    running_ = false;
}

} // namespace orderflow
