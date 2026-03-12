#pragma once

#include <string>
#include <vector>
#include <mutex>
#include <hdf5.h>
#include "Types.h"

namespace orderflow {

class TickStore {
public:
    explicit TickStore(const std::string& file_path);
    ~TickStore();

    TickStore(const TickStore&) = delete;
    TickStore& operator=(const TickStore&) = delete;

    void store_trade(const std::string& symbol, const Trade& trade);
    void store_trades(const std::string& symbol, const std::vector<Trade>& trades);

    void store_depth_snapshot(const std::string& symbol, const DepthUpdate& snapshot);
    void store_depth_update(const std::string& symbol, const DepthUpdate& update);

    std::vector<Trade> load_trades(const std::string& symbol,
                                    int64_t from_time, int64_t to_time) const;

    std::vector<DepthUpdate> load_depth_snapshots(const std::string& symbol,
                                                    int64_t from_time, int64_t to_time) const;
    std::vector<DepthUpdate> load_depth_updates(const std::string& symbol,
                                                  int64_t from_time, int64_t to_time) const;

    void flush();
    void close();

    std::vector<std::string> list_symbols() const;

private:
    void ensure_group(const std::string& name);
    void ensure_trade_dataset(const std::string& symbol);
    void ensure_depth_snapshot_dataset(const std::string& symbol);
    void ensure_depth_update_dataset(const std::string& symbol);

    void write_trades_unlocked(const std::string& symbol, const std::vector<Trade>& trades);

    hid_t append_to_dataset(hid_t dataset, const void* data,
                            hsize_t rows, hsize_t cols) const;

    mutable std::mutex mutex_;
    hid_t file_id_;
    std::string file_path_;
    bool is_open_ = false;

    std::vector<Trade> trade_buffer_;
    std::string buffer_symbol_;
    static constexpr size_t FLUSH_THRESHOLD = 10000;
};

} // namespace orderflow
