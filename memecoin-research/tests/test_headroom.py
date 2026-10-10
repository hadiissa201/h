"""The rule's entry must be the rule's entry, not the best one in hindsight.

oracle.py answers a different question on purpose: it holds the cheapest
entry it has seen, which is a ceiling nobody can trade. This script measures
what the rule actually pays, so the one thing that must not creep in is a
better entry than the rule would have taken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from headroom import (
    BUYS_THRESHOLD,
    ENTRY_WINDOW_S,
    best_after,
    entry_at_rule,
    expectancy,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Row:
    def __init__(self, offset, price, buys=None, liquidity=50_000.0):
        self.observed_ts = NOW + timedelta(seconds=offset)
        self.price_usd = price
        self.buys_5m = buys
        self.liquidity_usd = liquidity


# -------------------------------------------------------------- the entry
def test_the_rule_pays_the_price_at_the_first_trip_not_the_cheapest():
    """The whole point. A later dip is not available to a rule that already
    fired, and using it would manufacture headroom that does not exist."""
    rows = [Row(10, 1e-7, buys=2), Row(60, 5e-7, buys=20), Row(120, 1e-8, buys=99)]
    entry = entry_at_rule(rows, NOW, notional=100.0)
    assert entry is not None
    assert entry[1] == 5e-7


def test_a_trip_after_the_window_is_not_an_entry():
    rows = [Row(ENTRY_WINDOW_S + 60, 1e-7, buys=500)]
    assert entry_at_rule(rows, NOW, notional=100.0) is None


def test_a_token_that_never_trips_is_not_bought():
    rows = [Row(30, 1e-7, buys=BUYS_THRESHOLD - 1), Row(90, 2e-7, buys=1)]
    assert entry_at_rule(rows, NOW, notional=100.0) is None


def test_an_order_too_large_for_the_pool_is_not_a_free_fill():
    """A $100 order against a $200 pool is 50% of it. Taking the fill anyway
    is the error that produced 100% impact on a $20k pool earlier."""
    rows = [Row(30, 1e-7, buys=50, liquidity=200.0)]
    assert entry_at_rule(rows, NOW, notional=100.0) is None


# --------------------------------------------------------------- the exit
def test_the_chart_exit_takes_the_best_price_after_entry():
    rows = [Row(30, 1e-7, buys=50), Row(600, 1e-6), Row(900, 5e-7)]
    best = best_after(rows, NOW + timedelta(seconds=30), 1e-7, None, 100.0)
    assert best is not None
    assert round(best[0], 2) == 10.0


def test_a_price_before_entry_cannot_be_sold_into():
    rows = [Row(10, 9e-6), Row(600, 2e-7)]
    best = best_after(rows, NOW + timedelta(seconds=30), 1e-7, None, 100.0)
    assert best is not None
    assert round(best[0], 2) == 2.0


def test_a_sellable_exit_needs_a_verified_sale_near_that_moment():
    """Without this the sellable figure silently becomes the chart figure,
    which is the single most flattering mistake available here."""
    rows = [Row(600, 1e-6)]
    entry_ts = NOW + timedelta(seconds=30)
    assert best_after(rows, entry_ts, 1e-7, {}, 100.0) is None
    verified = {NOW + timedelta(seconds=580): 0.02}
    assert best_after(rows, entry_ts, 1e-7, verified, 100.0) is not None


def test_a_stale_verification_does_not_license_a_much_later_sale():
    rows = [Row(7200, 1e-6)]
    verified = {NOW + timedelta(seconds=60): 0.02}
    assert best_after(rows, NOW + timedelta(seconds=30), 1e-7,
                      verified, 100.0) is None


def test_exit_impact_is_charged_against_the_proceeds():
    rows = [Row(600, 1e-6)]
    entry_ts = NOW + timedelta(seconds=30)
    clean = best_after(rows, entry_ts, 1e-7,
                       {NOW + timedelta(seconds=590): 0.0}, 100.0)
    hit = best_after(rows, entry_ts, 1e-7,
                     {NOW + timedelta(seconds=590): 0.5}, 100.0)
    assert clean[1] > hit[1]


# -------------------------------------------------------------- the verdict
def test_expectancy_is_negative_when_the_winner_cannot_cover_the_losers():
    """At 5.80% wins and -77% per loser, a +500% winner is not enough."""
    assert expectancy(0.0580, 5.0) < 0


def test_expectancy_turns_positive_above_the_break_even_payoff():
    assert expectancy(0.0580, 12.51) > 0


def test_the_threshold_is_the_one_confirmed_out_of_sample_not_refitted():
    """Refitting the threshold on the graduates it is measured against would
    be the survivorship error one layer down."""
    assert BUYS_THRESHOLD == 10.0
