"""Send one saved plan to an Oanda practice account.

The client is constructed only against the practice host. A live account
type, or an account query that fails, raises before a plan is written and
before an order is sent. Historical simulation does not import this module's
order call.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from oanda_portfolio.plan import (
    MARGIN_CEILING,
    MARGIN_TARGET,
    OrderLeg,
    RebalancePlan,
    reconcile_plan,
)

logger = logging.getLogger(__name__)

_TERMINAL = {"complete", "failed", "skipped", "simulated"}
_MAX_ATTEMPTS = 2


@dataclass
class FillResult:
    status: str
    filled_units: float
    price: float
    message: str = ""


@dataclass(frozen=True)
class ExecutionInputs:
    nav: float
    margin_used: float
    margin_available: float
    margin_budget: float
    margin_capacity: float
    mids: dict[str, float]
    trade_steps: dict[str, float]
    margin_rates: dict[str, float]
    quote_home_conversions: dict[str, float]


class PlanStore:
    """Atomic JSON store, one file per New York session date."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, session_date: str) -> Path:
        return self.root / f"{session_date}.json"

    def load(self, session_date: str) -> RebalancePlan | None:
        path = self.path_for(session_date)
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        return RebalancePlan.from_dict(payload)

    def save(self, plan: RebalancePlan) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(plan.session_date)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        os.replace(temporary, path)


