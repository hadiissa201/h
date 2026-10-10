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


# ------------------------------------------- the crux test, which was vacuous
class Pos:
    def __init__(self, peak, unrealisable, strategy="control_any"):
        self.peak_multiple = peak
        self.unrealisable_peak_multiple = unrealisable
        self.strategy = strategy
        self.is_open = False
        self.token_id = 1


def exitability(positions, target=3.0, monkeypatch=None):
    """Call the real function against an in-memory stand-in session."""
    import verdict

    class FakeSession:
        def scalars(self, _query):
            class R:
                def all(_self):
                    return positions
            return R()

    original = verdict.TARGETS
    verdict.TARGETS = {"control_any": target}
    try:
        return verdict.exitability_by_outcome(FakeSession(), "control_any")
    finally:
        verdict.TARGETS = original


def test_an_unsellable_peak_counts_as_not_sellable():
    """The whole point. A position that touched 6x on the chart but could only
    ever have exited at 1.2x did NOT have a sellable win, and the first
    version of this test called it sellable because the token had produced a
    successful exit at some other moment."""
    rows = exitability([Pos(peak=1.2, unrealisable=6.0)])
    assert rows["reached target"]["n"] == 1
    assert rows["reached target"]["sellable"] == 0


def test_a_peak_that_was_reachable_counts_as_sellable():
    rows = exitability([Pos(peak=4.0, unrealisable=4.0)])
    assert rows["reached target"]["sellable"] == 1


def test_winners_and_losers_are_split_by_the_best_price_either_way():
    """A win is a win whether or not it could be sold, otherwise unsellable
    wins vanish from the numerator AND the denominator."""
    rows = exitability([Pos(peak=1.1, unrealisable=9.0),
                        Pos(peak=0.4, unrealisable=0.4)])
    assert rows["reached target"]["n"] == 1
    assert rows["stayed at a loss"]["n"] == 1


def test_the_thesis_fails_when_winners_are_less_sellable_than_losers():
    """The decision rule written down on 2026-09-30, verbatim: if positions at
    or above the profit target are sellable less often than positions at a
    loss, the thesis fails regardless of mean return."""
    rows = exitability([Pos(peak=1.0, unrealisable=8.0)] * 10
                       + [Pos(peak=0.3, unrealisable=0.3)] * 10)
    assert rows["reached target"]["rate"] < rows["stayed at a loss"]["rate"]


def test_it_no_longer_reports_everything_sellable_by_construction():
    """The bug: 35 of 35 and 400 of 400, from asking whether the token had
    ever produced a successful exit -- which it must have, to have opened a
    position at all."""
    rows = exitability([Pos(peak=1.0, unrealisable=5.0)] * 5)
    assert rows["reached target"]["rate"] == 0.0


# ------------------------------- a rate comparison is not a comparison
def test_the_crux_requires_significance_not_just_a_direction():
    """The live run fired THESIS FAILS on 32/35 against 391/400, whose
    intervals overlap, at p=0.064. The project's most consequential verdict
    rested on two point estimates."""
    from collector.verify import fisher_exact
    assert fisher_exact(3, 32, 9, 391) > 0.05


def test_a_stark_difference_still_fails_the_thesis():
    """The tightening must not make the test unfailable: a real gap clears it
    easily."""
    from collector.verify import fisher_exact
    assert fisher_exact(30, 5, 5, 395) < 0.05


def test_identical_rates_give_p_of_essentially_one():
    """Summing the tail probabilities will not land exactly on 1.0, so the
    assertion is approximate -- the code is right and an exact comparison
    would be the test being wrong."""
    from collector.verify import fisher_exact
    assert fisher_exact(5, 95, 5, 95) > 0.999


def test_an_empty_group_cannot_produce_a_significant_difference():
    from collector.verify import fisher_exact
    assert fisher_exact(0, 0, 9, 391) == 1.0


def test_the_amendment_is_recorded_in_the_pre_registration():
    """A pre-registered kill criterion was loosened after seeing the data, in
    the direction that favours the strategy. The document requires any change
    to be dated and to say what prompted it."""
    from pathlib import Path
    text = Path("docs/DECISION.md").read_text()
    assert "Amendment, 2026-10-10" in text
    assert "favours the strategy" in text
    assert "0.0635" in text


def test_the_alpha_is_declared_next_to_the_amendment_date():
    import verdict
    assert verdict.CRUX_ALPHA == 0.05
