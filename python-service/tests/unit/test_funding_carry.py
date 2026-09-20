"""Funding-carry arithmetic.

The fetch needs Binance, but the maths must not. These pin the calculation on
constructed histories where the right answer is known by hand, so a wrong number
cannot quietly become an investment decision.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2].parent / "scripts" / "funding_carry.py"
spec = importlib.util.spec_from_file_location("funding_carry", MODULE_PATH)
funding_carry = importlib.util.module_from_spec(spec)
sys.modules["funding_carry"] = funding_carry
spec.loader.exec_module(funding_carry)

EIGHT_HOURS_MS = 8 * 3600 * 1000
START_MS = 1_700_000_000_000


def history(rates: list[float]) -> list[dict]:
    return [
        {"fundingTime": START_MS + index * EIGHT_HOURS_MS, "fundingRate": str(rate)}
        for index, rate in enumerate(rates)
    ]


def test_a_steady_one_bp_per_period_annualises_to_about_eleven_percent():
    """0.01% three times a day is the textbook 'normal' funding level."""
    # 365 days of history at 3 periods a day.
    stats = funding_carry.analyse("BTCUSDT", history([0.0001] * (365 * 3)))

    assert stats is not None
    assert stats.periods == 1095
    assert stats.positive_share == 1.0
    # 0.01% x 3 x 365 = 10.95% a year gross.
    assert stats.annualised_gross == pytest.approx(0.1095, rel=0.02)
    # Net is lower by the one-off round trip, amortised over the year.
    assert stats.annualised_net < stats.annualised_gross
    assert stats.annualised_net == pytest.approx(0.1065, rel=0.02)


def test_negative_funding_is_reported_as_a_loss_not_hidden():
    """Shorts pay when the market is bearish. That must show as negative."""
    stats = funding_carry.analyse("BTCUSDT", history([-0.0001] * (365 * 3)))

    assert stats is not None
    assert stats.positive_share == 0.0
    assert stats.annualised_gross < 0
    assert stats.annualised_net < stats.annualised_gross, "costs make a loss worse"


def test_the_worst_stretch_captures_a_painful_run_inside_a_profitable_window():
    """A good average hides the weeks you had to pay. That is what breaks people."""
    # Collect for 200 periods (+4%), pay heavily for 50 (-5%), collect 200 more
    # (+4%): the window nets +3% while containing a 5% run of paying.
    rates = [0.0002] * 200 + [-0.0010] * 50 + [0.0002] * 200
    stats = funding_carry.analyse("BTCUSDT", history(rates))

    assert stats is not None
    assert stats.total_rate > 0, "the window as a whole was profitable"
    # The drawdown is the 50 periods at -0.10% each.
    assert stats.worst_stretch == pytest.approx(-0.05, rel=0.01)


def test_costs_bite_hardest_on_a_short_holding_period():
    """A 30bps round trip is trivial over a year and ruinous over a week."""
    long_window = funding_carry.analyse("BTCUSDT", history([0.0001] * (365 * 3)))
    short_window = funding_carry.analyse("BTCUSDT", history([0.0001] * (7 * 3)))

    assert long_window is not None and short_window is not None
    assert long_window.annualised_net > 0
    # Over a week, 0.21% collected does not cover a 0.30% round trip.
    assert short_window.annualised_net < 0, (
        "a week of carry cannot pay for getting in and out"
    )


def test_mixed_funding_averages_rather_than_counting_only_the_good_days():
    rates = [0.0003] * 200 + [-0.0003] * 100
    stats = funding_carry.analyse("BTCUSDT", history(rates))

    assert stats is not None
    assert stats.positive_share == pytest.approx(2 / 3, rel=0.01)
    assert stats.total_rate == pytest.approx(0.03, rel=0.01)


def test_too_little_history_returns_nothing_rather_than_a_number():
    assert funding_carry.analyse("BTCUSDT", []) is None
    assert funding_carry.analyse("BTCUSDT", history([0.0001])) is None


def test_timestamps_are_read_as_utc():
    stats = funding_carry.analyse("BTCUSDT", history([0.0001] * 10))

    assert stats is not None
    assert stats.first.tzinfo is UTC
    assert stats.first == datetime.fromtimestamp(START_MS / 1000, tz=UTC)


# ------------------------------------------------- return on deployed capital
# Funding accrues on notional, but the trade ties up spot capital AND futures
# margin. Quoting the notional figure alone overstates the return, which is the
# specific way carry trades get oversold.


def capital_multiple(leverage: float) -> float:
    """Capital tied up per unit of notional: the spot leg plus the short margin."""
    return 1.0 + 1.0 / leverage


@pytest.mark.parametrize(
    "leverage,expected_capital",
    [(2.0, 1.5), (3.0, 4 / 3), (5.0, 1.2), (10.0, 1.1)],
)
def test_capital_required_per_unit_of_notional(leverage, expected_capital):
    assert capital_multiple(leverage) == pytest.approx(expected_capital)


def test_return_on_capital_is_always_below_the_notional_headline():
    """There is no leverage at which the two are equal: spot is always funded."""
    notional_yield = 0.04
    for leverage in (2.0, 3.0, 5.0, 10.0, 50.0):
        on_capital = notional_yield / capital_multiple(leverage)
        assert on_capital < notional_yield


def test_the_headline_four_percent_is_three_percent_on_capital():
    """The correction that matters, against the measured BTC figure."""
    # 4.00%/yr on notional, short leg at 3x: $10,000 spot + $3,333 margin.
    on_capital = 0.0400 / capital_multiple(3.0)

    assert on_capital == pytest.approx(0.0300, abs=0.0001)


def test_higher_leverage_improves_return_and_shortens_the_distance_to_ruin():
    """The trade-off that the table must not hide."""
    notional_yield = 0.04
    low, high = 2.0, 10.0

    assert notional_yield / capital_multiple(high) > notional_yield / capital_multiple(low)
    # ...but the adverse move that wipes the short leg shrinks proportionally.
    assert (100.0 / high) < (100.0 / low)
    assert 100.0 / high == pytest.approx(10.0), "10x dies on roughly a 10% rally"


def test_a_negative_carry_stays_negative_at_every_leverage():
    """Leverage cannot rescue a trade that does not pay. SOL did not pay."""
    for leverage in (2.0, 3.0, 5.0, 10.0):
        assert -0.0055 / capital_multiple(leverage) < 0