class PracticeBroker:
    """Oanda practice-account broker. Refuses to construct a live client."""

    def __init__(self, *, account_id: str | None = None, access_token: str | None = None,
                 account_type: str | None = None) -> None:
        from dotenv import load_dotenv

        load_dotenv()
        self.account_type = account_type if account_type is not None else os.getenv(
            "OANDA_ACCOUNT_TYPE", "practice"
        )
        if str(self.account_type).lower().startswith("live"):
            raise RuntimeError(
                f"refusing to trade: OANDA_ACCOUNT_TYPE={self.account_type!r} is not practice"
            )
        self.account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
        self.access_token = access_token or os.getenv("OANDA_ACCESS_TOKEN")
        if not self.account_id or not self.access_token:
            raise RuntimeError("OANDA_ACCOUNT_ID and OANDA_ACCESS_TOKEN are required")
        from oandapyV20 import API

        self.environment = "practice"
        self.client = API(access_token=self.access_token, environment="practice")
        self.assert_practice()

    def assert_practice(self) -> None:
        if self.environment != "practice" or str(self.account_type).lower().startswith("live"):
            raise RuntimeError(
                f"refusing to trade environment={self.environment!r} account_type={self.account_type!r}"
            )
        summary = self.account_summary()
        if not summary:
            raise RuntimeError("practice account query returned no summary")

    def account_summary(self) -> dict:
        import oandapyV20.endpoints.accounts as accounts

        request = accounts.AccountSummary(accountID=self.account_id)
        try:
            payload = self.client.request(request)
        except Exception as exc:
            raise RuntimeError(f"practice account query failed: {exc}") from exc
        account = payload.get("account") if isinstance(payload, dict) else None
        if not account:
            raise RuntimeError(f"practice account query returned {payload!r}")
        return account

    def nav(self) -> float:
        summary = self.account_summary()
        try:
            value = float(summary["NAV"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"practice account has no NAV: {summary!r}") from exc
        if value <= 0:
            raise RuntimeError(f"practice NAV is not positive: {value}")
        return value

    def positions(self) -> dict[str, int]:
        import oandapyV20.endpoints.positions as positions

        request = positions.OpenPositions(accountID=self.account_id)
        try:
            payload = self.client.request(request)
        except Exception as exc:
            raise RuntimeError(f"open position query failed: {exc}") from exc
        held: dict[str, float] = {}
        for position in payload.get("positions", []):
            instrument = position.get("instrument")
            long_units = float(position.get("long", {}).get("units", 0) or 0)
            short_units = float(position.get("short", {}).get("units", 0) or 0)
            net = net_position_units(long_units, short_units)
            if instrument and not math.isclose(net, 0.0, abs_tol=1e-9):
                held[instrument] = net
        return held

    def trade_steps(self, instruments: list[str]) -> dict[str, float]:
        details = self._instrument_details(instruments)
        steps = {name: trade_step(details[name]) for name in instruments}
        logger.info("Practice trade steps: %s", steps)
        return steps

    def _instrument_details(self, instruments: list[str]) -> dict[str, dict]:
        import oandapyV20.endpoints.accounts as accounts

        request = accounts.AccountInstruments(accountID=self.account_id)
        try:
            payload = self.client.request(request)
        except Exception as exc:
            raise RuntimeError(f"instrument query failed: {exc}") from exc
        found = {
            item.get("name"): item
            for item in payload.get("instruments", [])
        }
        missing = [name for name in instruments if name not in found]
        if missing:
            raise RuntimeError(f"practice account cannot trade {missing}")
        return {name: found[name] for name in instruments}

    def execution_inputs(
        self,
        instruments: list[str],
        *,
        margin_utilization: float = MARGIN_TARGET,
    ) -> ExecutionInputs:
        """Read live conversion and margin inputs before a plan is written."""
        if not 0 < margin_utilization <= MARGIN_CEILING:
            raise ValueError(
                f"margin utilization must be between 0 and {MARGIN_CEILING:.0%}, "
                f"got {margin_utilization}"
            )
        names = sorted(set(instruments))
        if not names:
            raise ValueError("execution instrument list is empty")
        summary = self.account_summary()
        try:
            nav = float(summary["NAV"])
            margin_used = float(summary["marginUsed"])
            margin_available = float(summary["marginAvailable"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"account summary has incomplete margin fields: {summary!r}"
            ) from exc
        if (
            nav <= 0
            or margin_used < 0
            or margin_available <= 0
            or not all(
                math.isfinite(value)
                for value in (nav, margin_used, margin_available)
            )
        ):
            raise RuntimeError(
                "account margin state is invalid "
                f"NAV={nav} used={margin_used} available={margin_available}"
            )
        details = self._instrument_details(names)
        steps = {name: trade_step(details[name]) for name in names}
        rates: dict[str, float] = {}
        for name in names:
            try:
                rate = float(details[name]["marginRate"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"{name} has no marginRate") from exc
            if rate <= 0 or not math.isfinite(rate):
                raise RuntimeError(f"{name} marginRate={rate}")
            rates[name] = rate

        import oandapyV20.endpoints.pricing as pricing

        request = pricing.PricingInfo(
            accountID=self.account_id,
            params={
                "instruments": ",".join(names),
                "includeHomeConversions": True,
            },
        )
        try:
            payload = self.client.request(request)
        except Exception as exc:
            raise RuntimeError(f"pricing/conversion query failed: {exc}") from exc
        prices = {
            item.get("instrument"): item
            for item in payload.get("prices", [])
        }
        missing_prices = [name for name in names if name not in prices]
        if missing_prices:
            raise RuntimeError(f"practice pricing omitted {missing_prices}")
        mids: dict[str, float] = {}
        for name in names:
            price = prices[name]
            try:
                bid = float(price["closeoutBid"])
                ask = float(price["closeoutAsk"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"{name} has incomplete live price: {price!r}") from exc
            mid = (bid + ask) / 2.0
            if mid <= 0 or not math.isfinite(mid):
                raise RuntimeError(f"{name} live midpoint={mid}")
            mids[name] = mid
        home = {
            item.get("currency"): item
            for item in payload.get("homeConversions", [])
        }
        conversions: dict[str, float] = {}
        for name in names:
            try:
                quote = name.rsplit("_", 1)[1]
                conversion = float(home[quote]["positionValue"])
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{name} has no quote-to-home position value conversion"
                ) from exc
            if conversion <= 0 or not math.isfinite(conversion):
                raise RuntimeError(
                    f"{name} quote-to-home conversion={conversion}"
                )
            conversions[name] = conversion
        margin_capacity = margin_used + margin_available
        current_utilization = margin_used / margin_capacity
        margin_budget = margin_capacity * MARGIN_CEILING
        call_percent = summary.get("marginCallPercent")
        logger.info(
            "Execution margin NAV=%.2f used=%.2f available=%.2f "
            "utilization=%.2f target=%.0f%% ceiling=%.0f%% margin_call_percent=%s",
            nav,
            margin_used,
            margin_available,
            current_utilization,
            margin_utilization * 100,
            MARGIN_CEILING * 100,
            call_percent,
        )
        return ExecutionInputs(
            nav=nav,
            margin_used=margin_used,
            margin_available=margin_available,
            margin_budget=margin_budget,
            margin_capacity=margin_capacity,
            mids=mids,
            trade_steps=steps,
            margin_rates=rates,
            quote_home_conversions=conversions,
        )

    def place_market(
        self,
        instrument: str,
        units: float,
        client_order_id: str,
        position_fill: str,
    ) -> FillResult:
        self.assert_practice()
        import oandapyV20.endpoints.orders as orders

        request = orders.OrderCreate(
            accountID=self.account_id,
            data={
                "order": {
                    "type": "MARKET",
                    "instrument": instrument,
                    "units": format_units(units),
                    "timeInForce": "FOK",
                    "positionFill": position_fill,
                    "clientExtensions": {"id": client_order_id},
                }
            },
        )
        try:
            payload = self.client.request(request)
        except Exception as exc:
            logger.error("Order %s failed: %s", client_order_id, exc)
            return FillResult(status="rejected", filled_units=0, price=0.0, message=str(exc))
        return _parse_fill(payload, units)


def net_position_units(long_units: float, short_units: float) -> float:
    """Combine the two Oanda position sides into one signed holding.

    Oanda already signs each side: a long is positive and a short is
    negative. Subtracting a negative short turns it into a long and the
    next reduce order is sent on the wrong side.
    """
    if short_units > 0:
        logger.warning(
            "Oanda short units are positive (%s); treating them as a magnitude",
            short_units,
        )
        return long_units - short_units
    return long_units + short_units


def trade_step(instrument: dict) -> float:
    """Return the positive order step for one Oanda instrument.

    ``minimumTradeSize`` is the step. A quoted size between 0 and 1, such as
    ``0.1`` for AU200_AUD, is kept. A zero minimum falls back to
    ``tradeUnitsPrecision`` when that field is present.
    """
    name = str(instrument.get("name", "?"))
    raw = instrument.get("minimumTradeSize")
    try:
        step = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} has no minimumTradeSize") from exc
    if math.isfinite(step) and step > 0:
        return step
    precision = instrument.get("tradeUnitsPrecision")
    try:
        places = int(precision)
    except (TypeError, ValueError):
        places = -1
    if places >= 0:
        fallback = 10.0 ** (-places)
        logger.warning(
            "%s minimumTradeSize=%r; using tradeUnitsPrecision %s step %s",
            name,
            raw,
            places,
            fallback,
        )
        return fallback
    raise RuntimeError(f"{name} minimumTradeSize={raw!r}")


def format_units(units: float) -> str:
    """Render units the way Oanda expects them, without binary float noise."""
    cleaned = Decimal(str(units)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(cleaned, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        return "0"
    return text


def _parse_fill(payload: dict, requested_units: float) -> FillResult:
    if not isinstance(payload, dict):
        return FillResult("rejected", 0, 0.0, f"unexpected response {payload!r}")
    if payload.get("orderRejectTransaction") or payload.get("orderCancelTransaction"):
        message = (
            payload.get("orderRejectTransaction")
            or payload.get("orderCancelTransaction")
            or {}
        )
        reason = message.get("rejectReason") or message.get("reason") or message
        return FillResult("rejected", 0, 0.0, str(reason))
    fill = payload.get("orderFillTransaction")
    if not fill:
        return FillResult("rejected", 0, 0.0, f"no fill in {payload!r}")
    try:
        filled = float(fill.get("units", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"fill units are not numeric: {fill!r}") from exc
    price = float(fill.get("price", 0) or 0)
    if math.isclose(filled, 0.0, abs_tol=1e-9):
        return FillResult("rejected", 0, price, "fill reported zero units")
    status = "filled" if math.isclose(abs(filled), abs(float(requested_units)), abs_tol=1e-9) else "partial"
    return FillResult(status, filled, price, "")


def submit_plan(plan: RebalancePlan, broker, store: PlanStore) -> RebalancePlan:
    """Persist the plan before the first order, then send reduce legs first.

    A session that is already complete, failed, skipped, or simulated is
    returned unchanged. An unfinished plan is resumed. Broker fills replace
    the saved remainder. A second rejection of the same leg fails the session.
    """
    if plan.status == "simulated":
        raise RuntimeError(
            "refusing to send a simulated research plan; build a new practice plan"
        )
    broker.assert_practice()
    existing = store.load(plan.session_date)
    if existing is not None and existing.status == "simulated":
        raise RuntimeError(
            f"session {plan.session_date} is a simulated research plan and will not be sent"
        )
    if existing is not None and existing.status in _TERMINAL:
        logger.info("Session %s is already %s", plan.session_date, existing.status)
        return existing
    if existing is not None:
        if plan.allocation_scale > existing.allocation_scale + 1e-6:
            logger.warning(
                "Session %s lot size moves from scale %.6f to %.6f "
                "so margin can reach the target",
                existing.session_date,
                existing.allocation_scale,
                plan.allocation_scale,
            )
            existing.target_units = dict(plan.target_units)
            existing.weights = dict(plan.weights)
            existing.allocation_scale = plan.allocation_scale
            existing.estimated_margin = plan.estimated_margin
            existing.margin_utilization = plan.margin_utilization
            existing.margin_budget = plan.margin_budget
            existing.margin_capacity = plan.margin_capacity
            existing.nav = plan.nav
            existing.mids = dict(plan.mids)
            existing.margin_rates = dict(plan.margin_rates)
            existing.quote_home_conversions = dict(plan.quote_home_conversions)
            existing.trade_steps = dict(plan.trade_steps)
            existing.min_trade_units = plan.min_trade_units
            existing.note = plan.note
        plan = existing
        if callable(getattr(broker, "positions", None)):
            reconcile_plan(plan, broker.positions())
            store.save(plan)
            if plan.status == "complete":
                logger.info("Session %s already matches the broker", plan.session_date)
                return plan
    else:
        plan.status = "pending"
        plan.sent_to_broker = False
        store.save(plan)
    return _execute(plan, broker, store)


def _order_margin(plan: RebalancePlan, instrument: str, units: float) -> float:
    try:
        mid = float(plan.mids[instrument])
        conversion = float(plan.quote_home_conversions[instrument])
        rate = float(plan.margin_rates[instrument])
    except KeyError as exc:
        raise RuntimeError(f"{instrument} has no saved margin inputs") from exc
    if mid <= 0 or conversion <= 0 or rate <= 0:
        raise RuntimeError(
            f"{instrument} has invalid margin inputs mid={mid} conversion={conversion} rate={rate}"
        )
    return abs(float(units)) * mid * conversion * rate


def _execute(plan: RebalancePlan, broker, store: PlanStore) -> RebalancePlan:
    for phase in ("reduce", "increase"):
        if phase == "increase" and callable(getattr(broker, "positions", None)):
            reconcile_plan(plan, broker.positions())
            store.save(plan)
            if not any(
                leg.phase == "increase" and leg.status not in ("filled", "skipped")
                for leg in plan.legs
            ):
                continue
        plan.status = "reducing" if phase == "reduce" else "increasing"
        store.save(plan)
        for leg in plan.legs:
            if leg.phase != phase or leg.status == "filled":
                continue
            if math.isclose(leg.remainder, 0.0, abs_tol=1e-9):
                leg.status = "filled"
                continue
            if phase == "increase" and plan.margin_capacity and plan.margin_rates:
                summary = broker.account_summary()
                used = float(summary["marginUsed"])
                available = float(summary["marginAvailable"])
                capacity = used + available
                if capacity <= 0:
                    raise RuntimeError(f"account margin capacity is {capacity}")
                projected = (used + _order_margin(plan, leg.instrument, leg.remainder)) / capacity
                if projected > MARGIN_CEILING + 1e-8:
                    plan.note = (
                        f"{leg.instrument} projected margin utilization {projected:.2%} "
                        f"exceeds the {MARGIN_CEILING:.0%} ceiling"
                    )
                    logger.error(plan.note)
                    store.save(plan)
                    return plan
            result = broker.place_market(
                leg.instrument,
                leg.remainder,
                leg.client_order_id,
                leg.position_fill,
            )
            plan.sent_to_broker = True
            leg.attempts += 1
            leg.message = result.message
            if result.status == "rejected":
                leg.status = "rejected"
                store.save(plan)
                if "MARKET_HALTED" in result.message:
                    leg.status = "skipped"
                    logger.warning(
                        "Leg %s skipped because %s is halted; the remaining orders continue",
                        leg.client_order_id,
                        leg.instrument,
                    )
                    store.save(plan)
                    continue
                if "INSUFFICIENT_MARGIN" in result.message:
                    plan.status = "failed"
                    plan.note = (
                        f"{leg.client_order_id} rejected: INSUFFICIENT_MARGIN; "
                        "unsafe plan will not be retried"
                    )
                    logger.error(plan.note)
                    store.save(plan)
                elif leg.attempts >= _MAX_ATTEMPTS:
                    plan.status = "failed"
                    plan.note = f"{leg.client_order_id} rejected: {result.message}"
                    logger.error(plan.note)
                    store.save(plan)
                else:
                    logger.error(
                        "Leg %s rejected (%s); session %s left unfinished",
                        leg.client_order_id,
                        result.message,
                        plan.session_date,
                    )
                return plan
            leg.filled_units += result.filled_units
            if result.status == "partial" or not math.isclose(leg.remainder, 0.0, abs_tol=1e-9):
                leg.status = "partial"
                plan.note = (
                    f"{leg.instrument} partial fill {leg.filled_units} of {leg.units}"
                )
                logger.error(plan.note)
                store.save(plan)
                return plan
            leg.status = "filled"
            store.save(plan)
        if any(
            leg.phase == phase and leg.status not in ("filled", "skipped")
            for leg in plan.legs
        ):
            return plan
    plan.status = "complete"
    store.save(plan)
    logger.info("Session %s complete", plan.session_date)
    return plan
