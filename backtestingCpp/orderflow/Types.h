#pragma once

#include <string>
#include <vector>
#include <cstdint>
#include <functional>
#include <map>

namespace orderflow {

struct Trade {
    int64_t timestamp;
    double price;
    double quantity;
    bool is_buyer_maker;

    bool is_buy_aggressor() const { return !is_buyer_maker; }
    bool is_sell_aggressor() const { return is_buyer_maker; }
};

struct DepthLevel {
    double price;
    double quantity;
};

struct DepthUpdate {
    int64_t timestamp;
    std::vector<DepthLevel> bids;
    std::vector<DepthLevel> asks;
    int64_t first_update_id;
    int64_t final_update_id;
    bool is_snapshot;
};

enum class SignalType {
    ABSORPTION_BUY,
    ABSORPTION_SELL,
    STACKED_IMBALANCE_BUY,
    STACKED_IMBALANCE_SELL,
    DELTA_DIVERGENCE_BULL,
    DELTA_DIVERGENCE_BEAR,
    EXHAUSTION_BUY,
    EXHAUSTION_SELL,
    POC_REJECTION_BUY,
    POC_REJECTION_SELL
};

struct Signal {
    int64_t timestamp;
    SignalType type;
    double price;
    double strength;
    std::string description;

    int direction() const {
        switch (type) {
            case SignalType::ABSORPTION_BUY:
            case SignalType::STACKED_IMBALANCE_BUY:
            case SignalType::DELTA_DIVERGENCE_BULL:
            case SignalType::EXHAUSTION_BUY:
            case SignalType::POC_REJECTION_BUY:
                return 1;
            default:
                return -1;
        }
    }

    std::string type_name() const {
        switch (type) {
            case SignalType::ABSORPTION_BUY:         return "ABSORPTION_BUY";
            case SignalType::ABSORPTION_SELL:        return "ABSORPTION_SELL";
            case SignalType::STACKED_IMBALANCE_BUY:  return "STACKED_IMBALANCE_BUY";
            case SignalType::STACKED_IMBALANCE_SELL: return "STACKED_IMBALANCE_SELL";
            case SignalType::DELTA_DIVERGENCE_BULL:  return "DELTA_DIVERGENCE_BULL";
            case SignalType::DELTA_DIVERGENCE_BEAR:  return "DELTA_DIVERGENCE_BEAR";
            case SignalType::EXHAUSTION_BUY:         return "EXHAUSTION_BUY";
            case SignalType::EXHAUSTION_SELL:        return "EXHAUSTION_SELL";
            case SignalType::POC_REJECTION_BUY:      return "POC_REJECTION_BUY";
            case SignalType::POC_REJECTION_SELL:     return "POC_REJECTION_SELL";
        }
        return "UNKNOWN";
    }
};

struct FootprintCell {
    double bid_volume = 0.0;
    double ask_volume = 0.0;
    double imbalance() const {
        double total = bid_volume + ask_volume;
        if (total == 0.0) return 0.0;
        return (ask_volume - bid_volume) / total;
    }
};

struct VolumeNode {
    double price;
    double volume;
    double buy_volume;
    double sell_volume;
};

using TradeCallback = std::function<void(const Trade&)>;
using DepthCallback = std::function<void(const DepthUpdate&)>;
using SignalCallback = std::function<void(const Signal&)>;

} // namespace orderflow
