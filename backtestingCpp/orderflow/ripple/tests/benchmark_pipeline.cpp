// Phase 6: tick-to-decision latency benchmark.
// Target: < 100 µs per pipeline run (strategy.md §21.1, config.benchmark_latency_target_us).

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <cmath>
#include <algorithm>
#include <numeric>
#include "ripple/RippleEngine.h"
#include "OrderBook.h"
#include "Types.h"

using namespace orderflow;
using namespace orderflow::ripple;
using Clock = std::chrono::high_resolution_clock;

static DepthUpdate make_snap(int64_t ts,
                             std::vector<std::pair<double,double>> bids,
                             std::vector<std::pair<double,double>> asks) {
    DepthUpdate u;
    u.timestamp = ts;
    u.is_snapshot = true;
    for (auto& [p,q] : bids) u.bids.push_back({p, q});
    for (auto& [p,q] : asks) u.asks.push_back({p, q});
    return u;
}

static Trade make_trade(int64_t ts, double price, double qty, bool is_buyer_maker) {
    Trade t;
    t.timestamp = ts;
    t.price = price;
    t.quantity = qty;
    t.is_buyer_maker = is_buyer_maker;
    return t;
}

int main() {
    constexpr int WARMUP  = 500;
    constexpr int MEASURE = 5000;
    constexpr double TARGET_US = 100.0;

    RippleConfig cfg;
    cfg.tick_size = 0.01;
    cfg.pipeline_min_interval_ms = 0;
    cfg.enable_diagnostics = false;

    RippleEngine engine(cfg);
    OrderBook book;

    double base_price = 100.0;
    int64_t ts = 1000000;

    auto feed_depth = [&](int64_t t, double mid) {
        std::vector<std::pair<double,double>> bids, asks;
        for (int i = 0; i < 20; ++i) {
            bids.push_back({mid - (i+1)*0.01, 10.0 + (i % 5) * 50.0});
            asks.push_back({mid + (i+1)*0.01, 10.0 + (i % 5) * 50.0});
        }
        auto snap = make_snap(t, bids, asks);
        book.on_depth_update(snap);
        engine.on_depth(snap, book);
    };

    auto feed_trade = [&](int64_t t, double price, double qty, bool buyer_maker) {
        auto tr = make_trade(t, price, qty, buyer_maker);
        book.on_trade(tr);
        engine.on_trade(tr, book);
    };

    // Warm up: feed events to prime all internal buffers and state
    for (int i = 0; i < WARMUP; ++i) {
        ts += 300;
        double drift = 0.01 * std::sin(i * 0.05);
        double mid = base_price + drift;
        feed_depth(ts, mid);
        ts += 100;
        bool side = (i % 3) != 0;
        feed_trade(ts, mid + (side ? 0.01 : -0.01), 1.0 + (i % 10), side);
    }

    // Measurement: alternate depth and trade events, time each pipeline run
    std::vector<double> latencies_us;
    latencies_us.reserve(MEASURE);
    uint64_t runs_before = engine.pipeline_runs();

    for (int i = 0; i < MEASURE; ++i) {
        ts += 300;
        double drift = 0.01 * std::sin((WARMUP + i) * 0.05);
        double mid = base_price + drift;

        // Depth update
        feed_depth(ts, mid);

        // Trade event — this is the primary pipeline trigger
        ts += 100;
        bool side = (i % 3) != 0;
        double tprice = mid + (side ? 0.01 : -0.01);

        auto t0 = Clock::now();
        feed_trade(ts, tprice, 1.0 + (i % 10), side);
        auto t1 = Clock::now();

        double us = std::chrono::duration<double, std::micro>(t1 - t0).count();
        latencies_us.push_back(us);
    }

    uint64_t runs_after = engine.pipeline_runs();
    uint64_t actual_runs = runs_after - runs_before;

    // Compute statistics
    std::sort(latencies_us.begin(), latencies_us.end());
    double sum = std::accumulate(latencies_us.begin(), latencies_us.end(), 0.0);
    double mean = sum / latencies_us.size();
    double median = latencies_us[latencies_us.size() / 2];
    double p95 = latencies_us[static_cast<size_t>(latencies_us.size() * 0.95)];
    double p99 = latencies_us[static_cast<size_t>(latencies_us.size() * 0.99)];
    double max_val = latencies_us.back();

    std::printf("=== Pipeline Latency Benchmark ===\n");
    std::printf("Events:       %d trade events (+ %d depth)\n", MEASURE, MEASURE);
    std::printf("Pipeline runs: %llu (from %d events)\n",
                static_cast<unsigned long long>(actual_runs), MEASURE);
    std::printf("Mean:    %8.2f µs\n", mean);
    std::printf("Median:  %8.2f µs\n", median);
    std::printf("P95:     %8.2f µs\n", p95);
    std::printf("P99:     %8.2f µs\n", p99);
    std::printf("Max:     %8.2f µs\n", max_val);
    std::printf("Target:  %8.2f µs\n", TARGET_US);

    bool passed = p99 < TARGET_US;
    std::printf("Result:  %s (P99 %s target)\n",
                passed ? "PASS" : "FAIL",
                passed ? "<" : ">=");

    return passed ? 0 : 1;
}
