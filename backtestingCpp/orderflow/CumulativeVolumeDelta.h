#pragma once

#include <deque>
#include <vector>
#include <mutex>
#include <cstdint>
#include "Types.h"

namespace orderflow {

struct CVDPoint {
    int64_t timestamp;
    double delta;
    double price;
};

class CumulativeVolumeDelta {
public:
    CumulativeVolumeDelta() = default;

    void on_trade(const Trade& trade);
    void reset();

    double get_cvd() const;
    double get_session_delta() const;

    std::vector<CVDPoint> get_history() const;
    std::vector<CVDPoint> get_history_window(int64_t window_ms) const;

    struct DivergenceResult {
        bool detected;
        bool is_bearish;
        double price_change;
        double delta_change;
        int64_t start_ts;
        int64_t end_ts;
    };
    DivergenceResult detect_divergence(int lookback_points) const;

    struct ExhaustionResult {
        bool detected;
        bool is_bearish;
        int consecutive_declining;
        double price_at_extreme;
    };
    ExhaustionResult detect_exhaustion(int lookback_bars, double price_change_threshold) const;

    void set_max_history(size_t max) { max_history_ = max; }

private:
    mutable std::mutex mutex_;
    std::deque<CVDPoint> history_;
    double cumulative_delta_ = 0.0;
    size_t max_history_ = 500000;
};

} // namespace orderflow
