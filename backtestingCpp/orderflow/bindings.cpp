#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>
#include <pybind11/numpy.h>

#include "Types.h"
#include "OrderBook.h"
#include "TradeFlow.h"
#include "VolumeProfile.h"
#include "CumulativeVolumeDelta.h"
#include "FootprintChart.h"
#include "SignalEngine.h"
#include "OrderFlowEngine.h"
#include "TickStore.h"
#include "ReplayFeed.h"
#include "ripple/RippleTypes.h"
#include "ripple/RippleConfig.h"
#include "ripple/RippleEngine.h"
#include "ripple/RippleDiagnostics.h"
#include "ripple/TradeLifecycleEngine.h"
#include "ripple/LiquidityMapEngine.h"
#include "ripple/RiskEngine.h"
#include "ripple/HMMBasedInference.h"
#include "ripple/ScoreBasedInference.h"
#include "Schemas.h"

namespace py = pybind11;
using namespace orderflow;

PYBIND11_MODULE(orderflow_engine, m) {
    m.doc() = "Order Flow Trading Engine - Auction Market Theory";

    // --- Trade ---
    py::class_<Trade>(m, "Trade")
        .def(py::init<>())
        .def_readwrite("timestamp", &Trade::timestamp)
        .def_readwrite("price", &Trade::price)
        .def_readwrite("quantity", &Trade::quantity)
        .def_readwrite("is_buyer_maker", &Trade::is_buyer_maker)
        .def("is_buy_aggressor", &Trade::is_buy_aggressor)
        .def("is_sell_aggressor", &Trade::is_sell_aggressor);

    // --- DepthLevel ---
    py::class_<DepthLevel>(m, "DepthLevel")
        .def(py::init<>())
        .def_readwrite("price", &DepthLevel::price)
        .def_readwrite("quantity", &DepthLevel::quantity);

    // --- DepthUpdate ---
    //
    // KNOWN LIMITATION (Phase 13X / 13Z documented design choice):
    // ``bids`` and ``asks`` are exposed via ``def_readwrite`` over a
    // ``std::vector<DepthLevel>``. Pybind11's default vector binding
    // copies the vector in both directions, so per-element mutation
    // from Python (``update.bids.append(level)``) DOES NOT mutate the
    // C++ object — it modifies a temporary list that is immediately
    // discarded. ALWAYS assign a complete list:
    //     bids = [DepthLevel(...) for ...]
    //     update.bids = bids   # whole-list assignment is safe
    // All production callers (data_feed/binance_futures_ws.py,
    // data_feed/binance_depth_rest.py, data_service.py) and all tests
    // use the safe whole-list-assignment pattern.
    //
    // The "proper" fix is ``PYBIND11_MAKE_OPAQUE(std::vector<DepthLevel>)``
    // + ``py::bind_vector``. Deliberately deferred because:
    //   (a) it removes the implicit Python-list-to-vector conversion,
    //       breaking ``update.bids = python_list`` everywhere unless
    //       paired with a custom ``py::implicitly_convertible``;
    //   (b) the wrapped vector type leaks into Python error messages /
    //       repr (cosmetic but visible);
    //   (c) every caller and every test already uses the safe pattern,
    //       and ``tests/test_orderflow_backtest.py::TestBuildConfigBindingDrift``
    //       (Phase 13Y) plus ``tests/test_replay_determinism.py::test_*_actually_*``
    //       (Phase 13X) prevent silent regressions.
    py::class_<DepthUpdate>(m, "DepthUpdate")
        .def(py::init<>())
        .def_readwrite("timestamp", &DepthUpdate::timestamp)
        .def_readwrite("bids", &DepthUpdate::bids)
        .def_readwrite("asks", &DepthUpdate::asks)
        .def_readwrite("first_update_id", &DepthUpdate::first_update_id)
        .def_readwrite("final_update_id", &DepthUpdate::final_update_id)
        .def_readwrite("is_snapshot", &DepthUpdate::is_snapshot);

    // --- SignalType ---
    py::enum_<SignalType>(m, "SignalType")
        .value("ABSORPTION_BUY", SignalType::ABSORPTION_BUY)
        .value("ABSORPTION_SELL", SignalType::ABSORPTION_SELL)
        .value("STACKED_IMBALANCE_BUY", SignalType::STACKED_IMBALANCE_BUY)
        .value("STACKED_IMBALANCE_SELL", SignalType::STACKED_IMBALANCE_SELL)
        .value("DELTA_DIVERGENCE_BULL", SignalType::DELTA_DIVERGENCE_BULL)
        .value("DELTA_DIVERGENCE_BEAR", SignalType::DELTA_DIVERGENCE_BEAR)
        .value("EXHAUSTION_BUY", SignalType::EXHAUSTION_BUY)
        .value("EXHAUSTION_SELL", SignalType::EXHAUSTION_SELL)
        .value("POC_REJECTION_BUY", SignalType::POC_REJECTION_BUY)
        .value("POC_REJECTION_SELL", SignalType::POC_REJECTION_SELL);

    // --- Signal ---
    py::class_<Signal>(m, "Signal")
        .def(py::init<>())
        .def_readwrite("timestamp", &Signal::timestamp)
        .def_readwrite("type", &Signal::type)
        .def_readwrite("price", &Signal::price)
        .def_readwrite("strength", &Signal::strength)
        .def_readwrite("description", &Signal::description)
        .def("direction", &Signal::direction)
        .def("type_name", &Signal::type_name);

    // --- VolumeNode ---
    py::class_<VolumeNode>(m, "VolumeNode")
        .def_readwrite("price", &VolumeNode::price)
        .def_readwrite("volume", &VolumeNode::volume)
        .def_readwrite("buy_volume", &VolumeNode::buy_volume)
        .def_readwrite("sell_volume", &VolumeNode::sell_volume);

    // --- FootprintCell ---
    py::class_<FootprintCell>(m, "FootprintCell")
        .def_readwrite("bid_volume", &FootprintCell::bid_volume)
        .def_readwrite("ask_volume", &FootprintCell::ask_volume)
        .def("imbalance", &FootprintCell::imbalance);

    // --- ValueArea ---
    py::class_<ValueArea>(m, "ValueArea")
        .def_readwrite("poc_price", &ValueArea::poc_price)
        .def_readwrite("poc_volume", &ValueArea::poc_volume)
        .def_readwrite("vah", &ValueArea::vah)
        .def_readwrite("val", &ValueArea::val)
        .def_readwrite("total_volume", &ValueArea::total_volume);

    // --- CVDPoint ---
    py::class_<CVDPoint>(m, "CVDPoint")
        .def_readwrite("timestamp", &CVDPoint::timestamp)
        .def_readwrite("delta", &CVDPoint::delta)
        .def_readwrite("price", &CVDPoint::price);

    // --- OrderBookSnapshot ---
    py::class_<OrderBookSnapshot>(m, "OrderBookSnapshot")
        .def_readwrite("timestamp", &OrderBookSnapshot::timestamp)
        .def_readwrite("best_bid", &OrderBookSnapshot::best_bid)
        .def_readwrite("best_ask", &OrderBookSnapshot::best_ask)
        .def_readwrite("mid_price", &OrderBookSnapshot::mid_price)
        .def_readwrite("spread", &OrderBookSnapshot::spread)
        .def("get_bids", [](const OrderBookSnapshot& snap) {
            std::vector<std::pair<double, double>> result;
            for (const auto& [p, q] : snap.bids) result.emplace_back(p, q);
            return result;
        })
        .def("get_asks", [](const OrderBookSnapshot& snap) {
            std::vector<std::pair<double, double>> result;
            for (const auto& [p, q] : snap.asks) result.emplace_back(p, q);
            return result;
        });

    // --- FootprintBar ---
    py::class_<FootprintBar>(m, "FootprintBar")
        .def_readwrite("open_time", &FootprintBar::open_time)
        .def_readwrite("close_time", &FootprintBar::close_time)
        .def_readwrite("open", &FootprintBar::open)
        .def_readwrite("high", &FootprintBar::high)
        .def_readwrite("low", &FootprintBar::low)
        .def_readwrite("close", &FootprintBar::close)
        .def("total_delta", &FootprintBar::total_delta)
        .def("total_volume", &FootprintBar::total_volume)
        .def("get_imbalances", &FootprintBar::get_imbalances)
        .def("get_cells", [](const FootprintBar& bar) {
            std::vector<std::tuple<double, double, double>> result;
            for (const auto& [price, cell] : bar.cells) {
                result.emplace_back(price, cell.bid_volume, cell.ask_volume);
            }
            return result;
        });

    // --- SignalParams ---
    py::class_<SignalParams>(m, "SignalParams")
        .def(py::init<>())
        .def_readwrite("imbalance_threshold", &SignalParams::imbalance_threshold)
        .def_readwrite("stacked_imbalance_levels", &SignalParams::stacked_imbalance_levels)
        .def_readwrite("absorption_volume_ratio", &SignalParams::absorption_volume_ratio)
        .def_readwrite("absorption_window_ms", &SignalParams::absorption_window_ms)
        .def_readwrite("cvd_divergence_lookback", &SignalParams::cvd_divergence_lookback)
        .def_readwrite("exhaustion_lookback_bars", &SignalParams::exhaustion_lookback_bars)
        .def_readwrite("exhaustion_price_threshold", &SignalParams::exhaustion_price_threshold)
        .def_readwrite("poc_rejection_distance", &SignalParams::poc_rejection_distance)
        .def_readwrite("poc_rejection_window_ms", &SignalParams::poc_rejection_window_ms)
        .def_readwrite("signal_strength_min", &SignalParams::signal_strength_min);

    // --- EngineConfig ---
    py::class_<EngineConfig>(m, "EngineConfig")
        .def(py::init<>())
        .def_readwrite("tick_size", &EngineConfig::tick_size)
        .def_readwrite("large_trade_threshold", &EngineConfig::large_trade_threshold)
        .def_readwrite("footprint_bar_ms", &EngineConfig::footprint_bar_ms)
        .def_readwrite("cluster_window_ms", &EngineConfig::cluster_window_ms)
        .def_readwrite("signal_params", &EngineConfig::signal_params)
        .def_readwrite("ripple", &EngineConfig::ripple);

    // --- OrderBook ---
    py::class_<OrderBook>(m, "OrderBook")
        .def("get_snapshot", &OrderBook::get_snapshot)
        .def("get_best_bid", &OrderBook::get_best_bid)
        .def("get_best_ask", &OrderBook::get_best_ask)
        .def("get_mid_price", &OrderBook::get_mid_price)
        .def("get_spread", &OrderBook::get_spread)
        .def("get_bid_depth", &OrderBook::get_bid_depth)
        .def("get_ask_depth", &OrderBook::get_ask_depth)
        .def("get_imbalance", &OrderBook::get_imbalance)
        .def("get_bids", &OrderBook::get_bids)
        .def("get_asks", &OrderBook::get_asks);

    // --- TradeFlow ---
    py::class_<TradeFlow>(m, "TradeFlow")
        .def("get_delta", &TradeFlow::get_delta)
        .def("get_buy_volume", &TradeFlow::get_buy_volume)
        .def("get_sell_volume", &TradeFlow::get_sell_volume)
        .def("get_total_volume", &TradeFlow::get_total_volume)
        .def("get_delta_window", &TradeFlow::get_delta_window);

    // --- VolumeProfile ---
    py::class_<VolumeProfile>(m, "VolumeProfile")
        .def("get_poc_price", &VolumeProfile::get_poc_price)
        .def("get_volume_at_price", &VolumeProfile::get_volume_at_price)
        .def("compute_value_area", &VolumeProfile::compute_value_area,
             py::arg("pct") = 0.70)
        .def("get_profile", &VolumeProfile::get_profile)
        .def("get_total_volume", &VolumeProfile::get_total_volume)
        .def("set_window", &VolumeProfile::set_window, py::arg("window_ms"))
        .def("get_window", &VolumeProfile::get_window)
        .def("get_profile_numpy", [](const VolumeProfile& vp) {
            auto profile = vp.get_profile();
            py::array_t<double> result({(int)profile.size(), 4});
            auto buf = result.mutable_unchecked<2>();
            for (size_t i = 0; i < profile.size(); ++i) {
                buf(i, 0) = profile[i].price;
                buf(i, 1) = profile[i].volume;
                buf(i, 2) = profile[i].buy_volume;
                buf(i, 3) = profile[i].sell_volume;
            }
            return result;
        });

    // --- CumulativeVolumeDelta ---
    py::class_<CumulativeVolumeDelta>(m, "CumulativeVolumeDelta")
        .def("get_cvd", &CumulativeVolumeDelta::get_cvd)
        .def("get_history", &CumulativeVolumeDelta::get_history)
        .def("get_history_window", &CumulativeVolumeDelta::get_history_window)
        .def("get_cvd_numpy", [](const CumulativeVolumeDelta& cvd, int64_t window_ms) {
            auto history = cvd.get_history_window(window_ms);
            py::array_t<double> result({(int)history.size(), 3});
            auto buf = result.mutable_unchecked<2>();
            for (size_t i = 0; i < history.size(); ++i) {
                buf(i, 0) = static_cast<double>(history[i].timestamp);
                buf(i, 1) = history[i].delta;
                buf(i, 2) = history[i].price;
            }
            return result;
        });

    // --- FootprintChart ---
    py::class_<FootprintChart>(m, "FootprintChart")
        .def("get_bars", &FootprintChart::get_bars)
        .def("bar_count", &FootprintChart::bar_count);

    // --- SignalEngine ---
    py::class_<SignalEngine>(m, "SignalEngine")
        .def("get_signals", &SignalEngine::get_signals)
        .def("get_signals_since", &SignalEngine::get_signals_since)
        .def("get_position", &SignalEngine::get_position)
        .def("get_pnl", &SignalEngine::get_pnl)
        .def("get_max_drawdown", &SignalEngine::get_max_drawdown)
        .def("get_num_trades", &SignalEngine::get_num_trades)
        .def("get_returns", &SignalEngine::get_returns)
        .def("set_params", &SignalEngine::set_params)
        .def("get_params", &SignalEngine::get_params);

    // --- IDataFeed (base for ReplayFeed) ---
    py::class_<IDataFeed, std::shared_ptr<IDataFeed>>(m, "IDataFeed");

    // --- OrderFlowEngine ---
    py::class_<OrderFlowEngine>(m, "OrderFlowEngine")
        .def(py::init<const EngineConfig&>(), py::arg("config") = EngineConfig{})
        .def("start", &OrderFlowEngine::start)
        .def("stop", &OrderFlowEngine::stop)
        .def("is_running", &OrderFlowEngine::is_running)
        .def("get_order_book", py::overload_cast<>(&OrderFlowEngine::get_order_book),
             py::return_value_policy::reference_internal)
        .def("get_trade_flow", py::overload_cast<>(&OrderFlowEngine::get_trade_flow),
             py::return_value_policy::reference_internal)
        .def("get_volume_profile", py::overload_cast<>(&OrderFlowEngine::get_volume_profile),
             py::return_value_policy::reference_internal)
        .def("get_cvd", py::overload_cast<>(&OrderFlowEngine::get_cvd),
             py::return_value_policy::reference_internal)
        .def("get_footprint", py::overload_cast<>(&OrderFlowEngine::get_footprint),
             py::return_value_policy::reference_internal)
        .def("get_signal_engine", py::overload_cast<>(&OrderFlowEngine::get_signal_engine),
             py::return_value_policy::reference_internal)
        .def("set_config", &OrderFlowEngine::set_config)
        .def("get_config", &OrderFlowEngine::get_config)
        .def("set_tick_store", &OrderFlowEngine::set_tick_store)
        .def("process_trade", &OrderFlowEngine::process_trade,
             py::call_guard<py::gil_scoped_release>())
        .def("process_depth", &OrderFlowEngine::process_depth,
             py::call_guard<py::gil_scoped_release>())
        .def("set_signal_callback", [](OrderFlowEngine& engine, py::function cb) {
            engine.set_signal_callback([cb](const Signal& sig) {
                py::gil_scoped_acquire acquire;
                cb(sig);
            });
        })
        .def("reset_ripple", &OrderFlowEngine::reset_ripple)
        .def("get_ripple", py::overload_cast<>(&OrderFlowEngine::get_ripple),
             py::return_value_policy::reference_internal)
        .def("get_strategy_snapshot", &OrderFlowEngine::get_strategy_snapshot)
        .def("set_ripple_callback", [](OrderFlowEngine& engine, py::function cb) {
            engine.set_ripple_callback([cb](const ripple::RippleDecision& d) {
                py::gil_scoped_acquire acquire;
                cb(d);
            });
        });

    // --- TickStore ---
    py::class_<TickStore, std::shared_ptr<TickStore>>(m, "TickStore")
        .def(py::init<const std::string&>())
        .def("store_trade", &TickStore::store_trade)
        .def("store_trades", &TickStore::store_trades)
        .def("store_depth_snapshot", &TickStore::store_depth_snapshot)
        .def("store_depth_update", &TickStore::store_depth_update)
        .def("load_trades", &TickStore::load_trades)
        .def("load_depth_snapshots", &TickStore::load_depth_snapshots)
        .def("load_depth_updates", &TickStore::load_depth_updates)
        .def("flush", &TickStore::flush)
        .def("close", &TickStore::close)
        .def("list_symbols", &TickStore::list_symbols);

    // --- ReplayFeed ---
    py::class_<ReplayFeed, IDataFeed, std::shared_ptr<ReplayFeed>>(m, "ReplayFeed")
        .def(py::init<std::shared_ptr<TickStore>, double>(),
             py::arg("store"), py::arg("speed_multiplier") = 0.0)
        .def("set_time_range", &ReplayFeed::set_time_range)
        .def("set_speed", &ReplayFeed::set_speed)
        .def("subscribe_trades", &ReplayFeed::subscribe_trades)
        .def("subscribe_depth", &ReplayFeed::subscribe_depth,
             py::arg("symbol"), py::arg("levels") = 20)
        .def("start", &ReplayFeed::start)
        .def("stop", &ReplayFeed::stop)
        .def("is_running", &ReplayFeed::is_running)
        .def("is_complete", &ReplayFeed::is_complete)
        .def("run_sync", &ReplayFeed::run_sync);

    // Bind set_data_feed on OrderFlowEngine
    m.def("connect_feed", [](OrderFlowEngine& engine, std::shared_ptr<IDataFeed> feed) {
        engine.set_data_feed(std::move(feed));
    });

    // ========== Ripple Layer ==========
    using namespace orderflow::ripple;

    py::enum_<WallSide>(m, "WallSide")
        .value("BID", WallSide::BID)
        .value("ASK", WallSide::ASK);

    py::enum_<WallLifecycle>(m, "WallLifecycle")
        .value("FORMING", WallLifecycle::FORMING)
        .value("ACTIVE", WallLifecycle::ACTIVE)
        .value("DEPLETING", WallLifecycle::DEPLETING)
        .value("REFILLING", WallLifecycle::REFILLING)
        .value("WITHDRAWN", WallLifecycle::WITHDRAWN)
        .value("FAILED", WallLifecycle::FAILED);

    py::enum_<RippleState>(m, "RippleState")
        .value("IDLE", RippleState::IDLE)
        .value("WALL_FORMING", RippleState::WALL_FORMING)
        .value("ABSORBING", RippleState::ABSORBING)
        .value("EXHAUSTING", RippleState::EXHAUSTING)
        .value("WITHDRAWING", RippleState::WITHDRAWING)
        .value("BREAKING", RippleState::BREAKING)
        .value("REFILLING", RippleState::REFILLING)
        .value("STABILIZING", RippleState::STABILIZING);

    py::enum_<RippleIntent>(m, "RippleIntent")
        .value("NO_ACTION", RippleIntent::NO_ACTION)
        .value("PREPARE_BOUNCE_LONG", RippleIntent::PREPARE_BOUNCE_LONG)
        .value("PREPARE_BOUNCE_SHORT", RippleIntent::PREPARE_BOUNCE_SHORT)
        .value("ENTER_BOUNCE_LONG", RippleIntent::ENTER_BOUNCE_LONG)
        .value("ENTER_BOUNCE_SHORT", RippleIntent::ENTER_BOUNCE_SHORT)
        .value("ENTER_BREAKOUT_LONG", RippleIntent::ENTER_BREAKOUT_LONG)
        .value("ENTER_BREAKOUT_SHORT", RippleIntent::ENTER_BREAKOUT_SHORT)
        .value("EXIT_BOUNCE", RippleIntent::EXIT_BOUNCE)
        .value("EXIT_BREAKOUT", RippleIntent::EXIT_BREAKOUT)
        .value("REARM_FOR_NEXT_BOUNCE", RippleIntent::REARM_FOR_NEXT_BOUNCE)
        .value("CANCEL_PASSIVE_ORDERS", RippleIntent::CANCEL_PASSIVE_ORDERS);

    py::class_<WallCandidate>(m, "WallCandidate")
        .def_readonly("wall_id", &WallCandidate::wall_id)
        .def_readonly("side", &WallCandidate::side)
        .def_readonly("price", &WallCandidate::price)
        .def_readonly("initial_qty", &WallCandidate::initial_qty)
        .def_readonly("current_qty", &WallCandidate::current_qty)
        .def_readonly("peak_qty", &WallCandidate::peak_qty)
        .def_readonly("prev_qty", &WallCandidate::prev_qty)
        .def_readonly("first_seen", &WallCandidate::first_seen)
        .def_readonly("last_seen", &WallCandidate::last_seen)
        .def_readonly("lifecycle", &WallCandidate::lifecycle)
        .def_readonly("refill_count", &WallCandidate::refill_count)
        .def_readonly("cumulative_absorbed", &WallCandidate::cumulative_absorbed)
        .def_readonly("depletion_rate", &WallCandidate::depletion_rate)
        .def_readonly("refill_rate", &WallCandidate::refill_rate)
        .def_readonly("update_count", &WallCandidate::update_count);

    py::class_<WallMetrics>(m, "WallMetrics")
        .def_readonly("wall_id", &WallMetrics::wall_id)
        .def_readonly("side", &WallMetrics::side)
        .def_readonly("price", &WallMetrics::price)
        .def_readonly("absolute_size", &WallMetrics::absolute_size)
        .def_readonly("relative_size", &WallMetrics::relative_size)
        .def_readonly("baseline_depth", &WallMetrics::baseline_depth)
        .def_readonly("distance_from_mid_ticks", &WallMetrics::distance_from_mid_ticks)
        .def_readonly("distance_from_best_ticks", &WallMetrics::distance_from_best_ticks)
        .def_readonly("first_seen_ts", &WallMetrics::first_seen_ts)
        .def_readonly("persistence_sec", &WallMetrics::persistence_sec)
        .def_readonly("growth_rate", &WallMetrics::growth_rate)
        .def_readonly("depletion_rate", &WallMetrics::depletion_rate)
        .def_readonly("depletion_pct", &WallMetrics::depletion_pct)
        .def_readonly("local_rank", &WallMetrics::local_rank)
        .def_readonly("is_active", &WallMetrics::is_active)
        .def_readonly("same_price_persists", &WallMetrics::same_price_persists)
        .def_readonly("disappeared", &WallMetrics::disappeared)
        .def_readonly("withdrawn", &WallMetrics::withdrawn)
        .def_readonly("lifecycle", &WallMetrics::lifecycle);

    py::class_<RippleFeatures>(m, "RippleFeatures")
        .def_readonly("relative_wall_size", &RippleFeatures::relative_wall_size)
        .def_readonly("wall_persistence_sec", &RippleFeatures::wall_persistence_sec)
        .def_readonly("wall_depletion_rate", &RippleFeatures::wall_depletion_rate)
        .def_readonly("wall_refill_rate", &RippleFeatures::wall_refill_rate)
        .def_readonly("wall_cancel_rate", &RippleFeatures::wall_cancel_rate)
        .def_readonly("distance_to_wall_ticks", &RippleFeatures::distance_to_wall_ticks)
        .def_readonly("queue_stability", &RippleFeatures::queue_stability)
        .def_readonly("aggressive_flow_imbalance", &RippleFeatures::aggressive_flow_imbalance)
        .def_readonly("trade_count_imbalance", &RippleFeatures::trade_count_imbalance)
        .def_readonly("microprice_drift", &RippleFeatures::microprice_drift)
        .def_readonly("top1_imbalance", &RippleFeatures::top1_imbalance)
        .def_readonly("top5_imbalance", &RippleFeatures::top5_imbalance)
        .def_readonly("impact_per_unit_volume", &RippleFeatures::impact_per_unit_volume)
        .def_readonly("spread_shock", &RippleFeatures::spread_shock)
        .def_readonly("short_horizon_volatility", &RippleFeatures::short_horizon_volatility)
        .def_readonly("time_since_wall_formed_sec", &RippleFeatures::time_since_wall_formed_sec)
        .def_readonly("cvd_divergence_strength",   &RippleFeatures::cvd_divergence_strength)
        .def_readonly("cvd_divergence_detected",   &RippleFeatures::cvd_divergence_detected)
        .def_readonly("poc_price",                 &RippleFeatures::poc_price)
        .def_readonly("distance_to_poc_ticks",     &RippleFeatures::distance_to_poc_ticks)
        .def_readonly("vah",                       &RippleFeatures::vah)
        .def_readonly("val",                       &RippleFeatures::val);

    py::class_<RippleEvidence>(m, "RippleEvidence")
        .def_readonly("absorption", &RippleEvidence::absorption)
        .def_readonly("exhaustion", &RippleEvidence::exhaustion)
        .def_readonly("withdrawal", &RippleEvidence::withdrawal)
        .def_readonly("breakout", &RippleEvidence::breakout)
        .def_readonly("refill", &RippleEvidence::refill)
        .def_readonly("stabilization", &RippleEvidence::stabilization)
        .def("dominant", &RippleEvidence::dominant)
        .def("dominant_score", &RippleEvidence::dominant_score);

    py::class_<RippleInferenceResult>(m, "RippleInferenceResult")
        .def_readonly("state", &RippleInferenceResult::state)
        .def_readonly("prev_state", &RippleInferenceResult::prev_state)
        .def_readonly("is_transition", &RippleInferenceResult::is_transition)
        .def_readonly("winning_score", &RippleInferenceResult::winning_score)
        .def_readonly("runner_up_score", &RippleInferenceResult::runner_up_score)
        .def_readonly("runner_up_state", &RippleInferenceResult::runner_up_state)
        .def_readonly("confidence", &RippleInferenceResult::confidence)
        .def_readonly("state_age_ms", &RippleInferenceResult::state_age_ms)
        .def_readonly("reason", &RippleInferenceResult::reason)
        .def("score_for", &RippleInferenceResult::score_for);

    py::class_<RippleDecision>(m, "RippleDecision")
        .def_readonly("timestamp", &RippleDecision::timestamp)
        .def_readonly("intent", &RippleDecision::intent)
        .def_readonly("reference_side", &RippleDecision::reference_side)
        .def_readonly("reference_price", &RippleDecision::reference_price)
        .def_readonly("invalidation_price", &RippleDecision::invalidation_price)
        .def_readonly("confidence", &RippleDecision::confidence)
        .def_readonly("wall_id", &RippleDecision::wall_id)
        .def_readonly("triggering_state", &RippleDecision::triggering_state)
        .def_readonly("reason", &RippleDecision::reason);

    py::class_<RippleConfig>(m, "RippleConfig")
        .def(py::init<>())
        .def_readwrite("tick_size", &RippleConfig::tick_size)
        .def_readwrite("wall_min_relative_size", &RippleConfig::wall_min_relative_size)
        .def_readwrite("wall_max_distance_ticks", &RippleConfig::wall_max_distance_ticks)
        .def_readwrite("feature_window_ms", &RippleConfig::feature_window_ms)
        .def_readwrite("absorption_entry", &RippleConfig::absorption_entry)
        .def_readwrite("exhaustion_entry", &RippleConfig::exhaustion_entry)
        .def_readwrite("withdrawal_entry", &RippleConfig::withdrawal_entry)
        .def_readwrite("breakout_entry", &RippleConfig::breakout_entry)
        .def_readwrite("idle_exit_threshold", &RippleConfig::idle_exit_threshold)
        .def_readwrite("max_position", &RippleConfig::max_position)
        .def_readwrite("bounce_max_break_risk", &RippleConfig::bounce_max_break_risk)
        .def_readwrite("bounce_max_withdrawal_risk", &RippleConfig::bounce_max_withdrawal_risk)
        .def_readwrite("breakout_max_absorption", &RippleConfig::breakout_max_absorption)
        .def_readwrite("cancel_risk_threshold", &RippleConfig::cancel_risk_threshold)
        .def_readwrite("rearm_min_evidence", &RippleConfig::rearm_min_evidence)
        .def_readwrite("time_stop_ms", &RippleConfig::time_stop_ms)
        .def_readwrite("enable_diagnostics", &RippleConfig::enable_diagnostics)
        .def_readwrite("console_diagnostics", &RippleConfig::console_diagnostics)
        .def_readwrite("diagnostics_path", &RippleConfig::diagnostics_path)
        .def_readwrite("replay_mode", &RippleConfig::replay_mode)
        .def_readwrite("paper_fills", &RippleConfig::paper_fills)
        .def_readwrite("pipeline_min_interval_ms", &RippleConfig::pipeline_min_interval_ms)
        .def_readwrite("hmm_enabled", &RippleConfig::hmm_enabled)
        .def_readwrite("hmm_model_path", &RippleConfig::hmm_model_path)
        .def_readwrite("lifecycle", &RippleConfig::lifecycle);

    py::class_<InventorySnapshot>(m, "InventorySnapshot")
        .def(py::init<>())
        .def_readwrite("position", &InventorySnapshot::position)
        .def_readwrite("avg_entry_price", &InventorySnapshot::avg_entry_price)
        .def_readwrite("unrealized_pnl", &InventorySnapshot::unrealized_pnl);

    py::class_<TickContext>(m, "TickContext")
        .def(py::init<>())
        .def_readwrite("timestamp", &TickContext::timestamp)
        .def_readwrite("microprice", &TickContext::microprice)
        .def_readwrite("last_trade_price", &TickContext::last_trade_price)
        .def_readwrite("cvd_slope", &TickContext::cvd_slope)
        .def_readwrite("trade_rate", &TickContext::trade_rate)
        .def_readwrite("impact", &TickContext::impact)
        .def_readwrite("imbalance", &TickContext::imbalance)
        .def_readwrite("recent_sigma_P", &TickContext::recent_sigma_P);

    py::class_<LifecycleConfig>(m, "LifecycleConfig")
        .def(py::init<>())
        .def_readwrite("confirmation_window_ms", &LifecycleConfig::confirmation_window_ms)
        .def_readwrite("expand_threshold_sigma", &LifecycleConfig::expand_threshold_sigma)
        .def_readwrite("max_hold_time_ms", &LifecycleConfig::max_hold_time_ms)
        .def_readwrite("cooldown_ms", &LifecycleConfig::cooldown_ms)
        .def_readwrite("max_scale_ins", &LifecycleConfig::max_scale_ins)
        .def_readwrite("scale_in_threshold_sigma", &LifecycleConfig::scale_in_threshold_sigma)
        .def_readwrite("scale_in_cvd_slope_min", &LifecycleConfig::scale_in_cvd_slope_min)
        .def_readwrite("trailing_stop_sigma", &LifecycleConfig::trailing_stop_sigma)
        .def_readwrite("exhaustion_cvd_slope_thresh", &LifecycleConfig::exhaustion_cvd_slope_thresh)
        .def_readwrite("exhaustion_impact_ratio", &LifecycleConfig::exhaustion_impact_ratio)
        .def_readwrite("target_distance_sigma", &LifecycleConfig::target_distance_sigma)
        .def_readwrite("budget_exit_threshold", &LifecycleConfig::budget_exit_threshold)
        .def_readwrite("maturation_momentum_fade", &LifecycleConfig::maturation_momentum_fade)
        .def_readwrite("scale_out_count", &LifecycleConfig::scale_out_count);

    py::class_<LiquidityMapConfig>(m, "LiquidityMapConfig")
        .def(py::init<>())
        .def_readwrite("max_levels",            &LiquidityMapConfig::max_levels)
        .def_readwrite("wall_min_quality",      &LiquidityMapConfig::wall_min_quality)
        .def_readwrite("hvn_volume_ratio",      &LiquidityMapConfig::hvn_volume_ratio)
        .def_readwrite("lvn_volume_ratio",      &LiquidityMapConfig::lvn_volume_ratio)
        .def_readwrite("void_min_gap_sigma",    &LiquidityMapConfig::void_min_gap_sigma)
        .def_readwrite("dest_w_depth",          &LiquidityMapConfig::dest_w_depth)
        .def_readwrite("dest_w_volume",         &LiquidityMapConfig::dest_w_volume)
        .def_readwrite("dest_w_vwap_proximity", &LiquidityMapConfig::dest_w_vwap_proximity)
        .def_readwrite("dest_w_structural",     &LiquidityMapConfig::dest_w_structural)
        .def_readwrite("hold_w_quality",        &LiquidityMapConfig::hold_w_quality)
        .def_readwrite("hold_w_depth",          &LiquidityMapConfig::hold_w_depth)
        .def_readwrite("hold_w_imbalance",      &LiquidityMapConfig::hold_w_imbalance)
        .def_readwrite("hold_w_refill",         &LiquidityMapConfig::hold_w_refill);

    py::class_<LiquidityMapEngine>(m, "LiquidityMapEngine")
        .def(py::init<const LiquidityMapConfig&>(), py::arg("config") = LiquidityMapConfig{})
        .def("snapshot", &LiquidityMapEngine::snapshot,
             py::return_value_policy::reference_internal)
        .def("get_hold_score", &LiquidityMapEngine::get_hold_score)
        .def("get_dest_score", &LiquidityMapEngine::get_dest_score)
        .def("get_destinations_in_direction", &LiquidityMapEngine::get_destinations_in_direction)
        .def("level_count", &LiquidityMapEngine::level_count)
        .def("reset", &LiquidityMapEngine::reset);

    py::class_<ScaleOutTarget>(m, "ScaleOutTarget")
        .def(py::init<>())
        .def_readwrite("price",    &ScaleOutTarget::price)
        .def_readwrite("quantity", &ScaleOutTarget::quantity)
        .def_readwrite("new_stop", &ScaleOutTarget::new_stop)
        .def_readwrite("filled",   &ScaleOutTarget::filled);

    py::class_<TradeLifecycleEngine>(m, "TradeLifecycleEngine")
        .def(py::init<const LifecycleConfig&>(), py::arg("config") = LifecycleConfig{})
        .def("try_setup", &TradeLifecycleEngine::try_setup)
        .def("confirm_entry", &TradeLifecycleEngine::confirm_entry)
        .def("on_fill", &TradeLifecycleEngine::on_fill)
        .def("on_tick", &TradeLifecycleEngine::on_tick)
        .def("on_exit_signal", &TradeLifecycleEngine::on_exit_signal)
        .def("set_scale_out_targets", [](TradeLifecycleEngine& self, const std::vector<ScaleOutTarget>& targets) {
            self.set_scale_out_targets(targets.data(), static_cast<int>(targets.size()));
        })
        .def("get_lifecycle_state", &TradeLifecycleEngine::get_lifecycle_state)
        .def("get_archetype", &TradeLifecycleEngine::get_archetype)
        .def("get_side", &TradeLifecycleEngine::get_side)
        .def("get_last_exit_type", &TradeLifecycleEngine::get_last_exit_type)
        .def("get_stop_price", &TradeLifecycleEngine::get_stop_price)
        .def("get_target_price", &TradeLifecycleEngine::get_target_price)
        .def("get_scale_out_idx", &TradeLifecycleEngine::get_scale_out_idx)
        .def("get_scale_out_target_count", &TradeLifecycleEngine::get_scale_out_target_count)
        .def("get_snapshot", &TradeLifecycleEngine::get_snapshot)
        .def("is_active", &TradeLifecycleEngine::is_active)
        .def("is_idle", &TradeLifecycleEngine::is_idle)
        .def("has_pending_intent", &TradeLifecycleEngine::has_pending_intent)
        .def("consume_pending_intent", &TradeLifecycleEngine::consume_pending_intent)
        .def("completed_trades", &TradeLifecycleEngine::completed_trades)
        .def("cumulative_pnl", &TradeLifecycleEngine::cumulative_pnl)
        .def("peak_equity", &TradeLifecycleEngine::peak_equity)
        .def("max_drawdown_value", &TradeLifecycleEngine::max_drawdown_value)
        .def("reset", &TradeLifecycleEngine::reset);

    py::class_<RippleEngine>(m, "RippleEngine")
        .def("current_state", &RippleEngine::current_state)
        .def("last_features", &RippleEngine::last_features,
             py::return_value_policy::reference_internal)
        .def("last_evidence", &RippleEngine::last_evidence,
             py::return_value_policy::reference_internal)
        .def("last_inference", &RippleEngine::last_inference,
             py::return_value_policy::reference_internal)
        .def("last_decision", &RippleEngine::last_decision,
             py::return_value_policy::reference_internal)
        .def("set_inventory", &RippleEngine::set_inventory)
        .def("reset", &RippleEngine::reset)
        .def("pipeline_runs", &RippleEngine::pipeline_runs)
        .def("trade_count", &RippleEngine::trade_count)
        .def("depth_count", &RippleEngine::depth_count)
        .def("on_fill", &RippleEngine::on_fill)
        .def("get_trade_state", &RippleEngine::get_trade_state)
        .def("get_lifecycle_state", &RippleEngine::get_lifecycle_state)
        .def("has_active_trade", &RippleEngine::has_active_trade)
        .def("liquidity_map", &RippleEngine::liquidity_map,
             py::return_value_policy::reference_internal)
        .def("set_risk_budget", &RippleEngine::set_risk_budget)
        .def("set_realized_vol", &RippleEngine::set_realized_vol)
        .def("set_wave_snapshot", &RippleEngine::set_wave_snapshot)
        .def("wave_snapshot", &RippleEngine::wave_snapshot,
             py::return_value_policy::reference_internal)
        .def("has_wave_snapshot", &RippleEngine::has_wave_snapshot)
        .def("cumulative_pnl", &RippleEngine::cumulative_pnl)
        .def("lifecycle_peak_equity", &RippleEngine::lifecycle_peak_equity)
        .def("lifecycle_max_drawdown", &RippleEngine::lifecycle_max_drawdown)
        .def("completed_trades", &RippleEngine::completed_trades)
        .def("set_hmm_backend", [](RippleEngine& self, const std::string& model_json) {
            auto hmm = std::make_unique<ripple::HMMBasedInference>(self.config());
            if (!model_json.empty()) {
                if (!hmm->load_model_from_string(model_json))
                    throw std::runtime_error("Failed to load HMM model from JSON");
            }
            self.set_inference_backend(std::move(hmm));
        }, py::arg("model_json") = "")
        .def("set_score_backend", [](RippleEngine& self) {
            self.set_inference_backend(
                std::make_unique<ripple::ScoreBasedInference>(self.config()));
        });

    py::class_<ripple::HMMBasedInference>(m, "HMMBasedInference")
        .def(py::init<const RippleConfig&>())
        .def("load_model", &ripple::HMMBasedInference::load_model)
        .def("load_model_from_string", &ripple::HMMBasedInference::load_model_from_string)
        .def("model_loaded", &ripple::HMMBasedInference::model_loaded)
        .def("num_states", &ripple::HMMBasedInference::num_states)
        .def("reset_forward", &ripple::HMMBasedInference::reset_forward);

    py::class_<DiagnosticRecord>(m, "DiagnosticRecord")
        .def_readonly("ts", &DiagnosticRecord::ts)
        .def_readonly("features", &DiagnosticRecord::features)
        .def_readonly("evidence", &DiagnosticRecord::evidence)
        .def_readonly("inference", &DiagnosticRecord::inference)
        .def_readonly("decision", &DiagnosticRecord::decision);

    py::class_<RippleDiagnostics, std::shared_ptr<RippleDiagnostics>>(m, "RippleDiagnostics")
        .def("record_count", &RippleDiagnostics::record_count)
        .def("records", &RippleDiagnostics::records,
             py::return_value_policy::reference_internal)
        .def("flush", &RippleDiagnostics::flush)
        .def("set_console_output", &RippleDiagnostics::set_console_output)
        .def_static("compact_line", &RippleDiagnostics::compact_line);

    // ========== Strategy Schema Types (Schemas.h) ==========

    // --- Layer enums ---
    py::enum_<orderflow::TideBias>(m, "TideBias")
        .value("LONG", orderflow::TideBias::LONG)
        .value("SHORT", orderflow::TideBias::SHORT)
        .value("NEUTRAL", orderflow::TideBias::NEUTRAL);

    py::enum_<orderflow::VolRegime>(m, "VolRegime")
        .value("LOW", orderflow::VolRegime::LOW)
        .value("NORMAL", orderflow::VolRegime::NORMAL)
        .value("HIGH", orderflow::VolRegime::HIGH)
        .value("CRISIS", orderflow::VolRegime::CRISIS);

    py::enum_<orderflow::WaveRegime>(m, "WaveRegime")
        .value("MEAN_REVERSION", orderflow::WaveRegime::MEAN_REVERSION)
        .value("BREAKOUT", orderflow::WaveRegime::BREAKOUT)
        .value("BREAKDOWN", orderflow::WaveRegime::BREAKDOWN)
        .value("NEUTRAL", orderflow::WaveRegime::NEUTRAL);

    py::enum_<orderflow::PermissionLevel>(m, "PermissionLevel")
        .value("FULL", orderflow::PermissionLevel::FULL)
        .value("REDUCED", orderflow::PermissionLevel::REDUCED)
        .value("DISABLED", orderflow::PermissionLevel::DISABLED);

    py::enum_<orderflow::LevelType>(m, "LevelType")
        .value("WALL_BID", orderflow::LevelType::WALL_BID)
        .value("WALL_ASK", orderflow::LevelType::WALL_ASK)
        .value("HVN", orderflow::LevelType::HVN)
        .value("LVN", orderflow::LevelType::LVN)
        .value("POC", orderflow::LevelType::POC)
        .value("VWAP", orderflow::LevelType::VWAP)
        .value("VOID_BOUNDARY", orderflow::LevelType::VOID_BOUNDARY);

    // --- Trade enums ---
    py::enum_<orderflow::TradeArchetype>(m, "TradeArchetype")
        .value("BOUNCE", orderflow::TradeArchetype::BOUNCE)
        .value("BREAKOUT", orderflow::TradeArchetype::BREAKOUT);

    py::enum_<orderflow::TradeSide>(m, "TradeSide")
        .value("LONG", orderflow::TradeSide::LONG)
        .value("SHORT", orderflow::TradeSide::SHORT);

    py::enum_<orderflow::LifecycleState>(m, "LifecycleState")
        .value("SETUP", orderflow::LifecycleState::SETUP)
        .value("ENTRY", orderflow::LifecycleState::ENTRY)
        .value("CONFIRMATION", orderflow::LifecycleState::CONFIRMATION)
        .value("EXPANSION", orderflow::LifecycleState::EXPANSION)
        .value("MATURATION", orderflow::LifecycleState::MATURATION)
        .value("EXIT", orderflow::LifecycleState::EXIT)
        .value("COOLDOWN", orderflow::LifecycleState::COOLDOWN)
        .value("CANCELLED", orderflow::LifecycleState::CANCELLED);

    py::enum_<orderflow::ExitType>(m, "ExitType")
        .value("INVALIDATION", orderflow::ExitType::INVALIDATION)
        .value("TARGET", orderflow::ExitType::TARGET)
        .value("EXHAUSTION", orderflow::ExitType::EXHAUSTION)
        .value("TIME", orderflow::ExitType::TIME)
        .value("RISK_BUDGET", orderflow::ExitType::RISK_BUDGET);

    // --- Execution enums ---
    py::enum_<orderflow::OrderSide>(m, "OrderSide")
        .value("BUY", orderflow::OrderSide::BUY)
        .value("SELL", orderflow::OrderSide::SELL);

    py::enum_<orderflow::OrderType>(m, "OrderType")
        .value("MARKET", orderflow::OrderType::MARKET)
        .value("LIMIT", orderflow::OrderType::LIMIT);

    py::enum_<orderflow::IntentType>(m, "IntentType")
        .value("ENTRY", orderflow::IntentType::ENTRY)
        .value("SCALE_IN", orderflow::IntentType::SCALE_IN)
        .value("SCALE_OUT", orderflow::IntentType::SCALE_OUT)
        .value("EXIT", orderflow::IntentType::EXIT);

    py::enum_<orderflow::Urgency>(m, "Urgency")
        .value("IMMEDIATE", orderflow::Urgency::IMMEDIATE)
        .value("NORMAL", orderflow::Urgency::NORMAL);

    py::enum_<orderflow::ExitReason>(m, "ExitReason")
        .value("INVALIDATION", orderflow::ExitReason::INVALIDATION)
        .value("TARGET", orderflow::ExitReason::TARGET)
        .value("EXHAUSTION", orderflow::ExitReason::EXHAUSTION)
        .value("TIME", orderflow::ExitReason::TIME)
        .value("RISK_BUDGET", orderflow::ExitReason::RISK_BUDGET)
        .value("NONE", orderflow::ExitReason::NONE);

    py::enum_<orderflow::EventType>(m, "EventType")
        .value("TRADE", orderflow::EventType::TRADE)
        .value("DEPTH_UPDATE", orderflow::EventType::DEPTH_UPDATE)
        .value("DEPTH_SNAPSHOT", orderflow::EventType::DEPTH_SNAPSHOT);

    // --- Data contract structs ---

    py::class_<orderflow::PermissionSet>(m, "PermissionSet")
        .def(py::init<>())
        .def_readwrite("long_bounce", &orderflow::PermissionSet::long_bounce)
        .def_readwrite("short_bounce", &orderflow::PermissionSet::short_bounce)
        .def_readwrite("long_breakout", &orderflow::PermissionSet::long_breakout)
        .def_readwrite("short_breakout", &orderflow::PermissionSet::short_breakout)
        .def_readwrite("reduced_size_fraction", &orderflow::PermissionSet::reduced_size_fraction)
        .def("get", &orderflow::PermissionSet::get)
        .def("is_allowed", &orderflow::PermissionSet::is_allowed)
        .def("size_fraction", &orderflow::PermissionSet::size_fraction);

    py::class_<orderflow::TideSnapshot>(m, "TideSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::TideSnapshot::timestamp)
        .def_readwrite("bias", &orderflow::TideSnapshot::bias)
        .def_readwrite("risk_multiplier", &orderflow::TideSnapshot::risk_multiplier)
        .def_readwrite("max_position_usd", &orderflow::TideSnapshot::max_position_usd)
        .def_readwrite("es_budget", &orderflow::TideSnapshot::es_budget)
        .def_readwrite("consumed_es", &orderflow::TideSnapshot::consumed_es)
        .def_readwrite("vol_regime", &orderflow::TideSnapshot::vol_regime)
        .def_readwrite("crypto_beta_return", &orderflow::TideSnapshot::crypto_beta_return)
        .def_readwrite("realized_vol", &orderflow::TideSnapshot::realized_vol)
        .def_readwrite("liquidity_stress", &orderflow::TideSnapshot::liquidity_stress);

    py::class_<orderflow::WaveSnapshot>(m, "WaveSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::WaveSnapshot::timestamp)
        .def_readwrite("regime", &orderflow::WaveSnapshot::regime)
        .def_readwrite("trend_efficiency", &orderflow::WaveSnapshot::trend_efficiency)
        .def_readwrite("dispersion", &orderflow::WaveSnapshot::dispersion)
        .def_readwrite("absorption_ratio", &orderflow::WaveSnapshot::absorption_ratio)
        .def_readwrite("permissions", &orderflow::WaveSnapshot::permissions);

    py::class_<orderflow::RiskBudgetSnapshot>(m, "RiskBudgetSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::RiskBudgetSnapshot::timestamp)
        .def_readwrite("es_budget", &orderflow::RiskBudgetSnapshot::es_budget)
        .def_readwrite("consumed_es", &orderflow::RiskBudgetSnapshot::consumed_es)
        .def_readwrite("risk_multiplier", &orderflow::RiskBudgetSnapshot::risk_multiplier)
        .def_readwrite("max_position_usd", &orderflow::RiskBudgetSnapshot::max_position_usd)
        .def_readwrite("bias", &orderflow::RiskBudgetSnapshot::bias)
        .def_readwrite("vol_regime", &orderflow::RiskBudgetSnapshot::vol_regime);

    py::class_<orderflow::WallView>(m, "WallView")
        .def(py::init<>())
        .def_readwrite("price", &orderflow::WallView::price)
        .def_readwrite("depth", &orderflow::WallView::depth)
        .def_readwrite("side", &orderflow::WallView::side)
        .def_readwrite("quality", &orderflow::WallView::quality)
        .def_readwrite("persistence", &orderflow::WallView::persistence)
        .def_readwrite("cancellation_rate", &orderflow::WallView::cancellation_rate)
        .def_readwrite("first_seen_ts", &orderflow::WallView::first_seen_ts)
        .def_readwrite("last_update_ts", &orderflow::WallView::last_update_ts);

    py::class_<orderflow::LiquidityLevel>(m, "LiquidityLevel")
        .def(py::init<>())
        .def_readwrite("price", &orderflow::LiquidityLevel::price)
        .def_readwrite("depth", &orderflow::LiquidityLevel::depth)
        .def_readwrite("wall_quality", &orderflow::LiquidityLevel::wall_quality)
        .def_readwrite("profile_volume", &orderflow::LiquidityLevel::profile_volume)
        .def_readwrite("net_flow", &orderflow::LiquidityLevel::net_flow)
        .def_readwrite("hold_score", &orderflow::LiquidityLevel::hold_score)
        .def_readwrite("dest_score", &orderflow::LiquidityLevel::dest_score)
        .def_readwrite("type", &orderflow::LiquidityLevel::type);

    py::class_<orderflow::VoidCorridor>(m, "VoidCorridor")
        .def(py::init<>())
        .def_readwrite("lo", &orderflow::VoidCorridor::lo)
        .def_readwrite("hi", &orderflow::VoidCorridor::hi);

    py::class_<orderflow::LiquidityMapSnapshot>(m, "LiquidityMapSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::LiquidityMapSnapshot::timestamp)
        .def_readwrite("levels", &orderflow::LiquidityMapSnapshot::levels)
        .def_readwrite("void_corridors", &orderflow::LiquidityMapSnapshot::void_corridors)
        .def_readwrite("nearest_bid_wall", &orderflow::LiquidityMapSnapshot::nearest_bid_wall)
        .def_readwrite("nearest_ask_wall", &orderflow::LiquidityMapSnapshot::nearest_ask_wall)
        .def_readwrite("poc", &orderflow::LiquidityMapSnapshot::poc)
        .def_readwrite("vwap", &orderflow::LiquidityMapSnapshot::vwap);

    py::class_<orderflow::TradeStateSnapshot>(m, "TradeStateSnapshot")
        .def(py::init<>())
        .def_readwrite("trade_id", &orderflow::TradeStateSnapshot::trade_id)
        .def_readwrite("archetype", &orderflow::TradeStateSnapshot::archetype)
        .def_readwrite("side", &orderflow::TradeStateSnapshot::side)
        .def_readwrite("state", &orderflow::TradeStateSnapshot::state)
        .def_readwrite("entry_price", &orderflow::TradeStateSnapshot::entry_price)
        .def_readwrite("stop_price", &orderflow::TradeStateSnapshot::stop_price)
        .def_readwrite("target_price", &orderflow::TradeStateSnapshot::target_price)
        .def_readwrite("quantity", &orderflow::TradeStateSnapshot::quantity)
        .def_readwrite("unrealized_pnl", &orderflow::TradeStateSnapshot::unrealized_pnl)
        .def_readwrite("hold_time_ms", &orderflow::TradeStateSnapshot::hold_time_ms)
        .def_readwrite("scale_count", &orderflow::TradeStateSnapshot::scale_count);

    py::class_<orderflow::FillEvent>(m, "FillEvent")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::FillEvent::timestamp)
        .def_readwrite("trade_id", &orderflow::FillEvent::trade_id)
        .def_readwrite("order_id", &orderflow::FillEvent::order_id)
        .def_readwrite("symbol", &orderflow::FillEvent::symbol)
        .def_readwrite("side", &orderflow::FillEvent::side)
        .def_readwrite("price", &orderflow::FillEvent::price)
        .def_readwrite("quantity", &orderflow::FillEvent::quantity)
        .def_readwrite("commission", &orderflow::FillEvent::commission)
        .def_readwrite("is_maker", &orderflow::FillEvent::is_maker);

    py::class_<orderflow::FeatureSnapshot>(m, "FeatureSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::FeatureSnapshot::timestamp)
        .def_readwrite("ripple_features", &orderflow::FeatureSnapshot::ripple_features);

    py::class_<orderflow::RippleStateSnapshot>(m, "RippleStateSnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::RippleStateSnapshot::timestamp)
        .def_readwrite("liquidity_state", &orderflow::RippleStateSnapshot::liquidity_state)
        .def_readwrite("walls", &orderflow::RippleStateSnapshot::walls)
        .def_readwrite("microprice", &orderflow::RippleStateSnapshot::microprice)
        .def_readwrite("imbalance", &orderflow::RippleStateSnapshot::imbalance)
        .def_readwrite("cvd", &orderflow::RippleStateSnapshot::cvd)
        .def_readwrite("ofi", &orderflow::RippleStateSnapshot::ofi)
        .def_readwrite("lsi", &orderflow::RippleStateSnapshot::lsi);

    py::class_<orderflow::MarketEvent>(m, "MarketEvent")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::MarketEvent::timestamp)
        .def_readwrite("symbol", &orderflow::MarketEvent::symbol)
        .def_readwrite("event_type", &orderflow::MarketEvent::event_type);

    py::class_<orderflow::StrategyExecutionIntent>(m, "StrategyExecutionIntent")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::StrategyExecutionIntent::timestamp)
        .def_readwrite("trade_id", &orderflow::StrategyExecutionIntent::trade_id)
        .def_readwrite("symbol", &orderflow::StrategyExecutionIntent::symbol)
        .def_readwrite("side", &orderflow::StrategyExecutionIntent::side)
        .def_readwrite("quantity", &orderflow::StrategyExecutionIntent::quantity)
        .def_readwrite("order_type", &orderflow::StrategyExecutionIntent::order_type)
        .def_readwrite("limit_price", &orderflow::StrategyExecutionIntent::limit_price)
        .def_readwrite("intent_type", &orderflow::StrategyExecutionIntent::intent_type)
        .def_readwrite("exit_reason", &orderflow::StrategyExecutionIntent::exit_reason)
        .def_readwrite("urgency", &orderflow::StrategyExecutionIntent::urgency);

    // --- Default snapshots ---
    py::class_<orderflow::DefaultTideSnapshot>(m, "DefaultTideSnapshot")
        .def_static("make", &orderflow::DefaultTideSnapshot::make);

    py::class_<orderflow::DefaultWaveSnapshot>(m, "DefaultWaveSnapshot")
        .def_static("make", &orderflow::DefaultWaveSnapshot::make);

    // --- Configuration sub-structs ---

    py::class_<orderflow::TideConfig>(m, "TideConfig")
        .def(py::init<>())
        .def_readwrite("update_interval_ms", &orderflow::TideConfig::update_interval_ms)
        .def_readwrite("risk_multiplier_default", &orderflow::TideConfig::risk_multiplier_default)
        .def_readwrite("es_budget_global", &orderflow::TideConfig::es_budget_global)
        .def_readwrite("max_position_usd", &orderflow::TideConfig::max_position_usd)
        .def_readwrite("lsi_reduce_threshold", &orderflow::TideConfig::lsi_reduce_threshold)
        .def_readwrite("lsi_reduce_slope", &orderflow::TideConfig::lsi_reduce_slope);

    py::class_<orderflow::WaveConfig>(m, "WaveConfig")
        .def(py::init<>())
        .def_readwrite("update_interval_ms", &orderflow::WaveConfig::update_interval_ms)
        .def_readwrite("eta_mr_threshold", &orderflow::WaveConfig::eta_mr_threshold)
        .def_readwrite("eta_bo_threshold", &orderflow::WaveConfig::eta_bo_threshold)
        .def_readwrite("eta_neutral_threshold", &orderflow::WaveConfig::eta_neutral_threshold)
        .def_readwrite("dispersion_threshold", &orderflow::WaveConfig::dispersion_threshold)
        .def_readwrite("dispersion_critical", &orderflow::WaveConfig::dispersion_critical)
        .def_readwrite("ar_critical", &orderflow::WaveConfig::ar_critical)
        .def_readwrite("ar_recover", &orderflow::WaveConfig::ar_recover)
        .def_readwrite("reduced_size_fraction", &orderflow::WaveConfig::reduced_size_fraction);

    py::class_<orderflow::RiskConfig>(m, "RiskConfig")
        .def(py::init<>())
        .def_readwrite("es_confidence_level", &orderflow::RiskConfig::es_confidence_level)
        .def_readwrite("target_risk_usd", &orderflow::RiskConfig::target_risk_usd)
        .def_readwrite("sigma_target", &orderflow::RiskConfig::sigma_target)
        .def_readwrite("maker_fee_bps", &orderflow::RiskConfig::maker_fee_bps)
        .def_readwrite("taker_fee_bps", &orderflow::RiskConfig::taker_fee_bps)
        .def_readwrite("leverage", &orderflow::RiskConfig::leverage)
        .def_readwrite("budget_exit_threshold", &orderflow::RiskConfig::budget_exit_threshold);

    py::class_<orderflow::ExecutionConfig>(m, "ExecutionConfig")
        .def(py::init<>())
        .def_readwrite("bounce_order_type", &orderflow::ExecutionConfig::bounce_order_type)
        .def_readwrite("breakout_order_type", &orderflow::ExecutionConfig::breakout_order_type)
        .def_readwrite("slippage_tolerance_bps", &orderflow::ExecutionConfig::slippage_tolerance_bps);

    py::class_<orderflow::TestingConfig>(m, "TestingConfig")
        .def(py::init<>())
        .def_readwrite("replay_tolerance", &orderflow::TestingConfig::replay_tolerance)
        .def_readwrite("benchmark_latency_target_us", &orderflow::TestingConfig::benchmark_latency_target_us);

    py::class_<orderflow::StrategyConfig>(m, "StrategyConfig")
        .def(py::init<>())
        .def_readwrite("tide", &orderflow::StrategyConfig::tide)
        .def_readwrite("wave", &orderflow::StrategyConfig::wave)
        .def_readwrite("ripple", &orderflow::StrategyConfig::ripple)
        .def_readwrite("risk", &orderflow::StrategyConfig::risk)
        .def_readwrite("execution", &orderflow::StrategyConfig::execution)
        .def_readwrite("testing", &orderflow::StrategyConfig::testing);

    // --- Strategy-level data contracts §17.2 / §17.3 ---

    py::class_<orderflow::TradeEventContract>(m, "TradeEventContract")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::TradeEventContract::timestamp)
        .def_readwrite("symbol", &orderflow::TradeEventContract::symbol)
        .def_readwrite("price", &orderflow::TradeEventContract::price)
        .def_readwrite("quantity", &orderflow::TradeEventContract::quantity)
        .def_readwrite("is_buyer_maker", &orderflow::TradeEventContract::is_buyer_maker);

    py::class_<orderflow::DepthUpdateContract>(m, "DepthUpdateContract")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::DepthUpdateContract::timestamp)
        .def_readwrite("symbol", &orderflow::DepthUpdateContract::symbol)
        .def_readwrite("bids", &orderflow::DepthUpdateContract::bids)
        .def_readwrite("asks", &orderflow::DepthUpdateContract::asks)
        .def_readwrite("first_update_id", &orderflow::DepthUpdateContract::first_update_id)
        .def_readwrite("last_update_id", &orderflow::DepthUpdateContract::last_update_id);

    // --- Composite strategy snapshot ---

    py::class_<orderflow::StrategySnapshot>(m, "StrategySnapshot")
        .def(py::init<>())
        .def_readwrite("timestamp", &orderflow::StrategySnapshot::timestamp)
        .def_readwrite("tide", &orderflow::StrategySnapshot::tide)
        .def_readwrite("wave", &orderflow::StrategySnapshot::wave)
        .def_readwrite("ripple", &orderflow::StrategySnapshot::ripple)
        .def_readwrite("trade", &orderflow::StrategySnapshot::trade)
        .def_readwrite("liquidity_map", &orderflow::StrategySnapshot::liquidity_map)
        .def_readwrite("risk", &orderflow::StrategySnapshot::risk)
        .def_readwrite("features", &orderflow::StrategySnapshot::features)
        .def_static("make_default", &orderflow::StrategySnapshot::make_default);

    // --- Phase 4: RiskEngine ---

    py::class_<RiskEngine>(m, "RiskEngine")
        .def(py::init<const orderflow::RiskConfig&>(), py::arg("config") = orderflow::RiskConfig{})
        .def("set_budget", &RiskEngine::set_budget)
        .def("set_volatility", &RiskEngine::set_volatility)
        .def("on_fill", &RiskEngine::on_fill)
        .def("on_price_update", &RiskEngine::on_price_update)
        .def("check_new_order", &RiskEngine::check_new_order)
        .def("get_allowed_size", &RiskEngine::get_allowed_size)
        .def("compute_position_size", &RiskEngine::compute_position_size)
        .def("get_consumed_es", &RiskEngine::get_consumed_es)
        .def("get_remaining_budget", &RiskEngine::get_remaining_budget)
        .def("is_budget_exhausted", &RiskEngine::is_budget_exhausted)
        .def("get_position_qty", &RiskEngine::get_position_qty)
        .def("get_position_usd", &RiskEngine::get_position_usd)
        .def("get_unrealized_pnl", &RiskEngine::get_unrealized_pnl)
        .def("get_snapshot", &RiskEngine::get_snapshot)
        .def("reset", &RiskEngine::reset);

}
