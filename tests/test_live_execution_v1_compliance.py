"""Phase 14C — V1 §22.2 #12 live-execution compliance acceptance suite.

Pins the V1 contract end-to-end: when the local risk engine, Wave
permissions, Tide budget, or position cap say no, the live broker
MUST NOT see an order. Mirrors the gating logic in
``backtestingCpp/orderflow/ripple/RippleEngine.cpp::on_trigger_decision``
(lines 282-307) on the Python execute path.

The suite uses the real ``OrderFlowEngine`` to host Wave permissions /
risk budget state, a real ``ExecutionManager`` driven on a per-test
asyncio loop, and a ``StubBroker`` to count placed orders. Synthetic
``ExecutionIntent`` objects stand in for the ``RippleDecision`` →
``ripple_decision_to_intent`` adapter so the test stays deterministic
and independent of the C++ pattern-detection internals (which is
covered by the `Phase 13/14A/14B` regression suites already).

Acceptance criteria (`implementation_plan.md` §7.1 Phase 14C):
  - ES exhausted (`consumed_es >= es_budget`) → 0 broker orders.
  - Wave DISABLED for archetype/side                 → 0 broker orders.
  - Tide CRISIS (`risk_multiplier <= 0`)             → 0 broker orders.
  - max_position_usd cap met                         → 0 broker orders.
  - Two-trades-concurrent (V1 §5.5 / §22.3 #7)       → 0 second order.
  - Cooldown active (event-time)                     → 0 second order.

Plus two "must NOT over-block" tests:
  - Exit intents are NEVER blocked (V1 must always allow unwinding).
  - Happy path (all gates green) DOES place a real broker order.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Callable, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# C++ orderflow_engine module — bring in via the build dir like the
# wave / risk binding test suites. Tests are skipped cleanly if the
# native module is not built.
try:
    BUILD_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtestingCpp", "orderflow", "build")
    if BUILD_DIR not in sys.path:
        sys.path.insert(0, BUILD_DIR)
    import orderflow_engine as ofe  # noqa: E402
    HAS_OFE = (hasattr(ofe, "OrderFlowEngine")
               and hasattr(ofe, "RippleEngine")
               and hasattr(ofe, "WaveSnapshot")
               and hasattr(ofe, "RiskEngine"))
except ImportError:  # pragma: no cover - module not built in CI sandboxes
    HAS_OFE = False
    ofe = None

from execution.execution_manager import ExecutionManager  # noqa: E402
from execution.models import (  # noqa: E402
    ExecutionIntent,
    OrderSide,
    SizingConfig,
    SizingMode,
    intent_risk_block_reason,
)

# Reuse the StubBroker / helpers from the existing Phase 12 suite —
# this is intentional: the broker stub IS the V1 §22.2 #12 contract
# surface. We assert against `broker.placed_orders` exactly the same
# way the Phase 14A `on_intent` tests do.
from tests.test_execution_manager import StubBroker, _make_manager  # noqa: E402


# ----------------------------------------------------------------------
# Test helpers


def _build_engine_with_layered_state(
    *,
    es_budget: float = 1_000.0,
    consumed_es: float = 0.0,
    max_position_usd: float = 100_000.0,
    risk_multiplier: float = 1.0,
    realized_vol: float = 0.80,
    wave_long_bounce: Any = None,
    wave_short_bounce: Any = None,
    wave_long_breakout: Any = None,
    wave_short_breakout: Any = None,
    wave_regime: Any = None,
) -> Any:
    """Return a real `ofe.OrderFlowEngine` with the layered Tide / Wave
    state pre-loaded. The four ``wave_*`` arguments default to
    ``PermissionLevel.FULL`` when ``None``.

    ``consumed_es`` is applied via :func:`_force_consumed_es` (a
    deterministic fill that drives the C++ RiskEngine to the desired
    consumed-ES point) so we exercise the actual binding rather than
    a Python-side fiction."""
    if not HAS_OFE:
        raise unittest.SkipTest("orderflow_engine not built")

    engine = ofe.OrderFlowEngine(ofe.EngineConfig())
    ripple = engine.get_ripple()

    ripple.set_risk_budget(es_budget, max_position_usd, risk_multiplier)
    ripple.set_realized_vol(realized_vol)

    ws = ofe.WaveSnapshot()
    if wave_regime is not None:
        ws.regime = wave_regime
    full = ofe.PermissionLevel.FULL
    ws.permissions.long_bounce    = wave_long_bounce    or full
    ws.permissions.short_bounce   = wave_short_bounce   or full
    ws.permissions.long_breakout  = wave_long_breakout  or full
    ws.permissions.short_breakout = wave_short_breakout or full
    ripple.set_wave_snapshot(ws)

    if consumed_es > 0.0:
        _force_consumed_es(engine, ripple, consumed_es)

    return engine


def _force_consumed_es(engine: Any, ripple: Any, target_es: float) -> None:
    """Drive the C++ RiskEngine to consume approximately ``target_es``
    USD of expected shortfall by feeding it a synthetic fill on the
    private ``RiskEngine`` instance. We construct a standalone
    :class:`ofe.RiskEngine` and reuse the same budget so the test is
    independent of the bound ``RippleEngine`` internals."""
    # Use a temporary RiskEngine to compute the qty that would consume
    # `target_es` of expected shortfall at the engine's volatility.
    re = ofe.RiskEngine()
    re.set_budget(1e12, 1e12, 1.0)  # huge cap so check_new_order never refuses
    re.set_volatility(0.80)

    # ES_MULTIPLIER_095 * SQRT_DT_1DAY = 2.0627 * 0.052342 ≈ 0.10797.
    # ES = qty * price * realized_vol * 0.10797.
    # Solve for qty at price=100, vol=0.80 → qty ≈ target_es / 8.638.
    es_per_unit = 100.0 * 0.80 * 0.10797
    needed_qty = max(target_es / es_per_unit, 0.0001)

    fill = ofe.FillEvent()
    fill.timestamp = 1
    fill.side = ofe.OrderSide.BUY
    fill.price = 100.0
    fill.quantity = needed_qty
    # The RippleEngine doesn't expose its internal RiskEngine on the
    # Python side, but ``set_risk_budget`` resets ES consumption. To
    # actually consume ES we drive ``on_fill`` against ``ripple.on_fill``.
    ripple.on_fill(fill)


def _entry_intent(
    *,
    side: OrderSide = OrderSide.BUY,
    action: str = "ENTER_BREAKOUT_LONG",
    timestamp_ms: int = 1_700_000_000_000,
    reference_price: float = 100.0,
    reason: str = "wall fail confirmed",
) -> ExecutionIntent:
    return ExecutionIntent(
        timestamp=timestamp_ms,
        action=action,
        side=side,
        intent_type="entry",
        reference_price=reference_price,
        reason=reason,
    )


def _exit_intent(
    *,
    action: str = "EXIT_BREAKOUT",
    timestamp_ms: int = 1_700_000_001_000,
    reference_price: float = 100.0,
    reason: str = "invalidation",
) -> ExecutionIntent:
    return ExecutionIntent(
        timestamp=timestamp_ms,
        action=action,
        side=None,
        intent_type="exit",
        reference_price=reference_price,
        reason=reason,
    )


def _drive_through_gate_and_exec_mgr(
    *,
    intent: ExecutionIntent,
    engine: Any,
    exec_mgr: ExecutionManager,
    loop: asyncio.AbstractEventLoop,
) -> Optional[str]:
    """Mirror the production live_runner ``_ripple_cb`` / UI live
    branch exactly: gate first, then dispatch on the exec_mgr loop.

    Returns the suppression reason (or ``None`` if the gate let the
    intent through and ``exec_mgr.on_intent`` was invoked)."""
    qty = float(getattr(exec_mgr, "current_qty", 0.0) or 0.0)
    ref = float(intent.reference_price or 0.0)
    pos_usd = abs(qty) * ref
    block = intent_risk_block_reason(
        intent, engine,
        current_position_usd=pos_usd,
        ofe_module=ofe,
    )
    if block is not None:
        return block

    # Drive the coroutine on the test loop (mirrors the existing Phase
    # 14A on_intent test pattern in test_execution_manager.py).
    def _fake_run(coro, _loop):
        loop.run_until_complete(coro)

        class _F:
            def result(self, timeout=None):
                return None
        return _F()

    with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
               side_effect=_fake_run):
        exec_mgr.on_intent(intent)
    return None


# ----------------------------------------------------------------------
# Acceptance test suite


@unittest.skipUnless(HAS_OFE, "orderflow_engine native module not built")
class TestV1LiveExecutionCompliance(unittest.TestCase):
    """V1 §22.2 #12 contract: the broker MUST NOT see an order when
    any of the six engine-side risk gates blocks the intent.

    Each test follows the same shape:
      1. Build a real `OrderFlowEngine` with the gate scenario configured.
      2. Build a real `ExecutionManager` + `StubBroker`.
      3. Fire an `ExecutionIntent` through the production gate +
         exec_mgr coroutine path.
      4. Assert `broker.placed_orders == []`.
    """

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()

    def tearDown(self) -> None:
        self.loop.close()

    def _arm_exec_mgr(
        self,
        broker: StubBroker,
        *,
        cooldown_s: float = 0.0,
        max_position_units: float = 100.0,
    ) -> ExecutionManager:
        m = _make_manager(
            broker,
            sizing=SizingConfig(
                mode=SizingMode.FIXED_NOTIONAL,
                value=1_000.0,
                max_position=max_position_units,
            ),
            cooldown_s=cooldown_s,
        )
        m._loop = self.loop
        m._step_size = 0.0
        m.arm()
        return m

    # ── 1. ES budget exhausted ────────────────────────────────────────

    def test_es_budget_exhausted_blocks_broker_order(self):
        """`consumed_es >= es_budget` → V1 §22.2 #8 says new entries
        must be suppressed. Broker must see zero orders."""
        engine = _build_engine_with_layered_state(
            es_budget=10.0,
            consumed_es=50.0,  # well past budget
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        intent = _entry_intent()
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertEqual(reason, "ES_EXHAUSTED")
        self.assertEqual(
            len(broker.placed_orders), 0,
            "ES exhausted: broker.place_order MUST NOT be invoked "
            "(V1 §22.2 #12 contract)")

    # ── 2. Wave DISABLED ──────────────────────────────────────────────

    def test_wave_disabled_blocks_broker_order(self):
        """Wave regime DISABLED for the archetype/side must drop the
        intent (V1 §22.2 #9 / strategy.md §18 permissions matrix)."""
        engine = _build_engine_with_layered_state(
            wave_long_breakout=ofe.PermissionLevel.DISABLED,
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        intent = _entry_intent(action="ENTER_BREAKOUT_LONG",
                               side=OrderSide.BUY)
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("WAVE_DISABLED"))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_wave_disabled_short_bounce_blocks_short_bounce_only(self):
        """Permissions are per (archetype, side). A disabled
        short_bounce must NOT block a long_breakout — verifies the
        gate is correctly indexed."""
        engine = _build_engine_with_layered_state(
            wave_short_bounce=ofe.PermissionLevel.DISABLED,
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        # short_bounce → blocked
        i1 = _entry_intent(action="ENTER_BOUNCE_SHORT", side=OrderSide.SELL,
                           timestamp_ms=1_700_000_000_000)
        r1 = _drive_through_gate_and_exec_mgr(
            intent=i1, engine=engine, exec_mgr=m, loop=self.loop)
        self.assertIsNotNone(r1)
        self.assertTrue(r1.startswith("WAVE_DISABLED"))

        # long_breakout → passes the wave gate (broker actually fires).
        i2 = _entry_intent(action="ENTER_BREAKOUT_LONG", side=OrderSide.BUY,
                           timestamp_ms=1_700_000_000_500)
        r2 = _drive_through_gate_and_exec_mgr(
            intent=i2, engine=engine, exec_mgr=m, loop=self.loop)
        self.assertIsNone(r2)
        self.assertEqual(len(broker.placed_orders), 1,
                         "long_breakout should pass the gate when only "
                         "short_bounce is disabled")
        self.assertEqual(broker.placed_orders[0].signal_type,
                         "ENTER_BREAKOUT_LONG")

    # ── 3. Tide CRISIS ────────────────────────────────────────────────

    def test_tide_crisis_blocks_broker_order(self):
        """`risk_multiplier <= 0.0` is the Tide CRISIS sentinel
        (strategy.md §15.2) — engine refuses to open any new entries."""
        engine = _build_engine_with_layered_state(risk_multiplier=0.0)
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        intent = _entry_intent()
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertEqual(reason, "TIDE_CRISIS")
        self.assertEqual(len(broker.placed_orders), 0)

    # ── 4. max_position_usd exceeded ──────────────────────────────────

    def test_max_position_exceeded_blocks_broker_order(self):
        """When the ExecutionManager already holds a position whose
        notional meets/exceeds `risk.max_position_usd`, new entries
        must be blocked."""
        engine = _build_engine_with_layered_state(
            max_position_usd=500.0,
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)
        # Pre-populate ExecutionManager so `current_qty * ref_price`
        # exceeds the cap (100 units × $100 = $10 000 ≫ $500).
        m._current_side = OrderSide.BUY
        m._current_qty = 100.0

        # Use a SHORT entry intent so the same-side gate inside
        # `on_intent` does NOT short-circuit the test (we are pinning
        # the max_position gate, not the same-side gate).
        intent = _entry_intent(action="ENTER_BREAKOUT_SHORT",
                               side=OrderSide.SELL)
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertEqual(reason, "MAX_POSITION_EXCEEDED")
        self.assertEqual(len(broker.placed_orders), 0)

    # ── 5. Two trades concurrent (V1 §5.5 / §22.3 #7) ─────────────────

    def test_two_trades_concurrent_blocks_second_entry(self):
        """V1 §5.5 mandates at most one concurrent trade per symbol.
        A second entry on the SAME side must not fire a broker order.
        Enforced inside `ExecutionManager.on_intent` (Phase 14A) — this
        test pins that the V1 §22.2 #12 acceptance suite covers it."""
        engine = _build_engine_with_layered_state()
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        # 1st entry: BUY → broker sees one order.
        i1 = _entry_intent(side=OrderSide.BUY, action="ENTER_BREAKOUT_LONG",
                           timestamp_ms=1_700_000_000_000)
        r1 = _drive_through_gate_and_exec_mgr(
            intent=i1, engine=engine, exec_mgr=m, loop=self.loop)
        self.assertIsNone(r1)
        self.assertEqual(len(broker.placed_orders), 1)

        # 2nd entry: BUY again while position is open → suppressed
        # by the same-side gate in `ExecutionManager.on_intent`. The
        # broker placed-order count must NOT change.
        i2 = _entry_intent(side=OrderSide.BUY, action="ENTER_BREAKOUT_LONG",
                           timestamp_ms=1_700_000_100_000)
        r2 = _drive_through_gate_and_exec_mgr(
            intent=i2, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertIsNone(r2,
                          "the wave/risk gate must let the intent through "
                          "— same-side suppression happens in the exec_mgr")
        self.assertEqual(
            len(broker.placed_orders), 1,
            "second concurrent same-side entry MUST NOT reach the broker "
            "(V1 §5.5 contract)")

    # ── 6. Cooldown active ────────────────────────────────────────────

    def test_cooldown_active_blocks_second_entry(self):
        """Event-time cooldown gate inside `ExecutionManager.on_intent`
        (Phase 14A) must suppress the second intent when it arrives
        within `cooldown_s` of the first. Mirrors the
        AGENT_STRATEGY_RULES.md §7.4 determinism contract."""
        engine = _build_engine_with_layered_state()
        broker = StubBroker(step_size=0.0)
        # 5-second cooldown.
        m = self._arm_exec_mgr(broker, cooldown_s=5.0)

        # Use BUY → SHORT so the second intent does NOT trip the
        # same-side suppression (we are isolating the cooldown gate
        # for this test).
        i1 = _entry_intent(side=OrderSide.BUY, action="ENTER_BREAKOUT_LONG",
                           timestamp_ms=1_700_000_000_000)
        _drive_through_gate_and_exec_mgr(
            intent=i1, engine=engine, exec_mgr=m, loop=self.loop)
        self.assertEqual(len(broker.placed_orders), 1)

        # 2 s later — still inside the 5 s cooldown.
        i2 = _entry_intent(side=OrderSide.SELL, action="ENTER_BREAKOUT_SHORT",
                           timestamp_ms=1_700_000_002_000)
        _drive_through_gate_and_exec_mgr(
            intent=i2, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertEqual(
            len(broker.placed_orders), 1,
            "intent inside event-time cooldown MUST NOT reach the broker")

    # ── 7. Exit intents are NEVER blocked ─────────────────────────────

    def test_exit_intent_passes_even_when_es_exhausted(self):
        """V1 must always allow risk-reducing flows. An EXIT intent
        must NEVER be suppressed by the wave/risk gate — even when
        the engine is in TIDE_CRISIS + ES_EXHAUSTED + Wave DISABLED
        all at once."""
        engine = _build_engine_with_layered_state(
            es_budget=10.0, consumed_es=50.0,
            risk_multiplier=0.0,
            wave_long_breakout=ofe.PermissionLevel.DISABLED,
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)
        # Pre-populate so the exit has a position to close.
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1
        from execution.models import Position  # noqa: PLC0415
        broker.position = Position(
            symbol="BTCUSDT", side=OrderSide.BUY,
            quantity=0.1, entry_price=100.0)

        intent = _exit_intent()
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertIsNone(
            reason,
            "exits MUST never be blocked by the V1 §22.2 #12 risk gate")
        self.assertEqual(
            len(broker.placed_orders), 1,
            "exit intent must still drive a close on the broker")

    # ── 8. Happy path: gate must not over-block ───────────────────────

    def test_happy_path_fires_broker_order(self):
        """Negative-control: with all gates green the broker DOES see
        an order. Without this test, the gate could be over-zealous
        and still pass tests #1-#6."""
        engine = _build_engine_with_layered_state(
            es_budget=1_000.0, consumed_es=0.0,
            risk_multiplier=1.0,
            max_position_usd=100_000.0,
        )
        broker = StubBroker(step_size=0.0)
        m = self._arm_exec_mgr(broker)

        intent = _entry_intent()
        reason = _drive_through_gate_and_exec_mgr(
            intent=intent, engine=engine, exec_mgr=m, loop=self.loop)

        self.assertIsNone(reason)
        self.assertEqual(
            len(broker.placed_orders), 1,
            "happy path: gate must let the intent through")
        self.assertEqual(broker.placed_orders[0].side, OrderSide.BUY)
        self.assertEqual(broker.placed_orders[0].signal_type,
                         "ENTER_BREAKOUT_LONG")


# ----------------------------------------------------------------------
# Unit-level gate function tests (no engine, no broker)


@unittest.skipUnless(HAS_OFE, "orderflow_engine native module not built")
class TestIntentRiskBlockReasonHelper(unittest.TestCase):
    """Focused tests for :func:`execution.models.intent_risk_block_reason`.

    Verifies the helper returns the right reason string for each
    failure mode and gracefully short-circuits for non-entry intents.
    """

    def test_returns_none_for_none_intent(self):
        self.assertIsNone(intent_risk_block_reason(None, object()))

    def test_returns_none_for_exit(self):
        engine = _build_engine_with_layered_state(consumed_es=999.0)
        self.assertIsNone(intent_risk_block_reason(
            _exit_intent(), engine, ofe_module=ofe))

    def test_returns_none_for_cancel(self):
        engine = _build_engine_with_layered_state()
        intent = ExecutionIntent(
            timestamp=1, action="CANCEL_PASSIVE_ORDERS",
            side=None, intent_type="cancel")
        self.assertIsNone(intent_risk_block_reason(
            intent, engine, ofe_module=ofe))

    def test_returns_none_for_unknown_action(self):
        engine = _build_engine_with_layered_state()
        intent = ExecutionIntent(
            timestamp=1, action="MYSTERY_INTENT", side=OrderSide.BUY,
            intent_type="entry", reference_price=100.0)
        self.assertIsNone(intent_risk_block_reason(
            intent, engine, ofe_module=ofe))

    def test_returns_none_when_engine_has_no_snapshot(self):
        class _BrokenEngine:
            def get_strategy_snapshot(self):
                raise RuntimeError("no snapshot")
        self.assertIsNone(intent_risk_block_reason(
            _entry_intent(), _BrokenEngine(), ofe_module=ofe))

    def test_wave_gate_skipped_when_ofe_module_is_none(self):
        engine = _build_engine_with_layered_state(
            wave_long_breakout=ofe.PermissionLevel.DISABLED)
        # No ofe_module → wave permissions can't be looked up by enum,
        # so the gate must skip the wave check. Risk gates still apply.
        self.assertIsNone(intent_risk_block_reason(
            _entry_intent(), engine, ofe_module=None))

    def test_zero_position_usd_does_not_trigger_max_position(self):
        engine = _build_engine_with_layered_state(max_position_usd=100.0)
        # position_usd=0 < cap=100 → pass.
        self.assertIsNone(intent_risk_block_reason(
            _entry_intent(), engine,
            current_position_usd=0.0, ofe_module=ofe))


# ----------------------------------------------------------------------
# Live-runner wiring: the gate MUST live in the production callback.


@unittest.skipUnless(HAS_OFE, "orderflow_engine native module not built")
class TestLiveRunnerGateIntegration(unittest.TestCase):
    """Pins that ``execution.live_runner.run_live_execute`` actually
    invokes the gate inside ``_ripple_cb`` before forwarding to
    ``exec_mgr.on_intent``. Without this test the gate could be
    defined but never wired up."""

    def _spin_runner_and_capture_callback(
        self, *, engine: Any, exec_mgr_stub: Any) -> Callable[[Any], None]:
        """Run `run_live_execute` once with the WS feed stubbed and
        return the ripple callback the runner registered on the
        engine.

        The C++ ``OrderFlowEngine`` exposes ``set_ripple_callback`` as
        a read-only pybind11 method, so we wrap the real engine in a
        Python proxy that delegates every attribute access transparently
        but intercepts ``set_ripple_callback`` to capture the closure
        the runner registered."""
        from execution.live_runner import run_live_execute  # noqa: PLC0415

        captured: dict = {}

        class _EngineProxy:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def set_ripple_callback(self, cb: Callable[[Any], None]) -> None:
                captured["cb"] = cb
                self._inner.set_ripple_callback(cb)

        proxy = _EngineProxy(engine)

        interrupt_event = threading.Event()
        interrupt_event.set()

        def _noop_ws_feed(**_kwargs):
            return None

        class _NoopWebsockets:
            pass

        with patch("data_feed.run_binance_usdm_futures_ws_feed",
                   new=_noop_ws_feed), \
             patch("data_feed.stream_health.FeedStreamHealth"):
            run_live_execute(
                symbol="BTCUSDT",
                exec_mgr=exec_mgr_stub,
                ofe_module=ofe,
                websockets_module=_NoopWebsockets(),
                apply_rest_snapshot=False,
                engine_factory=lambda _ofe: proxy,
                interrupt_event=interrupt_event,
                disarm_grace_s=0.0,
                enable_layered_strategy=False,
            )
        return captured["cb"]

    def test_live_runner_gate_drops_intent_for_es_exhausted(self):
        """Drive a synthetic Ripple decision through the runner's
        actual `_ripple_cb`. With ES exhausted the gate must drop
        the intent BEFORE it reaches the exec_mgr."""
        engine = _build_engine_with_layered_state(
            es_budget=10.0, consumed_es=50.0)

        class _ExecMgrStub:
            def __init__(self):
                self.on_intent_calls: List[Any] = []
                self.disarm_calls = 0
                self.stop_calls = 0
                self.disarm_close_position: List[bool] = []
                self.current_side = None
                self.current_qty = 0.0
                self.orders: List[Any] = []
                self.armed = True

            def on_intent(self, intent: Any) -> None:
                self.on_intent_calls.append(intent)

            def on_signal(self, signal: Any) -> None:
                pass

            def disarm(self, close_position: bool = True) -> None:
                self.disarm_calls += 1
                self.disarm_close_position.append(close_position)

            def stop(self) -> None:
                self.stop_calls += 1

        exec_mgr_stub = _ExecMgrStub()
        cb = self._spin_runner_and_capture_callback(
            engine=engine, exec_mgr_stub=exec_mgr_stub)

        # Duck-typed RippleDecision matching `_make_decision` in
        # test_live_runner.py — `ripple_decision_to_intent` only needs
        # `.intent`, `.timestamp`, `.reference_price`, `.confidence`,
        # `.reason`, `.wall_id`, `.invalidation_price`.
        class _Intent:
            def __str__(self):
                return "RippleIntent.ENTER_BREAKOUT_LONG"

        class _Decision:
            def __init__(self):
                self.intent = _Intent()
                self.timestamp = 1_700_000_000_000
                self.reference_price = 100.0
                self.confidence = 0.8
                self.reason = "wall fail"
                self.wall_id = 7
                self.invalidation_price = 0.0

        cb(_Decision())

        self.assertEqual(
            len(exec_mgr_stub.on_intent_calls), 0,
            "Phase 14C gate must drop the intent in `_ripple_cb` so "
            "`exec_mgr.on_intent` is never called when ES is exhausted")

    def test_live_runner_gate_passes_happy_path(self):
        """Negative control for the wiring test: with all gates green
        the runner's `_ripple_cb` DOES call `exec_mgr.on_intent`."""
        engine = _build_engine_with_layered_state(
            es_budget=1_000.0, consumed_es=0.0,
            risk_multiplier=1.0,
            max_position_usd=100_000.0,
        )

        class _ExecMgrStub:
            def __init__(self):
                self.on_intent_calls: List[Any] = []
                self.disarm_calls = 0
                self.stop_calls = 0
                self.disarm_close_position: List[bool] = []
                self.current_side = None
                self.current_qty = 0.0
                self.orders: List[Any] = []
                self.armed = True

            def on_intent(self, intent: Any) -> None:
                self.on_intent_calls.append(intent)

            def on_signal(self, signal: Any) -> None:
                pass

            def disarm(self, close_position: bool = True) -> None:
                self.disarm_calls += 1
                self.disarm_close_position.append(close_position)

            def stop(self) -> None:
                self.stop_calls += 1

        exec_mgr_stub = _ExecMgrStub()
        cb = self._spin_runner_and_capture_callback(
            engine=engine, exec_mgr_stub=exec_mgr_stub)

        class _Intent:
            def __str__(self):
                return "RippleIntent.ENTER_BREAKOUT_LONG"

        class _Decision:
            def __init__(self):
                self.intent = _Intent()
                self.timestamp = 1_700_000_000_000
                self.reference_price = 100.0
                self.confidence = 0.8
                self.reason = "wall fail"
                self.wall_id = 7
                self.invalidation_price = 0.0

        cb(_Decision())

        self.assertEqual(
            len(exec_mgr_stub.on_intent_calls), 1,
            "happy path: runner must forward the intent to the exec_mgr")
        self.assertEqual(exec_mgr_stub.on_intent_calls[0].action,
                         "ENTER_BREAKOUT_LONG")


if __name__ == "__main__":
    unittest.main()
