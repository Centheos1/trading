#pragma once

#include <string>
#include "Types.h"

namespace orderflow {

class IDataFeed {
public:
    virtual ~IDataFeed() = default;

    virtual void subscribe_trades(const std::string& symbol) = 0;
    virtual void subscribe_depth(const std::string& symbol, int levels = 20) = 0;

    virtual void set_trade_callback(TradeCallback cb) = 0;
    virtual void set_depth_callback(DepthCallback cb) = 0;

    virtual void start() = 0;
    virtual void stop() = 0;
    virtual bool is_running() const = 0;
};

} // namespace orderflow
