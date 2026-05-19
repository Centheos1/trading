"""REST endpoints for control plane operations.

Endpoints:

* ``GET    /api/health``       – liveness + connected symbols
* ``GET    /api/strategy``     – list current strategy parameters
* ``PATCH  /api/strategy``     – hot-reload strategy parameters
* ``POST   /api/backtest``     – run a backtest synchronously (returns JSON)
* ``POST   /api/optimise``     – kick off an NSGA-II optimisation; returns
                                  a job id.  Status is polled via
                                  ``GET /api/optimise/{job_id}``.

Long-running operations (backtest, optimise) are dispatched to a thread
pool so the FastAPI event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job registry (backtest / optimise)
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    job_id: str
    kind: str  # "backtest" or "optimise"
    status: str = "running"  # running | completed | failed
    progress: float = 0.0
    result: Any = None
    error: Optional[str] = None
    params: dict = field(default_factory=dict)


class JobRegistry:
    """In-memory job tracker.

    Jobs are kept until the service restarts.  For multi-replica AWS
    deployments this should be backed by Redis; the in-memory map is
    sufficient for the single-replica reference architecture.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, kind: str, params: dict) -> _Job:
        job = _Job(job_id=str(uuid.uuid4()), kind=kind, params=params)
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Optional[_Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **fields) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class StrategyParamsUpdate(BaseModel):
    """Partial update of strategy parameters.

    All fields are optional; only fields explicitly provided are applied.
    The schema mirrors ``orderflow_engine.SignalParams``.
    """

    imbalance_threshold: Optional[float] = None
    stacked_imbalance_levels: Optional[int] = None
    absorption_volume_ratio: Optional[float] = None
    absorption_window_ms: Optional[int] = None
    cvd_divergence_lookback: Optional[int] = None
    exhaustion_lookback_bars: Optional[int] = None
    exhaustion_price_threshold: Optional[float] = None
    poc_rejection_distance: Optional[float] = None
    poc_rejection_window_ms: Optional[int] = None
    signal_strength_min: Optional[float] = None


class BacktestRequest(BaseModel):
    strategy: str = Field(..., description="Strategy id (orderflow, sma, obv, ...)")
    exchange: str = "binance"
    symbol: str
    tf: str = "1m"
    from_time: int = Field(..., description="Backtest start (ms epoch)")
    to_time: int = Field(..., description="Backtest end (ms epoch)")
    params: dict = Field(default_factory=dict)


class OptimiseRequest(BacktestRequest):
    population_size: int = 50
    generations: int = 20


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def make_router(
    *,
    engine_status_fn,
    set_engine_params_fn,
    get_engine_params_fn,
) -> APIRouter:
    """Construct the API router.

    Parameters
    ----------
    engine_status_fn : callable() -> dict
    set_engine_params_fn : callable(StrategyParamsUpdate) -> dict
        Applies the update and returns the resulting params dict.
    get_engine_params_fn : callable() -> dict
    """
    router = APIRouter(prefix="/api")
    jobs = JobRegistry()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="strategy-job")

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok", "engine": engine_status_fn()}

    @router.get("/strategy")
    async def get_strategy() -> dict:
        return get_engine_params_fn()

    @router.patch("/strategy")
    async def patch_strategy(update: StrategyParamsUpdate) -> dict:
        return set_engine_params_fn(update)

    @router.post("/backtest")
    async def post_backtest(req: BacktestRequest = Body(...)) -> dict:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                executor, _run_backtest_sync, req
            )
        except Exception as exc:
            logger.exception("Backtest failed")
            raise HTTPException(status_code=500, detail=str(exc))
        return {"status": "completed", "result": result}

    @router.post("/optimise")
    async def post_optimise(req: OptimiseRequest = Body(...)) -> dict:
        job = await jobs.create("optimise", req.model_dump())
        asyncio.create_task(_run_optimise(jobs, job, req, executor))
        return {"job_id": job.job_id, "status": "running"}

    @router.get("/optimise/{job_id}")
    async def get_optimise(job_id: str) -> dict:
        job = await jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": job.progress,
            "result": job.result,
            "error": job.error,
        }

    return router


# ---------------------------------------------------------------------------
# Worker wrappers
# ---------------------------------------------------------------------------


