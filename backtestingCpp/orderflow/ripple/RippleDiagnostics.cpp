#include "RippleDiagnostics.h"
#include <iostream>
#include <sstream>
#include <iomanip>

namespace orderflow::ripple {

RippleDiagnostics::RippleDiagnostics(const std::string& output_path,
                                       size_t max_memory_records,
                                       bool console_output)
    : output_path_(output_path),
      max_memory_(max_memory_records),
      console_output_(console_output) {
    if (!output_path_.empty()) {
        file_.open(output_path_, std::ios::out | std::ios::trunc);
    }
}

RippleDiagnostics::~RippleDiagnostics() {
    flush();
}

void RippleDiagnostics::log(Timestamp ts,
                             const RippleFeatures& features,
                             const RippleEvidence& evidence,
                             const RippleInferenceResult& inference,
                             const RippleDecision& decision) {
    DiagnosticRecord rec{ts, features, evidence, inference, decision};
    records_.push_back(rec);
    total_count_++;
    while (records_.size() > max_memory_)
        records_.pop_front();

    if (file_.is_open())
        write_json_line(rec);

    if (console_output_)
        write_console_line(rec);
}

void RippleDiagnostics::flush() {
    if (file_.is_open())
        file_.flush();
}

// -----------------------------------------------------------------------
//  Compact single-line console output
// -----------------------------------------------------------------------
std::string RippleDiagnostics::compact_line(const DiagnosticRecord& r) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(2);
    os << "t=" << r.ts
       << " st=" << to_string(r.inference.state);

    if (r.inference.is_transition)
        os << "*";

    os << " abs=" << r.evidence.absorption
       << " exh=" << r.evidence.exhaustion
       << " wdr=" << r.evidence.withdrawal
       << " brk=" << r.evidence.breakout
       << " ref=" << r.evidence.refill
       << " stb=" << r.evidence.stabilization;

    if (r.decision.intent != RippleIntent::NO_ACTION) {
        os << " >> " << to_string(r.decision.intent)
           << " @ " << std::setprecision(4) << r.decision.reference_price;
        if (r.decision.invalidation_price != 0.0)
            os << " inv=" << r.decision.invalidation_price;
    }

    if (!r.decision.reason.empty())
        os << " [" << r.decision.reason << "]";

    return os.str();
}

void RippleDiagnostics::write_console_line(const DiagnosticRecord& rec) {
    std::cout << compact_line(rec) << "\n";
}

// -----------------------------------------------------------------------
//  JSONL file output
// -----------------------------------------------------------------------
void RippleDiagnostics::write_json_line(const DiagnosticRecord& r) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(6);
    os << "{\"ts\":" << r.ts;

    os << ",\"f\":{";
    os << "\"rws\":" << r.features.relative_wall_size
       << ",\"wp\":" << r.features.wall_persistence_sec
       << ",\"wdr\":" << r.features.wall_depletion_rate
       << ",\"wrr\":" << r.features.wall_refill_rate
       << ",\"wcr\":" << r.features.wall_cancel_rate
       << ",\"dtw\":" << r.features.distance_to_wall_ticks
       << ",\"qs\":" << r.features.queue_stability
       << ",\"afi\":" << r.features.aggressive_flow_imbalance
       << ",\"tci\":" << r.features.trade_count_imbalance
       << ",\"mpd\":" << r.features.microprice_drift
       << ",\"t1i\":" << r.features.top1_imbalance
       << ",\"t5i\":" << r.features.top5_imbalance
       << ",\"ipv\":" << r.features.impact_per_unit_volume
       << ",\"ss\":" << r.features.spread_shock
       << ",\"shv\":" << r.features.short_horizon_volatility
       << ",\"tswf\":" << r.features.time_since_wall_formed_sec;
    os << "}";

    os << ",\"e\":{";
    os << "\"abs\":" << r.evidence.absorption
       << ",\"exh\":" << r.evidence.exhaustion
       << ",\"wth\":" << r.evidence.withdrawal
       << ",\"brk\":" << r.evidence.breakout
       << ",\"ref\":" << r.evidence.refill
       << ",\"stb\":" << r.evidence.stabilization;
    os << "}";

    os << ",\"i\":{";
    os << "\"st\":\"" << to_string(r.inference.state) << "\""
       << ",\"prev\":\"" << to_string(r.inference.prev_state) << "\""
       << ",\"tr\":" << (r.inference.is_transition ? "true" : "false")
       << ",\"conf\":" << r.inference.confidence
       << ",\"age\":" << r.inference.state_age_ms;
    os << "}";

    os << ",\"d\":{";
    os << "\"intent\":\"" << to_string(r.decision.intent) << "\""
       << ",\"side\":\"" << to_string(r.decision.reference_side) << "\""
       << ",\"price\":" << r.decision.reference_price
       << ",\"inv_price\":" << r.decision.invalidation_price
       << ",\"wid\":" << r.decision.wall_id
       << ",\"conf\":" << r.decision.confidence;
    if (!r.decision.reason.empty()) {
        os << ",\"reason\":\"";
        for (char c : r.decision.reason) {
            if (c == '"') os << "\\\"";
            else if (c == '\\') os << "\\\\";
            else os << c;
        }
        os << "\"";
    }
    os << "}";

    os << "}\n";
    file_ << os.str();
}

} // namespace orderflow::ripple
