"""Build 17:00 America/New_York daily bars from Oanda 1-minute OHLCV.

Oanda's stored history is 1-minute bars. A daily session is the interval
that ends at 17:00 New York. The bar timestamped exactly 17:00 belongs to
the session that is closing. Later bars belong to the next session. A
session with no minute bars is absent. Closures are not filled with zeros.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

NY = "America/New_York"
CLOSE_HOUR = 17
DAILY_COLUMNS = ("open", "high", "low", "close", "spread", "volume", "n_bars")


def ny_close_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Label each timestamp with the New York date of its 17:00 session close."""
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"expected a DatetimeIndex, got {type(index).__name__}")
    if len(index) == 0:
        return pd.DatetimeIndex([], name="session_date")
    if index.tz is None:
        local = index.tz_localize("UTC")
    else:
        local = index
    local = local.tz_convert(NY)
    midnight = local.normalize()
    close = midnight + pd.Timedelta(hours=CLOSE_HOUR)
    shift = pd.to_timedelta((local > close).astype(int), unit="D")
    labels = (midnight + shift).tz_localize(None)
    return pd.DatetimeIndex(labels, name="session_date")


def aggregate_to_daily(minutes: pd.DataFrame) -> pd.DataFrame:
    """Aggregate one instrument's minute bars into session bars.

    The daily close and spread are the last minute bar in the session.
    Raises if the close or spread column is missing. An empty input returns
    an empty frame with the daily columns.
    """
    missing = {"open", "high", "low", "close", "spread"} - set(minutes.columns)
    if missing:
        raise ValueError(f"minute bars are missing columns: {sorted(missing)}")
    if minutes.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS).rename_axis("session_date")
    if not isinstance(minutes.index, pd.DatetimeIndex):
        raise TypeError("minute bars must be indexed by timestamp")
    frame = minutes.sort_index()
    labels = ny_close_dates(pd.DatetimeIndex(frame.index))
    grouped = frame.groupby(labels, sort=True)
    daily = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "spread": grouped["spread"].last(),
            "volume": grouped["volume"].sum() if "volume" in frame.columns else 0.0,
            "n_bars": grouped["close"].size(),
        }
    )
    daily.index.name = "session_date"
    invalid = daily["close"].isna() | (daily["close"] <= 0) | daily["spread"].isna()
    n_invalid = int(invalid.sum())
    if n_invalid:
        logger.warning(
            "Dropped %d daily session(s) with a missing close or spread", n_invalid
        )
        daily = daily.loc[~invalid]
    return daily


def session_close_time(session_date) -> pd.Timestamp:
    """Return 17:00 America/New_York on a session date."""
    day = pd.Timestamp(session_date)
    if day.tzinfo is not None:
        day = day.tz_convert(NY).tz_localize(None)
    return day.normalize().tz_localize(NY) + pd.Timedelta(hours=CLOSE_HOUR)


def completed_sessions(
    index: pd.DatetimeIndex,
    now: Optional[pd.Timestamp] = None,
) -> pd.DatetimeIndex:
    """Keep sessions whose 17:00 New York close is already in the past.

    Bars after 17:00 belong to the next session, so a panel loaded during
    that session has a last row that has not closed. That row is not a
    daily candle and cannot be rebalanced.
    """
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(index)
    if len(index) == 0:
        return index
    if now is None:
        clock = pd.Timestamp.now(tz=NY)
    else:
        clock = pd.Timestamp(now)
        clock = clock.tz_localize(NY) if clock.tzinfo is None else clock.tz_convert(NY)
    closed = [day for day in index if session_close_time(day) <= clock]
    open_days = [day for day in index if session_close_time(day) > clock]
    if open_days:
        logger.warning(
            "Excluded open session(s) before the 17:00 New York close: %s",
            ", ".join(str(pd.Timestamp(day).date()) for day in open_days),
        )
    return pd.DatetimeIndex(closed, name=index.name)


def simple_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """Simple returns between successive observed sessions.

    A missing session is a missing row. The next return spans that gap.
    Missing closes are not replaced with zero.
    """
    if closes.empty:
        return closes.copy()
    return closes / closes.shift(1) - 1.0


def load_oanda_daily_panel(
    store,
    *,
    symbols: Optional[Sequence[str]] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    timeframe: str = "1m",
    now: Optional[pd.Timestamp] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Load Oanda minute bars and return daily close and spread panels.

    One symbol that fails to read is excluded, counted, and logged. The
    failure list is returned to the caller. If every symbol fails, or the
    symbol list is empty, this raises.
    """
    names = list(symbols) if symbols is not None else list(
        store.list_symbols("oanda", timeframe)
    )
    if not names:
        raise ValueError("Oanda symbol list is empty")
    closes: dict[str, pd.Series] = {}
    spreads: dict[str, pd.Series] = {}
    failures: list[dict] = []
    for symbol in names:
        try:
            minutes = store.read("oanda", symbol, timeframe, from_ts=from_ts, to_ts=to_ts)
        except Exception as exc:
            logger.error("Failed to read Oanda minutes for %s: %s", symbol, exc)
            failures.append({"symbol": symbol, "error": str(exc)})
            continue
        if minutes is None or minutes.empty:
            logger.error("No minute bars for %s", symbol)
            failures.append({"symbol": symbol, "error": "no minute bars"})
            continue
        try:
            daily = aggregate_to_daily(minutes)
        except Exception as exc:
            logger.error("Failed to aggregate %s: %s", symbol, exc)
            failures.append({"symbol": symbol, "error": str(exc)})
            continue
        if daily.empty:
            logger.error("No valid daily sessions for %s", symbol)
            failures.append({"symbol": symbol, "error": "no valid daily sessions"})
            continue
        closes[symbol] = daily["close"]
        spreads[symbol] = daily["spread"]
    if not closes:
        raise RuntimeError(
            f"No Oanda daily sessions were built. failures={len(failures)}"
        )
    close_panel = pd.DataFrame(closes).sort_index()
    spread_panel = pd.DataFrame(spreads).reindex(close_panel.index)
    closed = completed_sessions(pd.DatetimeIndex(close_panel.index), now=now)
    if len(closed) == 0:
        raise RuntimeError("No Oanda daily session has reached the 17:00 New York close")
    close_panel = close_panel.loc[closed]
    spread_panel = spread_panel.loc[closed]
    logger.info(
        "Built daily panels for %d symbols, excluded %d",
        close_panel.shape[1],
        len(failures),
    )
    return close_panel, spread_panel, failures
