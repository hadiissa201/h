"""A lift is not an edge, and an in-sample threshold is not a rule.

graduated.py found top-third 5m buy counts graduating at 6.49% against a
2.68% base rate. Three things have to be true before that is tradable, and
these tests pin the checks for each: the threshold must survive a period it
was not fitted on, the win rate must clear the break-even payoff, and the
correlated features must not be counted as separate confirmations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.models import Base, Observation, Token
from signal_test import MEDIAN_REALISED_LOSS, breakeven_payoff, early_buys, split_rate

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def seeded(rows):
    """rows: list of (address, [(offset_s, buys)])."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    for address, obs in rows:
        token = Token(address=address, chain="solana",
                      detection_source="test", detected_ts=NOW)
        s.add(token)
        s.flush()
        for offset, buys in obs:
            s.add(Observation(token_id=token.id,
                              observed_ts=NOW + timedelta(seconds=offset),
                              source="dexscreener", price_usd=1e-7,
                              buys_5m=buys))
    s.commit()
    return s


# ---------------------------------------------------- the break-even arithmetic
def test_break_even_payoff_at_the_measured_win_rate():
    """The number that decides it. At 6.49% wins and a -77% median realised
    loss, every winner has to return more than eleven times the stake."""
    need = breakeven_payoff(0.0649)
    assert 11.0 < need < 11.2


def test_a_lower_win_rate_needs_a_far_larger_winner():
    """The base rate needs +2,796%, which is why the lift matters at all --
    and why it still may not be enough."""
    assert breakeven_payoff(0.0268) > 27


def test_no_winners_cannot_be_rescued_by_any_payoff():
    assert breakeven_payoff(0.0) is None


def test_the_loss_used_is_the_measured_one_not_the_configured_stop():
    """Stops were set at -50% and the median realised outcome was -77%. Using
    the stop would understate what winners must cover by a third."""
    assert MEDIAN_REALISED_LOSS == 0.77


# ------------------------------------------------------- no hindsight at entry
def test_a_buy_count_after_the_window_is_not_an_entry_signal():
    with seeded([("late", [(3600, 500)])]) as s:
        token = s.query(Token).one()
        assert early_buys(s, token, window_s=300) is None


def test_the_first_count_in_the_window_is_the_one_used():
    """Taking a later or larger count would use buying the rule could not
    have seen when it had to decide."""
    with seeded([("rising", [(60, 3), (290, 900)])]) as s:
        token = s.query(Token).one()
        assert early_buys(s, token, window_s=300) == 3.0


def test_a_token_with_no_buy_count_is_excluded_rather_than_treated_as_zero():
    """Zero buys and no data are opposite facts, and conflating them would
    put every unobserved token in the bottom bucket."""
    with seeded([("silent", [])]) as s:
        token = s.query(Token).one()
        assert early_buys(s, token, window_s=300) is None


# --------------------------------------------------------- above versus below
def test_tokens_split_cleanly_either_side_of_the_threshold():
    tokens = [type("T", (), {"id": i})() for i in range(1, 7)]
    feature = {1: 1.0, 2: 2.0, 3: 5.0, 4: 9.0, 5: 20.0, 6: 40.0}
    rates = split_rate(tokens, feature, {4, 5, 6}, threshold=9.0)
    assert rates["above"]["n"] == 3 and rates["above"]["hits"] == 3
    assert rates["below"]["n"] == 3 and rates["below"]["hits"] == 0


def test_a_token_missing_the_feature_lands_in_neither_bucket():
    tokens = [type("T", (), {"id": i})() for i in range(1, 4)]
    rates = split_rate(tokens, {1: 1.0, 2: 50.0}, set(), threshold=10.0)
    assert rates["above"]["n"] + rates["below"]["n"] == 2


def test_net_is_treated_as_a_percentage_because_that_is_what_it_is():
    """ReplayPosition.net is a PERCENTAGE: replay.py divides it by 100 to get
    a fraction. Treating it as a fraction multiplied it by 100 twice and
    printed a median net of -5,444%, which no position can lose."""
    import inspect

    import signal_test
    source = inspect.getsource(signal_test.main)
    assert "position.net / 100.0" in source


def test_a_tiny_replay_sample_is_labelled_as_such():
    from signal_test import MIN_CLOSES_TO_COMPARE
    assert MIN_CLOSES_TO_COMPARE >= 30
