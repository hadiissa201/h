"""Money-safe numeric helpers.

All prices, quantities and balances are ``Decimal`` end to end. Floats are only
used inside the indicator/feature layer (numpy) where relative precision is
irrelevant and speed matters.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    """Convert anything reasonable into a Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        if default is None:
            raise ValueError("cannot convert None/empty to Decimal without a default")
        return default
    try:
        if isinstance(value, float):
            # str() first: Decimal(0.1) would carry binary noise.
            return Decimal(repr(value))
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"cannot convert {value!r} to Decimal") from exc


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    """Round a price DOWN/UP to the exchange tick grid (nearest tick)."""
    if tick_size <= ZERO:
        return value
    steps = (value / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * tick_size).normalize()


def quantize_quantity(value: Decimal, step_size: Decimal) -> Decimal:
    """Round a quantity DOWN to the exchange lot grid.

    Always rounds down: overshooting a lot size gets the order rejected, and
    rounding up would silently increase risk beyond the sized amount.
    """
    if step_size <= ZERO:
        return value
    steps = (value / step_size).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step_size).normalize()


def bps(value: Decimal | int | float) -> Decimal:
    """Basis points -> fraction. ``bps(10) == Decimal('0.001')``."""
    return to_decimal(value) / BPS


def pct_change(new: Decimal, old: Decimal) -> Decimal:
    if old == ZERO:
        return ZERO
    return (new - old) / old


def safe_div(numerator: Decimal, denominator: Decimal, default: Decimal = ZERO) -> Decimal:
    if denominator == ZERO:
        return default
    return numerator / denominator


def round_money(value: Decimal, places: int = 8) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
