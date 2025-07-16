#include "Database.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <cmath> // for std::isnan


#include <iostream>
#include <vector>
#include <string>

using namespace std;

Database::Database(const string &file_name)
{
    string FILE_NAME = filesystem::exists("../../data/" + file_name + ".h5")
                            ? "../../data/" + file_name + ".h5"
                            : "data/" + file_name + ".h5";
    hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);

    herr_t status = H5Pset_libver_bounds(fapl, H5F_LIBVER_LATEST, H5F_LIBVER_LATEST);
    status = H5Pset_fclose_degree(fapl, H5F_CLOSE_STRONG);

    printf("Opening %s\n", FILE_NAME.c_str());
    h5_file = H5Fopen(FILE_NAME.c_str(), H5F_ACC_RDONLY, fapl);

    if (h5_file < 0)
    {
        printf("Error while opening %s\n", FILE_NAME.c_str());
    }
}

void Database::close_file()
{
    H5Fclose(h5_file);
}


// WIP - adding optimised parameters
std::vector<BacktestResult> Database::read_optimised_parameters(const std::string &dataset_name) {
    std::vector<BacktestResult> results;

    hid_t dataset = H5Dopen2(h5_file, dataset_name.c_str(), H5P_DEFAULT);
    if (dataset < 0) {
        std::cerr << "Failed to open dataset: " << dataset_name << std::endl;
        return results;
    }

    hid_t dtype = H5Tcreate(H5T_COMPOUND, sizeof(BacktestResult));
    H5Tinsert(dtype, "symbol", HOFFSET(BacktestResult, symbol), H5Tcreate(H5T_STRING, 20));
    H5Tinsert(dtype, "strategy", HOFFSET(BacktestResult, strategy), H5Tcreate(H5T_STRING, 20));
    H5Tinsert(dtype, "tf", HOFFSET(BacktestResult, tf), H5Tcreate(H5T_STRING, 10));
    H5Tinsert(dtype, "from_time", HOFFSET(BacktestResult, from_time), H5Tcreate(H5T_STRING, 25));
    H5Tinsert(dtype, "to_time", HOFFSET(BacktestResult, to_time), H5Tcreate(H5T_STRING, 25));
    H5Tinsert(dtype, "pnl", HOFFSET(BacktestResult, pnl), H5T_NATIVE_DOUBLE);
    H5Tinsert(dtype, "max_dd", HOFFSET(BacktestResult, max_dd), H5T_NATIVE_DOUBLE);
    H5Tinsert(dtype, "num_trades", HOFFSET(BacktestResult, num_trades), H5T_NATIVE_INT);
    H5Tinsert(dtype, "cagr", HOFFSET(BacktestResult, cagr), H5T_NATIVE_DOUBLE);
    H5Tinsert(dtype, "sharpe_ratio", HOFFSET(BacktestResult, sharpe_ratio), H5T_NATIVE_DOUBLE);
    H5Tinsert(dtype, "rank", HOFFSET(BacktestResult, rank), H5T_NATIVE_INT);
    H5Tinsert(dtype, "crowding_distance", HOFFSET(BacktestResult, crowding_distance), H5T_NATIVE_DOUBLE);

    hid_t dspace = H5Dget_space(dataset);
    hsize_t dims[1];
    H5Sget_simple_extent_dims(dspace, dims, NULL);
    size_t num_records = dims[0];

    results.resize(num_records);
    H5Dread(dataset, dtype, H5S_ALL, H5S_ALL, H5P_DEFAULT, results.data());

    H5Sclose(dspace);
    H5Tclose(dtype);
    H5Dclose(dataset);

    std::cout << "Read " << num_records << " optimised parameter records." << std::endl;
    return results;
}


// WIP - Original Code
// double **Database::get_data(const string &symbol, const string &exchange, int &array_size)
// {
//     double **results = {};

//     hid_t dataset = H5Dopen2(h5_file, symbol.c_str(), H5P_DEFAULT);

//     if (dataset == -1)
//     {
//         return results;
//     }

//     auto start_ts = chrono::high_resolution_clock::now();

//     hid_t dspace = H5Dget_space(dataset);
//     hsize_t dims[2];

//     H5Sget_simple_extent_dims(dspace, dims, NULL);

//     array_size = (int)dims[0];

//     results = new double *[dims[0]];

//     for (size_t i = 0; i < dims[0]; ++i)
//     {
//         results[i] = new double[dims[1]];
//     }

//     double *candles_arr = new double[dims[0] * dims[1]];

//     H5Dread(dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, candles_arr);

//     int j = 0;

//     for (int i = 0; i < dims[0] * dims[1]; i += 6)
//     {
//         results[j][0] = candles_arr[i];     // timestamp
//         results[j][1] = candles_arr[i + 1]; // open
//         results[j][2] = candles_arr[i + 2]; // high
//         results[j][3] = candles_arr[i + 3]; // low
//         results[j][4] = candles_arr[i + 4]; // close
//         results[j][5] = candles_arr[i + 5]; // volume

//         j++;
//     }

//     delete[] candles_arr;

//     qsort(results, dims[0], sizeof(results[0]), compare);

//     H5Sclose(dspace);
//     H5Dclose(dataset);

//     auto end_ts = chrono::high_resolution_clock::now();
//     auto read_duration = chrono::duration_cast<chrono::milliseconds>(end_ts - start_ts);

