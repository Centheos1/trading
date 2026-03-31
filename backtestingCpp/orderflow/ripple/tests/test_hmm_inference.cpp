// Phase 7: HMM-based inference tests.
// Validates forward algorithm, probability distributions, determinism,
// model load/save, and fallback behavior.

#include <cstdio>
#include <cmath>
#include <cstring>
#include <string>
#include <sstream>
#include "../HMMBasedInference.h"

using namespace orderflow::ripple;

static int g_pass = 0, g_fail = 0;
#define CHECK(cond, msg) do { \
    if (cond) { ++g_pass; } \
    else { ++g_fail; std::printf("FAIL [%d]: %s\n", __LINE__, msg); } \
} while(0)

#define CLOSE(a, b, tol) (std::fabs((a) - (b)) < (tol))

// A minimal 3-state model JSON: states map to STABLE(1), ABSORBING(2), EXHAUSTING(3)
static const char* MODEL_3 = R"({
    "K": 3,
    "transition": [
        [0.8, 0.1, 0.1],
        [0.1, 0.8, 0.1],
        [0.1, 0.1, 0.8]
    ],
    "means": [
        [0.1, 0.1, 0.1, 0.1, 0.5, 0.5],
        [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
        [0.1, 0.8, 0.1, 0.1, 0.1, 0.1]
    ],
    "variances": [
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
    ],
    "state_map": [1, 2, 3]
})";

static RippleEvidence make_ev(double abs, double exh, double wth,
                               double brk, double ref, double stab) {
    RippleEvidence ev;
    ev.absorption    = abs;
    ev.exhaustion    = exh;
    ev.withdrawal    = wth;
    ev.breakout      = brk;
    ev.refill        = ref;
    ev.stabilization = stab;
    return ev;
}

void test_load_model() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    CHECK(!hmm.model_loaded(), "not loaded initially");
    CHECK(hmm.load_model_from_string(MODEL_3), "load 3-state model");
    CHECK(hmm.model_loaded(), "loaded after load");
    CHECK(hmm.num_states() == 3, "K=3");
}

void test_load_invalid() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    CHECK(!hmm.load_model_from_string("{}"), "reject empty");
    CHECK(!hmm.load_model_from_string("{\"K\": 0}"), "reject K=0");
    CHECK(!hmm.load_model_from_string("{\"K\": 99}"), "reject K > MAX_K");
}

void test_fallback_when_no_model() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    auto ev = make_ev(0.5, 0.3, 0.1, 0.1, 0.1, 0.1);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 1000, 0);
    CHECK(r.state == RippleState::IDLE, "fallback to prev_state");
    CHECK(r.confidence == 0.0, "zero confidence");
    CHECK(!r.is_transition, "no transition");
}

void test_posterior_sums_to_one() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    auto ev = make_ev(0.5, 0.2, 0.1, 0.1, 0.1, 0.1);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 1000, 0);

    double sum = 0.0;
    for (int i = 0; i < RippleInferenceResult::NUM_STATES; ++i)
        sum += r.scores[i];
    CHECK(CLOSE(sum, 1.0, 1e-6), "posterior sums to 1");
}

void test_all_posteriors_non_negative() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    auto ev = make_ev(0.8, 0.1, 0.1, 0.1, 0.1, 0.1);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 1000, 0);
    for (int i = 0; i < RippleInferenceResult::NUM_STATES; ++i)
        CHECK(r.scores[i] >= 0.0, "non-negative posterior");
}

void test_absorption_evidence_favors_absorbing() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    // Feed strong absorption evidence multiple times
    for (int i = 0; i < 5; ++i) {
        auto ev = make_ev(0.9, 0.05, 0.05, 0.05, 0.05, 0.05);
        hmm.infer(ev, RippleState::IDLE, nullptr, 1000 + i * 100, 0);
    }
    auto ev = make_ev(0.9, 0.05, 0.05, 0.05, 0.05, 0.05);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 2000, 0);

    // state_map[1] = ABSORBING (2)
    CHECK(r.state == RippleState::ABSORBING, "strong absorption → ABSORBING");
    CHECK(r.scores[static_cast<int>(RippleState::ABSORBING)] > 0.5,
          "ABSORBING posterior > 0.5");
}

void test_exhaustion_evidence_favors_exhausting() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    for (int i = 0; i < 5; ++i) {
        auto ev = make_ev(0.05, 0.9, 0.05, 0.05, 0.05, 0.05);
        hmm.infer(ev, RippleState::IDLE, nullptr, 1000 + i * 100, 0);
    }
    auto ev = make_ev(0.05, 0.9, 0.05, 0.05, 0.05, 0.05);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 2000, 0);
    CHECK(r.state == RippleState::EXHAUSTING, "strong exhaustion → EXHAUSTING");
}

