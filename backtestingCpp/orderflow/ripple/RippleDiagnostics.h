#pragma once

#include <fstream>
#include <string>
#include <vector>
#include <deque>
#include "RippleTypes.h"

namespace orderflow::ripple {

struct DiagnosticRecord {
    Timestamp            ts;
    RippleFeatures       features;
    RippleEvidence       evidence;
    RippleInferenceResult inference;
    RippleDecision       decision;
};

class RippleDiagnostics {
public:
    explicit RippleDiagnostics(const std::string& output_path = "",
                                size_t max_memory_records = 10000,
                                bool console_output = false);
    ~RippleDiagnostics();

    void log(Timestamp ts,
             const RippleFeatures& features,
             const RippleEvidence& evidence,
             const RippleInferenceResult& inference,
             const RippleDecision& decision);

    const std::deque<DiagnosticRecord>& records() const { return records_; }
    size_t record_count() const { return total_count_; }

    void set_console_output(bool enabled) { console_output_ = enabled; }

    void flush();

    static std::string compact_line(const DiagnosticRecord& rec);

private:
    void write_json_line(const DiagnosticRecord& rec);
    void write_console_line(const DiagnosticRecord& rec);

    std::string output_path_;
    std::ofstream file_;
    size_t max_memory_;
    size_t total_count_ = 0;
    bool console_output_ = false;
    std::deque<DiagnosticRecord> records_;
};

} // namespace orderflow::ripple
