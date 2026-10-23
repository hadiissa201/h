"""The analyser's failure modes are all silent ones.

Each test here pins a way the script could produce a confident number that
is wrong: a sign flip, a cost charged twice or not at all, an interval
narrowed by clustering, a missing quote scored as a flat trade.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "liquidations"))

import collect
from analyse import (
    ROUND_TRIP_FEES,
    Trade,
    bootstrap_difference,
    cascade_trades,
    episodes,
    trade_direction,
    verdict,
    weighted_mean,
)


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(collect.SCHEMA)
    return conn


def add_cascade(conn, cid, symbol, side, ts, entry, exit_, horizon=30.0):
    conn.execute(
        "INSERT INTO cascades (id, symbol, side, total_usd, events,"
        " first_event_ts, last_event_ts, detected_ts, min_cascade_usd,"
        " window_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, symbol, side, 300_000.0, 5, ts, ts, ts, 250_000.0, 10.0))
    for offset, (bid, ask) in ((0.0, entry), (horizon, exit_)):
        conn.execute(
            "INSERT INTO quotes (cascade_id, symbol, offset_s, bid, ask,"
            " quoted_ts) VALUES (?,?,?,?,?,?)",
            (cid, symbol, offset, bid, ask, ts + offset))
    conn.commit()


def test_liquidated_long_argues_for_a_buy():
    """Bybit reports the liquidated POSITION's side.

    A liquidated long is closed by selling, so the forced flow is selling and
    the trade is to buy into it. Getting this backwards would invert every
    return while still producing a plausible-looking number.
    """
    assert trade_direction("SELL") == "BUY"
    assert trade_direction("BUY") == "SELL"


def test_buy_pays_the_ask_and_exits_at_the_bid():
    """The spread must be paid, in the right direction."""
    flat = Trade(0.0, "BTCUSDT", "BUY", (100.0, 100.1), (100.0, 100.1))
    # Bought at 100.1, sold at 100.0: a loss of the spread plus fees.
    assert flat.net_return() == pytest.approx(
        (100.0 - 100.1) / 100.1 - ROUND_TRIP_FEES)
    assert flat.net_return() < 0


def test_sell_is_the_mirror_of_a_buy():
    sell = Trade(0.0, "BTCUSDT", "SELL", (100.0, 100.1), (100.0, 100.1))
    assert sell.net_return() == pytest.approx(
        (100.0 - 100.1) / 100.0 - ROUND_TRIP_FEES)


def test_spread_is_not_charged_twice():
    """Entering at the ask already pays it; subtracting again invents a loss.

    A 10bp round trip on a flat market should cost the spread once plus the
    fees, not twice.
    """
    t = Trade(0.0, "BTCUSDT", "BUY", (100.0, 100.1), (100.0, 100.1))
    one_spread = (100.1 - 100.0) / 100.1
    assert t.net_return() == pytest.approx(-one_spread - ROUND_TRIP_FEES)


def test_a_real_reversion_shows_up_as_a_gain():
    """Forced selling, then recovery: the whole hypothesis."""
    t = Trade(0.0, "BTCUSDT", "BUY", (100.0, 100.1), (102.0, 102.1))
    assert t.net_return() > 0.01


def test_fees_are_charged_both_ways():
    assert ROUND_TRIP_FEES == pytest.approx(2 * 0.00055)


def test_missing_follow_up_is_dropped_not_scored_flat():
    """The plan: 'Missing follow-ups are not zero returns.'

    Scoring an unpriceable cascade as flat would pull every mean toward zero
    and quietly shrink whatever effect exists.
    """
    conn = make_db()
    add_cascade(conn, 1, "BTCUSDT", "SELL", 1000.0, (100.0, 100.1),
                (101.0, 101.1))
    conn.execute(
        "INSERT INTO cascades (id, symbol, side, total_usd, events,"
        " first_event_ts, last_event_ts, detected_ts, min_cascade_usd,"
        " window_s) VALUES (2,'BTCUSDT','SELL',300000,5,2000,2000,2000,"
        "250000,10)")
    conn.execute(
        "INSERT INTO quotes (cascade_id, symbol, offset_s, bid, ask,"
        " quoted_ts) VALUES (2,'BTCUSDT',0,100.0,100.1,2000)")
    conn.commit()

    trades, _, missing = cascade_trades(conn, 30.0)
    assert len(trades) == 1
    assert missing == 1


def test_episodes_collapse_a_burst():
    """Three cascades in 140s are one market move, not three observations."""
    assert episodes([0.0, 60.0, 140.0]) == [0, 0, 0]
    assert episodes([0.0, 60.0, 140.0, 5000.0]) == [0, 0, 0, 1]


def test_clustered_bootstrap_is_wider_than_ignoring_clusters():
    """Resampling correlated cascades independently narrows the interval.

    Ten identical cascades inside one episode carry the information of one.
    An interval that treats them as ten is too narrow, which is how a null
    result gets reported as a finding.
    """
    import random as _r

    # Two bursts that disagree: five trades at +3%, five at 0%. Resampling
    # two whole episodes can draw all-high or all-low, so the interval is
    # wide. Resampling ten "independent" trades averages them every time and
    # reports a precision the data does not contain.
    trades = (
        [Trade(float(i), "BTCUSDT", "BUY", (100.0, 100.0), (103.0, 103.0))
         for i in range(5)]
        + [Trade(float(100 + i), "BTCUSDT", "BUY", (100.0, 100.0),
                 (100.0, 100.0)) for i in range(5)])
    controls = [Trade(float(i), "BTCUSDT", "BUY", (100.0, 100.0),
                      (100.0, 100.0)) for i in range(40)]
    weights = {"BTCUSDT": 1.0}

    two_episodes = [0] * 5 + [1] * 5
    ten_episodes = list(range(10))
    wide = bootstrap_difference(trades, two_episodes, controls, weights,
                                _r.Random(1), samples=2000)
    narrow = bootstrap_difference(trades, ten_episodes, controls, weights,
                                  _r.Random(1), samples=2000)
    assert wide is not None and narrow is not None
    # Measured at 1.67x on this fixture. The bar is set below that and well
    # above 1.0, so the test fails if clustering stops mattering rather than
    # passing on any arrangement -- an earlier version used identical trades,
    # where resampling cannot differ, and passed while testing nothing.
    assert (wide[1] - wide[0]) > 1.3 * (narrow[1] - narrow[0]), (
        f"clustering did not widen the interval: {wide} vs {narrow}")


def test_weighted_mean_matches_the_cascade_symbol_mix():
    """Controls draw symbols uniformly; cascades do not arrive uniformly."""
    by_symbol = {"BTCUSDT": [0.10], "SOLUSDT": [0.00]}
    assert weighted_mean(by_symbol, {"BTCUSDT": 0.9, "SOLUSDT": 0.1}) == (
        pytest.approx(0.09))
    assert weighted_mean(by_symbol, {"BTCUSDT": 0.5, "SOLUSDT": 0.5}) == (
        pytest.approx(0.05))


def test_weighted_mean_ignores_symbols_with_no_control_data():
    """A symbol with no controls must not count as a zero return."""
    assert weighted_mean({"BTCUSDT": [0.10]},
                         {"BTCUSDT": 0.5, "ETHUSDT": 0.5}) == (
        pytest.approx(0.10))


def test_verdict_follows_the_plans_table():
    assert verdict((0.001, 0.004))[0] == "CANDIDATE"
    assert verdict((-0.004, -0.001))[0] == "DEAD"
    assert verdict((-0.002, 0.003))[0] == "UNPROVEN"
    assert verdict(None)[0] == "NO DATA"


def test_verdict_at_exactly_zero_is_unproven():
    """A bound touching zero has not excluded it."""
    assert verdict((0.0, 0.004))[0] == "UNPROVEN"
    assert verdict((-0.004, 0.0))[0] == "UNPROVEN"


def test_control_direction_is_stable_across_horizons():
    """The same control entry must be the same trade at 30s and at 900s.

    Drawn from a shared generator it would get a fresh coin flip per
    horizon, so a difference between horizons could be the direction
    assignment rather than the market.
    """
    from analyse import control_direction

    for cid in range(50):
        assert control_direction(cid, 0.7) == control_direction(cid, 0.7)


def test_control_direction_follows_the_cascade_mix():
    """All-buy cascades must give all-buy controls, and vice versa."""
    from analyse import control_direction

    assert all(control_direction(i, 1.0) == "BUY" for i in range(50))
    assert all(control_direction(i, 0.0) == "SELL" for i in range(50))
    mixed = [control_direction(i, 0.5) for i in range(400)]
    share = mixed.count("BUY") / len(mixed)
    assert 0.4 < share < 0.6, f"direction draw is skewed: {share:.2f}"


def test_control_sample_size_is_a_precondition():
    """A difference against three control entries is not a measurement.

    The first live run showed a narrow-looking interval computed against a
    control arm forty minutes old, with no n displayed for it.
    """
    from analyse import CONTROLS_NEEDED, check_preconditions

    conn = make_db()
    add_cascade(conn, 1, "BTCUSDT", "SELL", 1000.0, (100.0, 100.1),
                (101.0, 101.1))
    names = [n for n, _, _ in check_preconditions(conn, 1, {30.0: 1.0}, 3)]
    assert any("control" in n for n in names)

    failing = {n: ok for n, ok, _ in
               check_preconditions(conn, 1, {30.0: 1.0}, 3)}
    passing = {n: ok for n, ok, _ in
               check_preconditions(conn, 1, {30.0: 1.0}, CONTROLS_NEEDED)}
    key = next(n for n in failing if "control" in n)
    assert failing[key] is False
    assert passing[key] is True


def test_tick_size_dominates_the_relative_spread():
    """Why the spread report must be split by symbol.

    BTC's $0.10 tick near $81,000 and SOL's $0.01 near $104 differ by about
    a hundredfold in relative terms, so a pooled comparison of a BTC-heavy
    cascade mix against uniform controls measures symbols, not liquidity.
    """
    btc = Trade(0.0, "BTCUSDT", "BUY", (81430.5, 81430.6), (81430.5, 81430.6))
    sol = Trade(0.0, "SOLUSDT", "BUY", (103.61, 103.62), (103.61, 103.62))
    assert btc.entry_spread < sol.entry_spread / 50
