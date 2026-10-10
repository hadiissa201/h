"""A spread you cannot fill at size, reach in time, or fund is not an edge.

docs/FINDINGS.md says any new idea must arrive with a reason why it should
work and then go through the same harness. Cross-exchange arbitrage has the
reason. These tests pin the three ways the measurement could flatter it, each
of which this project has already got wrong once elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from collector_stats import wilson
from cross_exchange import (
    MIN_SAMPLES,
    capital_multiple,
    net_edge,
    summarise,
    walk_book,
)


# ------------------------------- top of book is a quote, not a fill
def test_a_thin_top_level_does_not_set_the_price_for_the_whole_order():
    """$1,000 against $100 at the best price and the rest 1% higher. Using the
    top price would invent an edge that the second level eats."""
    fill = walk_book([[100.0, 1.0], [101.0, 100.0]], notional=1000.0)
    assert fill is not None
    assert 100.0 < fill.vwap < 101.0
    assert fill.levels_used == 2


def test_a_single_deep_level_fills_at_that_price():
    fill = walk_book([[100.0, 50.0]], notional=1000.0)
    assert fill.vwap == 100.0
    assert fill.complete


def test_a_book_too_thin_for_the_size_is_reported_incomplete():
    """The memecoin run reported a $100 order against a $1 pool as a clean
    fill. Pretending the remainder fills at the last price is how that
    happens."""
    fill = walk_book([[100.0, 0.5]], notional=1000.0)
    assert fill is not None
    assert not fill.complete
    assert fill.filled_notional < 1000.0


def test_an_empty_book_yields_nothing_rather_than_zero():
    assert walk_book([], notional=1000.0) is None


def test_zero_and_negative_levels_are_ignored():
    fill = walk_book([[0.0, 5.0], [-1.0, 5.0], [100.0, 50.0]], 1000.0)
    assert fill.vwap == 100.0


# ------------------------------------------- fees decide it, so charge both
def test_both_taker_fees_are_charged():
    """A 0.20% gross spread against binance+kraken taker fees of 0.36% is a
    loss. Charging one side would show it as a gain."""
    buy = walk_book([[100.0, 100.0]], 1000.0)
    sell = walk_book([[100.2, 100.0]], 1000.0)
    edge = net_edge(buy, 0.0010, sell, 0.0026)
    assert edge < 0
    assert abs(edge - (0.002 - 0.0036)) < 1e-9


def test_a_spread_wider_than_the_fees_is_a_real_positive():
    buy = walk_book([[100.0, 100.0]], 1000.0)
    sell = walk_book([[101.0, 100.0]], 1000.0)
    assert net_edge(buy, 0.0010, sell, 0.0026) > 0


def test_identical_prices_lose_exactly_the_fees():
    buy = walk_book([[100.0, 100.0]], 1000.0)
    sell = walk_book([[100.0, 100.0]], 1000.0)
    assert abs(net_edge(buy, 0.0010, sell, 0.0026) + 0.0036) < 1e-9


# ----------------------------------------- the return is on capital
def test_capital_is_double_the_notional_because_both_sides_are_prepositioned():
    """Funding carry looked like 4% until the same correction made it 3%:
    returns must be quoted on what is tied up, not on what moves."""
    assert capital_multiple() == 2.0


def test_the_summary_halves_the_edge_onto_capital():
    stats = summarise([0.01] * MIN_SAMPLES, 1000.0, {"a": 0.001, "b": 0.0026})
    assert abs(stats["median_on_capital"] - 0.005) < 1e-9


# --------------------------------------------- too few samples is not a result
def test_a_short_run_is_not_a_result():
    stats = summarise([0.01] * (MIN_SAMPLES - 1), 1000.0, {"a": 0.001})
    assert not stats["enough"]


def test_a_long_enough_run_is_described():
    stats = summarise([-0.001] * MIN_SAMPLES, 1000.0, {"a": 0.001})
    assert stats["enough"]
    assert stats["positive"] == 0


# ---------------------------------------------------------- the interval
def test_never_positive_gives_an_upper_bound_not_a_certainty():
    """0 of 60 is not proof the edge is impossible, and the interval says so."""
    low, high = wilson(0, 60)
    assert low == 0.0
    assert 0.0 < high < 0.10
