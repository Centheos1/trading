#include "TickStore.h"
#include <stdexcept>
#include <algorithm>
#include <iostream>
#include <cstring>

namespace orderflow {

TickStore::TickStore(const std::string& file_path) : file_path_(file_path) {
    H5Eset_auto2(H5E_DEFAULT, nullptr, nullptr);
    file_id_ = H5Fopen(file_path.c_str(), H5F_ACC_RDWR, H5P_DEFAULT);
    if (file_id_ < 0) {
        file_id_ = H5Fcreate(file_path.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
    }
    H5Eset_auto2(H5E_DEFAULT, (H5E_auto2_t)H5Eprint2, stderr);
    if (file_id_ < 0) {
        throw std::runtime_error("Failed to open/create HDF5 file: " + file_path);
    }
    is_open_ = true;
}

TickStore::~TickStore() {
    close();
}

void TickStore::ensure_group(const std::string& name) {
    htri_t exists = H5Lexists(file_id_, name.c_str(), H5P_DEFAULT);
    if (exists <= 0) {
        hid_t group = H5Gcreate2(file_id_, name.c_str(), H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
        if (group >= 0) H5Gclose(group);
    }
}

void TickStore::ensure_trade_dataset(const std::string& symbol) {
    ensure_group(symbol);
    std::string ds_name = symbol + "/trades";

    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists > 0) return;

    hsize_t dims[2] = {0, 4};
    hsize_t max_dims[2] = {H5S_UNLIMITED, 4};
    hsize_t chunk_dims[2] = {1024, 4};

    hid_t space = H5Screate_simple(2, dims, max_dims);
    hid_t plist = H5Pcreate(H5P_DATASET_CREATE);
    H5Pset_chunk(plist, 2, chunk_dims);
    H5Pset_deflate(plist, 4);

    hid_t ds = H5Dcreate2(file_id_, ds_name.c_str(), H5T_NATIVE_DOUBLE,
                           space, H5P_DEFAULT, plist, H5P_DEFAULT);

    H5Dclose(ds);
    H5Pclose(plist);
    H5Sclose(space);
}

void TickStore::ensure_depth_snapshot_dataset(const std::string& symbol) {
    ensure_group(symbol);
    std::string ds_name = symbol + "/depth_snapshots";

    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists > 0) return;

    hsize_t dims[2] = {0, 4};
    hsize_t max_dims[2] = {H5S_UNLIMITED, 4};
    hsize_t chunk_dims[2] = {512, 4};

    hid_t space = H5Screate_simple(2, dims, max_dims);
    hid_t plist = H5Pcreate(H5P_DATASET_CREATE);
    H5Pset_chunk(plist, 2, chunk_dims);
    H5Pset_deflate(plist, 4);

    hid_t ds = H5Dcreate2(file_id_, ds_name.c_str(), H5T_NATIVE_DOUBLE,
                           space, H5P_DEFAULT, plist, H5P_DEFAULT);

    H5Dclose(ds);
    H5Pclose(plist);
    H5Sclose(space);
}

void TickStore::ensure_depth_update_dataset(const std::string& symbol) {
    ensure_group(symbol);
    std::string ds_name = symbol + "/depth_updates";

    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists > 0) return;

    hsize_t dims[2] = {0, 4};
    hsize_t max_dims[2] = {H5S_UNLIMITED, 4};
    hsize_t chunk_dims[2] = {2048, 4};

    hid_t space = H5Screate_simple(2, dims, max_dims);
    hid_t plist = H5Pcreate(H5P_DATASET_CREATE);
    H5Pset_chunk(plist, 2, chunk_dims);
    H5Pset_deflate(plist, 4);

    hid_t ds = H5Dcreate2(file_id_, ds_name.c_str(), H5T_NATIVE_DOUBLE,
                           space, H5P_DEFAULT, plist, H5P_DEFAULT);

    H5Dclose(ds);
    H5Pclose(plist);
    H5Sclose(space);
}

void TickStore::store_trade(const std::string& symbol, const Trade& trade) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!buffer_symbol_.empty() && buffer_symbol_ != symbol && !trade_buffer_.empty()) {
        write_trades_unlocked(buffer_symbol_, trade_buffer_);
        trade_buffer_.clear();
    }
    buffer_symbol_ = symbol;
    trade_buffer_.push_back(trade);
    if (trade_buffer_.size() >= FLUSH_THRESHOLD) {
        write_trades_unlocked(symbol, trade_buffer_);
        trade_buffer_.clear();
    }
}

void TickStore::store_trades(const std::string& symbol, const std::vector<Trade>& trades) {
    if (trades.empty()) return;
    std::lock_guard<std::mutex> lock(mutex_);
    write_trades_unlocked(symbol, trades);
}

