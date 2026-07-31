"""Headless OHLCV historical-data collector.

Two-phase, long-lived process:

  Phase 1 — Backfill (once, may take many hours)
    For each (exchange, symbol):
        last_ts = ohlcv_store.get_last_timestamp(exchange, symbol)
        from_ts = last_ts + tf_ms  if last_ts else --from-date
        fetch all 1m candles in [from_ts, now), append to Parquet store

  Phase 2 — Continuous loop (runs forever, default)
    Every --poll-interval seconds:
        for each (exchange, symbol) in parallel (--workers N):
            fetch candles since last_ts, append, update last_ts

The Parquet store is configurable and yearly-partitioned:
  DATA_STORE=local_parquet (default) →  data/ohlcv/{exchange}/{symbol}/1m/{year}.parquet
  DATA_STORE=s3                       →  s3://{S3_BUCKET}/ohlcv/{exchange}/{symbol}/1m/{year}.parquet

The process is fully restartable: backfill runs year-by-year and flushes each
year before moving on, and on restart the read of ``last_ts`` from the Parquet
store guarantees it resumes from the right point (losing at most the current
in-progress year, which is re-fetched and de-duplicated on append).

Usage:
    # Local dev — backfill BTCUSDT from Binance to local Parquet, then exit
    python collect_ohlcv.py --exchange binance --symbols BTCUSDT --mode backfill

    # EC2 — backfill everything from 2020-01-01 then run forever
    python collect_ohlcv.py --exchange all --all-symbols \
        --from-date 2020-01-01 --s3-bucket trading-data-centheos
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import zip_longest
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ohlcv_store import OhlcvStore, get_ohlcv_store


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(level: str, log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG)
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "collect_ohlcv.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Suppress chatty AWS SDK internals that log one "Found credentials"
    # line per worker per S3 call — during backfill this generates thousands
    # of lines and is the primary cause of container log disk exhaustion.
    for noisy in (
        "aiobotocore",
        "aiobotocore.credentials",
        "botocore",
        "botocore.credentials",
        "s3transfer",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger("collect_ohlcv")


# ---------------------------------------------------------------------------
# Shutdown signalling
# ---------------------------------------------------------------------------

_shutdown = threading.Event()

# Per-symbol fetch retry policy. A transient API/network error should not
# leave a permanent gap in the OHLCV history, so each fetch is retried with
# exponential backoff before the (exchange, symbol) pair is skipped for this
# pass. The next pass re-reads ``last_ts`` and resumes from the same point.
_FETCH_MAX_ATTEMPTS: int = 3
_FETCH_BACKOFF_BASE_S: float = 2.0
# Inner adapter loops must not retry forever (SSL wedges OOM'd the host).
# After this many consecutive failures at one cursor, raise so
# ``_fetch_with_retries`` can bound the outer attempts and skip the symbol.
_ADAPTER_MAX_RETRIES: int = 5


def _handle_signal(signum, frame):  # noqa: ANN001
    name = signal.Signals(signum).name
    logger.info("%s received — shutting down at next safe point", name)
    _shutdown.set()


# ---------------------------------------------------------------------------
# Timeframe helpers (1m only for v1, but written to extend)
# ---------------------------------------------------------------------------

_TF_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def _year_of_ms(ts_ms: int) -> int:
    """UTC calendar year for an epoch-millisecond timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).year


def _year_start_ms(year: int) -> int:
    """Epoch-millisecond timestamp of Jan 1 00:00:00 UTC for ``year``."""
    return int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

_BINANCE_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

_OANDA_GRANULARITY = {
    "1m": "M1", "5m": "M5", "15m": "M15",
    "1h": "H1", "4h": "H4", "1d": "D",
}


def _parse_from_date(s: str) -> int:
    """Parse YYYY-MM-DD into epoch milliseconds at 00:00:00 UTC."""
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Rate limiter — simple sleep-based throttle
# ---------------------------------------------------------------------------


class RateLimiter:
    """Crude token-bucket: ensures at most ``rate_per_sec`` calls/second."""

    def __init__(self, rate_per_sec: float) -> None:
        self.min_gap = 1.0 / max(rate_per_sec, 0.001)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + self.min_gap


# ---------------------------------------------------------------------------
# Exchange adapters
# ---------------------------------------------------------------------------


