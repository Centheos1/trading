#include "BinanceWsFeed.h"
#include <nlohmann/json.hpp>
#include <boost/beast/http.hpp>
#include <boost/asio/connect.hpp>
#include <iostream>
#include <algorithm>

namespace orderflow {

using json = nlohmann::json;
namespace http = beast::http;

BinanceWsFeed::BinanceWsFeed(bool futures) : futures_(futures) {
    if (futures_) {
        base_ws_host_ = "fstream.binance.com";
        base_rest_host_ = "fapi.binance.com";
    } else {
        base_ws_host_ = "stream.binance.com";
        base_rest_host_ = "api.binance.com";
    }
    ssl_ctx_.set_default_verify_paths();
}

BinanceWsFeed::~BinanceWsFeed() {
    stop();
}

void BinanceWsFeed::subscribe_trades(const std::string& symbol) {
    symbol_ = symbol;
    std::transform(symbol_.begin(), symbol_.end(), symbol_.begin(), ::tolower);
    subscribe_trades_ = true;
}

void BinanceWsFeed::subscribe_depth(const std::string& symbol, int levels) {
    symbol_ = symbol;
    std::transform(symbol_.begin(), symbol_.end(), symbol_.begin(), ::tolower);
    depth_levels_ = levels;
    subscribe_depth_ = true;
}

void BinanceWsFeed::start() {
    if (running_.load()) return;
    running_ = true;

    if (subscribe_trades_) {
        trade_read_thread_ = std::thread([this]() {
            try {
                net::io_context trade_ioc;
                ssl::context ctx{ssl::context::tlsv12_client};
                ctx.set_default_verify_paths();

                tcp::resolver resolver{trade_ioc};
                auto results = resolver.resolve(base_ws_host_, "443");

                trade_ws_ = std::make_unique<websocket::stream<beast::ssl_stream<tcp::socket>>>(
                    trade_ioc, ctx);

                auto& tcp_layer = beast::get_lowest_layer(*trade_ws_);
                net::connect(tcp_layer, results);

                if (!SSL_set_tlsext_host_name(trade_ws_->next_layer().native_handle(),
                                               base_ws_host_.c_str())) {
                    return;
                }
                trade_ws_->next_layer().handshake(ssl::stream_base::client);

                std::string target = "/ws/" + symbol_ + "@trade";
                trade_ws_->handshake(base_ws_host_, target);

                beast::flat_buffer buffer;
                while (running_.load()) {
                    buffer.clear();
                    trade_ws_->read(buffer);
                    std::string msg = beast::buffers_to_string(buffer.data());
                    on_trade_message(msg);
                }
            } catch (const std::exception& e) {
                if (running_.load()) {
                    std::cerr << "Trade WS error: " << e.what() << std::endl;
                }
            }
        });
    }

    if (subscribe_depth_) {
        depth_read_thread_ = std::thread([this]() {
            try {
                std::string upper_symbol = symbol_;
                std::transform(upper_symbol.begin(), upper_symbol.end(),
                               upper_symbol.begin(), ::toupper);
                fetch_depth_snapshot(upper_symbol);

                net::io_context depth_ioc;
                ssl::context ctx{ssl::context::tlsv12_client};
                ctx.set_default_verify_paths();

                tcp::resolver resolver{depth_ioc};
                auto results = resolver.resolve(base_ws_host_, "443");

                depth_ws_ = std::make_unique<websocket::stream<beast::ssl_stream<tcp::socket>>>(
                    depth_ioc, ctx);

                auto& tcp_layer = beast::get_lowest_layer(*depth_ws_);
                net::connect(tcp_layer, results);

                if (!SSL_set_tlsext_host_name(depth_ws_->next_layer().native_handle(),
                                               base_ws_host_.c_str())) {
                    return;
                }
                depth_ws_->next_layer().handshake(ssl::stream_base::client);

                std::string target = "/ws/" + symbol_ + "@depth@100ms";
                depth_ws_->handshake(base_ws_host_, target);

                beast::flat_buffer buffer;
                while (running_.load()) {
                    buffer.clear();
                    depth_ws_->read(buffer);
                    std::string msg = beast::buffers_to_string(buffer.data());
                    on_depth_message(msg);
                }
            } catch (const std::exception& e) {
                if (running_.load()) {
                    std::cerr << "Depth WS error: " << e.what() << std::endl;
                }
            }
        });
    }
}

void BinanceWsFeed::stop() {
    running_ = false;

    try {
        if (trade_ws_) {
            beast::get_lowest_layer(*trade_ws_).close();
        }
    } catch (...) {}

    try {
        if (depth_ws_) {
            beast::get_lowest_layer(*depth_ws_).close();
        }
    } catch (...) {}

    if (trade_read_thread_.joinable()) trade_read_thread_.join();
    if (depth_read_thread_.joinable()) depth_read_thread_.join();

    trade_ws_.reset();
    depth_ws_.reset();
}

void BinanceWsFeed::on_trade_message(const std::string& msg) {
    if (!trade_callback_) return;

    try {
        auto j = json::parse(msg);
        Trade trade;
        trade.timestamp = j["T"].get<int64_t>();
        trade.price = std::stod(j["p"].get<std::string>());
        trade.quantity = std::stod(j["q"].get<std::string>());
        trade.is_buyer_maker = j["m"].get<bool>();
        trade_callback_(trade);
    } catch (const std::exception& e) {
        std::cerr << "Trade parse error: " << e.what() << std::endl;
    }
}

void BinanceWsFeed::on_depth_message(const std::string& msg) {
    if (!depth_callback_) return;

    try {
        auto j = json::parse(msg);
        DepthUpdate update;
        update.timestamp = j.value("E", 0LL);
        update.first_update_id = j.value("U", 0LL);
        update.final_update_id = j.value("u", 0LL);
        update.is_snapshot = false;

        for (const auto& bid : j["b"]) {
            double price = std::stod(bid[0].get<std::string>());
            double qty = std::stod(bid[1].get<std::string>());
            update.bids.push_back({price, qty});
        }
        for (const auto& ask : j["a"]) {
            double price = std::stod(ask[0].get<std::string>());
            double qty = std::stod(ask[1].get<std::string>());
            update.asks.push_back({price, qty});
        }

        depth_callback_(update);
    } catch (const std::exception& e) {
        std::cerr << "Depth parse error: " << e.what() << std::endl;
    }
}

void BinanceWsFeed::fetch_depth_snapshot(const std::string& symbol) {
    if (!depth_callback_) return;

    try {
        net::io_context ioc;
        ssl::context ctx{ssl::context::tlsv12_client};
        ctx.set_default_verify_paths();

        tcp::resolver resolver{ioc};
        auto results = resolver.resolve(base_rest_host_, "443");

        beast::ssl_stream<tcp::socket> stream{ioc, ctx};
        if (!SSL_set_tlsext_host_name(stream.native_handle(), base_rest_host_.c_str())) {
            return;
        }

        net::connect(beast::get_lowest_layer(stream), results);
        stream.handshake(ssl::stream_base::client);

        std::string endpoint = futures_ ? "/fapi/v1/depth" : "/api/v3/depth";
        std::string target = endpoint + "?symbol=" + symbol + "&limit=1000";

        http::request<http::empty_body> req{http::verb::get, target, 11};
        req.set(http::field::host, base_rest_host_);
        req.set(http::field::user_agent, "OrderFlowEngine/1.0");
        http::write(stream, req);

        beast::flat_buffer buffer;
        http::response<http::string_body> res;
        http::read(stream, buffer, res);

        auto j = json::parse(res.body());

        DepthUpdate snapshot;
        snapshot.timestamp = j.value("E",
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count());
        snapshot.first_update_id = j.value("lastUpdateId", 0LL);
        snapshot.final_update_id = snapshot.first_update_id;
        snapshot.is_snapshot = true;

        for (const auto& bid : j["bids"]) {
            double price = std::stod(bid[0].get<std::string>());
            double qty = std::stod(bid[1].get<std::string>());
            snapshot.bids.push_back({price, qty});
        }
        for (const auto& ask : j["asks"]) {
            double price = std::stod(ask[0].get<std::string>());
            double qty = std::stod(ask[1].get<std::string>());
            snapshot.asks.push_back({price, qty});
        }

        depth_callback_(snapshot);
    } catch (const std::exception& e) {
        std::cerr << "Depth snapshot error: " << e.what() << std::endl;
    }
}

} // namespace orderflow
