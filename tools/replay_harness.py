"""Phase 13 — Deterministic-replay harness.

Re-runs a captured live session through a fresh ``OrderFlowEngine``
plus ``PaperEngine`` and compares the regenerated event stream against
the recorded sidecar JSONL. The goal is to prove that, given identical
trade + depth input (loaded from the captured ``TickStore``) and the
same ``EngineConfig`` / ``SizingConfig``, the strategy stack produces
byte-identical Tide / Wave / Ripple / paper-order output.

Bypasses :class:`execution.execution_manager.ExecutionManager` on
purpose — that layer uses ``time.time()`` for cooldown gating, which
breaks reproducibility under replay. The harness wires
``RippleDecision -> ExecutionIntent -> PaperEngine`` directly via
``execution.models.ripple_decision_to_intent``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.session_recorder import (
    SidecarTrace, apply_engine_config, apply_sizing_config, load_sidecar,
)

logger = logging.getLogger(__name__)


_FLOAT_TOL_DEFAULT = 0.0  # exact equality by default; override per-call


@dataclass
class Divergence:
    """First mismatch detected during a replay diff."""
    section: str          # "signals" | "ripples" | "orders" | "session_ticks" | "counts"
    index: int            # offset within the section (-1 for length mismatches)
    field: str            # field name that differs (or "<length>")
    expected: Any
    actual: Any
    detail: str = ""

    def __str__(self) -> str:
        if self.section == "counts":
            return (f"{self.section}: {self.field} expected={self.expected} "
                    f"actual={self.actual}")
        return (f"{self.section}[{self.index}].{self.field}: "
                f"expected={self.expected!r} actual={self.actual!r} "
                f"({self.detail})" if self.detail
                else f"{self.section}[{self.index}].{self.field}: "
                     f"expected={self.expected!r} actual={self.actual!r}")


@dataclass
class ReplayCounts:
    signals: int = 0
    ripples: int = 0
    orders: int = 0


@dataclass
class ReplayReport:
    """Outcome of :func:`replay_session`."""
    ok: bool
    symbol: str
    expected: ReplayCounts = field(default_factory=ReplayCounts)
    actual: ReplayCounts = field(default_factory=ReplayCounts)
    divergences: List[Divergence] = field(default_factory=list)

    def summary(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            f"replay {verdict}  symbol={self.symbol}",
            (f"  signals: expected={self.expected.signals} "
             f"actual={self.actual.signals}"),
            (f"  ripples: expected={self.expected.ripples} "
             f"actual={self.actual.ripples}"),
            (f"  orders:  expected={self.expected.orders} "
             f"actual={self.actual.orders}"),
        ]
        if self.divergences:
            lines.append(f"  first {len(self.divergences)} divergence(s):")
            for d in self.divergences[:10]:
                lines.append(f"    - {d}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Capture callbacks (mirror SessionRecorder serialization for byte-identical
# diffs against a sidecar trace)
# ---------------------------------------------------------------------------


from tools.session_recorder import _enum_name as _enum  # noqa: E402  (local re-export)


def _signal_to_dict(sig: Any) -> Dict[str, Any]:
    try:
        type_name = sig.type_name()
    except Exception:
        type_name = _enum(getattr(sig, "type", ""))
    try:
        direction = int(sig.direction())
    except Exception:
        direction = 0
    return {
        "event": "signal",
        "ts": int(getattr(sig, "timestamp", 0)),
        "type": type_name,
        "price": float(getattr(sig, "price", 0.0)),
        "strength": float(getattr(sig, "strength", 0.0)),
        "description": str(getattr(sig, "description", "")),
        "direction": direction,
    }


def _ripple_to_dict(d: Any) -> Dict[str, Any]:
    return {
        "event": "ripple",
        "ts": int(getattr(d, "timestamp", 0)),
        "intent": _enum(getattr(d, "intent", "")),
        "reference_side": _enum(getattr(d, "reference_side", "")),
        "reference_price": float(getattr(d, "reference_price", 0.0)),
        "invalidation_price": float(getattr(d, "invalidation_price", 0.0)),
        "confidence": float(getattr(d, "confidence", 0.0)),
        "wall_id": int(getattr(d, "wall_id", 0)),
        "triggering_state": _enum(getattr(d, "triggering_state", "")),
        "reason": str(getattr(d, "reason", "")),
    }


def _order_to_dict(order: Any) -> Dict[str, Any]:
    return {
        "event": "order",
        "ts_ms": int(round(float(getattr(order, "timestamp", 0.0)) * 1000.0)),
        "symbol": str(getattr(order, "symbol", "")),
        "side": _enum(getattr(order, "side", "")),
        "quantity": float(getattr(order, "quantity", 0.0)),
        "order_type": _enum(getattr(order, "order_type", "")),
        "status": _enum(getattr(order, "status", "")),
        "fill_price": float(getattr(order, "fill_price", 0.0)),
        "fill_quantity": float(getattr(order, "fill_quantity", 0.0)),
        "signal_type": str(getattr(order, "signal_type", "")),
        "ripple_reason": str(getattr(order, "ripple_reason", "")),
    }


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

# Volatile fields that legitimately drift between recorder runs (none today;
# kept for future schema evolution).
_IGNORE_FIELDS = frozenset()


def _close_enough(expected: Any, actual: Any, tol: float) -> bool:
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        if tol <= 0.0:
            return float(expected) == float(actual)
        return abs(float(expected) - float(actual)) <= tol
    return expected == actual


def _diff_records(
    section: str,
    expected: List[Dict[str, Any]],
    actual: List[Dict[str, Any]],
    tol: float,
    max_divergences: int,
) -> List[Divergence]:
    out: List[Divergence] = []
    if len(expected) != len(actual):
        out.append(Divergence(
            section=section,
            index=-1,
            field="<length>",
            expected=len(expected),
            actual=len(actual),
        ))
    for idx, (e, a) in enumerate(zip(expected, actual)):
        keys = set(e.keys()) | set(a.keys())
        keys.discard("event")
        for k in sorted(keys):
            if k in _IGNORE_FIELDS:
                continue
            ev = e.get(k)
            av = a.get(k)
            if not _close_enough(ev, av, tol):
                out.append(Divergence(
                    section=section, index=idx, field=k,
                    expected=ev, actual=av,
                ))
                if len(out) >= max_divergences:
                    return out
        if len(out) >= max_divergences:
            return out
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class SessionTickReport:
    """Outcome of :func:`verify_session_ticks` (Phase 13B)."""
    ok: bool
    expected_count: int
    actual_count: int
    divergences: List[Divergence] = field(default_factory=list)

    def summary(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            f"session-tick replay {verdict}",
            (f"  ticks: expected={self.expected_count} "
             f"actual={self.actual_count}"),
        ]
        if self.divergences:
            lines.append(f"  first {len(self.divergences)} divergence(s):")
            for d in self.divergences[:10]:
                lines.append(f"    - {d}")
        return "\n".join(lines)


def verify_session_ticks(
    expected: List[Dict[str, Any]],
    actual: List[Dict[str, Any]],
    *,
    float_tolerance: float = _FLOAT_TOL_DEFAULT,
    max_divergences: int = 25,
) -> SessionTickReport:
    """Phase 13B — verify replayed ``LiveTradingSession.on_timer_tick``
    state matches the recorded sidecar.

    Each entry must be a dict shaped like the events written by
    :meth:`SessionRecorder.record_session_tick`. Extra keys are
    permitted on either side (compared via field-set union with
    missing fields treated as ``None``).

    The ``tick_n`` field is treated as part of the field comparison so
    a missing or out-of-order tick surfaces as a divergence at the
    first mismatch index. If ``len(expected) != len(actual)`` a
    length-mismatch divergence is emitted with ``index = -1``.
    """
    divergences = _diff_records(
        "session_ticks", expected, actual,
        float_tolerance, max_divergences,
    )
    return SessionTickReport(
        ok=(not divergences) and (len(expected) == len(actual)),
        expected_count=len(expected),
        actual_count=len(actual),
        divergences=divergences,
    )


def replay_session(
    *,
    sidecar_path: str,
    tick_store_path: str,
    ofe_module: Any,
    sidecar_trace: Optional[SidecarTrace] = None,
    time_range: Optional[Tuple[int, int]] = None,
    depth_levels: int = 20,
    float_tolerance: float = _FLOAT_TOL_DEFAULT,
    max_divergences: int = 25,
    paper_engine_factory: Optional[Callable[[str, Any], Any]] = None,
    on_engine_ready: Optional[Callable[[Any], None]] = None,
) -> ReplayReport:
    """Replay a captured session and verify it matches the sidecar trace.

    Parameters
    ----------
    sidecar_path
        Path to the JSONL sidecar written by :class:`SessionRecorder`.
    tick_store_path
        Path to the HDF5 ``TickStore`` captured during the same run.
    ofe_module
        The compiled ``orderflow_engine`` module.
    sidecar_trace
        Optional preloaded trace (used by tests to skip filesystem I/O).
    time_range
        Optional ``(from_ms, to_ms)`` to scope the replay.
    depth_levels
        Number of depth levels to subscribe through ``ReplayFeed``.
    float_tolerance
        Allowed absolute difference for float comparisons (default ``0.0``
        — strict bit-identical equality).
    max_divergences
        Cap the number of divergences captured before short-circuiting.
    paper_engine_factory
        Override hook used by tests to substitute a fake paper engine.
        Receives ``(symbol, sizing_config)`` and must return an object
        exposing ``on_intent(intent)``.
    on_engine_ready
        Diagnostic hook invoked with the freshly-constructed engine
        before replay starts (after callbacks are attached).
    """
    trace = sidecar_trace or load_sidecar(sidecar_path)
    symbol = trace.symbol or ""

    cfg = apply_engine_config(trace.engine_config, ofe_module)
    engine = ofe_module.OrderFlowEngine(cfg)

    captured_signals: List[Dict[str, Any]] = []
    captured_ripples: List[Dict[str, Any]] = []
    captured_orders: List[Dict[str, Any]] = []

    engine.set_signal_callback(
        lambda sig: captured_signals.append(_signal_to_dict(sig)))

    record_no_action = trace.record_no_action

    paper = None
    if trace.sizing_config is not None:
        sizing_cfg = apply_sizing_config(trace.sizing_config)
        if paper_engine_factory is not None:
            paper = paper_engine_factory(symbol, sizing_cfg)
        else:
            from execution.paper_engine import PaperEngine
            paper = PaperEngine(
                symbol or "REPLAY",
                sizing_cfg,
                order_callback=lambda o: captured_orders.append(
                    _order_to_dict(o)),
            )
        # PaperEngine forwards via the order_callback we passed above; only
        # custom factories need an explicit wrapper.
        if paper_engine_factory is not None:
            _wrap_paper_orders(paper, captured_orders)

    from execution.models import _parse_intent_name, ripple_decision_to_intent

    def _on_ripple(decision: Any) -> None:
        intent_name = _parse_intent_name(decision)
        if intent_name == "NO_ACTION" and not record_no_action:
            return
        captured_ripples.append(_ripple_to_dict(decision))
        if paper is None:
            return
        try:
            intent = ripple_decision_to_intent(decision, intent_name=intent_name)
        except Exception:
            logger.exception("replay_harness: ripple→intent conversion failed")
            return
        if intent is None:
            return
        try:
            paper.on_intent(intent)
        except Exception:
            logger.exception("replay_harness: paper.on_intent raised")

    engine.set_ripple_callback(_on_ripple)

    if on_engine_ready is not None:
        on_engine_ready(engine)

    store = ofe_module.TickStore(tick_store_path)
    try:
        from_ts, to_ts = (
            time_range if time_range is not None else (0, 2 ** 62))
        from_ts = int(from_ts)
        to_ts = int(to_ts)
        load_symbol = symbol or ""

        # Drive events directly via process_trade / process_depth instead
        # of routing through ReplayFeed.run_sync(). The latter spawns a
        # background thread inside engine.start() *and* runs synchronously
        # via run_sync(), producing a 2× event stream that breaks
        # byte-identical replay verification.
        trades = list(store.load_trades(load_symbol, from_ts, to_ts))
        snaps = list(store.load_depth_snapshots(load_symbol, from_ts, to_ts))
        updates = list(store.load_depth_updates(load_symbol, from_ts, to_ts))

        events: List[Tuple[int, int, str, Any]] = []
        # The third tuple element is a stable secondary key used to break
        # timestamp ties deterministically (matches ReplayFeed: snapshots
        # before updates before trades is not specified, so we settle on
        # ``(ts, kind_priority)`` with depth before trades on equal ts).
        for s in snaps:
            events.append((int(s.timestamp), 0, "depth", s))
        for u in updates:
            events.append((int(u.timestamp), 1, "depth", u))
        for t in trades:
            events.append((int(t.timestamp), 2, "trade", t))
        events.sort(key=lambda x: (x[0], x[1]))

        engine.start(load_symbol)
        try:
            for _, _, kind, evt in events:
                if kind == "trade":
                    engine.process_trade(evt)
                else:
                    engine.process_depth(evt)
        finally:
            engine.stop()

        _ = depth_levels  # depth_levels reserved for future ReplayFeed path
    finally:
        try:
            store.close()
        except Exception:
            pass

    actual = ReplayCounts(
        signals=len(captured_signals),
        ripples=len(captured_ripples),
        orders=len(captured_orders),
    )
    expected = ReplayCounts(
        signals=len(trace.signals),
        ripples=len(trace.ripples),
        orders=len(trace.orders),
    )

    divergences: List[Divergence] = []
    divergences.extend(_diff_records(
        "signals", trace.signals, captured_signals,
        float_tolerance, max_divergences - len(divergences)))
    if len(divergences) < max_divergences:
        divergences.extend(_diff_records(
            "ripples", trace.ripples, captured_ripples,
            float_tolerance, max_divergences - len(divergences)))
    if len(divergences) < max_divergences:
        divergences.extend(_diff_records(
            "orders", trace.orders, captured_orders,
            float_tolerance, max_divergences - len(divergences)))

    ok = (not divergences) and (expected == actual)
    return ReplayReport(
        ok=ok, symbol=symbol,
        expected=expected, actual=actual, divergences=divergences,
    )


def _wrap_paper_orders(paper: Any, sink: List[Dict[str, Any]]) -> None:
    """Attach a record-then-forward order callback to a custom paper engine."""
    existing = getattr(paper, "_order_callback", None)

    def _cb(order: Any) -> None:
        sink.append(_order_to_dict(order))
        if existing is not None:
            try:
                existing(order)
            except Exception:
                pass
    try:
        paper._order_callback = _cb  # type: ignore[attr-defined]
    except Exception:
        pass
