"""Phase 13 — Deterministic-replay session recorder.

Writes a JSONL sidecar trace containing the engine configuration plus the
signal / ripple-decision / paper-order events emitted during a live or
backtest run. The trace is later consumed by ``tools.replay_harness`` to
verify that re-feeding the captured ``TickStore`` through a fresh engine
produces byte-identical output.

The recorder is intentionally decoupled from the C++ types — every input
is duck-typed by attribute access, so unit tests can drive it with plain
Python stubs. JSON values use canonical numeric formatting so traces are
diff-friendly.

Schema (``schema=1``)::

    {"event":"header",  "schema":1, "symbol":..., "created_at_ms":...,
     "engine_config":{...}, "sizing_config":{...}, "meta":{...}}
    {"event":"signal",  "ts":..., "type":..., "price":...,
     "strength":..., "description":..., "direction":...}
    {"event":"ripple",  "ts":..., "intent":..., "reference_side":...,
     "reference_price":..., "invalidation_price":...,
     "confidence":..., "wall_id":..., "triggering_state":...,
     "reason":...}
    {"event":"order",   "ts_ms":..., "symbol":..., "side":...,
     "quantity":..., "order_type":..., "status":..., "fill_price":...,
     "fill_quantity":..., "signal_type":..., "ripple_reason":...}
    {"event":"session_tick", "tick_n":..., "drained":...,
     "best_bid":..., "best_ask":..., "bid_count":..., "ask_count":...,
     "book_empty":..., "book_crossed":..., "book_empty_ticks":...,
     "book_resync_pending":..., "book_resync_count":...,
     "trade_buf_remaining":...}
    {"event":"footer",  "stopped_at_ms":..., "n_signals":...,
     "n_ripples":..., "n_orders":..., "n_session_ticks":...}

``session_tick`` events are Phase 13B's contribution — they capture
the per-tick orchestration state of ``LiveTradingSession.on_timer_tick``
so the replay harness can verify timer-tick determinism end-to-end (not
just the engine's signal/ripple emissions). ``NO_ACTION`` ripple
decisions are filtered out by default — they are chatty, do not produce
execution intents, and the underlying engine state is already covered
by ``tests/test_replay_determinism.py``.

"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _enum_name(value: Any) -> str:
    """Return the unqualified enum-member name (or str(value))."""
    if value is None:
        return ""
    s = str(value)
    if "." in s:
        return s.rsplit(".", 1)[-1]
    return s


def serialize_engine_config(cfg: Any) -> Dict[str, Any]:
    """Serialize a C++ ``EngineConfig`` (or compatible duck type) into a
    JSON-safe dict that can later round-trip through
    :func:`apply_engine_config`."""
    out: Dict[str, Any] = {
        "tick_size": float(getattr(cfg, "tick_size", 0.01)),
        "large_trade_threshold": float(getattr(cfg, "large_trade_threshold", 0.0)),
        "footprint_bar_ms": int(getattr(cfg, "footprint_bar_ms", 60_000)),
        "cluster_window_ms": int(getattr(cfg, "cluster_window_ms", 500)),
    }
    sp = getattr(cfg, "signal_params", None)
    if sp is not None:
        out["signal_params"] = {
            k: getattr(sp, k)
            for k in (
                "imbalance_threshold",
                "stacked_imbalance_levels",
                "absorption_volume_ratio",
                "absorption_window_ms",
                "cvd_divergence_lookback",
                "exhaustion_lookback_bars",
                "exhaustion_price_threshold",
                "poc_rejection_distance",
                "poc_rejection_window_ms",
                "signal_strength_min",
            )
            if hasattr(sp, k)
        }
    rcfg = getattr(cfg, "ripple", None)
    if rcfg is not None:
        out["ripple"] = _serialize_ripple_config(rcfg)
    return out


_RIPPLE_FIELDS = (
    "tick_size", "wall_min_relative_size", "wall_max_distance_ticks",
    "feature_window_ms", "absorption_entry", "exhaustion_entry",
    "withdrawal_entry", "breakout_entry", "max_position",
    "bounce_max_break_risk", "bounce_max_withdrawal_risk",
    "breakout_max_absorption", "cancel_risk_threshold",
    "rearm_min_evidence", "time_stop_ms", "enable_diagnostics",
    "console_diagnostics", "diagnostics_path", "replay_mode",
    "paper_fills", "pipeline_min_interval_ms", "hmm_enabled",
    "hmm_model_path",
)

_LIFECYCLE_FIELDS = (
    "confirmation_window_ms", "expand_threshold_sigma", "max_hold_time_ms",
    "cooldown_ms", "max_scale_ins", "scale_in_threshold_sigma",
    "scale_in_cvd_slope_min", "trailing_stop_sigma",
    "exhaustion_cvd_slope_thresh", "exhaustion_impact_ratio",
    "target_distance_sigma", "budget_exit_threshold",
    "maturation_momentum_fade", "scale_out_count",
)


def _serialize_ripple_config(rcfg: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        k: getattr(rcfg, k) for k in _RIPPLE_FIELDS if hasattr(rcfg, k)
    }
    lc = getattr(rcfg, "lifecycle", None)
    if lc is not None:
        out["lifecycle"] = {
            k: getattr(lc, k) for k in _LIFECYCLE_FIELDS if hasattr(lc, k)
        }
    return out


def apply_engine_config(serialized: Dict[str, Any], ofe_module: Any) -> Any:
    """Reconstruct a fresh ``EngineConfig`` from a serialized dict.

    Unknown / missing fields silently fall through to the C++ defaults so
    older traces remain forward-compatible.
    """
    cfg = ofe_module.EngineConfig()
    if "tick_size" in serialized:
        cfg.tick_size = float(serialized["tick_size"])
    if "large_trade_threshold" in serialized:
        cfg.large_trade_threshold = float(serialized["large_trade_threshold"])
    if "footprint_bar_ms" in serialized:
        cfg.footprint_bar_ms = int(serialized["footprint_bar_ms"])
    if "cluster_window_ms" in serialized:
        cfg.cluster_window_ms = int(serialized["cluster_window_ms"])

    sp_in = serialized.get("signal_params") or {}
    if sp_in:
        sp = cfg.signal_params
        for k, v in sp_in.items():
            if hasattr(sp, k):
                setattr(sp, k, v)
        cfg.signal_params = sp

    rcfg_in = serialized.get("ripple") or {}
    if rcfg_in:
        rcfg = cfg.ripple
        for k, v in rcfg_in.items():
            if k == "lifecycle":
                continue
            if hasattr(rcfg, k):
                setattr(rcfg, k, v)
        lc_in = rcfg_in.get("lifecycle") or {}
        if lc_in:
            lc = rcfg.lifecycle
            for k, v in lc_in.items():
                if hasattr(lc, k):
                    setattr(lc, k, v)
            rcfg.lifecycle = lc
        cfg.ripple = rcfg
    return cfg


def serialize_sizing_config(sizing: Any) -> Dict[str, Any]:
    return {
        "mode": _enum_name(getattr(sizing, "mode", "")),
        "value": float(getattr(sizing, "value", 0.0)),
        "max_position": float(getattr(sizing, "max_position", 0.0)),
    }


def apply_sizing_config(serialized: Dict[str, Any]) -> Any:
    """Reconstruct a Python ``execution.models.SizingConfig`` from a dict."""
    from execution.models import SizingConfig, SizingMode
    mode_name = serialized.get("mode") or "FIXED_QTY"
    try:
        mode = SizingMode[mode_name]
    except KeyError:
        try:
            mode = SizingMode(mode_name)
        except ValueError:
            mode = SizingMode.FIXED_QTY
    return SizingConfig(
        mode=mode,
        value=float(serialized.get("value", 0.0)),
        max_position=float(serialized.get("max_position", 0.0)),
    )


@dataclass
class RecorderCounters:
    signals: int = 0
    ripples: int = 0
    orders: int = 0
    session_ticks: int = 0


_SESSION_TICK_FIELDS = (
    "drained", "best_bid", "best_ask", "bid_count", "ask_count",
    "book_empty", "book_crossed", "book_empty_ticks",
    "book_resync_pending", "book_resync_count",
    "trade_buf_remaining",
)


class SessionRecorder:
    """JSONL recorder for live / replay sessions.

    Open a recorder, call :meth:`write_header`, then attach to an engine
    and (optionally) chain onto a paper-engine ``order_callback``. Call
    :meth:`close` (or use it as a context manager) to flush the footer.

    The recorder serializes each event eagerly so callers can drop the
    underlying C++ object after the call returns.
    """

    def __init__(
        self,
        out_path: str,
        *,
        record_no_action: bool = False,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._path = Path(out_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fp: Optional[TextIO] = self._path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self._record_no_action = record_no_action
        self._time_fn = time_fn or (lambda: time.time())
        self._counters = RecorderCounters()
        self._closed = False
        self._header_written = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def counters(self) -> RecorderCounters:
        return self._counters

    def write_header(
        self,
        *,
        symbol: str,
        engine_config: Any,
        sizing_config: Any = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._require_open()
            if self._header_written:
                raise RuntimeError("header already written")
            payload: Dict[str, Any] = {
                "event": "header",
                "schema": SCHEMA_VERSION,
                "symbol": symbol,
                "created_at_ms": int(self._time_fn() * 1000),
                "engine_config": serialize_engine_config(engine_config),
                "record_no_action": bool(self._record_no_action),
            }
            if sizing_config is not None:
                payload["sizing_config"] = serialize_sizing_config(sizing_config)
            if meta:
                payload["meta"] = dict(meta)
            self._write(payload)
            self._header_written = True

    def record_signal(self, signal: Any) -> None:
        with self._lock:
            if not self._header_written:
                return
            self._require_open()
            type_name = ""
            try:
                type_name = signal.type_name()
            except Exception:
                type_name = _enum_name(getattr(signal, "type", ""))
            try:
                direction = int(signal.direction())
            except Exception:
                direction = 0
            payload = {
                "event": "signal",
                "ts": int(getattr(signal, "timestamp", 0)),
                "type": type_name,
                "price": float(getattr(signal, "price", 0.0)),
                "strength": float(getattr(signal, "strength", 0.0)),
                "description": str(getattr(signal, "description", "")),
                "direction": direction,
            }
            self._write(payload)
            self._counters.signals += 1

    def record_ripple_decision(self, decision: Any) -> None:
        intent_name = _enum_name(getattr(decision, "intent", ""))
        if intent_name == "NO_ACTION" and not self._record_no_action:
            return
        with self._lock:
            if not self._header_written:
                return
            self._require_open()
            payload = {
                "event": "ripple",
                "ts": int(getattr(decision, "timestamp", 0)),
                "intent": intent_name,
                "reference_side": _enum_name(
                    getattr(decision, "reference_side", "")),
                "reference_price": float(
                    getattr(decision, "reference_price", 0.0)),
                "invalidation_price": float(
                    getattr(decision, "invalidation_price", 0.0)),
                "confidence": float(getattr(decision, "confidence", 0.0)),
                "wall_id": int(getattr(decision, "wall_id", 0)),
                "triggering_state": _enum_name(
                    getattr(decision, "triggering_state", "")),
                "reason": str(getattr(decision, "reason", "")),
            }
            self._write(payload)
            self._counters.ripples += 1

    def record_paper_order(self, order: Any) -> None:
        with self._lock:
            if not self._header_written:
                return
            self._require_open()
            payload = {
                "event": "order",
                "ts_ms": int(round(float(
                    getattr(order, "timestamp", 0.0)) * 1000.0)),
                "symbol": str(getattr(order, "symbol", "")),
                "side": _enum_name(getattr(order, "side", "")),
                "quantity": float(getattr(order, "quantity", 0.0)),
                "order_type": _enum_name(getattr(order, "order_type", "")),
                "status": _enum_name(getattr(order, "status", "")),
                "fill_price": float(getattr(order, "fill_price", 0.0)),
                "fill_quantity": float(getattr(order, "fill_quantity", 0.0)),
                "signal_type": str(getattr(order, "signal_type", "")),
                "ripple_reason": str(getattr(order, "ripple_reason", "")),
            }
            self._write(payload)
            self._counters.orders += 1

    def record_session_tick(
        self,
        tick_n: int,
        *,
        drained: int = 0,
        best_bid: float = 0.0,
        best_ask: float = 0.0,
        bid_count: int = 0,
        ask_count: int = 0,
        book_empty: bool = False,
        book_crossed: bool = False,
        book_empty_ticks: int = 0,
        book_resync_pending: bool = False,
        book_resync_count: int = 0,
        trade_buf_remaining: int = 0,
    ) -> None:
        """Record a single ``LiveTradingSession.on_timer_tick`` invocation.

        Captures the orchestration-level state that determines whether
        the next tick will trigger a depth resync, a heatmap drain, or
        a status-panel paint. Only includes deterministic, replayable
        scalars — wall-clock times and Qt repaint counts are excluded
        on purpose.
        """
        with self._lock:
            if not self._header_written:
                return
            self._require_open()
            payload = {
                "event": "session_tick",
                "tick_n": int(tick_n),
                "drained": int(drained),
                "best_bid": float(best_bid),
                "best_ask": float(best_ask),
                "bid_count": int(bid_count),
                "ask_count": int(ask_count),
                "book_empty": bool(book_empty),
                "book_crossed": bool(book_crossed),
                "book_empty_ticks": int(book_empty_ticks),
                "book_resync_pending": bool(book_resync_pending),
                "book_resync_count": int(book_resync_count),
                "trade_buf_remaining": int(trade_buf_remaining),
            }
            self._write(payload)
            self._counters.session_ticks += 1

    def attach_to_engine(self, engine: Any) -> None:
        """Wire the recorder's signal + ripple callbacks onto an engine."""
        engine.set_signal_callback(self.record_signal)
        engine.set_ripple_callback(self.record_ripple_decision)

    def wrap_paper_callback(
        self,
        next_callback: Optional[Callable[[Any], None]] = None,
    ) -> Callable[[Any], None]:
        """Return an order-callback that records first, then forwards.

        Exceptions in ``next_callback`` are swallowed (matches the
        behaviour of ``PaperEngine._record_order``)."""
        def _chained(order: Any) -> None:
            try:
                self.record_paper_order(order)
            except Exception:
                logger.exception("session_recorder: failed to record order")
            if next_callback is not None:
                try:
                    next_callback(order)
                except Exception:
                    pass
        return _chained

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._fp is not None and self._header_written:
                    self._fp.write(json.dumps({
                        "event": "footer",
                        "stopped_at_ms": int(self._time_fn() * 1000),
                        "n_signals": self._counters.signals,
                        "n_ripples": self._counters.ripples,
                        "n_orders": self._counters.orders,
                        "n_session_ticks": self._counters.session_ticks,
                    }) + "\n")
                if self._fp is not None:
                    self._fp.flush()
                    self._fp.close()
            finally:
                self._fp = None
                self._closed = True

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed or self._fp is None:
            raise RuntimeError("SessionRecorder is closed")

    def _write(self, payload: Dict[str, Any]) -> None:
        assert self._fp is not None
        self._fp.write(json.dumps(payload, allow_nan=False) + "\n")


