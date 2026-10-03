"""Faber's tactical allocation rule.

The tests that matter here are not about returns. They are about the three ways
this strategy flatters itself: using a signal to trade the month it was computed
from, comparing against a benchmark over a different window, and charging no
cost for monthly turnover.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from app.backtesting.tactical import (
    PUBLISHED_LOOKBACK_MONTHS,
    buy_and_hold,
    monthly_closes,
    robustness,
    run_tactical,
    signals_from_closes,
)


def daily(prices: list[float], start: str = "2020-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(prices), freq="D", tz="UTC")
    return pd.DataFrame({"timestamp": index, "close": prices})


def monthly_series(values: list[float], start: str = "2020-01-31") -> pd.DataFrame:
    """One row per month-end, which resamples to itself."""
    index = pd.date_range(start, periods=len(values), freq="ME", tz="UTC")
    return pd.DataFrame({"timestamp": index, "close": values})


# ------------------------------------------------------------------ resampling
def test_only_the_last_close_of_each_month_is_used():
    """Running the rule on daily bars is a different, far more active strategy."""
    frame = daily([float(i) for i in range(1, 93)])      # 92 days = 3 months
    closes = monthly_closes(frame)
    assert len(closes) == 3
    assert closes.iloc[0] == 31.0        # 31 Jan
    assert closes.iloc[1] == 60.0        # 29 Feb (2020 is a leap year)


# -------------------------------------------------------------- the look-ahead
def test_the_signal_month_is_not_the_month_it_trades():
    """The most common way this strategy gets inflated.

    A rule that buys during the month whose return it already knows is not a
    strategy. Here a flat series jumps in its final month; the signal only turns
    on at that close, so the gain must NOT be captured.
    """
    flat = [100.0] * 12 + [400.0]
    result = run_tactical({"X": monthly_series(flat)}, lookback_months=10,
                          round_trip_cost_pct=Decimal("0"), annual_yield=Decimal("0"))
    # The jump is the last month, so no following month exists to earn it in.
    assert result.final_equity == result.starting_equity, (
        "captured a move the signal had not yet seen")


def test_a_signal_needs_a_full_lookback_before_it_fires():
    short = monthly_series([100.0] * 5)
    assert signals_from_closes(monthly_closes(short), 10).notna().sum() == 0


def test_the_average_includes_the_current_month():
    closes = monthly_closes(monthly_series([1.0] * 9 + [100.0]))
    signal = signals_from_closes(closes, 10)
    # SMA of nine 1s and one 100 is 10.9; the close of 100 is above it.
    assert signal.iloc[-1] is True or bool(signal.iloc[-1]) is True


# ---------------------------------------------------------------- the mechanic
def test_it_sits_in_yield_while_the_trend_is_down():
    """A falling asset must put the whole book in the risk-free leg, earning."""
    falling = [100.0 - i for i in range(26)]
    result = run_tactical({"X": monthly_series(falling)}, lookback_months=10,
                          round_trip_cost_pct=Decimal("0"),
                          annual_yield=Decimal("0.06"))
    assert result.months_in_market == 0
    assert result.final_equity > result.starting_equity, "cash leg earned nothing"


def test_it_holds_the_asset_while_the_trend_is_up():
    rising = [100.0 * (1.05 ** i) for i in range(26)]
    result = run_tactical({"X": monthly_series(rising)}, lookback_months=10,
                          round_trip_cost_pct=Decimal("0"), annual_yield=Decimal("0"))
    assert result.months_in_market == result.months
    assert result.final_equity > result.starting_equity


def test_the_drawdown_is_smaller_than_holding_through_a_crash():
    """The actual claim being tested: risk reduction, not extra return."""
    series = [100.0 * (1.04 ** i) for i in range(24)] + \
             [100.0 * (1.04 ** 23) * (0.75 ** i) for i in range(1, 13)]
    prices = {"X": monthly_series(series)}
    timed = run_tactical(prices, round_trip_cost_pct=Decimal("0.003"))
    held = buy_and_hold(prices, round_trip_cost_pct=Decimal("0.003"))
    assert timed.max_drawdown_pct > held.max_drawdown_pct, (
        "timing did not reduce the drawdown through an 8-month collapse")


# ------------------------------------------------------------------- the costs
def test_turnover_is_charged():
    """Faber's paper charged nothing. Switching monthly is not free."""
    whipsaw = [100.0, 130.0, 95.0, 135.0, 90.0, 140.0, 85.0, 145.0,
               80.0, 150.0, 75.0, 155.0, 70.0, 160.0, 65.0, 165.0,
               60.0, 170.0, 55.0, 175.0, 50.0, 180.0, 45.0, 185.0]
    free = run_tactical({"X": monthly_series(whipsaw)},
                        round_trip_cost_pct=Decimal("0"), annual_yield=Decimal("0"))
    charged = run_tactical({"X": monthly_series(whipsaw)},
                           round_trip_cost_pct=Decimal("0.01"), annual_yield=Decimal("0"))
    assert charged.final_equity < free.final_equity
    assert sum(p.costs for p in charged.periods) > 0