class ExchangeAdapter:
    name: str = ""

    def list_symbols(self) -> List[str]:
        raise NotImplementedError

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
    ) -> List[Tuple]:
        """Return a list of OHLCV tuples for [from_ts, to_ts] inclusive."""
        raise NotImplementedError


class BinanceAdapter(ExchangeAdapter):
    """Direct REST adapter for Binance futures klines."""

    name = "binance"
    BASE = "https://fapi.binance.com"
    LIMIT = 1500
    # Conservative: Binance allows ~20 req/s on klines weight=1, but we share
    # IP with other consumers and want to leave headroom for retries.
    RATE_PER_SEC = 8.0

    def __init__(self) -> None:
        import requests
        self._requests = requests
        self._rl = RateLimiter(self.RATE_PER_SEC)

    def list_symbols(self) -> List[str]:
        self._rl.acquire()
        url = f"{self.BASE}/fapi/v1/exchangeInfo"
        resp = self._requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        symbols = [
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
        ]
        return sorted(symbols)

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
    ) -> List[Tuple]:
        interval = _BINANCE_INTERVAL[timeframe]
        rows: List[Tuple] = []
        cursor = from_ts
        fail_streak = 0
        while cursor <= to_ts and not _shutdown.is_set():
            self._rl.acquire()
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": to_ts,
                "limit": self.LIMIT,
            }
            try:
                resp = self._requests.get(
                    f"{self.BASE}/fapi/v1/klines", params=params, timeout=20
                )
                if resp.status_code == 429:
                    fail_streak += 1
                    if fail_streak > _ADAPTER_MAX_RETRIES:
                        raise RuntimeError(
                            f"Binance 429 exceeded {_ADAPTER_MAX_RETRIES} retries "
                            f"for {symbol} @ {cursor}"
                        )
                    logger.warning(
                        "Binance 429 rate-limit for %s — sleeping 60s "
                        "(%d/%d)",
                        symbol, fail_streak, _ADAPTER_MAX_RETRIES,
                    )
                    time.sleep(60)
                    continue
                resp.raise_for_status()
                fail_streak = 0
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                fail_streak += 1
                if fail_streak > _ADAPTER_MAX_RETRIES:
                    raise RuntimeError(
                        f"Binance fetch failed after {_ADAPTER_MAX_RETRIES} "
                        f"retries for {symbol} @ {cursor}: {exc}"
                    ) from exc
                logger.warning(
                    "Binance error %s for %s @ %s: %s; retrying in 10s "
                    "(%d/%d)",
                    exc.__class__.__name__,
                    symbol,
                    cursor,
                    exc,
                    fail_streak,
                    _ADAPTER_MAX_RETRIES,
                )
                time.sleep(10)
                continue

            raw = resp.json()
            if not raw:
                break
            batch: List[Tuple] = []
            for c in raw:
                batch.append(
                    (
                        int(c[0]),
                        float(c[1]),
                        float(c[2]),
                        float(c[3]),
                        float(c[4]),
                        float(c[5]),
                    )
                )
            rows.extend(batch)
            last_ts = batch[-1][0]
            if len(raw) < self.LIMIT:
                break
            cursor = last_ts + _TF_MS[timeframe]
        return rows


