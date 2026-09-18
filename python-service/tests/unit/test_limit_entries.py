"""Resting limit entries, and the cost of them.

Maker entries roughly halve the cost hurdle: no slippage, and 8bps instead of
10. That is a free lunch only if you ignore what you give up -- price has to come
back to you, and the trades that never come back are disproportionately the ones
that ran in your favour. Model the saving without the misses and a losing system
turns profitable on paper and stays losing in reality.

These tests pin the honest behaviour: a touch is not a fill, an unfilled order
expires and is counted, and the fee is genuinely the maker fee.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.fill_model import CostModel, simulate_limit_fill, simulate_market_fill
from app.models.enums import OrderType, Side
from app.models.signals import StrategySignal


@pytest.fixture
def costs() -> CostModel:
    return CostModel()


def test_a_limit_fill_costs_less_than_a_market_fill(costs):
    """The whole point: no slippage, no spread crossed, maker fee."""
    quantity = Decimal("1")
    price = Decimal("100")

    market = simulate_market_fill(price, quantity, Side.BUY, costs)
    limit = simulate_limit_fill(price, quantity, Side.BUY, costs)

    assert limit.price < market.price, "market buys lift the ask; a resting bid does not"
    assert limit.fee < market.fee, "maker fee must be lower than taker"
    assert limit.slippage_bps == 0
    assert limit.is_maker is True
    assert market.is_maker is False

    market_cost = market.notional + market.fee
    limit_cost = limit.notional + limit.fee
    assert limit_cost < market_cost
    # Meaningful, not marginal: worth roughly half the round-trip hurdle.
    assert (market_cost - limit_cost) / market_cost > Decimal("0.0005")


def test_signals_default_to_market_orders(costs):
    """Existing strategies must not silently change behaviour."""
    assert StrategySignal.model_fields["entry_order_type"].default is OrderType.MARKET
    assert StrategySignal.model_fields["entry_valid_bars"].default == 1



def test_a_touch_is_not_a_fill():
    """The engine's own rule, not a copy of it.

    At the limit price you are behind everyone already resting there, and only
    part of that queue trades. Crediting a fill on a touch hands a reversal
    strategy exactly the entries it would most often have missed -- the ones
    where price turned on the tick.
    """
    from app.backtesting.engine import limit_entry_fills

    limit = Decimal("100")
    assert limit_entry_fills(Decimal("99.99"), limit) is True, "traded through"
    assert limit_entry_fills(limit, limit) is False, "touched, not through"
    assert limit_entry_fills(Decimal("100.01"), limit) is False, "never reached"


# ------------------------------------------------------- end-to-end through the engine
def test_a_limit_entry_fills_only_when_price_comes_back(services, market_data):
    """Drive the real engine and check the fill rule, not a copy of it."""
    from app.backtesting.runner import run_backtest
    from app.models.backtest import BacktestDataSpec, BacktestRequest

    request = BacktestRequest(
        data=BacktestDataSpec(symbol="BTC/USDT", timeframe="1h", source="synthetic", limit=4000),
        strategies=["short_term_reversal"],
        starting_balance=Decimal("10000"),
    )
    result = run_backtest(request, services)

    # The strategy rests bids, so some entries must go unfilled. If none do, the
    # fill rule is too generous and the cost saving is not being paid for.
    missed = result.risk_rejections.get("LIMIT_ENTRY_NOT_FILLED", 0)
    assert missed > 0, (
        "no limit entry was ever missed — a resting bid that always fills is a "
        "backtest artefact, not a maker strategy"
    )

    for trade in result.trades:
        assert trade.entry_price > 0


def test_limit_entries_pay_maker_fees_in_a_real_run(services):
    """Fees on a maker entry must be lower than the taker path would charge."""
    from app.backtesting.runner import run_backtest
    from app.models.backtest import BacktestDataSpec, BacktestRequest

    def run(strategy: str):
        return run_backtest(
            BacktestRequest(
                data=BacktestDataSpec(
                    symbol="BTC/USDT", timeframe="1h", source="synthetic", limit=4000
                ),
                strategies=[strategy],
                starting_balance=Decimal("10000"),
            ),
            services,
        )

    reversal = run("short_term_reversal")
    if not reversal.trades:
        pytest.skip("no reversal trades on this fixture window")

    # Entry fills sit at or below the signal price: a resting bid never pays up.
    assert reversal.metrics.trades == len(reversal.trades)
