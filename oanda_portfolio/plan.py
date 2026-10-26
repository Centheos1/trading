"""Turn target weights into a two-phase Oanda order plan.

Reducing legs, including a flatten when the sign changes, are ordered
before any increasing leg. A gap smaller than the minimum trade size is
omitted and counted. The plan is data. Nothing in this module contacts
a broker.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

MARGIN_TARGET = 0.25
MARGIN_FLOOR = 0.20
MARGIN_CEILING = 0.30


@dataclass
class OrderLeg:
    client_order_id: str
    instrument: str
    phase: str
    units: float
    filled_units: float = 0
    status: str = "pending"
    attempts: int = 0
    position_fill: str = "REDUCE_ONLY"
    message: str = ""

    @property
    def remainder(self) -> float:
        return self.units - self.filled_units


@dataclass
class RebalancePlan:
    session_date: str
    list_version: str
    nav: float
    status: str
    target_return_used: float | None
    target_units: dict[str, float]
    weights: dict[str, float]
    mix_weights: dict[str, float] = field(default_factory=dict)
    legs: list[OrderLeg] = field(default_factory=list)
    skips: list[dict] = field(default_factory=list)
    sent_to_broker: bool = False
    note: str = ""
    allocation_scale: float = 1.0
    estimated_margin: float | None = None
    margin_budget: float | None = None
    margin_capacity: float | None = None
    margin_utilization: float | None = None
    min_trade_units: float = 1.0
    mids: dict[str, float] = field(default_factory=dict)
    margin_rates: dict[str, float] = field(default_factory=dict)
    quote_home_conversions: dict[str, float] = field(default_factory=dict)
    trade_steps: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["legs"] = [asdict(leg) for leg in self.legs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "RebalancePlan":
        legs = [OrderLeg(**leg) for leg in payload.get("legs", [])]
        data = dict(payload)
        data["legs"] = legs
        data.setdefault("mix_weights", {})
        return cls(**data)


def quantize_units(raw: float, step: float) -> int | float:
    """Round units to the instrument step. Whole steps stay integers."""
    if step <= 0 or not _finite(step) or not _finite(raw):
        raise ValueError(f"cannot quantize units raw={raw} step={step}")
    quantum = Decimal(str(step))
    count = (Decimal(str(raw)) / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    units = count * quantum
    if units == units.to_integral_value():
        return int(units)
    return float(units)


def units_from_weight(
    weight: float,
    nav: float,
    mid: float,
    *,
    step: float = 1,
    quote_home_conversion: float = 1.0,
) -> int | float:
    """Convert an account-currency weight into signed instrument units."""
    if step <= 0 or not _finite(step):
        raise ValueError(f"trade step must be positive, got {step}")
    if nav <= 0 or not _finite(nav):
        raise ValueError(f"NAV must be positive, got {nav}")
    if mid <= 0 or not _finite(mid):
        raise ValueError(f"mid price must be positive, got {mid}")
    if quote_home_conversion <= 0 or not _finite(quote_home_conversion):
        raise ValueError(
            f"quote-to-home conversion must be positive, got {quote_home_conversion}"
        )
    return quantize_units(
        weight * nav / (mid * quote_home_conversion),
        step,
    )


def _margin_required(
    units: dict[str, float],
    mids: dict[str, float],
    quote_home_conversions: dict[str, float],
    margin_rates: dict[str, float],
) -> float:
    required = 0.0
    for name, quantity in units.items():
        if _near_zero(quantity):
            continue
        try:
            mid = float(mids[name])
            conversion = float(quote_home_conversions[name])
            rate = float(margin_rates[name])
        except KeyError as exc:
            raise ValueError(f"{name} has incomplete margin inputs") from exc
        if mid <= 0 or conversion <= 0 or rate <= 0:
            raise ValueError(
                f"{name} has invalid margin inputs "
                f"mid={mid} conversion={conversion} rate={rate}"
            )
        required += abs(float(quantity)) * mid * conversion * rate
    return required


def build_legs(
    *,
    session_date: date | str,
    current_units: dict[str, float],
    target_units: dict[str, float],
    min_trade_units: float,
    trade_steps: dict[str, float] | None = None,
    used_ids: set[str] | None = None,
) -> tuple[list[OrderLeg], list[dict]]:
    """Split the gap to the target into reduce and increase legs.

    A sign change is a flatten followed by an open. Dust below the minimum
    trade size is skipped. A sign change that cannot flatten the current
    position does not open the other side. A per-instrument step, when
    present, is that instrument's minimum order.
    """
    if min_trade_units <= 0:
        raise ValueError("min_trade_units must be positive")
    steps = trade_steps or {}
    day = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    names = sorted(set(current_units) | set(target_units))
    reduce_legs: list[OrderLeg] = []
    increase_legs: list[OrderLeg] = []
    skips: list[dict] = []
    sequence = 0
    taken_ids = set(used_ids or ())

    def minimum(instrument: str) -> float:
        step = float(steps.get(instrument, min_trade_units))
        if step <= 0 or not _finite(step):
            raise ValueError(f"{instrument} trade step must be positive, got {step}")
        return step

    def add(phase: str, instrument: str, units: float) -> None:
        nonlocal sequence
        units = quantize_units(float(units), minimum(instrument))
        if _near_zero(units):
            skip(instrument, "rounded order is zero", units)
            return
        token = "".join(character for character in instrument if character.isalnum())
        while True:
            sequence += 1
            client_order_id = f"{day.replace('-', '')}{phase[0]}{sequence:02d}{token}"
            if client_order_id not in taken_ids:
                break
        taken_ids.add(client_order_id)
        leg = OrderLeg(
            client_order_id=client_order_id,
            instrument=instrument,
            phase=phase,
            units=units,
            position_fill="REDUCE_ONLY" if phase == "reduce" else "OPEN_ONLY",
        )
        if phase == "reduce":
            reduce_legs.append(leg)
        else:
            increase_legs.append(leg)

    def skip(instrument: str, reason: str, units: float) -> None:
        skips.append({"instrument": instrument, "reason": reason, "units": units})
        logger.info("Skipped %s units=%s reason=%s", instrument, units, reason)

    for name in names:
        current = float(current_units.get(name, 0))
        target = float(target_units.get(name, 0))
        floor = minimum(name)
        if _near_zero(current - target):
            continue
        sign_change = (
            not _near_zero(current)
            and not _near_zero(target)
            and (current > 0) != (target > 0)
        )
        flatten = not _near_zero(current) and (_near_zero(target) or sign_change)
        if flatten:
            if abs(current) < floor:
                skip(name, "flatten below minimum trade size", -current)
                if not _near_zero(target):
                    skip(name, "open blocked because the current position could not be flattened", target)
                continue
            add("reduce", name, -current)
            if _near_zero(target):
                continue
            if abs(target) < floor:
                skip(name, "open below minimum trade size", target)
                continue
            add("increase", name, target)
            continue
        delta = quantize_units(target - current, floor)
        if abs(delta) < floor:
            skip(name, "delta below minimum trade size", delta)
            continue
        phase = "reduce" if abs(target) < abs(current) else "increase"
        add(phase, name, delta)
    return reduce_legs + increase_legs, skips


def reconcile_plan(
    plan: RebalancePlan,
    current_units: dict[str, float],
    trade_steps: dict[str, float] | None = None,
) -> RebalancePlan:
    """Replace unfilled legs with the gap from the broker's current positions.

    Filled legs stay as the record of what the broker already did. The new
    legs are the difference between the saved targets and the positions the
    broker reports now.
    """
    steps = trade_steps or plan.trade_steps
    filled = [leg for leg in plan.legs if leg.status == "filled"]
    fresh, skips = build_legs(
        session_date=plan.session_date,
        current_units=current_units,
        target_units=plan.target_units,
        min_trade_units=plan.min_trade_units,
        trade_steps=steps,
        used_ids={leg.client_order_id for leg in plan.legs},
    )
    plan.legs = filled + fresh
    plan.skips.extend(skips)
    plan.trade_steps = {name: float(step) for name, step in steps.items()}
    plan.status = "pending" if fresh else "complete"
    logger.info(
        "Reconciled session %s to broker positions; open legs=%d",
        plan.session_date,
        len(fresh),
    )
    return plan


def margin_notional_weights(
    mix: dict[str, float],
    margin_rates: dict[str, float],
    *,
    margin_fraction: float,
) -> dict[str, float]:
    """Turn a fully invested mix into notional fractions of NAV.

    ``mix`` sums to about 1. Each name receives
    ``margin_fraction * |mix| / gross(mix)`` of NAV as margin, and its
    notional is that margin divided by the instrument margin rate.
    The account return is the sum of these notionals times price returns.
    """
    if margin_fraction <= 0 or not _finite(margin_fraction) or margin_fraction > MARGIN_CEILING:
        raise ValueError(
            f"margin fraction must be in (0, {MARGIN_CEILING:.0%}], got {margin_fraction}"
        )
    gross = sum(abs(float(weight)) for weight in mix.values())
    if gross <= 0 or not _finite(gross):
        raise ValueError(f"mix gross must be positive, got {gross}")
    traded: dict[str, float] = {}
    for name, weight in mix.items():
        signed = float(weight)
        if _near_zero(signed):
            traded[name] = 0.0
            continue
        try:
            rate = float(margin_rates[name])
        except KeyError as exc:
            raise ValueError(f"{name} has no margin rate") from exc
        if rate <= 0 or not _finite(rate):
            raise ValueError(f"{name} margin rate must be positive, got {rate}")
        traded[name] = margin_fraction * signed / (gross * rate)
    return traded


def _allocation_scale(mix: dict[str, float], traded: dict[str, float]) -> float:
    """Return the common notional multiple when every name shares one rate."""
    ratios: list[float] = []
    for name, weight in mix.items():
        signed = float(weight)
        if _near_zero(signed):
            continue
        ratios.append(float(traded[name]) / signed)
    if not ratios:
        return 1.0
    if all(abs(ratio - ratios[0]) <= 1e-6 * max(1.0, abs(ratios[0])) for ratio in ratios):
        return ratios[0]
    return 1.0


def build_plan(
    *,
    session_date: date | str,
    list_version: str,
    nav: float,
    weights: dict[str, float],
    mids: dict[str, float],
    current_units: dict[str, float],
    min_trade_units: float,
    target_return_used: float | None,
    trade_steps: dict[str, float] | None = None,
    quote_home_conversions: dict[str, float] | None = None,
    margin_rates: dict[str, float] | None = None,
    margin_budget: float | None = None,
    margin_capacity: float | None = None,
    weight_cap: float = 0.30,
    gross_cap: float = 2.0,
    note: str = "",
    sent_to_broker: bool = False,
) -> RebalancePlan:
    """Build the full plan for one New York session. Does not send it.

    ``weights`` is the sum-to-1 mix. When ``margin_capacity`` is set, each
    name's notional is its share of the 25% margin budget divided by its
    margin rate. ``weight_cap`` and ``gross_cap`` describe that mix. They
    are not applied again to the notionals.
    """
    steps = trade_steps or {}
    conversions = quote_home_conversions or {
        name: 1.0 for name in weights
    }
    rates = margin_rates or {}
    if weight_cap <= 0 or not _finite(weight_cap):
        raise ValueError(f"weight cap must be positive, got {weight_cap}")
    if gross_cap < 1 or not _finite(gross_cap):
        raise ValueError(f"gross cap must be at least 1, got {gross_cap}")
    if margin_budget is not None and (
        margin_budget <= 0 or not _finite(margin_budget)
    ):
        raise ValueError(f"margin budget must be positive, got {margin_budget}")
    mix = {name: float(value) for name, value in weights.items()}
    sized = dict(mix)
    allocation_scale = 1.0
    margin_utilization = None

    def targets(scale: float, book: dict[str, float]) -> dict[str, float]:
        result: dict[str, float] = {}
        for name, weight in book.items():
            step = float(steps.get(name, min_trade_units))
            try:
                conversion = float(conversions[name])
            except KeyError as exc:
                raise ValueError(
                    f"{name} has no quote-to-home conversion"
                ) from exc
            result[name] = units_from_weight(
                float(weight) * scale,
                nav,
                float(mids[name]),
                step=step,
                quote_home_conversion=conversion,
            )
        return result

    target_units = targets(allocation_scale, sized)
    estimated_margin = None
    if margin_capacity is not None:
        if not rates:
            raise ValueError("margin rates are required when margin capacity is set")
        fraction = MARGIN_TARGET * float(margin_capacity) / float(nav)
        if fraction > MARGIN_CEILING:
            logger.warning(
                "margin target %.2f%% of capacity is above the %.0f%% ceiling; clamping",
                fraction * 100,
                MARGIN_CEILING * 100,
            )
            fraction = MARGIN_CEILING
        sized = margin_notional_weights(mix, rates, margin_fraction=fraction)
        allocation_scale = _allocation_scale(mix, sized)
        target_units = targets(1.0, sized)
        estimated_margin = _margin_required(target_units, mids, conversions, rates)
        ceiling = margin_capacity * MARGIN_CEILING
        for _ in range(8):
            if estimated_margin <= ceiling + 1e-6:
                break
            shrink = ceiling / estimated_margin * 0.999
            sized = {name: value * shrink for name, value in sized.items()}
            allocation_scale *= shrink
            target_units = targets(1.0, sized)
            estimated_margin = _margin_required(target_units, mids, conversions, rates)
        if estimated_margin > ceiling + 1e-6:
            raise ValueError(
                f"rounded target margin {estimated_margin:.2f} exceeds the "
                f"{MARGIN_CEILING:.0%} ceiling {ceiling:.2f}"
            )
        margin_utilization = estimated_margin / margin_capacity
        if margin_budget is None or ceiling < margin_budget:
            margin_budget = ceiling
        margin_note = (
            f"margin budget {fraction:.2%} of NAV split by mix weight and divided by "
            f"margin rate; utilization {margin_utilization:.2%}"
        )
        if margin_utilization < MARGIN_FLOOR:
            logger.warning(margin_note)
        else:
            logger.info(margin_note)
        note = f"{note}; {margin_note}" if note else margin_note
    elif margin_budget is not None:
        if not rates:
            raise ValueError("margin rates are required when a margin budget is set")
        estimated_margin = _margin_required(
            target_units, mids, conversions, rates
        )
        for _ in range(10):
            if estimated_margin <= margin_budget + 1e-8:
                break
            allocation_scale *= margin_budget / estimated_margin * 0.999
            target_units = targets(allocation_scale, mix)
            estimated_margin = _margin_required(
                target_units, mids, conversions, rates
            )
        if estimated_margin > margin_budget + 1e-8:
            raise ValueError(
                "rounded target margin exceeds the budget "
                f"required={estimated_margin} budget={margin_budget}"
            )
        if allocation_scale < 1.0:
            margin_note = (
                f"allocation scaled to {allocation_scale:.6f}; "
                f"estimated margin {estimated_margin:.2f} of "
                f"{margin_budget:.2f}"
            )
            logger.warning(margin_note)
            note = f"{note}; {margin_note}" if note else margin_note

    held = {name for name, units in current_units.items() if not _near_zero(units)}
    for name in held:
        target_units.setdefault(name, 0)
    day = session_date if isinstance(session_date, date) else date.fromisoformat(str(session_date)[:10])
    legs, skips = build_legs(
        session_date=day,
        current_units=current_units,
        target_units=target_units,
        min_trade_units=min_trade_units,
        trade_steps=steps,
    )
    status = "pending" if legs else "complete"
    return RebalancePlan(
        session_date=day.isoformat(),
        list_version=list_version,
        nav=float(nav),
        status=status,
        target_return_used=target_return_used,
        target_units=target_units,
        weights={name: float(value) for name, value in sized.items()},
        mix_weights=mix,
        legs=legs,
        skips=skips,
        sent_to_broker=sent_to_broker,
        note=note,
        allocation_scale=allocation_scale,
        estimated_margin=estimated_margin,
        margin_budget=margin_budget,
        margin_capacity=margin_capacity,
        margin_utilization=margin_utilization,
        min_trade_units=float(min_trade_units),
        mids={name: float(value) for name, value in mids.items()},
        margin_rates={name: float(value) for name, value in rates.items()},
        quote_home_conversions={name: float(value) for name, value in conversions.items()},
        trade_steps={name: float(value) for name, value in steps.items()},
    )


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _near_zero(value: float) -> bool:
    return abs(float(value)) <= 1e-9