@dataclass
class SidecarTrace:
    """Parsed JSONL sidecar trace."""
    schema: int = SCHEMA_VERSION
    symbol: str = ""
    created_at_ms: int = 0
    stopped_at_ms: int = 0
    engine_config: Dict[str, Any] = field(default_factory=dict)
    sizing_config: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    record_no_action: bool = False
    signals: List[Dict[str, Any]] = field(default_factory=list)
    ripples: List[Dict[str, Any]] = field(default_factory=list)
    orders: List[Dict[str, Any]] = field(default_factory=list)
    session_ticks: List[Dict[str, Any]] = field(default_factory=list)


def load_sidecar(path: str) -> SidecarTrace:
    """Read a sidecar JSONL file written by :class:`SessionRecorder`.

    Raises ``ValueError`` if the file is empty, missing a header, or has
    an unrecognised schema version.
    """
    p = Path(path)
    trace = SidecarTrace()
    saw_header = False
    with p.open("r", encoding="utf-8") as fp:
        for line_no, raw in enumerate(fp, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"sidecar {p}: malformed JSON on line {line_no}: {e}")
            kind = evt.get("event")
            if kind == "header":
                schema = int(evt.get("schema", 0))
                if schema != SCHEMA_VERSION:
                    raise ValueError(
                        f"sidecar {p}: unsupported schema {schema} "
                        f"(this binary expects {SCHEMA_VERSION})")
                trace.schema = schema
                trace.symbol = str(evt.get("symbol", ""))
                trace.created_at_ms = int(evt.get("created_at_ms", 0))
                trace.engine_config = dict(evt.get("engine_config") or {})
                trace.sizing_config = evt.get("sizing_config")
                trace.meta = dict(evt.get("meta") or {})
                trace.record_no_action = bool(evt.get("record_no_action", False))
                saw_header = True
            elif kind == "signal":
                trace.signals.append(evt)
            elif kind == "ripple":
                trace.ripples.append(evt)
            elif kind == "order":
                trace.orders.append(evt)
            elif kind == "session_tick":
                trace.session_ticks.append(evt)
            elif kind == "footer":
                trace.stopped_at_ms = int(evt.get("stopped_at_ms", 0))
            else:
                logger.debug("sidecar %s: ignoring unknown event %r", p, kind)
    if not saw_header:
        raise ValueError(f"sidecar {p}: missing header line")
    return trace