class OandaAdapter(ExchangeAdapter):
    """Adapter around the existing OandaClient in exchanges/oanda.py."""

    name = "oanda"
    BATCH_MINUTES = 5000
    # Oanda allows 120 req/min; stay well under.
    RATE_PER_SEC = 1.5

    def __init__(self) -> None:
        from exchanges.oanda import OandaClient
        self._client = OandaClient()
        self._rl = RateLimiter(self.RATE_PER_SEC)

    def list_symbols(self) -> List[str]:
        return list(self._client.symbols)

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
    ) -> List[Tuple]:
        granularity = _OANDA_GRANULARITY[timeframe]
        rows: List[Tuple] = []
        cursor = from_ts
        tf_ms = _TF_MS[timeframe]
        batch_ms = self.BATCH_MINUTES * 60_000

        from oandapyV20.exceptions import V20Error
        import oandapyV20.endpoints.instruments as instruments

        fail_streak = 0
        while cursor <= to_ts and not _shutdown.is_set():
            batch_end = min(cursor + batch_ms, to_ts)
            self._rl.acquire()
            params = {
                "from": _ms_to_oanda(cursor),
                "to": _ms_to_oanda(batch_end),
                "granularity": granularity,
                "price": "MBA",
            }
            try:
                r = instruments.InstrumentsCandles(
                    instrument=symbol, params=params
                )
                rv = self._client.client.request(r)
                fail_streak = 0
            except V20Error as exc:
                msg = str(exc)
                low = msg.lower()
                if "invalid value" in low or "future" in low:
                    break
                # A dense window can exceed Oanda's 5000-candle response cap.
                # Shrink the batch and retry the SAME cursor instead of looping
                # forever on an identical over-sized request.
                if "maximum value for 'count'" in low or "maximum value for count" in low:
                    if batch_ms > tf_ms:
                        batch_ms = max(tf_ms, batch_ms // 2)
                        logger.warning(
                            "Oanda count exceeded %s @ %s; shrinking batch to %d ms and retrying",
                            symbol, cursor, batch_ms,
                        )
                        continue
                    # Already at the minimum window — skip ahead one candle so
                    # we can never wedge on a single timestamp.
                    logger.error(
                        "Oanda count exceeded %s @ %s at minimum batch; skipping ahead",
                        symbol, cursor,
                    )
                    cursor = batch_end + tf_ms
                    continue
                fail_streak += 1
                if fail_streak > _ADAPTER_MAX_RETRIES:
                    raise RuntimeError(
                        f"Oanda V20Error after {_ADAPTER_MAX_RETRIES} retries "
                        f"for {symbol} @ {cursor}: {exc}"
                    ) from exc
                logger.warning(
                    "Oanda V20Error %s @ %s: %s; sleeping 10s (%d/%d)",
                    symbol,
                    cursor,
                    exc,
                    fail_streak,
                    _ADAPTER_MAX_RETRIES,
                )
                time.sleep(10)
                continue
            except Exception as exc:  # noqa: BLE001
                fail_streak += 1
                if fail_streak > _ADAPTER_MAX_RETRIES:
                    raise RuntimeError(
                        f"Oanda fetch failed after {_ADAPTER_MAX_RETRIES} "
                        f"retries for {symbol} @ {cursor}: {exc}"
                    ) from exc
                logger.warning(
                    "Oanda error %s @ %s: %s; sleeping 10s (%d/%d)",
                    symbol,
                    cursor,
                    exc,
                    fail_streak,
                    _ADAPTER_MAX_RETRIES,
                )
                time.sleep(10)
                continue

            candles = rv.get("candles", [])
            for c in candles:
                if not c.get("complete", True):
                    continue
                mid = c.get("mid") or {}
                bid = c.get("bid") or {}
                ask = c.get("ask") or {}
                try:
                    t = _oanda_ts_to_ms(c.get("time"))
                    o = float(mid.get("o"))
                    h = float(mid.get("h"))
                    lo = float(mid.get("l"))
                    cl = float(mid.get("c"))
                    v = int(c.get("volume", 0))
                    s = round(
                        float(ask.get("c", cl)) - float(bid.get("c", cl)), 5
                    )
                except (TypeError, ValueError):
                    continue
                rows.append((t, o, h, lo, cl, float(v), s))
            cursor = batch_end + tf_ms
        return rows


def _ms_to_oanda(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _oanda_ts_to_ms(s: str) -> int:
    import pandas as pd
    return int(pd.to_datetime(s).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Per-symbol fetch worker
# ---------------------------------------------------------------------------


def _resolve_from_ts(
    store: OhlcvStore,
    exchange: str,
    symbol: str,
    timeframe: str,
    default_from_ts: int,
) -> int:
    last_ts = store.get_last_timestamp(exchange, symbol, timeframe)
    if last_ts is None:
        return default_from_ts
    return int(last_ts) + _TF_MS[timeframe]


def _fetch_with_retries(
    adapter: ExchangeAdapter,
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
) -> Optional[List[Sequence[float]]]:
    """Fetch one ``[from_ts, to_ts]`` window with bounded retries.

    Returns the rows on success (possibly empty), or ``None`` if every attempt
    failed — the caller then leaves a visible gap and resumes from the same
    point on the next pass.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, _FETCH_MAX_ATTEMPTS + 1):
        if _shutdown.is_set():
            return None
        try:
            return adapter.fetch_candles(symbol, timeframe, from_ts, to_ts)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _FETCH_MAX_ATTEMPTS:
                backoff = _FETCH_BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "[%s/%s/%s] fetch failed (attempt %d/%d, retrying in %.1fs): %s",
                    adapter.name, symbol, timeframe,
                    attempt, _FETCH_MAX_ATTEMPTS, backoff, exc,
                )
                _shutdown.wait(backoff)
    # Exhausted retries. Logged at ERROR so the gap is visible to the
    # feed-health monitor / log scrapers rather than silently swallowed.
    logger.error(
        "[%s/%s/%s] fetch failed after %d attempts in [%d, %d]: %s",
        adapter.name, symbol, timeframe, _FETCH_MAX_ATTEMPTS, from_ts, to_ts, last_exc,
    )
    return None


def _collect_one(
    adapter: ExchangeAdapter,
    store: OhlcvStore,
    symbol: str,
    timeframe: str,
    default_from_ts: int,
    to_ts: int,
) -> int:
    """Fetch + persist a symbol **year-by-year** from its resume point to ``to_ts``.

    Iterating one calendar year at a time bounds a worker's peak memory to
    roughly a single year of candles and flushes progress to the store after
    each year.  A restart (or an OOM-kill of this hard-capped container) then
    loses at most the current in-progress year — it is re-fetched and
    de-duplicated on ``append`` — instead of discarding the whole multi-year
    backfill.  This is what makes the collector survive unattended for weeks
    and lets the slow Oanda feed actually land data between restarts.
    """
    if _shutdown.is_set():
        return 0
    from_ts = _resolve_from_ts(
        store, adapter.name, symbol, timeframe, default_from_ts
    )
    if from_ts > to_ts:
        logger.debug(
            "[%s/%s/%s] already up to date (last_ts=%d)",
            adapter.name, symbol, timeframe, from_ts,
        )
        return 0

    tf_ms = _TF_MS[timeframe]
    total_new = 0
    start_year = _year_of_ms(from_ts)
    end_year = _year_of_ms(to_ts)

    for year in range(start_year, end_year + 1):
        if _shutdown.is_set():
            break
        # Clamp this year's window to the overall [from_ts, to_ts] range.
        y_from = max(from_ts, _year_start_ms(year))
        y_to = min(to_ts, _year_start_ms(year + 1) - tf_ms)
        if y_from > y_to:
            continue

        t0 = time.monotonic()
        rows = _fetch_with_retries(adapter, symbol, timeframe, y_from, y_to)
        if rows is None:
            # Gap left for this year; continue so later years still persist and
            # the next pass retries this window from the same resume point.
            continue
        if not rows:
            logger.debug(
                "[%s/%s/%s] %d: no new rows", adapter.name, symbol, timeframe, year,
            )
            continue

        try:
            n = store.append(adapter.name, symbol, rows, timeframe)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[%s/%s/%s] store.append failed for %d: %s",
                adapter.name, symbol, timeframe, year, exc,
            )
            continue

        total_new += n
        dt = time.monotonic() - t0
        logger.info(
            "[%s/%s/%s] %d: %s → %s — %d new rows (%.1fs)",
            adapter.name, symbol, timeframe, year,
            datetime.fromtimestamp(y_from / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            ),
            datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            ),
            n, dt,
        )

    return total_new


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _resolve_symbols(adapter: ExchangeAdapter, requested: Optional[List[str]]) -> List[str]:
    available = adapter.list_symbols()
    if not requested:
        return available
    avail_upper = {s.upper(): s for s in available}
    out: List[str] = []
    missing: List[str] = []
    for r in requested:
        ru = r.upper()
        if ru in avail_upper:
            out.append(avail_upper[ru])
        elif r in available:
            out.append(r)
        else:
            missing.append(r)
    if missing:
        logger.warning(
            "[%s] requested symbols not available: %s",
            adapter.name, ", ".join(missing),
        )
    return out


def _run_pass(
    adapters: Sequence[ExchangeAdapter],
    symbols_by_adapter: Dict[str, List[str]],
    store: OhlcvStore,
    timeframe: str,
    default_from_ts: int,
    workers: int,
) -> int:
    """Run one full pass across all (adapter, symbol) pairs in parallel.

    Returns total rows written this pass.
    """
    to_ts = _now_ms()

    # Interleave jobs round-robin across adapters rather than queuing every
    # symbol of one exchange before the next. The thread pool dispatches jobs
    # in submission order, so a naive "all Binance, then all Oanda" ordering
    # means the ~300 Binance perpetual backfills (many hours) drain entirely
    # before a single Oanda fetch starts — and if the process restarts before
    # then (OOM, redeploy, reboot) Oanda is never reached at all. Round-robin
    # guarantees every exchange makes progress concurrently, so Oanda data
    # starts landing in S3 from the first pass.
    per_adapter_jobs: List[List[Tuple[ExchangeAdapter, str]]] = [
        [(adapter, sym) for sym in symbols_by_adapter.get(adapter.name, [])]
        for adapter in adapters
    ]
    jobs: List[Tuple[ExchangeAdapter, str]] = [
        job
        for tier in zip_longest(*per_adapter_jobs)
        for job in tier
        if job is not None
    ]

    if not jobs:
        return 0

    logger.info("Pass starting: %d symbols across %d exchanges, %d workers",
                len(jobs), len(adapters), workers)
    total = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _collect_one, adapter, store, sym, timeframe,
                default_from_ts, to_ts,
            ): (adapter.name, sym)
            for (adapter, sym) in jobs
        }
        for fut in as_completed(futures):
            if _shutdown.is_set():
                break
            try:
                n = fut.result()
                total += n
            except Exception as exc:  # noqa: BLE001
                exchange, sym = futures[fut]
                logger.error("[%s/%s] worker exception: %s", exchange, sym, exc)
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Continuous OHLCV collector (Binance + Oanda → Parquet).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--exchange",
        default="all",
        choices=["binance", "oanda", "all"],
        help="Which exchange(s) to collect from.",
    )
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols; otherwise --all-symbols is implied.",
    )
    p.add_argument(
        "--all-symbols",
        action="store_true",
        help="Discover every available symbol on each exchange.",
    )
    p.add_argument(
        "--timeframe",
        default="1m",
        choices=list(_TF_MS.keys()),
        help="Candle granularity.",
    )
    p.add_argument(
        "--from-date",
        default="2020-01-01",
        help="Earliest candle to fetch when no prior data is present (YYYY-MM-DD UTC).",
    )
    p.add_argument(
        "--mode",
        default="continuous",
        choices=["backfill", "continuous"],
        help="'backfill' = one-shot fill then exit; 'continuous' = poll forever.",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=3600,
        help="Continuous-mode sleep between passes (seconds).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("OHLCV_WORKERS", "2")),
        help="Parallel symbol fetches (default 2 — keep tick collector safe).",
    )
    p.add_argument(
        "--data-store",
        default=None,
        choices=["local_parquet", "s3"],
        help="Override DATA_STORE env var.",
    )
    p.add_argument(
        "--data-dir",
        default="data",
        help="Root directory for local Parquet files.",
    )
    p.add_argument(
        "--s3-bucket",
        default=None,
        help="S3 bucket for the s3 backend (overrides S3_BUCKET env var).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _build_adapters(exchange_arg: str) -> List[ExchangeAdapter]:
    adapters: List[ExchangeAdapter] = []
    if exchange_arg in ("binance", "all"):
        adapters.append(BinanceAdapter())
    if exchange_arg in ("oanda", "all"):
        try:
            adapters.append(OandaAdapter())
            logger.info("Oanda adapter ready")
        except EnvironmentError as exc:
            # Missing credentials — this is always a fatal configuration error.
            # In "all" mode we surface it as ERROR so it's impossible to miss,
            # and we still exit 1 to prevent silently collecting only Binance.
            logger.error(
                "Oanda adapter failed (credentials not configured): %s  "
                "Set OANDA_ACCOUNT_ID, OANDA_ACCESS_TOKEN, and "
                "OANDA_ACCOUNT_TYPE in .env and restart.",
                exc,
            )
            # Return empty list: main() will detect no adapters and exit 1.
            return []
        except Exception as exc:  # noqa: BLE001
            # Transient errors (network, API outage) — log prominently.
            logger.error(
                "Oanda adapter initialisation failed: %s  "
                "Oanda data will NOT be collected this run.",
                exc,
            )
    return adapters


def _resolve_symbols_by_adapter(
    adapters: Sequence[ExchangeAdapter],
    args: argparse.Namespace,
) -> Dict[str, List[str]]:
    requested: Optional[List[str]] = None
    if args.symbols:
        requested = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out: Dict[str, List[str]] = {}
    for adapter in adapters:
        if requested and not args.all_symbols:
            syms = _resolve_symbols(adapter, requested)
        else:
            syms = _resolve_symbols(adapter, None)
        out[adapter.name] = syms
        logger.info(
            "[%s] %d symbol(s) selected", adapter.name, len(syms),
        )
    return out


def _check_storage_safety(args: argparse.Namespace) -> None:
    """Refuse to run --all-symbols with a local store to prevent disk exhaustion.

    Collecting every Binance perpetual + all Oanda instruments at 1m
    from 2020 generates many gigabytes.  If the caller has an S3 bucket
    configured but has not set DATA_STORE=s3 they almost certainly
    intended to write to S3, not to the local disk.
    """
    backend = (
        args.data_store
        or os.environ.get("DATA_STORE")
        or "local_parquet"
    ).lower()
    is_local = backend in ("local", "local_parquet", "parquet")
    all_symbols_mode = args.all_symbols or not args.symbols
    s3_bucket_configured = bool(
        args.s3_bucket or os.environ.get("S3_BUCKET")
    )

    if is_local and all_symbols_mode:
        if s3_bucket_configured:
            logger.critical(
                "SAFETY ABORT: DATA_STORE=%s but --all-symbols is set and "
                "S3_BUCKET is configured.  Collecting all candles locally "
                "will exhaust EC2 disk.  "
                "Fix: set DATA_STORE=s3 in .env (or pass --data-store s3). "
                "Aborting.",
                backend,
            )
        else:
            logger.critical(
                "SAFETY ABORT: DATA_STORE=%s with --all-symbols and no "
                "S3_BUCKET set.  Collecting all instruments to local disk "
                "will exhaust storage.  "
                "Fix: set DATA_STORE=s3 and S3_BUCKET=<bucket> in .env, "
                "or restrict symbols with --symbols BTCUSDT,EURUSD,...  "
                "Aborting.",
                backend,
            )
        sys.exit(1)


def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_level)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _check_storage_safety(args)

    store = get_ohlcv_store(
        backend=args.data_store,
        data_dir=args.data_dir,
        s3_bucket=args.s3_bucket,
    )
    backend_name = type(store).__name__
    logger.info(
        "Backend: %s | timeframe: %s | mode: %s | poll: %ss | workers: %d",
        backend_name, args.timeframe, args.mode, args.poll_interval, args.workers,
    )

    adapters = _build_adapters(args.exchange)
    if not adapters:
        logger.error("No exchange adapters available — aborting.")
        return 1

    symbols_by_adapter = _resolve_symbols_by_adapter(adapters, args)
    default_from_ts = _parse_from_date(args.from_date)
    logger.info(
        "Default from-date: %s (%d ms)", args.from_date, default_from_ts,
    )

    logger.info("Phase 1: backfill starting")
    n_backfill = _run_pass(
        adapters,
        symbols_by_adapter,
        store,
        args.timeframe,
        default_from_ts,
        args.workers,
    )
    logger.info("Phase 1 done: %d total rows written", n_backfill)

    if args.mode == "backfill":
        logger.info("--mode backfill — exiting after Phase 1")
        return 0

    logger.info(
        "Phase 2: continuous loop (poll every %ds)", args.poll_interval,
    )
    while not _shutdown.is_set():
        next_run = time.monotonic() + args.poll_interval
        while not _shutdown.is_set() and time.monotonic() < next_run:
            time.sleep(min(5.0, max(0.0, next_run - time.monotonic())))
        if _shutdown.is_set():
            break
        logger.info("Continuous pass starting")
        try:
            n = _run_pass(
                adapters,
                symbols_by_adapter,
                store,
                args.timeframe,
                default_from_ts,
                args.workers,
            )
            logger.info("Continuous pass complete: %d new rows", n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Continuous pass crashed: %s", exc)

    logger.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
