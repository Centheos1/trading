"""
Command-line entry point for the Wave-layer pipeline.

Subcommands:
    backtest   — run a single Wave backtest (isolated or stacked), write report.
    optimise   — NSGA-II search over Wave parameters, write Pareto report.

Usage:
    from wave.wave_cli import run_cli
    run_cli(["backtest", "--symbol", "BTCUSDT", "--timeframe", "1h"])

Or via main.py:
    python main.py
    > wave
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from wave.wave_backtest import SizingMode, WaveBacktester, WaveStrategyParams
from wave.wave_metrics import compute_wave_report
from wave.wave_report import render_text, write_report
from wave.wave_optimiser import WaveOptimiser, WaveOptimiserConfig, ParamSpec

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Data loading helper (reuses the same HDF5 layout as Tide)
# ────────────────────────────────────────────────────────────────────────

def _load_ohlcv(
    exchange: str,
    symbol: str,
    timeframe: str,
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
) -> pd.DataFrame:
    """Load OHLCV from HDF5 and slice to the requested window."""
    try:
        from tide.tide_backtest import load_ohlcv
        return load_ohlcv(
            exchange, symbol,
            timeframe=timeframe,
            from_time=from_ms,
            to_time=to_ms,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not load OHLCV: {exc}") from exc


def _parse_date(s: Optional[str]) -> Optional[int]:
    if s is None or s == "":
        return None
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


# ────────────────────────────────────────────────────────────────────────
# Params builder
# ────────────────────────────────────────────────────────────────────────

def _params_from_args(args: argparse.Namespace) -> WaveStrategyParams:
    sizing = SizingMode(getattr(args, "sizing_mode", "base"))
    horizons = tuple(getattr(args, "accuracy_horizons", [1, 2, 4, 8, 12, 24]))
    p = WaveStrategyParams(
        symbol=args.symbol,
        exchange=args.exchange,
        mode=getattr(args, "mode", "isolated"),
        sizing_mode=sizing,
        initial_capital=args.initial_capital,
        max_position_usd=args.max_position_usd,
        max_position_base=getattr(args, "max_position_base", 1.0),
        leverage=args.leverage,
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        use_taker_fees=not getattr(args, "maker_fees", False),
        rebalance_threshold=args.rebalance_threshold,
        eta_window=args.eta_window,
        vwap_window=args.vwap_window,
        structure_window=args.structure_window,
        disp_window=args.disp_window,
        ar_window=args.ar_window,
        disp_scale=args.disp_scale,
        eta_mr_threshold=args.eta_mr_threshold,
        eta_bo_threshold=args.eta_bo_threshold,
        eta_neutral_threshold=args.eta_neutral_threshold,
        dispersion_threshold=args.dispersion_threshold,
        dispersion_critical=args.dispersion_critical,
        ar_critical=args.ar_critical,
        ar_recover=args.ar_recover,
        reduced_size_fraction=args.reduced_size_fraction,
        update_interval_ms=args.update_interval_ms,
        accuracy_horizons=horizons,
        # Phase 9 — backtest hardening
        liquidation_equity_frac=getattr(args, "liquidation_equity_frac", 0.0),
        slippage_bps=getattr(args, "slippage_bps", 0.0),
        slippage_per_unit_bps=getattr(args, "slippage_per_unit_bps", 0.0),
    )
    return p.with_timeframe(
        args.timeframe,
        rescale_windows=getattr(args, "rescale_windows", False),
    )


# ────────────────────────────────────────────────────────────────────────
# Shared argument groups
# ────────────────────────────────────────────────────────────────────────

def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", default="BTCUSDT")
    # Wave's minimum update interval is 5 000 ms (strategy.md §27.2).
    # Use 1m or 5m bars to stay within Wave's intended "minutes to hours" horizon.
    # 1h+ bars are Tide territory and should only be used for comparison purposes.
    _d = WaveStrategyParams
    p.add_argument("--timeframe", default=_d.timeframe,
                   choices=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"],
                   help="Bar timeframe.  Wave operates at 5s–minute cadence; "
                        "5m bars are the recommended minimum.  Default: 5m")
    p.add_argument("--from-time", default=None, help="YYYY-MM-DD")
    p.add_argument("--to-time", default=None, help="YYYY-MM-DD")
    p.add_argument("--mode", default="isolated", choices=["isolated", "stacked"],
                   help="isolated: TideBias pinned NEUTRAL; stacked: ingest Tide signals")
    p.add_argument("--tide-report", default=None,
                   help="Path to a previously saved Tide backtest result (HDF5/pickle) "
                        "for stacked mode.  Not yet implemented — stacked mode runs "
                        "Tide inline for now.")

    # Feature pipeline — all defaults pulled from WaveStrategyParams dataclass
    p.add_argument("--eta-window", type=int, default=_d.eta_window)
    p.add_argument("--vwap-window", type=int, default=_d.vwap_window)
    p.add_argument("--structure-window", type=int, default=_d.structure_window)
    p.add_argument("--disp-window", type=int, default=_d.disp_window)
    p.add_argument("--ar-window", type=int, default=_d.ar_window)
    p.add_argument("--disp-scale", type=float, default=_d.disp_scale)
    p.add_argument("--eta-mr-threshold", type=float, default=_d.eta_mr_threshold)
    p.add_argument("--eta-bo-threshold", type=float, default=_d.eta_bo_threshold)
    p.add_argument("--eta-neutral-threshold", type=float, default=_d.eta_neutral_threshold)
    p.add_argument("--dispersion-threshold", type=float, default=_d.dispersion_threshold)
    p.add_argument("--dispersion-critical", type=float, default=_d.dispersion_critical)
    p.add_argument("--ar-critical", type=float, default=_d.ar_critical)
    p.add_argument("--ar-recover", type=float, default=_d.ar_recover)
    p.add_argument("--reduced-size-fraction", type=float, default=_d.reduced_size_fraction)
    p.add_argument("--update-interval-ms", type=int, default=_d.update_interval_ms)

    # Portfolio
    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--max-position-usd", type=float, default=10_000.0)
    p.add_argument("--max-position-base", type=float, default=1.0)
    p.add_argument("--sizing-mode", default="base", choices=["usd", "base"])
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--taker-fee-bps", type=float, default=4.0)
    p.add_argument("--maker-fee-bps", type=float, default=2.0)
    p.add_argument("--maker-fees", action="store_true",
                   help="Use maker fees instead of taker fees")
    p.add_argument("--rebalance-threshold", type=float, default=0.0)

    # Accuracy horizons (in bar counts).  At 5m bars the defaults map to:
    #   1→5min  3→15min  6→30min  12→1h  24→2h  48→4h
    p.add_argument("--accuracy-horizons", nargs="+", type=int,
                   default=list(_d.accuracy_horizons),
                   help="Forward-accuracy look-ahead in bars.  Use horizon_labels() "
                        "or the report to see equivalent clock times.")

    # ── Phase 9: Backtest hardening ─────────────────────────────────────
    # NOTE: Wave thresholds (η, dispersion, AR) are calibrated against the
    # *bar-count* distributions of the chosen timeframe.  Default parameter
    # ranges were tuned for 1h bars (see wave_optimiser.py docstring).  When
    # switching to 5m without rescaling, the same window of e.g. 24 bars
    # covers ~12x less wall-clock time, materially shifting feature
    # distributions.  Use --rescale-windows to opt into automatic scaling.
    p.add_argument("--liquidation-equity-frac", type=float,
                   default=_d.liquidation_equity_frac,
                   help="Equity floor as a fraction of initial_capital.  "
                        "When > 0 the backtester forces position to 0 once "
                        "equity drops to this level (mirrors a margin call).  "
                        "Default 0.0 disables the floor.")
    p.add_argument("--slippage-bps", type=float,
                   default=_d.slippage_bps,
                   help="Linear slippage charged on |delta|·px alongside "
                        "fees, in basis points.  Use ~1-2 bps for liquid "
                        "majors as a starting point.  Default 0.0.")
    p.add_argument("--slippage-per-unit-bps", type=float,
                   default=_d.slippage_per_unit_bps,
                   help="Quadratic / market-impact term: extra bps per unit "
                        "of |delta|/max_position_base.  Default 0.0 disables.")
    p.add_argument("--rescale-windows", action="store_true",
                   help="When changing --timeframe, rescale feature-window "
                        "lengths (eta_window, vwap_window, structure_window, "
                        "disp_window, ar_window) so wall-clock coverage stays "
                        "constant.  Recommended whenever a parameter set "
                        "tuned at one timeframe is applied at another.")


def _add_optimise_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pop-size", type=int, default=20)
    p.add_argument("--generations", type=int, default=12)
    p.add_argument("--mutation-rate", type=float, default=0.3)
    p.add_argument("--crossover-rate", type=float, default=0.8)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--top-n", type=int, default=5,
                   help="Number of Pareto-front individuals to write full reports for")
    p.add_argument("--timeframes", nargs="+",
                   default=["5m", "15m", "30m", "1h"],
                   choices=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"],
                   help="Timeframes to include in multi-timeframe optimisation. "
                        "Wave operates at 5s–minute cadence; include 1m/5m for best results.")
    p.add_argument("--no-multi-timeframe", action="store_true",
                   help="Disable 1m data caching; data must already be at target resolution")
    p.add_argument("--max-opt-bars", type=int, default=20_000,
                   help="Cap bars used per GA evaluation (0 = use all). "
                        "Final top-N reports always use the full dataset. "
                        "Default 20 000 keeps each eval fast at fine timeframes "
                        "(5m × 20 000 bars ≈ 10 weeks).")


# ────────────────────────────────────────────────────────────────────────
# Backtest command
# ────────────────────────────────────────────────────────────────────────

def cmd_backtest(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    params = _params_from_args(args)
    from_ms = _parse_date(args.from_time)
    to_ms = _parse_date(args.to_time)

    print(f"Loading OHLCV: {params.exchange}/{params.symbol} @ {params.timeframe}")
    try:
        ohlcv = _load_ohlcv(params.exchange, params.symbol, params.timeframe,
                             from_ms, to_ms)
    except Exception as exc:
        print(f"ERROR loading data: {exc}")
        return 1

    print(f"Bars loaded: {len(ohlcv):,}  ({ohlcv.index[0]} → {ohlcv.index[-1]})")

    tide_result = None
    if params.mode == "stacked":
        # Tide always runs at 1h (its native cadence per strategy.md §5.1).
        # WaveBacktester forward-fills Tide signals to the finer Wave bars.
        print("Stacked mode: running Tide at 1h cadence to generate Tide signals...")
        try:
            tide_result = _run_tide_inline(params, ohlcv, tide_timeframe="1h")
            print(f"Tide signals ready: {len(tide_result.bars):,} 1h bars")
        except Exception as exc:
            print(f"WARNING: Tide inline failed ({exc}); falling back to isolated mode")
            params = params.__class__(**{**params.__dict__, "mode": "isolated"})

    backtester = WaveBacktester(params, tide_result=tide_result)
    print("Running Wave backtest...")
    result = backtester.run(ohlcv)

    report = compute_wave_report(result)
    print(render_text(report))

    out_dir = Path("reports")
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    name = f"wave_{params.symbol}_{ts}"
    md_path = write_report(result, report, out_dir, name)
    print(f"\nReport written → {md_path}")
    return 0


# ────────────────────────────────────────────────────────────────────────
# Optimise command
# ────────────────────────────────────────────────────────────────────────

def cmd_optimise(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    params = _params_from_args(args)
    from_ms = _parse_date(args.from_time)
    to_ms = _parse_date(args.to_time)
    multi_tf = not getattr(args, "no_multi_timeframe", False)
    timeframes = getattr(args, "timeframes", ["1h", "4h", "12h", "1d"])

    # Load 1m data when multi-timeframe; else load at target resolution.
    load_tf = "1m" if multi_tf else params.timeframe
    print(f"Loading {'1m (multi-TF cache)' if multi_tf else load_tf} OHLCV "
          f"for {params.symbol}...")
    try:
        ohlcv_1m = _load_ohlcv(params.exchange, params.symbol, load_tf, from_ms, to_ms)
    except Exception as exc:
        print(f"ERROR loading data: {exc}")
        return 1

    print(f"Bars: {len(ohlcv_1m):,}  ({ohlcv_1m.index[0]} → {ohlcv_1m.index[-1]})")

    tide_result = None
    if params.mode == "stacked":
        try:
            # Tide's native cadence is 1h (strategy.md §5.1, §27.1).
            # Pre-compute Tide once on 1h bars; WaveBacktester will forward-fill
            # its signals to any finer Wave timeframe via merge_asof.
            print("Stacked mode: running Tide at 1h cadence to generate Tide signals...")
            tide_result = _run_tide_inline(params, ohlcv_1m, tide_timeframe="1h")
            print(f"Tide signals ready: {len(tide_result.bars):,} 1h bars")
        except Exception as exc:
            print(f"WARNING: Tide stacked setup failed ({exc}); using isolated mode")
            params = params.__class__(**{**params.__dict__, "mode": "isolated"})

    max_opt_bars = getattr(args, "max_opt_bars", 20_000)
    if max_opt_bars > 0:
        print(f"GA evaluation window: last {max_opt_bars:,} bars per timeframe "
              f"(final reports use full dataset)")

    config = WaveOptimiserConfig(
        population_size=args.pop_size,
        generations=args.generations,
        crossover_rate=args.crossover_rate,
        mutation_rate=args.mutation_rate,
        seed=args.seed,
        max_opt_bars=max_opt_bars,
        multi_timeframe=multi_tf,
        rescale_windows=getattr(args, "rescale_windows", False),
        param_space=[
            ParamSpec("eta_window", "int", 10, 120),
            ParamSpec("vwap_window", "int", 8, 96),
            ParamSpec("structure_window", "int", 8, 96),
            ParamSpec("disp_window", "int", 10, 120),
            ParamSpec("ar_window", "int", 20, 240),
            ParamSpec("disp_scale", "float", 0.01, 0.2, 3),
            ParamSpec("eta_mr_threshold", "float", 0.1, 0.5, 2),
            ParamSpec("eta_bo_threshold", "float", 0.5, 0.95, 2),
            ParamSpec("eta_neutral_threshold", "float", 0.3, 0.7, 2),
            ParamSpec("dispersion_threshold", "float", 0.1, 2.0, 3),
            ParamSpec("dispersion_critical", "float", 0.5, 3.0, 3),
            ParamSpec("ar_critical", "float", 0.6, 0.99, 2),
            ParamSpec("ar_recover", "float", 0.4, 0.9, 2),
            ParamSpec("reduced_size_fraction", "float", 0.1, 0.9, 2),
            ParamSpec("rebalance_threshold", "float", 0.0, 0.05, 4),
            ParamSpec("timeframe", "choice", choices=timeframes),
        ],
    )

    opt = WaveOptimiser(params, ohlcv_1m, config=config, tide_result=tide_result)
    print(f"Running NSGA-II: pop={config.population_size}, "
          f"gen={config.generations}, timeframes={timeframes}")
    front = opt.run()
    print("\n" + opt.format_pareto_front(front))

    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    top_n = getattr(args, "top_n", 5)
    for rank, ind in enumerate(front[:top_n], 1):
        ind_params = ind.params
        # Use full dataset for final reports (bypasses max_opt_bars cap)
        full_data = opt._get_full_data(ind_params.timeframe)
        backtester = WaveBacktester(ind_params, tide_result=tide_result)
        result = backtester.run(full_data)
        report = compute_wave_report(result)
        name = f"wave_optimise_{params.symbol}_best{rank:02d}"
        write_report(result, report, out_dir, name)
        print(f"  Individual #{rank} report → reports/{name}.md")

    return 0


# ────────────────────────────────────────────────────────────────────────
# Tide inline helper (stacked mode)
# ────────────────────────────────────────────────────────────────────────

def _run_tide_inline(wave_params: WaveStrategyParams, ohlcv: pd.DataFrame,
                     tide_timeframe: str = "1h"):
    """Run Tide at its native cadence to produce Tide signals for stacked mode.

    Tide's intended update cadence is minutes (minimum 60 000 ms per strategy.md
    §27.1), so it should always run on 1h bars regardless of the Wave timeframe.
    If ``ohlcv`` is at a finer resolution it is resampled to ``tide_timeframe``
    before being fed to the Tide backtester.
    """
    from tide.tide_backtest import TideBacktester, TideStrategyParams, SizingMode as TideSizing

    # Resample to Tide's native cadence if the supplied data is finer-grained.
    tide_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                    "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400}
    wave_sec = tide_seconds.get(wave_params.timeframe, 3600)
    target_sec = tide_seconds.get(tide_timeframe, 3600)
    if wave_sec < target_sec:
        # ohlcv is finer than Tide's cadence → resample up
        rule_map = {"1h": "1h", "4h": "4h", "12h": "12h", "1d": "1D"}
        rule = rule_map.get(tide_timeframe, tide_timeframe)
        ohlcv = ohlcv.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    tide_p = TideStrategyParams(
        symbol=wave_params.symbol,
        exchange=wave_params.exchange,
        sizing_mode=TideSizing.BASE,
        max_position_base=wave_params.max_position_base,
        initial_capital=wave_params.initial_capital,
        taker_fee_bps=wave_params.taker_fee_bps,
        maker_fee_bps=wave_params.maker_fee_bps,
        accuracy_horizons=(),
    ).with_timeframe(tide_timeframe)
    return TideBacktester(tide_p).run(ohlcv)


# ────────────────────────────────────────────────────────────────────────
# Interactive mode (called from main.py)
# ────────────────────────────────────────────────────────────────────────

def run_interactive() -> int:
    print("\n=== Wave Layer Pipeline ===")
    sub = input("Subcommand [backtest / optimise]: ").strip().lower()
    if sub not in ("backtest", "optimise"):
        print("Invalid subcommand.")
        return 1

    symbol = input("Symbol [BTCUSDT]: ").strip().upper() or "BTCUSDT"
    exchange = input("Exchange [binance]: ").strip().lower() or "binance"
    timeframe = input("Timeframe [1h]: ").strip() or "1h"
    mode = input("Mode [isolated / stacked] (default: isolated): ").strip().lower() or "isolated"
    from_time = input("From date YYYY-MM-DD (blank = all): ").strip() or None
    to_time = input("To   date YYYY-MM-DD (blank = now):  ").strip() or None
    sizing_mode = input("Sizing mode [base / usd] (default: base): ").strip().lower() or "base"
    max_pos_base = float(input("Max position base [1.0]: ").strip() or "1.0")

    argv = [
        sub,
        "--symbol", symbol,
        "--exchange", exchange,
        "--timeframe", timeframe,
        "--mode", mode,
        "--sizing-mode", sizing_mode,
        "--max-position-base", str(max_pos_base),
    ]
    if from_time:
        argv += ["--from-time", from_time]
    if to_time:
        argv += ["--to-time", to_time]

    if sub == "optimise":
        timeframes_raw = input(
            "Timeframes for optimisation (space-separated) [1h 4h 12h 1d]: "
        ).strip() or "1h 4h 12h 1d"
        argv += ["--timeframes"] + timeframes_raw.split()

    return run_cli(argv)


def run_cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="wave", description="Wave layer pipeline")
    subs = parser.add_subparsers(dest="cmd")

    bt_p = subs.add_parser("backtest")
    _add_common_args(bt_p)

    opt_p = subs.add_parser("optimise")
    _add_common_args(opt_p)
    _add_optimise_args(opt_p)

    args = parser.parse_args(argv)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "optimise":
        return cmd_optimise(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(run_cli())
