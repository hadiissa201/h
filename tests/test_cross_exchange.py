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


# ----------------------------------------- venues do not agree on level shape
def test_a_three_element_level_is_read_not_rejected():
    """Kraken's book levels are [price, volume, timestamp] and ccxt passes the
    third element through. Unpacking exactly two killed a 60-sample run on its
    first Kraken fetch."""
    fill = walk_book([[100.0, 50.0, 1760000000]], notional=1000.0)
    assert fill is not None
    assert fill.vwap == 100.0
    assert fill.complete


def test_a_malformed_level_is_skipped_rather_than_crashing():
    fill = walk_book([[100.0], [], [101.0, 50.0]], notional=1000.0)
    assert fill is not None
    assert fill.vwap == 101.0


def test_mixed_level_shapes_in_one_book_both_count():
    fill = walk_book([[100.0, 1.0, 1760000000], [101.0, 100.0]], 1000.0)
    assert 100.0 < fill.vwap < 101.0
    assert fill.levels_used == 2


def test_the_better_of_the_two_directions_is_chosen():
    """Buy where it is cheap, sell where it is dear -- and which venue that is
    flips sample to sample."""
    from cross_exchange import _best_direction
    cheap = {"asks": [[100.0, 100.0]], "bids": [[99.9, 100.0]]}
    dear = {"asks": [[102.0, 100.0]], "bids": [[101.9, 100.0]]}
    fees = {"x": 0.0, "y": 0.0}
    best = _best_direction(cheap, dear, "x", "y", fees, 1000.0)
    assert best is not None
    assert best[1] == "x" and best[2] == "y"
    assert best[0] > 0


def test_a_side_too_thin_for_the_size_is_counted_not_traded():
    from cross_exchange import _best_direction
    thin = {"asks": [[100.0, 0.001]], "bids": [[99.0, 0.001]]}
    deep = {"asks": [[101.0, 100.0]], "bids": [[100.5, 100.0]]}
    fees = {"x": 0.0, "y": 0.0}
    best = _best_direction(thin, deep, "x", "y", fees, 1000.0)
    assert best is None or best[3] > 0


# ------------------------------- the verdict must agree with its own table
def test_zero_positive_samples_reads_as_no_edge():
    """The live run had 0 of 55 positive and printed "positive edges occur",
    because the branch tested the Wilson upper bound -- which is never exactly
    zero -- instead of the count."""
    stats = summarise([-0.0035] * MIN_SAMPLES, 1000.0,
                      {"binance": 0.0010, "kraken": 0.0026})
    assert stats["positive"] == 0


def test_the_gross_spread_is_reported_so_the_cause_is_visible():
    """Net alone cannot tell "the fees are too high" from "the venues agree on
    the price". The first is a fee-tier problem; the second means no fee
    schedule or faster connection would help."""
    fees = {"binance": 0.0010, "kraken": 0.0026}
    stats = summarise([-0.003543] * MIN_SAMPLES, 1000.0, fees)
    assert abs(stats["median_gross"] - 0.000057) < 1e-9
    assert stats["fee_hurdle"] == 0.0036


def test_a_gross_spread_below_the_hurdle_means_no_dislocation():
    """The live numbers: best gross 1.62 bp against a 36 bp hurdle. Nothing
    about costs explains that -- the venues simply agree."""
    fees = {"binance": 0.0010, "kraken": 0.0026}
    stats = summarise([-0.003438] * MIN_SAMPLES, 1000.0, fees)
    assert stats["best_gross"] < stats["fee_hurdle"]
    assert stats["best_gross"] * 10000 < 2.0


def test_a_wide_gross_spread_eaten_by_fees_is_a_different_finding():
    fees = {"a": 0.0026, "b": 0.0060}
    stats = summarise([-0.001] * MIN_SAMPLES, 1000.0, fees)
    assert stats["best_gross"] > stats["fee_hurdle"] * 0.5