def test_staying_put_costs_nothing():
    rising = [100.0 * (1.05 ** i) for i in range(26)]
    result = run_tactical({"X": monthly_series(rising)},
                          round_trip_cost_pct=Decimal("0.01"), annual_yield=Decimal("0"))
    # One entry, then held: only the first month should pay.
    paying = [p for p in result.periods if p.costs > 0]
    assert len(paying) == 1, f"{len(paying)} months paid costs while holding"


# --------------------------------------------------------------- the benchmark
def test_the_benchmark_covers_the_same_months():
    """A benchmark on a different window measures start dates, not skill."""
    series = [100.0 * (1.02 ** i) for i in range(30)]
    prices = {"X": monthly_series(series)}
    timed = run_tactical(prices)
    held = buy_and_hold(prices)
    assert timed.months == held.months
    assert timed.periods[0].month_end == held.periods[0].month_end
    assert timed.periods[-1].month_end == held.periods[-1].month_end


# ------------------------------------------------------------------- reporting
def test_a_short_window_refuses_to_annualise():
    """An 11-month CAGR is an extrapolation dressed as a measurement."""
    result = run_tactical({"X": monthly_series([100.0 * (1.02 ** i) for i in range(21)])})
    assert result.months < 12
    assert result.cagr is None
    assert result.volatility_pct() is None
    assert result.sharpe(Decimal("0.04")) is None


def test_sharpe_is_measured_against_the_lending_rate():
    """Against cash at zero, any stablecoin yield would look like skill."""
    rising = [100.0 * (1.03 ** i) for i in range(40)]
    result = run_tactical({"X": monthly_series(rising)}, annual_yield=Decimal("0.04"))
    assert result.sharpe(Decimal("0.00")) > result.sharpe(Decimal("0.10"))


def test_robustness_reports_every_window_and_picks_none():
    series = [100.0 * (1.02 ** i) for i in range(48)]
    results = robustness({"X": monthly_series(series)}, windows=(6, 10, 14))
    assert set(results) == {6, 10, 14}
    assert all(r.lookback_months == w for w, r in results.items())


def test_the_published_window_is_ten_months():
    """Pinned so a later 'tweak' has to be a deliberate, visible decision."""
    assert PUBLISHED_LOOKBACK_MONTHS == 10


def test_an_incomplete_current_month_is_not_treated_as_a_month_end():
    """Caught by a test, and it would have mattered every single month.

    Resampling labels each bucket with the month's last calendar day, so a
    series ending on the 3rd still yields a row dated the 31st whose close is
    the 3rd's price. Live, the current month is always partial -- so the rule
    would have decided every month on an incomplete one and measured its return
    against a date that had not happened yet.
    """
    # Jan + Feb complete, then two days of March.
    frame = daily([float(i) for i in range(1, 62)])     # 2020-01-01 .. 2020-03-01
    closes = monthly_closes(frame)
    assert list(closes.index.month) == [1, 2], "a partial month was kept"


def test_a_series_ending_exactly_on_month_end_keeps_that_month():
    frame = daily([float(i) for i in range(1, 32)])     # all of January
    assert len(monthly_closes(frame)) == 1


def test_a_sharpe_comes_with_an_interval():
    """A point estimate invites confidence eight years of data cannot support."""
    rising = [100.0 * (1.03 ** i) for i in range(60)]
    result = run_tactical({"X": monthly_series(rising)}, annual_yield=Decimal("0.04"))
    low, high = result.sharpe_interval(Decimal("0.04"))
    point = float(result.sharpe(Decimal("0.04")))
    assert float(low) < point < float(high)


def test_a_shorter_sample_gives_a_wider_sharpe_interval():
    """The whole reason to report it: fewer cycles, less certainty.

    Both series repeat the SAME monthly return pattern, so their Sharpe ratios
    match and only the length differs. Comparing two runs with different Sharpe
    ratios would not test this, because the standard error depends on both.
    """
    def repeating(cycles: int) -> list[float]:
        pattern = [1.06, 1.04, 0.97, 1.08, 1.02, 0.95,
                   1.07, 1.03, 0.99, 1.05, 1.01, 0.96]
        price, out = 100.0, [100.0]
        for _ in range(cycles):
            for step in pattern:
                price *= step
                out.append(price)
        return out

    long_run = run_tactical({"X": monthly_series(repeating(12))},
                            annual_yield=Decimal("0.04"))
    short_run = run_tactical({"X": monthly_series(repeating(3))},
                             annual_yield=Decimal("0.04"))
    assert long_run.months > short_run.months * 2

    long_low, long_high = long_run.sharpe_interval(Decimal("0.04"))
    short_low, short_high = short_run.sharpe_interval(Decimal("0.04"))
    assert (float(short_high) - float(short_low)) > (float(long_high) - float(long_low)), (
        "a shorter sample reported no more uncertainty than a longer one")


def test_no_interval_when_the_window_is_too_short_to_annualise():
    result = run_tactical({"X": monthly_series([100.0] * 20)})
    assert result.sharpe_interval(Decimal("0.04")) is None
