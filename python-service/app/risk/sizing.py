"""Position sizing.

The only formula that decides trade size:

    risk_amount   = equity x risk_per_trade
    stop_distance = |entry - stop_loss|
    quantity      = risk_amount / stop_distance

``risk_per_trade`` is the fraction of equity intentionally lost if the stop is
hit — it is *not* the fraction of equity deployed. At 0.5% risk with a 2% stop,
a 10,000 account buys 2,500 of notional (25% of equity) while risking 50.

The raw quantity is then reduced (never increased) by, in order:

1. maximum position size as a share of equity
2. remaining portfolio exposure headroom
3. available cash including the entry fee
4. the exchange lot grid, rounding DOWN

Every cap that bites is reported in ``capped_by`` so the log shows exactly why a
trade was smaller than the risk budget implied. Rounding is always down and
caps never expand size, so the realised risk can only ever be *less* than the
budget.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.numeric import ZERO, bps, quantize_quantity, round_money, safe_div
from app.models.risk import PositionSizing
from app.models.trading import SymbolSpec


def calculate_position_size(
    *,
    equity: Decimal,
    cash: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    risk_per_trade: Decimal,
    max_position_pct_equity: Decimal,
    max_portfolio_exposure_pct: Decimal,
    current_exposure_value: Decimal,
    spec: SymbolSpec,
    taker_fee_bps: Decimal | None = None,
) -> PositionSizing:
    capped_by: list[str] = []

    if equity <= ZERO:
        return _rejected(
            equity, cash, entry, stop_loss, risk_per_trade, "equity is zero or negative"
        )
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= ZERO:
        return _rejected(
            equity, cash, entry, stop_loss, risk_per_trade, "stop distance is zero"
        )

    risk_amount = round_money(equity * risk_per_trade, 8)
    raw_quantity = risk_amount / stop_distance
    quantity = raw_quantity

    # 1. maximum single-position notional
    max_notional = equity * max_position_pct_equity
    position_cap = safe_div(max_notional, entry)
    if position_cap < quantity:
        quantity = position_cap
        capped_by.append("max_position_pct_equity")

    # 2. portfolio exposure headroom
    exposure_budget = equity * max_portfolio_exposure_pct
    headroom = exposure_budget - current_exposure_value
    if headroom <= ZERO:
        return _rejected(
            equity,
            cash,
            entry,
            stop_loss,
            risk_per_trade,
            "portfolio exposure limit already reached",
            stop_distance=stop_distance,
            raw_quantity=raw_quantity,
        )
    exposure_cap = safe_div(headroom, entry)
    if exposure_cap < quantity:
        quantity = exposure_cap
        capped_by.append("max_portfolio_exposure_pct")

    # 3. cash, including the fee we will pay on entry
    fee_multiplier = Decimal("1") + bps(taker_fee_bps if taker_fee_bps is not None else spec.taker_fee_bps)
    affordable = safe_div(cash, entry * fee_multiplier)
    if affordable < quantity:
        quantity = affordable
        capped_by.append("available_cash")

    # 4. exchange lot grid (always DOWN)
    quantity = quantize_quantity(quantity, spec.quantity_step)

    notional = round_money(quantity * entry, 8)
    if quantity <= ZERO:
        return _rejected(
            equity,
            cash,
            entry,
            stop_loss,
            risk_per_trade,
            f"quantity rounds to zero on lot step {spec.quantity_step}",
            stop_distance=stop_distance,
            raw_quantity=raw_quantity,
            capped_by=capped_by,
        )
    if spec.min_quantity and quantity < spec.min_quantity:
        return _rejected(
            equity,
            cash,
            entry,
            stop_loss,
            risk_per_trade,
            f"quantity {quantity} below exchange minimum {spec.min_quantity}",
            stop_distance=stop_distance,
            raw_quantity=raw_quantity,
            capped_by=capped_by,
        )
    if spec.min_notional and notional < spec.min_notional:
        return _rejected(
            equity,
            cash,
            entry,
            stop_loss,
            risk_per_trade,
            f"notional {notional} below exchange minimum {spec.min_notional}",
            stop_distance=stop_distance,
            raw_quantity=raw_quantity,
            capped_by=capped_by,
        )

    effective_risk = round_money(quantity * stop_distance, 8)
    return PositionSizing(
        equity=equity,
        risk_pct=risk_per_trade,
        risk_amount=risk_amount,
        entry=entry,
        stop_loss=stop_loss,
        stop_distance=stop_distance,
        stop_distance_pct=safe_div(stop_distance, entry),
        raw_quantity=raw_quantity,
        quantity=quantity,
        notional=notional,
        notional_pct_equity=safe_div(notional, equity),
        effective_risk_amount=effective_risk,
        effective_risk_pct=safe_div(effective_risk, equity),
        capped_by=capped_by,
    )


def _rejected(
    equity: Decimal,
    cash: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    risk_per_trade: Decimal,
    reason: str,
    *,
    stop_distance: Decimal | None = None,
    raw_quantity: Decimal | None = None,
    capped_by: list[str] | None = None,
) -> PositionSizing:
    distance = stop_distance if stop_distance is not None else abs(entry - stop_loss)
    return PositionSizing(
        equity=equity,
        risk_pct=risk_per_trade,
        risk_amount=round_money(max(ZERO, equity) * risk_per_trade, 8),
        entry=entry,
        stop_loss=stop_loss,
        stop_distance=distance,
        stop_distance_pct=safe_div(distance, entry) if entry > ZERO else ZERO,
        raw_quantity=raw_quantity or ZERO,
        quantity=ZERO,
        notional=ZERO,
        notional_pct_equity=ZERO,
        effective_risk_amount=ZERO,
        effective_risk_pct=ZERO,
        capped_by=capped_by or [],
        rejected_reason=reason,
    )