void TickStore::write_trades_unlocked(const std::string& symbol, const std::vector<Trade>& trades) {
    if (trades.empty()) return;

    ensure_trade_dataset(symbol);
    std::string ds_name = symbol + "/trades";

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return;

    hid_t space = H5Dget_space(ds);
    hsize_t current_dims[2];
    H5Sget_simple_extent_dims(space, current_dims, nullptr);
    H5Sclose(space);

    hsize_t new_rows = trades.size();
    hsize_t new_dims[2] = {current_dims[0] + new_rows, 4};
    H5Dset_extent(ds, new_dims);

    std::vector<double> data(new_rows * 4);
    for (size_t i = 0; i < trades.size(); ++i) {
        data[i * 4 + 0] = static_cast<double>(trades[i].timestamp);
        data[i * 4 + 1] = trades[i].price;
        data[i * 4 + 2] = trades[i].quantity;
        data[i * 4 + 3] = trades[i].is_buyer_maker ? 1.0 : 0.0;
    }

    hsize_t offset[2] = {current_dims[0], 0};
    hsize_t count[2] = {new_rows, 4};

    space = H5Dget_space(ds);
    H5Sselect_hyperslab(space, H5S_SELECT_SET, offset, nullptr, count, nullptr);

    hid_t memspace = H5Screate_simple(2, count, nullptr);
    H5Dwrite(ds, H5T_NATIVE_DOUBLE, memspace, space, H5P_DEFAULT, data.data());

    H5Sclose(memspace);
    H5Sclose(space);
    H5Dclose(ds);
}

void TickStore::store_depth_snapshot(const std::string& symbol, const DepthUpdate& snapshot) {
    std::lock_guard<std::mutex> lock(mutex_);
    ensure_depth_snapshot_dataset(symbol);
    std::string ds_name = symbol + "/depth_snapshots";

    std::vector<double> rows;
    for (const auto& bid : snapshot.bids) {
        rows.push_back(static_cast<double>(snapshot.timestamp));
        rows.push_back(0.0);
        rows.push_back(bid.price);
        rows.push_back(bid.quantity);
    }
    for (const auto& ask : snapshot.asks) {
        rows.push_back(static_cast<double>(snapshot.timestamp));
        rows.push_back(1.0);
        rows.push_back(ask.price);
        rows.push_back(ask.quantity);
    }

    hsize_t new_rows = rows.size() / 4;
    if (new_rows == 0) return;

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return;

    hid_t space = H5Dget_space(ds);
    hsize_t current_dims[2];
    H5Sget_simple_extent_dims(space, current_dims, nullptr);
    H5Sclose(space);

    hsize_t new_dims[2] = {current_dims[0] + new_rows, 4};
    H5Dset_extent(ds, new_dims);

    hsize_t offset[2] = {current_dims[0], 0};
    hsize_t count[2] = {new_rows, 4};

    space = H5Dget_space(ds);
    H5Sselect_hyperslab(space, H5S_SELECT_SET, offset, nullptr, count, nullptr);

    hid_t memspace = H5Screate_simple(2, count, nullptr);
    H5Dwrite(ds, H5T_NATIVE_DOUBLE, memspace, space, H5P_DEFAULT, rows.data());

    H5Sclose(memspace);
    H5Sclose(space);
    H5Dclose(ds);
}

void TickStore::store_depth_update(const std::string& symbol, const DepthUpdate& update) {
    std::lock_guard<std::mutex> lock(mutex_);
    ensure_depth_update_dataset(symbol);
    std::string ds_name = symbol + "/depth_updates";

    std::vector<double> rows;
    for (const auto& bid : update.bids) {
        rows.push_back(static_cast<double>(update.timestamp));
        rows.push_back(0.0);
        rows.push_back(bid.price);
        rows.push_back(bid.quantity);
    }
    for (const auto& ask : update.asks) {
        rows.push_back(static_cast<double>(update.timestamp));
        rows.push_back(1.0);
        rows.push_back(ask.price);
        rows.push_back(ask.quantity);
    }

    hsize_t new_rows = rows.size() / 4;
    if (new_rows == 0) return;

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return;

    hid_t space = H5Dget_space(ds);
    hsize_t current_dims[2];
    H5Sget_simple_extent_dims(space, current_dims, nullptr);
    H5Sclose(space);

    hsize_t new_dims[2] = {current_dims[0] + new_rows, 4};
    H5Dset_extent(ds, new_dims);

    hsize_t offset[2] = {current_dims[0], 0};
    hsize_t count[2] = {new_rows, 4};

    space = H5Dget_space(ds);
    H5Sselect_hyperslab(space, H5S_SELECT_SET, offset, nullptr, count, nullptr);

    hid_t memspace = H5Screate_simple(2, count, nullptr);
    H5Dwrite(ds, H5T_NATIVE_DOUBLE, memspace, space, H5P_DEFAULT, rows.data());

    H5Sclose(memspace);
    H5Sclose(space);
    H5Dclose(ds);
}

