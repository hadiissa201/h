"""Fill simulation maths.

Pure functions so the cost model can be unit-tested on its own and reused
identically by the paper exchange and the backtester. If these two ever diverge,
backtest results stop predicting paper results — which is the single most common
way a "profitable" system turns out not to be.

Cost stack applied to every simulated market fill:

1. **Spread** — you buy at the ask, sell at the bid, never at the mid.
2. **Slippage** — a fixed base in bps plus a size-dependent impact term.
3. **Fee** — taker or maker bps on the filled notional.

The defaults are deliberately pessimistic. Under-modelling costs is how a
strategy looks profitable in simulation and bleeds in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.numeric import ZERO, bps, round_money, to_decimal
from app.models.enums import Side


@dataclass(frozen=True)
class CostModel:
    taker_fee_bps: Decimal = Decimal("10")
    maker_fee_bps: Decimal = Decimal("8")
    slippage_bps: Decimal = Decimal("5")
    spread_bps: Decimal = Decimal("4")
    # Extra slippage per 100k of quote notional — a crude but non-zero
    # acknowledgement that larger orders walk the book.
    impact_bps_per_100k: Decimal = Decimal("2")

    @classmethod
    def from_settings(cls, settings) -> CostModel:  # noqa: ANN001 - avoids import cycle
        return cls(
            taker_fee_bps=to_decimal(settings.paper_taker_fee_bps),
            maker_fee_bps=to_decimal(settings.paper_maker_fee_bps),
            slippage_bps=to_decimal(settings.paper_slippage_bps),
            spread_bps=to_decimal(settings.paper_spread_bps),
        )


@dataclass(frozen=True)
class SimulatedFill:
    price: Decimal
    quantity: Decimal
    notional: Decimal
    fee: Decimal
    slippage_bps: Decimal
    reference_price: Decimal
    is_maker: bool


def apply_spread(reference_price: Decimal, side: Side, spread_bps: Decimal) -> Decimal:
    """Cross the spread: buys lift the ask, sells hit the bid."""
    half = bps(spread_bps) / Decimal("2")
    if side is Side.BUY:
        return reference_price * (Decimal("1") + half)
    return reference_price * (Decimal("1") - half)


def impact_bps(notional: Decimal, impact_bps_per_100k: Decimal) -> Decimal:
    if notional <= ZERO or impact_bps_per_100k <= ZERO:
        return ZERO
    return impact_bps_per_100k * (notional / Decimal("100000"))


def simulate_market_fill(
    reference_price: Decimal,
    quantity: Decimal,
    side: Side,
    model: CostModel,
    *,
    fee_currency_is_quote: bool = True,
) -> SimulatedFill:
    """Simulate a taker fill at ``reference_price`` (the mid).

    Returns the achieved price including spread and slippage, and the fee on the
    resulting notional. Slippage always moves *against* the order.
    """
    if quantity <= ZERO:
        raise ValueError("quantity must be positive")
    if reference_price <= ZERO:
        raise ValueError("reference price must be positive")

    price_after_spread = apply_spread(reference_price, side, model.spread_bps)
    rough_notional = price_after_spread * quantity
    total_slippage_bps = model.slippage_bps + impact_bps(
        rough_notional, model.impact_bps_per_100k
    )
    slip = bps(total_slippage_bps)
    price = (
        price_after_spread * (Decimal("1") + slip)
        if side is Side.BUY
        else price_after_spread * (Decimal("1") - slip)
    )
    price = round_money(price, 8)
    notional = round_money(price * quantity, 8)
    fee = round_money(notional * bps(model.taker_fee_bps), 8)
    if not fee_currency_is_quote:  # pragma: no cover - spot uses quote fees
        fee = round_money(quantity * bps(model.taker_fee_bps), 8)
    # Effective slippage relative to the mid, including the spread cost, which
    # is what actually matters when comparing to a backtest assumption.
    effective_bps = abs(price - reference_price) / reference_price * Decimal("10000")
    return SimulatedFill(
        price=price,
        quantity=quantity,
        notional=notional,
        fee=fee,
        slippage_bps=round_money(effective_bps, 4),
        reference_price=reference_price,
        is_maker=False,
    )


def simulate_limit_fill(
    limit_price: Decimal,
    quantity: Decimal,
    side: Side,
    model: CostModel,
) -> SimulatedFill:
    """Simulate a resting limit order that gets filled at its limit price.

    No slippage (the price is the price) and maker fees, but only ever used when
    the market has actually traded through the limit.
    """
    if quantity <= ZERO:
        raise ValueError("quantity must be positive")
    notional = round_money(limit_price * quantity, 8)
    fee = round_money(notional * bps(model.maker_fee_bps), 8)
    return SimulatedFill(
        price=round_money(limit_price, 8),
        quantity=quantity,
        notional=notional,
        fee=fee,
        slippage_bps=ZERO,
        reference_price=limit_price,
        is_maker=True,
    )


def stop_fill_price(
    stop_price: Decimal, side: Side, model: CostModel, gap_price: Decimal | None = None
) -> Decimal:
    """Price a stop actually fills at.

    A stop becomes a market order, so it pays spread and slippage, and when the
    market gapped through the level it fills at the gap price — not at the stop.
    Modelling this is the difference between an honest backtest and a fantasy
    one where stops always hold.
    """
    reference = stop_price
    if gap_price is not None:
        if side is Side.SELL:
            reference = min(stop_price, gap_price)
        else:
            reference = max(stop_price, gap_price)
    fill = simulate_market_fill(reference, Decimal("1"), side, model)
    return fill.price
