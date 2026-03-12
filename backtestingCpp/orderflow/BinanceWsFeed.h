#pragma once

#include <string>
#include <thread>
#include <atomic>
#include <memory>
#include <functional>

#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/beast/websocket/ssl.hpp>
#include <boost/asio.hpp>
#include <boost/asio/ssl.hpp>

#include "IDataFeed.h"
#include "Types.h"

namespace orderflow {

namespace beast = boost::beast;
namespace websocket = beast::websocket;
namespace net = boost::asio;
namespace ssl = net::ssl;
using tcp = net::ip::tcp;

class BinanceWsFeed : public IDataFeed {
public:
    explicit BinanceWsFeed(bool futures = true);
    ~BinanceWsFeed() override;

    void subscribe_trades(const std::string& symbol) override;
    void subscribe_depth(const std::string& symbol, int levels = 20) override;

    void set_trade_callback(TradeCallback cb) override { trade_callback_ = std::move(cb); }
    void set_depth_callback(DepthCallback cb) override { depth_callback_ = std::move(cb); }

    void start() override;
    void stop() override;
    bool is_running() const override { return running_.load(); }

private:
    void run_io();
    void connect_stream(const std::string& stream_name);
    void on_trade_message(const std::string& msg);
    void on_depth_message(const std::string& msg);

    void fetch_depth_snapshot(const std::string& symbol);

    bool futures_;
    std::string base_ws_host_;
    std::string base_rest_host_;

    std::string symbol_;
    int depth_levels_ = 20;
    bool subscribe_trades_ = false;
    bool subscribe_depth_ = false;

    TradeCallback trade_callback_;
    DepthCallback depth_callback_;

    net::io_context ioc_;
    ssl::context ssl_ctx_{ssl::context::tlsv12_client};
    std::unique_ptr<websocket::stream<beast::ssl_stream<tcp::socket>>> trade_ws_;
    std::unique_ptr<websocket::stream<beast::ssl_stream<tcp::socket>>> depth_ws_;

    std::thread io_thread_;
    std::thread trade_read_thread_;
    std::thread depth_read_thread_;
    std::atomic<bool> running_{false};
};

} // namespace orderflow