std::vector<Trade> TickStore::load_trades(const std::string& symbol,
                                           int64_t from_time, int64_t to_time) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<Trade> trades;

    std::string ds_name = symbol + "/trades";
    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists <= 0) return trades;

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return trades;

    hid_t space = H5Dget_space(ds);
    hsize_t dims[2];
    H5Sget_simple_extent_dims(space, dims, nullptr);

    std::vector<double> data(dims[0] * dims[1]);
    H5Dread(ds, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, data.data());

    for (hsize_t i = 0; i < dims[0]; ++i) {
        int64_t ts = static_cast<int64_t>(data[i * 4]);
        if (ts < from_time) continue;
        if (ts > to_time) break;

        Trade t;
        t.timestamp = ts;
        t.price = data[i * 4 + 1];
        t.quantity = data[i * 4 + 2];
        t.is_buyer_maker = data[i * 4 + 3] > 0.5;
        trades.push_back(t);
    }

    H5Sclose(space);
    H5Dclose(ds);
    return trades;
}

std::vector<DepthUpdate> TickStore::load_depth_snapshots(const std::string& symbol,
                                                           int64_t from_time, int64_t to_time) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<DepthUpdate> updates;

    std::string ds_name = symbol + "/depth_snapshots";
    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists <= 0) return updates;

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return updates;

    hid_t space = H5Dget_space(ds);
    hsize_t dims[2];
    H5Sget_simple_extent_dims(space, dims, nullptr);

    std::vector<double> data(dims[0] * dims[1]);
    H5Dread(ds, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, data.data());

    DepthUpdate current;
    current.is_snapshot = true;
    int64_t current_ts = -1;

    for (hsize_t i = 0; i < dims[0]; ++i) {
        int64_t ts = static_cast<int64_t>(data[i * 4]);
        if (ts < from_time) continue;
        if (ts > to_time) break;

        if (ts != current_ts) {
            if (current_ts >= 0) {
                updates.push_back(current);
            }
            current = DepthUpdate{};
            current.timestamp = ts;
            current.is_snapshot = true;
            current_ts = ts;
        }

        double side = data[i * 4 + 1];
        double price = data[i * 4 + 2];
        double qty = data[i * 4 + 3];

        if (side < 0.5) {
            current.bids.push_back({price, qty});
        } else {
            current.asks.push_back({price, qty});
        }
    }
    if (current_ts >= 0) {
        updates.push_back(current);
    }

    H5Sclose(space);
    H5Dclose(ds);
    return updates;
}

std::vector<DepthUpdate> TickStore::load_depth_updates(const std::string& symbol,
                                                         int64_t from_time, int64_t to_time) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<DepthUpdate> updates;

    std::string ds_name = symbol + "/depth_updates";
    htri_t exists = H5Lexists(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (exists <= 0) return updates;

    hid_t ds = H5Dopen2(file_id_, ds_name.c_str(), H5P_DEFAULT);
    if (ds < 0) return updates;

    hid_t space = H5Dget_space(ds);
    hsize_t dims[2];
    H5Sget_simple_extent_dims(space, dims, nullptr);

    std::vector<double> data(dims[0] * dims[1]);
    H5Dread(ds, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, data.data());

    DepthUpdate current;
    current.is_snapshot = false;
    int64_t current_ts = -1;

    for (hsize_t i = 0; i < dims[0]; ++i) {
        int64_t ts = static_cast<int64_t>(data[i * 4]);
        if (ts < from_time) continue;
        if (ts > to_time) break;

        if (ts != current_ts) {
            if (current_ts >= 0) {
                updates.push_back(current);
            }
            current = DepthUpdate{};
            current.timestamp = ts;
            current.is_snapshot = false;
            current_ts = ts;
        }

        double side = data[i * 4 + 1];
        double price = data[i * 4 + 2];
        double qty = data[i * 4 + 3];

        if (side < 0.5) {
            current.bids.push_back({price, qty});
        } else {
            current.asks.push_back({price, qty});
        }
    }
    if (current_ts >= 0) {
        updates.push_back(current);
    }

    H5Sclose(space);
    H5Dclose(ds);
    return updates;
}

void TickStore::flush() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (is_open_) {
        if (!trade_buffer_.empty() && !buffer_symbol_.empty()) {
            write_trades_unlocked(buffer_symbol_, trade_buffer_);
            trade_buffer_.clear();
        }
        H5Fflush(file_id_, H5F_SCOPE_GLOBAL);
    }
}

void TickStore::close() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (is_open_) {
        H5Fclose(file_id_);
        is_open_ = false;
    }
}

std::vector<std::string> TickStore::list_symbols() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<std::string> symbols;

    hsize_t num_objs;
    H5Gget_num_objs(file_id_, &num_objs);

    for (hsize_t i = 0; i < num_objs; ++i) {
        char name[256];
        H5Gget_objname_by_idx(file_id_, i, name, sizeof(name));
        if (H5Gget_objtype_by_idx(file_id_, i) == H5G_GROUP) {
            symbols.emplace_back(name);
        }
    }
    return symbols;
}

} // namespace orderflow
