"""Phase 21 — live-execution bridge for the distributed strategy service.

Restores the V1-compliant live path that the monolith's
``execution/live_runner.py::run_live_execute`` provided, but adapted to
the Redis + FastAPI service topology. The C++ engine is fed from Redis
(see :class:`strategy.engine.live_engine.LiveEngine`); this module owns
the *decision → order* leg:

    set_ripple_callback
        └─► ripple_decision_to_intent          (execution.models)
              └─► intent_risk_block_reason       (execution.models — V1 §22.2 #12 gate)
                    └─► OBSERVE: log only        (ARM_EXECUTION=false, default)
                    └─► PAPER:   PaperEngine.on_intent
                    └─► LIVE:    ExecutionManager.on_intent → BinanceBroker

The risk gate, intent decoding, and layered-push helpers are imported
from ``execution/`` rather than re-implemented, so there is exactly one
source of truth for the V1 contract (acceptance criterion #7). Exits are
never blocked (risk-reducing flows must always fire).

Thread-safety: ``dispatch`` runs on whatever thread invokes the C++
ripple callback. ``ExecutionManager.on_intent`` is itself thread-safe
(it marshals onto its own asyncio loop); ``PaperEngine.on_intent`` is
synchronous and single-symbol so callbacks for one symbol are naturally
serialised by the engine. Diagnostics counters are guarded by a lock.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Any, Callable, Dict, Optional

from execution.models import (
    SizingConfig,
    intent_risk_block_reason,
    ripple_decision_to_intent,
)

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """Live-execution posture of the strategy service.

    * ``OBSERVE`` — default. Decisions are gated and logged but **never**
      routed to a broker. Byte-for-byte safe: zero orders are placed.
    * ``PAPER`` — decisions drive a synchronous :class:`PaperEngine`
      simulation (no broker connection).
    * ``LIVE`` — decisions drive an :class:`ExecutionManager` backed by a
      real broker (``BinanceBroker``; TESTNET unless explicitly disabled).
    """

    OBSERVE = "observe"
    PAPER = "paper"
    LIVE = "live"


def resolve_mode(arm_execution: bool, broker: str) -> ExecutionMode:
    """Map the ``ARM_EXECUTION`` flag + broker selector to a mode.

    ``arm_execution=False`` always yields ``OBSERVE`` regardless of the
    broker selector, so a misconfigured ``EXECUTION_BROKER`` can never
    cause live orders without the explicit arm flag.
    """
    if not arm_execution:
        return ExecutionMode.OBSERVE
    if (broker or "").strip().lower() in ("binance", "live"):
        return ExecutionMode.LIVE
    return ExecutionMode.PAPER


class ExecutionBridge:
    """Per-symbol gate + dispatch for one C++ ``OrderFlowEngine``.

    Parameters
    ----------
    symbol :
        Trading symbol (upper-cased).
    ofe_module :
        The imported ``orderflow_engine`` module — needed by
        :func:`intent_risk_block_reason` to resolve the Wave permission
        enums. May be ``None`` (the Wave gate is then skipped, matching
        the best-effort contract in ``execution.models``).
    mode :
        :class:`ExecutionMode`.
    target :
        The execution sink: an :class:`ExecutionManager` (LIVE), a
        :class:`PaperEngine` (PAPER), or ``None`` (OBSERVE). The target
        must expose ``on_intent(intent)``.
    sizing :
        Sizing config (informational; the target owns actual sizing).
    """

    def __init__(
        self,
        *,
        symbol: str,
        ofe_module: Any,
        mode: ExecutionMode = ExecutionMode.OBSERVE,
        target: Any = None,
        sizing: Optional[SizingConfig] = None,
    ) -> None:
        self.symbol = symbol.upper()
        self._ofe_module = ofe_module
        self.mode = mode
        self._target = target
        self._sizing = sizing or SizingConfig()

        # OBSERVE never arms; PAPER/LIVE start armed (an explicit
        # POST /api/execution/arm {armed:false} can disarm).
        self._armed = mode is not ExecutionMode.OBSERVE

        self._lock = threading.Lock()
        self._dispatched = 0
        self._blocked = 0
        self._last_block_reason: Optional[str] = None
        self._block_reason_counts: Dict[str, int] = {}

    # ----------------------------------------------------------- state

    @property
    def armed(self) -> bool:
        return self._armed

    def set_armed(self, value: bool) -> bool:
        """Arm / disarm the live target. No-op in OBSERVE mode.

        Returns the resulting armed state.
        """
        if self.mode is ExecutionMode.OBSERVE:
            logger.info("arm request ignored for %s — mode is OBSERVE", self.symbol)
            return False
        self._armed = bool(value)
        target = self._target
        if target is not None:
            try:
                if value and hasattr(target, "arm"):
                    target.arm()
                elif not value and hasattr(target, "disarm"):
                    target.disarm()
            except Exception:
                logger.exception("target arm/disarm failed for %s", self.symbol)
        logger.info("execution %s for %s", "ARMED" if value else "DISARMED", self.symbol)
        return self._armed

    def _position_qty(self) -> float:
        """Best-effort current position quantity from whichever target
        surface is present (``ExecutionManager`` or ``PaperEngine``)."""
        target = self._target
        if target is None:
            return 0.0
        try:
            if hasattr(target, "current_qty"):
                return float(getattr(target, "current_qty") or 0.0)
            pos = getattr(target, "position", None)
            if pos is not None:
                return float(getattr(pos, "quantity", 0.0) or 0.0)
        except Exception:
            return 0.0
        return 0.0

    # ----------------------------------------------------------- wiring

    def make_ripple_callback(self, engine: Any) -> Callable[[Any], None]:
        """Return a callback suitable for ``engine.set_ripple_callback``.

        The callback decodes the C++ ``RippleDecision`` to an
        ``ExecutionIntent`` and dispatches it through the risk gate.
        """

        def _cb(decision: Any) -> None:
            try:
                intent = ripple_decision_to_intent(decision)
            except Exception:
                logger.exception("ripple_decision_to_intent failed (%s)", self.symbol)
                return
            self.dispatch(engine, intent)

        return _cb

    def dispatch(self, engine: Any, intent: Any) -> None:
        """Apply the V1 §22.2 #12 gate and forward to the target.

        ``NO_ACTION`` decisions decode to ``None`` and are dropped. Entry
        intents are gated by Wave permissions / ES budget / Tide crisis /
        max-position; exits/cancels/rearms pass the gate unconditionally
        (``intent_risk_block_reason`` short-circuits non-entries).
        """
        if intent is None:
            return

        ref = float(getattr(intent, "reference_price", 0.0) or 0.0)
        pos_usd = abs(self._position_qty()) * ref
        try:
            block = intent_risk_block_reason(
                intent,
                engine,
                current_position_usd=pos_usd,
                ofe_module=self._ofe_module,
            )
        except Exception:
            logger.exception("intent_risk_block_reason raised (%s)", self.symbol)
            block = None

        if block is not None:
            self._record_block(block, intent)
            return

        # OBSERVE (or no target wired) → log the would-be order only.
        if self.mode is ExecutionMode.OBSERVE or self._target is None:
            logger.info(
                "OBSERVE %s would-execute: action=%s side=%s type=%s ref=%.6f ts=%d",
                self.symbol,
                getattr(intent, "action", ""),
                getattr(getattr(intent, "side", None), "value", ""),
                getattr(intent, "intent_type", ""),
                ref,
                int(getattr(intent, "timestamp", 0) or 0),
            )
            with self._lock:
                self._dispatched += 1
            return

        if not self._armed:
            return

        try:
            self._target.on_intent(intent)
            with self._lock:
                self._dispatched += 1
        except Exception:
            logger.exception("target.on_intent failed (%s)", self.symbol)

    def _record_block(self, reason: str, intent: Any) -> None:
        with self._lock:
            self._blocked += 1
            self._last_block_reason = reason
            self._block_reason_counts[reason] = (
                self._block_reason_counts.get(reason, 0) + 1
            )
        logger.warning(
            "V1 §22.2 #12 gate blocked intent (%s): action=%s reason=%s",
            self.symbol,
            getattr(intent, "action", ""),
            reason,
        )

    # ------------------------------------------------------- diagnostics

    def diagnostics(self, engine: Any = None) -> dict:
        """Return a JSON-serialisable diagnostics snapshot for REST.

        When ``engine`` is supplied the live risk-budget snapshot
        (ES budget / consumed / multiplier / max-position) is included.
        """
        with self._lock:
            out: dict = {
                "symbol": self.symbol,
                "mode": self.mode.value,
                "armed": self._armed,
                "dispatched": self._dispatched,
                "blocked": self._blocked,
                "last_block_reason": self._last_block_reason,
                "block_reason_counts": dict(self._block_reason_counts),
                "position_qty": self._position_qty(),
            }
        out["risk"] = self._risk_snapshot(engine)
        return out

    @staticmethod
    def _risk_snapshot(engine: Any) -> Optional[dict]:
        if engine is None:
            return None
        try:
            risk = engine.get_strategy_snapshot().risk
        except Exception:
            return None
        return {
            "es_budget": float(getattr(risk, "es_budget", 0.0) or 0.0),
            "consumed_es": float(getattr(risk, "consumed_es", 0.0) or 0.0),
            "risk_multiplier": float(getattr(risk, "risk_multiplier", 0.0) or 0.0),
            "max_position_usd": float(getattr(risk, "max_position_usd", 0.0) or 0.0),
        }
