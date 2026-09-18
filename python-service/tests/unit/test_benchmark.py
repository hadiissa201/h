"""Buy-and-hold benchmark.

The number that tells you whether a strategy earned its existence. Without it,
+8% reads as success even in a window where the asset returned +40% and the
strategy destroyed value by trading.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.backtesting.benchmark import buy_and_hold
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
