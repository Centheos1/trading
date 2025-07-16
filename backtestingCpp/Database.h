#include <string>
#include <hdf5.h>


// WIP - adding optimised parameters
struct BacktestResult {
    char symbol[20];
    char strategy[20];
    char tf[10];
    char from_time[25];
    char to_time[25];
    double pnl;
    double max_dd;
    int num_trades;
    double cagr;
    double sharpe_ratio;
    int rank;
    double crowding_distance;
};


class Database
{
public:
    Database(const std::string &file_name);
    void close_file();
    double **get_data(const std::string &symbol, const std::string &exchange, int &array_size);

    hid_t h5_file;

    // WIP - adding optimised parameters
    std::vector<BacktestResult> read_optimised_parameters(const std::string &dataset_name);

};

int compare(const void *pa, const void *pb);
