"""A stop can only be fixed if the loss is the kind a threshold can reach.

Stops are set at -50% and the median realised loss is -77%. Those 27 points
are either gap risk -- the price was already past the stop the first time we
saw it, which no threshold touches -- or execution wait, which a tighter stop
does reduce. The project has one suspicious datum already: stop_10 realised
-37.3% against stop_50's -43.5%, six points for a five-fold tighter stop,
which is what gap risk looks like.
"""

from __future__ import annotations

from stop_anatomy import anatomy, intervals


# --------------------------------------------------------------- gap risk
def test_a_price_that_gaps_straight_past_the_stop_is_all_gap():
    """The -50% stop fires at -90% because -90% is the first price seen. A
    tighter stop would have fired at the same -90%, so the threshold is not
    the problem and tuning it is decoration."""
    result = anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.1,
                     path=[(60.0, 0.1)])
    assert result is not None
    assert round(result["gap"], 4) == 0.4      # 0.5 threshold -> 0.1 seen
    assert round(result["wait"], 4) == 0.0
    assert result["gapped"] is True


def test_a_gradual_fall_through_the_stop_is_mostly_wait():
    """Seen at the stop, sold lower. Here the threshold does real work."""
    result = anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.2,
                     path=[(30.0, 0.8), (60.0, 0.5), (90.0, 0.2)])
    assert round(result["gap"], 4) == 0.0
    assert round(result["wait"], 4) == 0.3
    assert result["gapped"] is False


def test_the_two_components_plus_the_stop_account_for_the_whole_loss():
    """gap + wait + the stop's own 50% must equal the realised loss, or the
    split is hiding something."""
    result = anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.15,
                     path=[(60.0, 0.4)])
    assert abs((result["gap"] + result["wait"] + 0.5) - result["total"]) < 1e-9


# ------------------------------------------------- not everything is a stop
def test_a_position_that_never_hit_the_stop_is_not_counted():
    """A time exit at -20% on a -50% stop is not a stop failure, and blaming
    the threshold for it would overstate how broken stops are."""
    assert anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.8,
                   path=[(60.0, 0.9), (120.0, 0.8)]) is None


def test_a_position_with_no_observed_path_cannot_be_decomposed():
    assert anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.1, path=[]) is None


def test_a_zero_entry_price_is_refused_rather_than_dividing():
    assert anatomy(entry=0.0, stop_multiple=0.5, exit_price=0.1,
                   path=[(60.0, 0.1)]) is None


def test_the_first_crossing_is_used_not_the_lowest_price():
    """Using the lowest price would attribute the whole collapse to the gap
    and leave the wait reading zero on every position."""
    result = anatomy(entry=1.0, stop_multiple=0.5, exit_price=0.01,
                     path=[(60.0, 0.45), (120.0, 0.02), (180.0, 0.01)])
    assert round(result["gap"], 4) == 0.05
    assert round(result["wait"], 4) == 0.44


def test_a_profitable_exit_never_reports_a_negative_component():
    result = anatomy(entry=1.0, stop_multiple=0.5, exit_price=1.5,
                     path=[(60.0, 0.4), (120.0, 1.5)])
    assert result["gap"] >= 0 and result["wait"] >= 0 and result["total"] >= 0


# ------------------------------------------------- how often we were looking
def test_observation_intervals_are_measured_because_gap_risk_scales_with_them():
    """A token cannot be stopped at a price nobody looked at."""
    assert intervals([(0.0, 1.0), (60.0, 0.9), (150.0, 0.5)]) == [60.0, 90.0]


def test_a_single_observation_has_no_interval():
    assert intervals([(0.0, 1.0)]) == []
