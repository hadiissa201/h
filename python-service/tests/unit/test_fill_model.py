"""Fill simulation: spread, slippage, fees.

Under-modelling costs is the most common way a strategy looks profitable in
simulation and loses money live, so these are checked arithmetically and for
direction (costs must always work against the order).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.fill_model import (
    CostModel,
    apply_spread,
    impact_bps,
    simulate_limit_fill,
    simulate_market_fill,
    stop_fill_price,
)
from app.models.enums import Side

MODEL = CostModel(
    taker_fee_bps=Decimal("10"),
    maker_fee_bps=Decimal("8"),
    slippage_bps=Decimal("5"),
    spread_bps=Decimal("4"),
    impact_bps_per_100k=Decimal("2"),
)
NO_IMPACT = CostModel(
    taker_fee_bps=Decimal("10"),
    maker_fee_bps=Decimal("8"),
    slippage_bps=Decimal("5"),
    spread_bps=Decimal("4"),
    impact_bps_per_100k=Decimal("0"),
)


def test_buys_lift_the_ask_and_sells_hit_the_bid():
    mid = Decimal("100")
    assert apply_spread(mid, Side.BUY, Decimal("4")) == Decimal("100.02")
    assert apply_spread(mid, Side.SELL, Decimal("4")) == Decimal("99.98")


def test_market_buy_pays_spread_and_slippage():
    fill = simulate_market_fill(Decimal("100"), Decimal("1"), Side.BUY, NO_IMPACT)
    # 100 * (1 + 0.0002) * (1 + 0.0005)
    assert fill.price == pytest.approx(Decimal("100.07001"), abs=Decimal("0.00002"))
    assert fill.price > Decimal("100")


def test_market_sell_receives_less_than_the_mid():
    fill = simulate_market_fill(Decimal("100"), Decimal("1"), Side.SELL, NO_IMPACT)
    assert fill.price < Decimal("100")
    assert fill.price == pytest.approx(Decimal("99.93001"), abs=Decimal("0.00002"))


def test_costs_always_work_against_the_order():
    for side in (Side.BUY, Side.SELL):
        fill = simulate_market_fill(Decimal("250"), Decimal("2"), side, MODEL)
        if side is Side.BUY:
            assert fill.price > Decimal("250")
        else:
            assert fill.price < Decimal("250")
        assert fill.fee > 0


def test_fee_is_bps_of_notional():
    fill = simulate_market_fill(Decimal("100"), Decimal("10"), Side.BUY, NO_IMPACT)
    assert fill.fee == pytest.approx(fill.notional * Decimal("0.001"), abs=Decimal("1e-6"))


def test_round_trip_at_a_flat_price_loses_money():
    """Costs alone must produce a loss — otherwise the model is a fantasy."""
    entry = simulate_market_fill(Decimal("100"), Decimal("10"), Side.BUY, MODEL)
    exit_ = simulate_market_fill(Decimal("100"), Decimal("10"), Side.SELL, MODEL)
    pnl = exit_.notional - exit_.fee - entry.notional - entry.fee
    assert pnl < 0
    # Roughly: 2 x (half-spread + slippage + fee) on 1,000 of notional.
    assert pnl == pytest.approx(Decimal("-3.4"), abs=Decimal("0.3"))


def test_larger_orders_suffer_more_slippage():
    small = simulate_market_fill(Decimal("100"), Decimal("1"), Side.BUY, MODEL)
    large = simulate_market_fill(Decimal("100"), Decimal("5000"), Side.BUY, MODEL)
    assert large.slippage_bps > small.slippage_bps
    assert large.price > small.price


def test_impact_scales_with_notional():
    assert impact_bps(Decimal("100000"), Decimal("2")) == Decimal("2")
    assert impact_bps(Decimal("50000"), Decimal("2")) == Decimal("1")
    assert impact_bps(Decimal("0"), Decimal("2")) == Decimal("0")


def test_reported_slippage_includes_the_spread():
    """Comparing against a backtest assumption needs the all-in number."""
    fill = simulate_market_fill(Decimal("100"), Decimal("1"), Side.BUY, NO_IMPACT)
    assert fill.slippage_bps == pytest.approx(Decimal("7"), abs=Decimal("0.01"))


def test_limit_fills_pay_maker_fees_and_no_slippage():
    fill = simulate_limit_fill(Decimal("100"), Decimal("10"), Side.BUY, MODEL)
    assert fill.price == Decimal("100")
    assert fill.slippage_bps == Decimal("0")
    assert fill.is_maker
    assert fill.fee == pytest.approx(Decimal("0.8"), abs=Decimal("1e-6"))


def test_stops_fill_worse_than_the_stop_level():
    price = stop_fill_price(Decimal("95"), Side.SELL, MODEL)
    assert price < Decimal("95")


def test_stops_gap_through_when_the_market_jumped():
    """A stop does not protect you at its level when price gaps past it."""
    without_gap = stop_fill_price(Decimal("95"), Side.SELL, MODEL)
    with_gap = stop_fill_price(Decimal("95"), Side.SELL, MODEL, gap_price=Decimal("90"))
    assert with_gap < without_gap
    assert with_gap < Decimal("90")


def test_buy_stop_gaps_upward():
    price = stop_fill_price(Decimal("105"), Side.BUY, MODEL, gap_price=Decimal("110"))
    assert price > Decimal("110")


def test_zero_or_negative_inputs_are_rejected():
    with pytest.raises(ValueError):
        simulate_market_fill(Decimal("100"), Decimal("0"), Side.BUY, MODEL)
    with pytest.raises(ValueError):
        simulate_market_fill(Decimal("0"), Decimal("1"), Side.BUY, MODEL)
    with pytest.raises(ValueError):
        simulate_limit_fill(Decimal("100"), Decimal("-1"), Side.BUY, MODEL)


def test_cost_model_reads_from_settings(settings):
    model = CostModel.from_settings(settings)
    assert model.taker_fee_bps == Decimal(str(settings.paper_taker_fee_bps))
    assert model.slippage_bps == Decimal(str(settings.paper_slippage_bps))


def test_zero_cost_model_is_possible_but_not_the_default(settings):
    free = CostModel(
        taker_fee_bps=Decimal("0"),
        maker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        spread_bps=Decimal("0"),
        impact_bps_per_100k=Decimal("0"),
    )
    fill = simulate_market_fill(Decimal("100"), Decimal("1"), Side.BUY, free)
    assert fill.price == Decimal("100")
    assert fill.fee == 0
    # The shipped defaults are never free.
    assert CostModel().taker_fee_bps > 0
    assert CostModel().slippage_bps > 0
