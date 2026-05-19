"""Binance REST historical OHLCV (klines) — futures USDM.

Thin HTTP fetcher that returns ``(open_time_ms, o, h, l, c, v)`` tuples
sorted oldest-first.

Mirrors the layering of ``binance_depth_rest.py``: this module owns the
HTTP layer + parsing only; conversion into caller-specific types is the
caller's responsibility.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BINANCE_FUTURES_USDM_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"

DEFAULT_KLINES_LIMIT = 200
KLINES_FETCH_TIMEOUT_S = 10.0

# Canonical interval-ms → Binance interval label.  Single source of
# truth so callers do not embed label literals in their own code
# (AGENT_STRATEGY_RULES.md §20).
_INTERVAL_MS_TO_LABEL: dict[int, str] = {
    60_000: "1m",
    180_000: "3m",
    300_000: "5m",
    900_000: "15m",
    1_800_000: "30m",
    3_600_000: "1h",
    7_200_000: "2h",
    14_400_000: "4h",
    21_600_000: "6h",
    28_800_000: "8h",
    43_200_000: "12h",
    86_400_000: "1d",
}


def interval_ms_to_label(interval_ms: int) -> Optional[str]:
    """Translate an interval in milliseconds to a Binance label.

    Returns ``None`` for unsupported intervals so callers can fail fast
    rather than issuing an HTTP request that Binance would reject.
    """
    return _INTERVAL_MS_TO_LABEL.get(int(interval_ms))


def fetch_binance_klines(
    base_url: str,
    symbol: str,
    interval_ms: int,
    *,
    limit: int = DEFAULT_KLINES_LIMIT,
    timeout: float = KLINES_FETCH_TIMEOUT_S,
    session: requests.Session | None = None,
) -> list[tuple[int, float, float, float, float, float]]:
    """Fetch historical OHLCV from Binance and return parsed candles.

    Parameters
    ----------
    base_url:
        ``BINANCE_FUTURES_USDM_KLINES_URL`` or
        ``BINANCE_SPOT_KLINES_URL`` (or compatible ``.../klines``
        endpoint).
    symbol:
        Trading pair, e.g. ``"BTCUSDT"`` (Binance expects upper case).
    interval_ms:
        Bucket duration in milliseconds; resolved to a Binance label via
        :func:`interval_ms_to_label`.
    limit:
        Maximum number of candles to fetch (Binance caps to 1500; we
        default to 200 — enough to fill ~80 visible candles at 1m plus
        headroom).
    timeout:
        Seconds for the HTTP request.
    session:
        Optional ``requests.Session`` for connection reuse.

    Returns
    -------
    list of tuples ``(open_time_ms, open, high, low, close, volume)``
    sorted oldest-first.

    Raises
    ------
    ValueError
        If ``interval_ms`` is not a Binance-supported interval.
    requests.RequestException
        On HTTP failure. Callers should wrap in try/except and treat
        network failures as a soft no-op.
    """
    interval = interval_ms_to_label(int(interval_ms))
    if interval is None:
        raise ValueError(f"Unsupported klines interval_ms: {interval_ms}")

    sym = symbol.strip().upper()
    sess = session or requests
    resp = sess.get(
        base_url,
        params={"symbol": sym, "interval": interval, "limit": int(limit)},
        timeout=timeout,
    )
    resp.raise_for_status()
    j = resp.json()

    out: list[tuple[int, float, float, float, float, float]] = []
    if not isinstance(j, list):
        return out
    for row in j:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            out.append((
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: r[0])
    return out
