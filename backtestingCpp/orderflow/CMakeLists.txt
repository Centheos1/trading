cmake_minimum_required(VERSION 3.14...3.27)
project(orderflow VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

cmake_policy(SET CMP0167 NEW)

set(HOMEBREW_PREFIX "/opt/homebrew")
list(PREPEND CMAKE_PREFIX_PATH "${HOMEBREW_PREFIX}")

find_package(HDF5 REQUIRED COMPONENTS C)

set(BOOST_ROOT "${HOMEBREW_PREFIX}")
set(Boost_NO_SYSTEM_PATHS ON)
find_package(Boost 1.70 REQUIRED)

find_package(OpenSSL REQUIRED)
find_package(pybind11 REQUIRED)
find_package(nlohmann_json REQUIRED)

set(ORDERFLOW_SOURCES
    OrderBook.cpp
    TradeFlow.cpp
    VolumeProfile.cpp
    CumulativeVolumeDelta.cpp
    FootprintChart.cpp
    SignalEngine.cpp
    OrderFlowEngine.cpp
    BinanceWsFeed.cpp
    TickStore.cpp
    ReplayFeed.cpp
    ripple/RippleContext.cpp
    ripple/WallDetector.cpp
    ripple/RippleFeatureEngine.cpp
    ripple/RippleEvidenceEngine.cpp
    ripple/ScoreBasedInference.cpp
    ripple/RippleStateTracker.cpp
    ripple/TriggerDecisionEngine.cpp
    ripple/RippleEngine.cpp
    ripple/RippleDiagnostics.cpp
    ripple/HMMBasedInference.cpp
    ripple/TradeLifecycleEngine.cpp
    ripple/LiquidityMapEngine.cpp
    ripple/RiskEngine.cpp
)

set(ORDERFLOW_HEADERS
    Types.h
    Schemas.h
    IDataFeed.h
    OrderBook.h
    TradeFlow.h
    VolumeProfile.h
    CumulativeVolumeDelta.h
    FootprintChart.h
    SignalEngine.h
    OrderFlowEngine.h
    BinanceWsFeed.h
    TickStore.h
    ReplayFeed.h
    ripple/RippleTypes.h
    ripple/RippleConfig.h
    ripple/RippleContext.h
    ripple/WallDetector.h
    ripple/RippleFeatureEngine.h
    ripple/RippleEvidenceEngine.h
    ripple/IRippleStateInference.h
    ripple/ScoreBasedInference.h
    ripple/RippleStateTracker.h
    ripple/TriggerDecisionEngine.h
    ripple/RippleDiagnostics.h
    ripple/RippleEngine.h
    ripple/HMMBasedInference.h
    ripple/TradeLifecycleEngine.h
    ripple/LiquidityMapEngine.h
    ripple/RiskEngine.h
    ripple/IWallDetector.h
    ripple/IRippleFeatureEngine.h
    ripple/IRippleEvidenceEngine.h
    ripple/ITriggerDecisionEngine.h
)

add_library(orderflow_core STATIC ${ORDERFLOW_SOURCES} ${ORDERFLOW_HEADERS})
target_include_directories(orderflow_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${HDF5_INCLUDE_DIRS})
target_link_libraries(orderflow_core PUBLIC
    ${HDF5_LIBRARIES}
    ${HDF5_C_LIBRARIES}
    Boost::boost
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
)

pybind11_add_module(orderflow_engine bindings.cpp)
target_link_libraries(orderflow_engine PRIVATE orderflow_core)

# Ripple test suite
add_executable(test_ripple ripple/tests/test_ripple.cpp)
target_link_libraries(test_ripple PRIVATE orderflow_core)

# Schema / data-contract test suite (Phase 1)
add_executable(test_schemas ripple/tests/test_schemas.cpp)
target_link_libraries(test_schemas PRIVATE orderflow_core)

# Trade lifecycle FSM test suite (Phase 2)
add_executable(test_trade_lifecycle ripple/tests/test_trade_lifecycle.cpp)
target_link_libraries(test_trade_lifecycle PRIVATE orderflow_core)

# Liquidity map test suite (Phase 3)
add_executable(test_liquidity_map ripple/tests/test_liquidity_map.cpp)
target_link_libraries(test_liquidity_map PRIVATE orderflow_core)

# Risk engine test suite (Phase 4)
add_executable(test_risk_engine ripple/tests/test_risk_engine.cpp)
target_link_libraries(test_risk_engine PRIVATE orderflow_core)

# Wave integration test suite (Phase 5)
add_executable(test_wave_integration ripple/tests/test_wave_integration.cpp)
target_link_libraries(test_wave_integration PRIVATE orderflow_core)

# HMM inference test suite (Phase 7)
add_executable(test_hmm_inference ripple/tests/test_hmm_inference.cpp)
target_link_libraries(test_hmm_inference PRIVATE orderflow_core)

# Performance benchmark (Phase 6)
add_executable(benchmark_pipeline ripple/tests/benchmark_pipeline.cpp)
target_link_libraries(benchmark_pipeline PRIVATE orderflow_core)

# Ripple stage-1 demo
add_executable(ripple_demo ripple/demo/ripple_demo.cpp)
target_link_libraries(ripple_demo PRIVATE orderflow_core)
