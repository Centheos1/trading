"""
Command-line entry point for the Tide-overlay pipeline.

Subcommands:
    backtest   — run a single Tide-overlay backtest, write a full report.
    optimise   — multi-objective NSGA-II search over Tide parameters; write
                 a Pareto report and a per-best-individual backtest report.

Use programmatically:
    from tide.tide_cli import run_cli
    run_cli(["backtest", "--symbol", "BTCUSDT", "--timeframe", "1h"])

Or via main.py:
    python main.py
    > tide
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from tide.tide_backtest import (
    SizingMode,
    TideBacktester,
    TideStrategyParams,
    load_ohlcv,
    timeframe_to_seconds,
)
from tide.tide_optimiser import (
    TideOptimiser,
    TideOptimiserConfig,
    format_pareto_front,
)
from tide.tide_report import write_report

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _parse_date(s: str | None) -> Optional[int]:
    """Parse YYYY-MM-DD into ms-since-epoch.  ``None`` if blank/None."""
    if s is None or s == "":
        return None
    return int(datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _params_from_args(args: argparse.Namespace) -> TideStrategyParams:
    sizing = SizingMode(getattr(args, "sizing_mode", "usd"))
    horizons = tuple(getattr(args, "accuracy_horizons", [1, 2, 4, 8, 12, 24]))
    p = TideStrategyParams(
        symbol=args.symbol,
        exchange=args.exchange,
        vol_window=args.vol_window,
        eta_window=args.eta_window,
        lsi_window=args.lsi_window,
        eta_threshold=args.eta_threshold,
        bias_deadband=args.bias_deadband,
        vol_regime_thresholds=tuple(args.vol_thresholds),
        lsi_reduce_threshold=args.lsi_reduce_threshold,
        lsi_reduce_slope=args.lsi_reduce_slope,
        risk_mult_by_regime=tuple(args.risk_mult),
        es_budget_global=args.es_budget,
        update_interval_ms=args.update_interval_ms,
        initial_capital=args.initial_capital,
        max_position_usd=args.max_position_usd,
        max_position_base=getattr(args, "max_position_base", 1.0),
        sizing_mode=sizing,
        leverage=args.leverage,
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        use_taker_fees=args.use_taker_fees,
        rebalance_threshold=args.rebalance_threshold,
        accuracy_horizons=horizons,
    )
    return p.with_timeframe(args.timeframe)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--exchange", default="binance",
                   help="HDF5 exchange dataset name (default: binance)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1h",
                   choices=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"])
    p.add_argument("--from-time", default=None,
                   help="YYYY-MM-DD inclusive start; default = entire history")
    p.add_argument("--to-time", default=None,
                   help="YYYY-MM-DD inclusive end; default = now")

    # Feature pipeline
    p.add_argument("--vol-window", type=int, default=60)
    p.add_argument("--eta-window", type=int, default=60)
    p.add_argument("--lsi-window", type=int, default=240)
    p.add_argument("--eta-threshold", type=float, default=0.3)
    p.add_argument("--bias-deadband", type=float, default=0.0)
    p.add_argument("--vol-thresholds", nargs=3, type=float,
                   default=[0.4, 0.8, 1.5],
                   help="LOW/NORMAL/HIGH/CRISIS boundaries (3 ascending floats)")
    p.add_argument("--risk-mult", nargs=4, type=float,
                   default=[1.0, 0.8, 0.5, 0.0],
                   help="Multiplier for [LOW, NORMAL, HIGH, CRISIS]")
    p.add_argument("--lsi-reduce-threshold", type=float, default=1.5)
    p.add_argument("--lsi-reduce-slope", type=float, default=0.2)

    # Engine + portfolio
    p.add_argument("--es-budget", type=float, default=1000.0)
    p.add_argument("--update-interval-ms", type=int, default=60_000)
    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--max-position-usd", type=float, default=10_000.0)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--taker-fee-bps", type=float, default=4.0)
    p.add_argument("--maker-fee-bps", type=float, default=2.0)
    p.add_argument("--use-taker-fees", action="store_true", default=True)
    p.add_argument("--use-maker-fees", dest="use_taker_fees",
                   action="store_false")
    p.add_argument("--rebalance-threshold", type=float, default=0.0,
                   help="Min fractional position change to incur a rebalance")

    # Sizing mode (NEW)
    p.add_argument(
        "--sizing-mode", default="usd", choices=["usd", "base"],
        help="'usd': size by max_position_usd/price. "
             "'base': hold max_position_base units (e.g. 1 BTC). Default: usd",
    )
    p.add_argument(
        "--max-position-base", type=float, default=1.0,
        help="Max position in base-asset units (e.g. 1.0 = 1 BTC). "
             "Used when --sizing-mode=base.",
    )

    # Signal accuracy (NEW)
    p.add_argument(
        "--accuracy-horizons", nargs="+", type=int,
        default=[1, 2, 4, 8, 12, 24],
        help="Forward horizons (in bars) for signal accuracy analysis.",
    )

    # Output
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--name", default=None,
                   help="Output base name (default: tide_<symbol>_<UTC>)")
    p.add_argument("--no-plots", action="store_true",
                   help="Skip PNG plot generation")


def _add_optimise_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--population", type=int, default=20)
    p.add_argument("--generations", type=int, default=12)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--top-k", type=int, default=5,
                   help="Best-K Pareto individuals to write full reports for")
    p.add_argument(
        "--timeframes", nargs="+",
        default=["1h", "4h", "12h", "1d"],
        choices=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"],
        help="Timeframes to sweep in multi-TF optimisation. "
             "Data must be at 1m resolution for resampling to work. "
             "Default: 1h 4h 12h 1d",
    )
    p.add_argument(
        "--no-multi-timeframe", action="store_true",
        help="Disable timeframe sweeping (data is already at the target resolution).",
    )


# ────────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ────────────────────────────────────────────────────────────────────────

def cmd_backtest(args: argparse.Namespace) -> int:
    params = _params_from_args(args)
    from_ms = _parse_date(args.from_time)
    to_ms = _parse_date(args.to_time)

    logger.info(
        "[Tide] Loading %s/%s @ %s from=%s to=%s",
        args.exchange, args.symbol, args.timeframe, args.from_time, args.to_time,
    )
    df = load_ohlcv(
        args.exchange, args.symbol,
        timeframe=args.timeframe, from_time=from_ms, to_time=to_ms,
    )
    if df.empty:
        print("ERROR: no data loaded.")
        return 1
    logger.info("[Tide] Loaded %d bars (%s → %s)", len(df), df.index[0], df.index[-1])

    bt = TideBacktester(params).run(df)
    written = write_report(
        bt, out_dir=args.out_dir, name=args.name, make_plots=not args.no_plots,
    )
    print(written.summary_text)
    print(f"\nReport written:\n  text     : {written.text_path}\n"
          f"  markdown : {written.markdown_path}")
    if not args.no_plots:
        print(f"  plots    : {written.equity_path}, {written.drawdown_path}, "
              f"{written.regime_path}")
    return 0


def cmd_optimise(args: argparse.Namespace) -> int:
    from tide.tide_optimiser import ParamSpec

    base = _params_from_args(args)
    from_ms = _parse_date(args.from_time)
    to_ms = _parse_date(args.to_time)

    multi_tf = not getattr(args, "no_multi_timeframe", False)
    timeframes = getattr(args, "timeframes", ["1h", "4h", "12h", "1d"])

    # For multi-TF optimisation load raw 1m data so the optimiser can resample.
    # If multi-TF is disabled, load the target resolution directly.
    load_tf = "1m" if multi_tf else args.timeframe
    df = load_ohlcv(
        args.exchange, args.symbol,
        timeframe=load_tf, from_time=from_ms, to_time=to_ms,
    )
    logger.info(
        "[Tide-Opt] data %s/%s @ %s  bars=%d  pop=%d  gen=%d  seed=%d  "
        "multi_tf=%s tfs=%s",
        args.exchange, args.symbol, load_tf, len(df),
        args.population, args.generations, args.seed,
        multi_tf, timeframes,
    )

    # Build param space with timeframe choice if multi-TF.
    default_space = TideOptimiserConfig().param_space
    if multi_tf:
        # Replace the default timeframe spec with the user-supplied choices.
        new_space = [s for s in default_space if s.name != "timeframe"]
        new_space.append(ParamSpec("timeframe", "choice", choices=timeframes))
    else:
        new_space = [s for s in default_space if s.name != "timeframe"]

    cfg = TideOptimiserConfig(
        population_size=args.population,
        generations=args.generations,
        seed=args.seed,
        multi_timeframe=multi_tf,
        param_space=new_space,
    )
    opt = TideOptimiser(base, df, config=cfg)
    front = opt.run()
    if not front:
        print("Optimiser returned an empty Pareto front.")
        return 1

    print("\nPareto front (top by Sharpe):")
    print(format_pareto_front(front, top_k=args.top_k))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pareto_path = out_dir / (
        (args.name or f"tide_optimise_{args.symbol}") + "_pareto.txt"
    )
    pareto_path.write_text(format_pareto_front(front, top_k=len(front)) + "\n")
    print(f"\nFull Pareto front written to: {pareto_path}")

    top = opt.all_evaluated_individuals(front, top_k=args.top_k)
    for i, ind in enumerate(top, 1):
        ind_data = opt._get_data(ind.params.timeframe)
        bt = TideBacktester(ind.params).run(ind_data)
        sub_name = (
            f"{args.name or 'tide_optimise_' + args.symbol}_best{i:02d}"
        )
        written = write_report(
            bt, out_dir=args.out_dir, name=sub_name, make_plots=not args.no_plots,
        )
        print(f"  best #{i} [{ind.params.timeframe}] → {written.markdown_path}")
    return 0


# ────────────────────────────────────────────────────────────────────────
# Top-level dispatch (also exposed for main.py)
# ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tide",
        description="Tide-layer backtest, optimisation, and reporting.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest", help="Run a single Tide overlay backtest.")
    _add_common_args(bt)
    bt.set_defaults(func=cmd_backtest)
    op = sub.add_parser("optimise", help="NSGA-II search over Tide parameters.")
    _add_common_args(op)
    _add_optimise_args(op)
    op.set_defaults(func=cmd_optimise)
    return p


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def run_interactive() -> int:
    """Prompt-driven entry called from ``main.py`` when the user picks 'tide'.

    Mirrors the existing interactive style used by other modes in
    ``main.py`` (no argparse at the user-facing layer).
    """
    print("\n=== Tide overlay pipeline ===")
    while True:
        sub = input(
            "Choose a Tide subcommand (backtest / optimise): "
        ).strip().lower() or "backtest"
        if sub in {"backtest", "optimise"}:
            break

    exchange = input("Exchange [binance]: ").strip().lower() or "binance"
    symbol = input("Symbol [BTCUSDT]: ").strip().upper() or "BTCUSDT"
    tf = input("Timeframe (1m/5m/15m/30m/1h/4h/12h/1d) [1h]: ").strip().lower() \
        or "1h"
    from_time = input("From (yyyy-mm-dd, blank = all): ").strip() or None
    to_time = input("To   (yyyy-mm-dd, blank = now): ").strip() or None

    sizing = input(
        "Sizing mode (usd / base) [base for 1-BTC proxy]: "
    ).strip().lower() or "base"
    if sizing not in ("usd", "base"):
        sizing = "base"
    max_base = input("Max position in BTC [1.0]: ").strip() or "1.0"

    argv: list[str] = [
        sub,
        "--exchange", exchange,
        "--symbol", symbol,
        "--timeframe", tf,
        "--sizing-mode", sizing,
        "--max-position-base", max_base,
    ]
    if from_time:
        argv += ["--from-time", from_time]
    if to_time:
        argv += ["--to-time", to_time]

    if sub == "optimise":
        pop = input("Population [20]: ").strip() or "20"
        gens = input("Generations [12]: ").strip() or "12"
        seed = input("Seed [42]: ").strip() or "42"
        topk = input("Top-K best to write reports for [5]: ").strip() or "5"
        tfs_raw = input(
            "Timeframes to sweep [1h 4h 12h 1d]: "
        ).strip() or "1h 4h 12h 1d"
        argv += [
            "--population", pop,
            "--generations", gens,
            "--seed", seed,
            "--top-k", topk,
            "--timeframes"] + tfs_raw.split()

    return run_cli(argv)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )
    sys.exit(run_cli())
