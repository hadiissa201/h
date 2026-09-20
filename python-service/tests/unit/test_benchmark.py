"""Buy-and-hold benchmark.

The number that tells you whether a strategy earned its existence. Without it,
+8% reads as success even in a window where the asset returned +40% and the
strategy destroyed value by trading.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from app.backtesting.benchmark import buy_and_hold, yield_baseline
from app.execution.fill_model import CostModel

START = Decimal("10000")


def frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


@pytest.fixture
def costs() -> CostModel:
    return CostModel()


def test_a_doubling_market_is_reported_as_roughly_plus_100_percent(costs):
    result = buy_and_hold(frame([100.0] * 1 + [200.0]), START, costs)

    assert result is not None
    # Just under +100%: costs are charged on both legs, as they would be live.
    assert Decimal("0.95") < result.return_pct < Decimal("1.0")
    assert result.net_pnl > 0


def test_a_halving_market_is_reported_as_roughly_minus_50_percent(costs):
    result = buy_and_hold(frame([100.0, 50.0]), START, costs)

    assert result is not None
    assert Decimal("-0.52") < result.return_pct < Decimal("-0.49")


def test_a_flat_market_loses_exactly_the_trading_costs(costs):
    """Buy and sell at the same price and you are down by the round trip."""
    result = buy_and_hold(frame([100.0, 100.0]), START, costs)

    assert result is not None
    assert result.net_pnl < 0, "a flat round trip must not be free"
    assert result.return_pct > Decimal("-0.01"), "costs should be well under 1%"


def test_entry_uses_the_first_open_not_the_first_close(costs):
    """Entering at the close would hand the benchmark a free bar of hindsight."""
    candles = frame([100.0, 120.0])
    candles.loc[candles.index[0], "open"] = 90.0  # open well below the close

    result = buy_and_hold(candles, START, costs)

    assert result is not None
    assert result.start_price == Decimal("90.00000000")


def test_drawdown_reflects_the_ride_not_just_the_endpoints(costs):
    """A strategy earning less with a smoother ride deserves to show it."""
    result = buy_and_hold(frame([100.0, 150.0, 60.0, 140.0]), START, costs)

    assert result is not None
    # Peak 150 down to 60 is a 60% drawdown, even though it ended up.
    assert Decimal("0.55") < result.max_drawdown_pct < Decimal("0.65")
    assert result.net_pnl > 0


def test_too_little_data_returns_nothing_rather_than_a_fake_number(costs):
    assert buy_and_hold(frame([100.0]), START, costs) is None
    assert buy_and_hold(None, START, costs) is None


# --------------------------------------------------------------- yield baseline
# Cash at 0% is the wrong floor. These pin the bar that actually matters: the
# yield the capital gave up to be traded.

def test_one_year_at_four_percent_returns_about_four_percent():
    result = yield_baseline(
        START, Decimal("0.04"), datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result is not None
    assert float(result.return_pct) == pytest.approx(0.04, abs=0.0005)
    assert float(result.net_pnl) == pytest.approx(400.0, abs=5.0)


def test_half_a_year_earns_less_than_half_a_year_of_simple_interest():
    """Compounding is applied, so the fractional-year figure is not linear."""
    half = yield_baseline(
        START, Decimal("0.04"), datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 7, 2, tzinfo=UTC)
    )
    assert half is not None
    # (1.04)^0.5 - 1 = 1.98%, slightly under the 2% a simple rate would pay.
    assert float(half.return_pct) == pytest.approx(0.0198, abs=0.0005)
    assert float(half.return_pct) < 0.02


def test_the_rate_travels_with_the_result():
    """A benchmark whose assumption is not recorded can be quoted dishonestly."""
    result = yield_baseline(
        START, Decimal("0.06"), datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result is not None
    assert result.annual_rate == Decimal("0.06")
    assert float(result.days) == pytest.approx(365.0, abs=0.01)


def test_a_higher_rate_is_a_higher_bar():
    window = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    low = yield_baseline(START, Decimal("0.04"), *window)
    high = yield_baseline(START, Decimal("0.06"), *window)
    assert low is not None and high is not None
    assert high.net_pnl > low.net_pnl


def test_zero_rate_reproduces_the_old_cash_baseline():
    """The previous CASH column is just this benchmark with the rate set to 0."""
    result = yield_baseline(
        START, Decimal("0"), datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result is not None
    assert float(result.net_pnl) == pytest.approx(0.0, abs=1e-6)


def test_missing_or_inverted_window_yields_nothing_rather_than_a_wrong_number():
    stamp = datetime(2025, 1, 1, tzinfo=UTC)
    assert yield_baseline(START, Decimal("0.04"), None, stamp) is None
    assert yield_baseline(START, Decimal("0.04"), stamp, None) is None
    # Same instant: no time passed, so no yield was earned.
    assert yield_baseline(START, Decimal("0.04"), stamp, stamp) is None
    # End before start would otherwise produce a negative "yield".
    assert yield_baseline(START, Decimal("0.04"), stamp, datetime(2024, 1, 1, tzinfo=UTC)) is None


def test_a_negative_rate_is_refused():
    """Lending does not charge you. A negative rate is a caller bug, not a bar."""
    assert yield_baseline(
        START, Decimal("-0.01"), datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    ) is None
