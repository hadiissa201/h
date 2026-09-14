"""Position sizing.

The arithmetic here decides how much money is at stake, so every case is checked
against a number worked out by hand in the test itself.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.trading import SymbolSpec
from app.risk.sizing import calculate_position_size

SPEC = SymbolSpec(
    symbol="BTC/USDT",
    base="BTC",
    quote="USDT",
    price_tick=Decimal("0.01"),
    quantity_step=Decimal("0.00001"),
    min_quantity=Decimal("0.00001"),
    min_notional=Decimal("10"),
    taker_fee_bps=Decimal("10"),
)


def size(**overrides):
    payload = {
        "equity": Decimal("10000"),
        "cash": Decimal("10000"),
        "entry": Decimal("100"),
        "stop_loss": Decimal("98"),
        "risk_per_trade": Decimal("0.005"),
        "max_position_pct_equity": Decimal("0.20"),
        "max_portfolio_exposure_pct": Decimal("0.50"),
        "current_exposure_value": Decimal("0"),
        "spec": SPEC,
    }
    payload.update(overrides)
    return calculate_position_size(**payload)


def test_quantity_is_risk_budget_divided_by_stop_distance():
    # 10,000 x 0.5% = 50 risked; stop is 4 away => 12.5 units.
    result = size(stop_loss=Decimal("96"))
    assert result.risk_amount == Decimal("50.00000000")
    assert result.stop_distance == Decimal("4")
    assert result.quantity == Decimal("12.5")
    assert result.notional == Decimal("1250.00000000")
    assert not result.capped_by


def test_risk_is_not_the_same_as_position_size():
    """The classic confusion: 0.5% risk deploys 12.5% of equity at a 4% stop."""
    result = size(stop_loss=Decimal("96"))
    assert result.effective_risk_pct == pytest.approx(Decimal("0.005"), abs=Decimal("1e-9"))
    assert result.notional_pct_equity == pytest.approx(Decimal("0.125"), abs=Decimal("1e-9"))


def test_tight_stops_hit_the_position_cap_at_default_limits():
    """Worth knowing: with 0.5% risk and a 20% position cap, any stop tighter
    than 2.5% is trimmed by the cap rather than by the risk budget."""
    result = size(stop_loss=Decimal("98"))  # 2% stop
    assert "max_position_pct_equity" in result.capped_by
    assert result.notional == Decimal("2000.00000000")  # exactly the 20% cap
    assert result.quantity == Decimal("20")
    # Less size than the risk budget wanted means less risk, never more.
    assert result.effective_risk_pct < Decimal("0.005")


def test_tighter_stop_buys_more_units_for_the_same_risk():
    wide = size(stop_loss=Decimal("90"))
    tight = size(stop_loss=Decimal("96"))
    assert tight.quantity > wide.quantity
    # Same money at risk either way (neither is capped).
    assert not wide.capped_by and not tight.capped_by
    assert tight.effective_risk_amount == pytest.approx(
        wide.effective_risk_amount, abs=Decimal("0.01")
    )


def test_max_position_cap_reduces_size_and_is_reported():
    # A 0.5% stop would want 500 units (50,000 notional); the 20% cap allows 2,000.
    result = size(stop_loss=Decimal("99.9"))
    assert "max_position_pct_equity" in result.capped_by
    assert result.notional <= Decimal("10000") * Decimal("0.20")
    # Capping can only ever reduce realised risk.
    assert result.effective_risk_pct < Decimal("0.005")


def test_exposure_headroom_caps_size():
    result = size(current_exposure_value=Decimal("4500"))  # 45% of 50% budget used
    assert "max_portfolio_exposure_pct" in result.capped_by
    assert result.notional <= Decimal("500.00000001")


def test_no_headroom_is_rejected_outright():
    result = size(current_exposure_value=Decimal("5000"))
    assert not result.is_viable
    assert "exposure limit" in result.rejected_reason


def test_available_cash_caps_size():
    result = size(cash=Decimal("400"))
    assert "available_cash" in result.capped_by
    # Must leave room for the entry fee.
    assert result.notional * Decimal("1.001") <= Decimal("400.01")


def test_quantity_is_rounded_down_to_the_lot_grid():
    spec = SymbolSpec(
        symbol="X/USDT", base="X", quote="USDT", quantity_step=Decimal("0.3"),
        min_notional=Decimal("1"),
    )
    # 12.5 units wanted; the grid allows 41 x 0.3 = 12.3, never 12.6.
    result = size(spec=spec, stop_loss=Decimal("96"))
    assert result.quantity == Decimal("12.3")
    # Rounding down means realised risk never exceeds the budget.
    assert result.effective_risk_amount <= result.risk_amount


def test_rounding_down_never_increases_risk():
    spec = SymbolSpec(
        symbol="X/USDT", base="X", quote="USDT", quantity_step=Decimal("1"),
        min_notional=Decimal("1"),
    )
    result = size(spec=spec, equity=Decimal("9999"), stop_loss=Decimal("97.3"))
    assert result.quantity == result.quantity.to_integral_value()
    assert result.effective_risk_amount <= result.risk_amount


def test_sub_minimum_notional_is_rejected_not_rounded_up():
    result = size(equity=Decimal("100"), cash=Decimal("100"), stop_loss=Decimal("50"))
    assert not result.is_viable
    assert "min" in result.rejected_reason.lower()


def test_below_exchange_minimum_quantity_is_rejected():
    spec = SymbolSpec(
        symbol="X/USDT", base="X", quote="USDT",
        quantity_step=Decimal("0.00001"),
        min_quantity=Decimal("100"),
        min_notional=Decimal("1"),
    )
    result = size(spec=spec)
    assert not result.is_viable
    assert "minimum" in result.rejected_reason


def test_zero_stop_distance_is_rejected():
    result = size(stop_loss=Decimal("100"))
    assert not result.is_viable
    assert "stop distance" in result.rejected_reason


def test_zero_equity_is_rejected():
    result = size(equity=Decimal("0"), cash=Decimal("0"))
    assert not result.is_viable
    assert "equity" in result.rejected_reason


def test_short_side_sizing_uses_absolute_distance():
    result = size(entry=Decimal("100"), stop_loss=Decimal("104"))
    assert result.stop_distance == Decimal("4")
    assert result.quantity == Decimal("12.5")


def test_every_intermediate_value_is_reported():
    """A human must be able to re-do the arithmetic from the record alone."""
    result = size()
    assert result.equity * result.risk_pct == result.risk_amount
    assert result.raw_quantity == result.risk_amount / result.stop_distance
    assert result.stop_distance_pct == result.stop_distance / result.entry
    assert result.notional == pytest.approx(
        result.quantity * result.entry, abs=Decimal("1e-8")
    )
