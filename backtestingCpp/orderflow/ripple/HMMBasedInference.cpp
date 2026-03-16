#include "HMMBasedInference.h"
#include <cmath>
#include <cstring>
#include <fstream>
#include <sstream>
#include <algorithm>

// Minimal JSON parsing — avoids external dependency.
// Handles the simple nested-array format produced by the Python trainer.
namespace {

double safe_log(double x) {
    constexpr double FLOOR = 1e-300;
    return std::log(std::max(x, FLOOR));
}

double log_sum_exp(const double* v, int n) {
    double mx = v[0];
    for (int i = 1; i < n; ++i)
        mx = std::max(mx, v[i]);
    if (mx <= orderflow::ripple::HMMBasedInference::LOG_ZERO + 1.0)
        return orderflow::ripple::HMMBasedInference::LOG_ZERO;
    double s = 0.0;
    for (int i = 0; i < n; ++i)
        s += std::exp(v[i] - mx);
    return mx + std::log(s);
}

// Tiny JSON helpers — parse arrays of doubles/ints from a flat string.
// We skip whitespace and structural chars to locate numeric tokens.
struct JsonCursor {
    const char* p;
    const char* end;

    void skip_ws() {
        while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r'))
            ++p;
    }
    bool expect(char c) {
        skip_ws();
        if (p < end && *p == c) { ++p; return true; }
        return false;
    }
    double read_double() {
        skip_ws();
        char* e = nullptr;
        double v = std::strtod(p, &e);
        if (e == p) return 0.0;
        p = e;
        return v;
    }
    int read_int() {
        skip_ws();
        char* e = nullptr;
        long v = std::strtol(p, &e, 10);
        if (e == p) return 0;
        p = e;
        return static_cast<int>(v);
    }
    bool find_key(const char* key) {
        const char* k = std::strstr(p, key);
        if (!k || k >= end) return false;
        p = k + std::strlen(key);
        skip_ws();
        expect(':');
        return true;
    }
};

} // anon

