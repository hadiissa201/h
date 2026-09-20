"""Buy-and-hold benchmark.

A strategy's P&L means nothing on its own. +8% looks fine until the asset itself
returned +40% over the same window, at which point the strategy destroyed value:
it took risk, paid fees, and finished behind someone who did nothing. Equally,
-5% is a good result in a year the asset fell 40%.

So every backtest reports what buying at the first bar and selling at the last
would have produced, over the identical window, with the same cost model applied
to both legs. Not an argument for buy-and-hold -- just the bar to clear.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd

from app.core.numeric import ZERO, round_money, safe_div, to_decimal
from app.execution.fill_model import CostModel, simulate_market_fill
from app.models.backtest import BuyAndHoldBenchmark, YieldBaseline
from app.models.enums import Side


def buy_and_hold(
    candles: pd.DataFrame,
    starting_balance: Decimal,
    cost_model: CostModel,
) -> BuyAndHoldBenchmark | None:
    """Return of buying the first bar's open and selling the last bar's close.

    Entry is the first bar's OPEN, not its close, to match how the backtester
    fills: a decision made before the window starts executes at the first price
    actually available. Using the close would hand the benchmark a free bar of
    hindsight and flatter it against the strategies.
    """
    if candles is None or len(candles) < 2:
        return None

    start_price = to_decimal(float(candles["open"].iloc[0]))
    end_price = to_decimal(float(candles["close"].iloc[-1]))
    if start_price <= ZERO:
        return None

    # Buy as much as the balance allows, paying the same costs a strategy pays.
    # Solving exactly for fees is overkill; 1% held back covers entry cost at any
    # realistic fee level and leaves the comparison conservative.
    budget = starting_balance * Decimal("0.99")
    quantity = budget / start_price
    if quantity <= ZERO:
        return None

    entry = simulate_market_fill(start_price, quantity, Side.BUY, cost_model)
    exit_fill = simulate_market_fill(end_price, quantity, Side.SELL, cost_model)

    spent = entry.notional + entry.fee
    received = exit_fill.notional - exit_fill.fee
    net_pnl = round_money(received - spent, 8)

    # Drawdown of the holding itself, so a strategy's smoother ride is visible
    # as a genuine advantage even when it earns less.
    closes = candles["close"].astype(float)
    running_peak = closes.cummax()
    drawdowns = (running_peak - closes) / running_peak.replace(0.0, float("nan"))
    max_drawdown = to_decimal(float(drawdowns.max() or 0.0))

    return BuyAndHoldBenchmark(
        start_price=round_money(start_price, 8),
        end_price=round_money(end_price, 8),
        return_pct=safe_div(net_pnl, starting_balance),
        net_pnl=net_pnl,
        max_drawdown_pct=round_money(max(ZERO, max_drawdown), 8),
    )


def yield_baseline(
    starting_balance: Decimal,
    annual_rate: Decimal,
    period_start: datetime | None,
    period_end: datetime | None,
) -> YieldBaseline | None:
    """What the same capital would have earned lent out over the same window.

    Cash at 0% flatters every strategy. Idle USDT earns on any major venue, so
    the correct floor is not "did it beat zero" but "did it beat the yield the
    money was giving up to be traded". An independent researcher killed a
    funding-carry strategy on exactly this test: +3.13% APY is a loss against a
    ~6% lending rate, and it only looks like a win when the benchmark is zero.

    Compounded daily rather than simple, because that is how the venues pay it.
    A flat rate over the whole window is an approximation -- real rates float --
    so `annual_rate` travels with the result and the caller states its source.
    """
    if period_start is None or period_end is None:
        return None
    if starting_balance <= ZERO or annual_rate < ZERO:
        return None

    elapsed_days = (period_end - period_start).total_seconds() / 86400.0
    if elapsed_days <= 0:
        return None

    # (1 + r)^(days/365) - 1. Decimal has no fractional power, and the float
    # round-trip is harmless here: this is a benchmark, not an accounting entry.
    growth = (1.0 + float(annual_rate)) ** (elapsed_days / 365.0)
    net_pnl = round_money(starting_balance * to_decimal(growth - 1.0), 8)

    return YieldBaseline(
        annual_rate=annual_rate,
        days=round_money(to_decimal(elapsed_days), 4),
        return_pct=safe_div(net_pnl, starting_balance),
        net_pnl=net_pnl,
    )
