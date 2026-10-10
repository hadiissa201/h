"""docs/DECISION.md was written before the data so the bar could not move.

These tests pin the bar. Each one corresponds to a rule written down on
2026-09-30, and the point of testing them is that a verdict script is exactly
the place where a criterion quietly loosens.
"""

from __future__ import annotations

import statistics

from verdict import (
    MAX_FALSE_POSITIVE_RATE,
    MIN_CLOSED,
    MIN_VERIFICATIONS,
    bootstrap_ci,
    net_return,
)


class Position:
    def __init__(self, notional=100.0, gross=None, costs=0.0):
        self.notional_usd = notional
        self.gross_pnl_usd = gross
        self.costs_usd = costs


# ------------------------------------------------------- the return itself
def test_net_is_after_costs_not_gross():
    """Reporting gross would make a strategy that pays its whole edge away in
    fees and impact look profitable."""
    assert net_return(Position(gross=20.0, costs=5.0)) == 0.15


def test_a_position_with_no_gross_recorded_is_excluded_not_zeroed():
    """Treating missing as zero would pull every mean toward break-even."""
    assert net_return(Position(gross=None)) is None


def test_a_zero_notional_is_refused_rather_than_dividing():
    assert net_return(Position(notional=0.0, gross=5.0)) is None


def test_a_total_loss_reads_as_minus_one_hundred_percent():
    assert net_return(Position(gross=-100.0, costs=0.0)) == -1.0


# ---------------------------------------------------------- the interval
def test_the_interval_is_a_bootstrap_because_the_returns_are_skewed():
    """Memecoin returns are bounded at -100% and unbounded above, so the
    sampling distribution of the mean is skewed and a symmetric interval
    misstates it. On a sample like the real one the bootstrap interval sits
    off-centre from the mean."""
    values = [-1.0] * 90 + [0.5] * 9 + [40.0]
    low, high = bootstrap_ci(values)
    mean = statistics.fmean(values)
    assert low < mean < high
    assert (high - mean) > (mean - low)      # right-skewed, as it must be


def test_the_interval_is_deterministic_so_a_verdict_cannot_be_re_rolled():
    """A seeded bootstrap means nobody can run it again for a nicer answer."""
    values = [-1.0] * 50 + [3.0] * 5
    assert bootstrap_ci(values) == bootstrap_ci(values)


def test_an_all_losing_sample_gives_an_interval_entirely_below_zero():
    low, high = bootstrap_ci([-0.8, -0.9, -1.0, -0.7, -0.95] * 30)
    assert high < 0


def test_a_single_value_cannot_support_an_interval():
    low, high = bootstrap_ci([0.5])
    assert low != low      # NaN
    assert high != high


# ----------------------------------------------- the bar, exactly as written
def test_the_preconditions_are_the_pre_registered_numbers():
    """100 closed positions, 20 verifications, under 10% false positives.
    Loosening any of these is how a failing result becomes a passing one."""
    assert MIN_CLOSED == 100
    assert MIN_VERIFICATIONS == 20
    assert MAX_FALSE_POSITIVE_RATE == 0.10


def test_an_unmeasured_false_positive_rate_cannot_pass_the_precondition():
    """disagreement_rate returns None, not 0.0, when nothing has been
    verified. Defaulting that to zero would let the precondition pass on no
    evidence, which is the opposite of what it is for."""
    import inspect

    import verdict
    source = inspect.getsource(verdict.main)
    assert 'rate is not None' in source
    assert '"quote_false_positive_rate"' in source