namespace orderflow::ripple {

static constexpr double LOG_2PI = 1.8378770664093453;  // log(2π)

HMMBasedInference::HMMBasedInference(const RippleConfig& cfg) : cfg_(cfg) {
    reset_forward();
}

void HMMBasedInference::reset_forward() {
    for (int k = 0; k < MAX_K; ++k)
        log_alpha_[k] = LOG_ZERO;
    if (K_ > 0) {
        for (int k = 0; k < K_; ++k)
            log_alpha_[k] = log_prior_[k];
    }
}

void HMMBasedInference::extract_obs(const RippleEvidence& ev, double out[OBS_DIM]) const {
    out[0] = ev.absorption;
    out[1] = ev.exhaustion;
    out[2] = ev.withdrawal;
    out[3] = ev.breakout;
    out[4] = ev.refill;
    out[5] = ev.stabilization;
}

double HMMBasedInference::log_emission(int k, const double obs[OBS_DIM]) const {
    double ll = log_norm_[k];
    for (int d = 0; d < OBS_DIM; ++d) {
        double diff = obs[d] - means_[k][d];
        ll -= 0.5 * diff * diff / variances_[k][d];
    }
    return ll;
}

void HMMBasedInference::forward_step(const double obs[OBS_DIM]) {
    double new_log_alpha[MAX_K];
    for (int j = 0; j < K_; ++j) {
        double terms[MAX_K];
        for (int i = 0; i < K_; ++i)
            terms[i] = log_alpha_[i] + log_A_[i][j];
        new_log_alpha[j] = log_sum_exp(terms, K_) + log_emission(j, obs);
    }
    double norm = log_sum_exp(new_log_alpha, K_);
    for (int k = 0; k < K_; ++k)
        log_alpha_[k] = new_log_alpha[k] - norm;
}

RippleState HMMBasedInference::map_state(int k) const {
    int idx = state_map_[k];
    if (idx >= 0 && idx < RippleInferenceResult::NUM_STATES)
        return static_cast<RippleState>(idx);
    return RippleState::IDLE;
}

RippleInferenceResult HMMBasedInference::infer(
    const RippleEvidence& evidence,
    RippleState prev_state,
    const WallCandidate* /*wall*/,
    Timestamp /*now*/,
    Duration state_age_ms) {

    RippleInferenceResult result;
    result.prev_state   = prev_state;
    result.state_age_ms = state_age_ms;

    if (!model_loaded_ || K_ <= 0) {
        result.state         = prev_state;
        result.confidence    = 0.0;
        result.is_transition = false;
        result.reason        = "HMM: model not loaded";
        return result;
    }

    double obs[OBS_DIM];
    extract_obs(evidence, obs);
    forward_step(obs);

    // Convert log-alpha to posterior probabilities and fill scores[]
    std::memset(result.scores, 0, sizeof(result.scores));
    double posteriors[MAX_K];
    for (int k = 0; k < K_; ++k)
        posteriors[k] = std::exp(log_alpha_[k]);

    int best_k = 0;
    double best_p = posteriors[0];
    for (int k = 0; k < K_; ++k) {
        int rs = state_map_[k];
        if (rs >= 0 && rs < RippleInferenceResult::NUM_STATES)
            result.scores[rs] += posteriors[k];
        if (posteriors[k] > best_p) {
            best_p = posteriors[k];
            best_k = k;
        }
    }

    // MAP state
    result.state = map_state(best_k);

    // Runner-up
    double second_p = 0.0;
    int second_k = 0;
    for (int k = 0; k < K_; ++k) {
        if (k != best_k && posteriors[k] > second_p) {
            second_p = posteriors[k];
            second_k = k;
        }
    }
    result.runner_up_state = map_state(second_k);
    result.winning_score   = best_p;
    result.runner_up_score = second_p;
    result.confidence      = std::min(best_p - second_p, 1.0);

    result.is_transition = (result.state != prev_state);
    result.reason = "HMM: posterior MAP";

    return result;
}

bool HMMBasedInference::load_model(const std::string& path) {
    std::ifstream f(path);
    if (!f.is_open()) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    return load_model_from_string(ss.str());
}

bool HMMBasedInference::load_model_from_string(const std::string& json) {
    JsonCursor c{json.c_str(), json.c_str() + json.size()};

    if (!c.find_key("\"K\"")) return false;
    int K = c.read_int();
    if (K < 1 || K > MAX_K) return false;

    // transition matrix [K][K]
    if (!c.find_key("\"transition\"")) return false;
    c.expect('[');
    for (int i = 0; i < K; ++i) {
        c.expect('[');
        for (int j = 0; j < K; ++j) {
            double v = c.read_double();
            log_A_[i][j] = safe_log(v);
            c.expect(',');
        }
        c.expect(']');
        c.expect(',');
    }
    c.expect(']');

    // means [K][OBS_DIM]
    if (!c.find_key("\"means\"")) return false;
    c.expect('[');
    for (int k = 0; k < K; ++k) {
        c.expect('[');
        for (int d = 0; d < OBS_DIM; ++d) {
            means_[k][d] = c.read_double();
            c.expect(',');
        }
        c.expect(']');
        c.expect(',');
    }
    c.expect(']');

    // variances [K][OBS_DIM]
    if (!c.find_key("\"variances\"")) return false;
    c.expect('[');
    for (int k = 0; k < K; ++k) {
        c.expect('[');
        for (int d = 0; d < OBS_DIM; ++d) {
            variances_[k][d] = std::max(c.read_double(), 1e-10);
            c.expect(',');
        }
        c.expect(']');
        c.expect(',');
    }
    c.expect(']');

    // state_map [K]
    if (!c.find_key("\"state_map\"")) return false;
    c.expect('[');
    for (int k = 0; k < K; ++k) {
        state_map_[k] = c.read_int();
        c.expect(',');
    }
    c.expect(']');

    // log_prior [K] (optional — defaults to uniform)
    for (int k = 0; k < K; ++k)
        log_prior_[k] = safe_log(1.0 / K);

    if (c.find_key("\"log_prior\"")) {
        c.expect('[');
        for (int k = 0; k < K; ++k) {
            log_prior_[k] = c.read_double();
            c.expect(',');
        }
        c.expect(']');
    }

    // Precompute log normalization constants for emission
    for (int k = 0; k < K; ++k) {
        double lnorm = 0.0;
        for (int d = 0; d < OBS_DIM; ++d)
            lnorm += LOG_2PI + std::log(variances_[k][d]);
        log_norm_[k] = -0.5 * lnorm;
    }

    K_ = K;
    model_loaded_ = true;
    reset_forward();
    return true;
}

void HMMBasedInference::record_observation(const Observation& obs) {
    training_buffer_.push_back(obs);
}

} // namespace orderflow::ripple