void test_determinism() {
    RippleConfig cfg;
    std::string model(MODEL_3);

    auto run = [&]() {
        HMMBasedInference hmm(cfg);
        hmm.load_model_from_string(model);

        RippleInferenceResult last;
        for (int i = 0; i < 20; ++i) {
            double t = i * 0.05;
            auto ev = make_ev(0.5 + 0.3 * std::sin(t), 0.2, 0.1, 0.15, 0.1, 0.1);
            last = hmm.infer(ev, last.state, nullptr, 1000 + i * 100, 0);
        }
        return last;
    };

    auto r1 = run();
    auto r2 = run();
    CHECK(r1.state == r2.state, "deterministic state");
    CHECK(r1.confidence == r2.confidence, "deterministic confidence");
    for (int i = 0; i < RippleInferenceResult::NUM_STATES; ++i)
        CHECK(r1.scores[i] == r2.scores[i], "deterministic scores");
}

void test_reset_forward() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    auto ev1 = make_ev(0.9, 0.05, 0.05, 0.05, 0.05, 0.05);
    hmm.infer(ev1, RippleState::IDLE, nullptr, 1000, 0);
    hmm.infer(ev1, RippleState::IDLE, nullptr, 1100, 0);

    hmm.reset_forward();
    auto ev2 = make_ev(0.1, 0.1, 0.1, 0.1, 0.5, 0.5);
    auto r = hmm.infer(ev2, RippleState::IDLE, nullptr, 2000, 0);

    // After reset, the prior history should be cleared
    CHECK(r.scores[static_cast<int>(RippleState::ABSORBING)] < 0.5,
          "reset clears absorption bias");
}

void test_transition_is_detected() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    // Start in stabilization zone
    for (int i = 0; i < 5; ++i) {
        auto ev = make_ev(0.1, 0.1, 0.1, 0.1, 0.5, 0.5);
        hmm.infer(ev, RippleState::IDLE, nullptr, 1000 + i * 100, 0);
    }
    // Switch to absorption
    auto ev = make_ev(0.9, 0.05, 0.05, 0.05, 0.05, 0.05);
    auto r = hmm.infer(ev, RippleState::WALL_FORMING, nullptr, 2000, 0);
    // After several absorption ticks the state should change
    for (int i = 0; i < 5; ++i) {
        r = hmm.infer(ev, r.state, nullptr, 2100 + i * 100, 0);
    }
    CHECK(r.state == RippleState::ABSORBING, "transition detected");
}

void test_confidence_bounded() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    hmm.load_model_from_string(MODEL_3);

    for (int i = 0; i < 10; ++i) {
        auto ev = make_ev(0.3 + 0.4 * (i % 2), 0.2, 0.1, 0.1, 0.1, 0.1);
        auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 1000 + i * 100, 0);
        CHECK(r.confidence >= 0.0, "confidence >= 0");
        CHECK(r.confidence <= 1.0, "confidence <= 1");
    }
}

void test_record_observation() {
    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    auto ev = make_ev(0.5, 0.3, 0.1, 0.1, 0.1, 0.1);
    hmm.record_observation({1000, ev, RippleState::ABSORBING});
    hmm.record_observation({1100, ev, RippleState::EXHAUSTING});
    CHECK(hmm.training_buffer().size() == 2, "2 observations recorded");
    hmm.clear_training_buffer();
    CHECK(hmm.training_buffer().empty(), "buffer cleared");
}

void test_5state_model() {
    const char* model5 = R"({
        "K": 5,
        "transition": [
            [0.7, 0.1, 0.1, 0.05, 0.05],
            [0.1, 0.7, 0.1, 0.05, 0.05],
            [0.1, 0.1, 0.7, 0.05, 0.05],
            [0.1, 0.1, 0.1, 0.60, 0.10],
            [0.1, 0.1, 0.1, 0.10, 0.60]
        ],
        "means": [
            [0.1, 0.1, 0.1, 0.1, 0.5, 0.5],
            [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.1, 0.8, 0.1, 0.1, 0.1],
            [0.1, 0.1, 0.1, 0.8, 0.1, 0.1]
        ],
        "variances": [
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
        ],
        "state_map": [1, 2, 3, 4, 5]
    })";

    RippleConfig cfg;
    HMMBasedInference hmm(cfg);
    CHECK(hmm.load_model_from_string(model5), "load 5-state model");
    CHECK(hmm.num_states() == 5, "K=5");

    auto ev = make_ev(0.1, 0.1, 0.9, 0.1, 0.1, 0.1);
    for (int i = 0; i < 5; ++i)
        hmm.infer(ev, RippleState::IDLE, nullptr, 1000 + i * 100, 0);
    auto r = hmm.infer(ev, RippleState::IDLE, nullptr, 2000, 0);
    CHECK(r.state == RippleState::WITHDRAWING, "withdrawal evidence → WITHDRAWING");
}

int main() {
    test_load_model();
    test_load_invalid();
    test_fallback_when_no_model();
    test_posterior_sums_to_one();
    test_all_posteriors_non_negative();
    test_absorption_evidence_favors_absorbing();
    test_exhaustion_evidence_favors_exhausting();
    test_determinism();
    test_reset_forward();
    test_transition_is_detected();
    test_confidence_bounded();
    test_record_observation();
    test_5state_model();

    std::printf("\n=== HMM Inference: %d / %d tests passed ===\n",
                g_pass, g_pass + g_fail);
    return g_fail > 0 ? 1 : 0;
}