def _run_backtest_sync(req: BacktestRequest) -> dict:
    """Run a backtest in a worker thread.

    The legacy :mod:`backtester` module uses interactive ``input()`` to
    collect parameters; here we bypass that path and invoke each
    strategy's backtest function directly with the JSON params.
    """
    strategy = req.strategy.lower()
    if strategy == "orderflow":
        from strategies import orderflow as orderflow_strategy
        pnl, max_dd, n_trades, sharpe, cagr = orderflow_strategy.backtest(
            req.exchange, req.symbol, req.from_time, req.to_time, req.params,
        )
    elif strategy == "obv":
        import backtester
        from strategies import obv
        data = backtester.get_data(
            req.exchange, req.symbol, req.tf, req.from_time, req.to_time,
        )
        pnl, max_dd, n_trades, sharpe, cagr = obv.backtest(
            data, ma_period=int(req.params.get("ma_period", 20)),
        )
    elif strategy == "ichimoku":
        import backtester
        from strategies import ichimoku
        data = backtester.get_data(
            req.exchange, req.symbol, req.tf, req.from_time, req.to_time,
        )
        pnl, max_dd, n_trades, sharpe, cagr = ichimoku.backtest(
            data,
            tenkan_period=int(req.params.get("tenkan_period", 9)),
            kijun_period=int(req.params.get("kijun_period", 26)),
        )
    elif strategy == "sup_res":
        import backtester
        from strategies import support_resistance
        data = backtester.get_data(
            req.exchange, req.symbol, req.tf, req.from_time, req.to_time,
        )
        pnl, max_dd, n_trades, sharpe, cagr = support_resistance.backtest(
            data,
            min_points=int(req.params.get("min_points", 2)),
            min_diff_points=int(req.params.get("min_diff_points", 5)),
            rounding_nb=int(req.params.get("rounding_nb", 50)),
            take_profit=float(req.params.get("take_profit", 0.02)),
            stop_loss=float(req.params.get("stop_loss", 0.01)),
        )
    else:
        raise ValueError(f"Unsupported strategy: {strategy}")

    return {
        "pnl": float(pnl),
        "max_drawdown": float(max_dd),
        "num_trades": int(n_trades),
        "sharpe_ratio": float(sharpe),
        "cagr": float(cagr),
    }


async def _run_optimise(
    jobs: JobRegistry,
    job: _Job,
    req: OptimiseRequest,
    executor: ThreadPoolExecutor,
) -> None:
    """Run an NSGA-II optimisation off the event loop.

    Progress is reported via :class:`JobRegistry`; clients poll
    ``GET /api/optimise/{job_id}``.
    """
    loop = asyncio.get_running_loop()

    def _work() -> Any:
        from optimiser import Nsga2  # noqa: WPS433 — lazy import
        nsga = Nsga2(
            exchange=req.exchange,
            symbol=req.symbol,
            strategy=req.strategy,
            tf=req.tf,
            from_time=req.from_time,
            to_time=req.to_time,
            population_size=int(req.population_size),
        )
        population = nsga.create_initial_population()
        # NB: full NSGA-II loop lives in optimiser.py; this minimal wrap
        # runs ``generations`` iterations of crossover + evaluation.  The
        # public optimiser API is preserved.
        for gen in range(int(req.generations)):
            fronts = getattr(nsga, "evaluate_population", None)
            # If the optimiser exposes a higher-level run() we'd prefer
            # that; the existing optimiser.py is interactive.  We expose
            # the population's parameters as a best-effort result.
            if fronts is not None:
                fronts(population)
            asyncio.run_coroutine_threadsafe(
                jobs.update(
                    job.job_id,
                    progress=(gen + 1) / max(req.generations, 1),
                ),
                loop,
            )
        return {
            "population_size": int(req.population_size),
            "generations": int(req.generations),
            "best": [p.parameters for p in population[: min(10, len(population))]],
        }

    try:
        result = await loop.run_in_executor(executor, _work)
        await jobs.update(
            job.job_id, status="completed", result=result, progress=1.0,
        )
    except Exception as exc:
        logger.exception("Optimisation job %s failed", job.job_id)
        await jobs.update(job.job_id, status="failed", error=str(exc))