//     printf("Fetched %i %s %s data in %i ms\n", (int)dims[0], exchange.c_str(), symbol.c_str(), (int)read_duration.count());

//     return results;
// }

// WIP
double **Database::get_data(const string &symbol, const string &exchange, int &array_size)
{
    double **results = nullptr;

    hid_t dataset = H5Dopen2(h5_file, symbol.c_str(), H5P_DEFAULT);
    if (dataset < 0)
    {
        return results;
    }

    auto start_ts = chrono::high_resolution_clock::now();

    hid_t dspace = H5Dget_space(dataset);
    hsize_t dims[2];
    H5Sget_simple_extent_dims(dspace, dims, NULL);

    int num_columns = static_cast<int>(dims[1]);
    int max_rows = static_cast<int>(dims[0]);

    double *flat_data = new double[max_rows * num_columns];
    H5Dread(dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_data);

    // Temporary array to hold valid rows
    double **temp_results = new double *[max_rows];
    int valid_row_count = 0;

    for (int row = 0; row < max_rows; ++row)
    {
        bool has_nan = false;

        for (int col = 0; col < num_columns; ++col)
        {
            if (std::isnan(flat_data[row * num_columns + col]))
            {
                has_nan = true;
                break;
            }
        }

        if (!has_nan)
        {
            temp_results[valid_row_count] = new double[num_columns];
            for (int col = 0; col < num_columns; ++col)
            {
                temp_results[valid_row_count][col] = flat_data[row * num_columns + col];
            }
            valid_row_count++;
        }
    }

    delete[] flat_data;

    // Allocate final result with only valid rows
    results = new double *[valid_row_count];
    for (int i = 0; i < valid_row_count; ++i)
    {
        results[i] = temp_results[i];
    }
    delete[] temp_results;

    array_size = valid_row_count;

    qsort(results, array_size, sizeof(results[0]), compare);

    H5Sclose(dspace);
    H5Dclose(dataset);

    auto end_ts = chrono::high_resolution_clock::now();
    auto read_duration = chrono::duration_cast<chrono::milliseconds>(end_ts - start_ts);

    printf("Fetched %i valid rows of %s %s data in %i ms\n", array_size, exchange.c_str(), symbol.c_str(), (int)read_duration.count());

    return results;
}

int compare(const void *pa, const void *pb)
{
    const double *a = *(const double **)pa;
    const double *b = *(const double **)pb;

    if (a[0] == b[0])
    {
        return 0;
    }
    else if (a[0] < b[0])
    {
        return -1;
    }
    else
    {
        return 1;
    }
}


void read_dataset(H5::H5File &file, const std::string &path) {
    try {
        H5::DataSet dataset = file.openDataSet(path);
        H5::DataType dtype = dataset.getDataType();
        H5T_class_t type_class = dtype.getClass();

        std::cout << path << ": ";

        if (type_class == H5T_STRING) {
            std::string str;
            dataset.read(str, dtype);
            std::cout << "(string) " << str << std::endl;
        }
        else if (type_class == H5T_INTEGER) {
            int val;
            dataset.read(&val, H5::PredType::NATIVE_INT);
            std::cout << "(int) " << val << std::endl;
        }
        else if (type_class == H5T_FLOAT) {
            double val;
            dataset.read(&val, H5::PredType::NATIVE_DOUBLE);
            std::cout << "(float) " << val << std::endl;
        }
        else if (type_class == H5T_ARRAY || type_class == H5T_COMPOUND) {
            std::cout << "(complex type)" << std::endl;
        }
        else {
            H5::DataSpace dataspace = dataset.getSpace();
            int rank = dataspace.getSimpleExtentNdims();
            if (rank == 1) {
                hsize_t dims[1];
                dataspace.getSimpleExtentDims(dims, NULL);
                std::vector<double> vec(dims[0]);
                dataset.read(vec.data(), H5::PredType::NATIVE_DOUBLE);
                std::cout << "(array) ";
                for (double v : vec) std::cout << v << " ";
                std::cout << std::endl;
            }
            else {
                std::cout << "(unsupported rank)" << std::endl;
            }
        }
    } catch (...) {
        std::cout << "(failed to read)" << std::endl;
    }
}

void read_group(H5::H5File &file, const std::string &group_path) {
    H5G_info_t group_info;
    H5Gget_info_by_name(file.getId(), group_path.c_str(), &group_info, H5P_DEFAULT);

    for (hsize_t i = 0; i < group_info.nlinks; ++i) {
        char name_buf[1024];
        ssize_t len = H5Lget_name_by_idx(file.getId(), group_path.c_str(),
                                         H5_INDEX_NAME, H5_ITER_INC, i, name_buf,
                                         sizeof(name_buf), H5P_DEFAULT);
        std::string name(name_buf, len);
        std::string full_path = group_path + "/" + name;

        H5O_info_t obj_info;
        H5Oget_info_by_name(file.getId(), full_path.c_str(), &obj_info, H5P_DEFAULT);

        if (obj_info.type == H5O_TYPE_GROUP) {
            read_group(file, full_path);
        } else if (obj_info.type == H5O_TYPE_DATASET) {
            read_dataset(file, full_path);
        }
    }
}

int main() {
    try {
        H5::H5File file("your_file.h5", H5F_ACC_RDONLY);
        read_group(file, "/object");  // Start at root group of the serialized object
    } catch (H5::Exception &e) {
        e.printErrorStack();
        return 1;
    }

    return 0;
}