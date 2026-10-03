"""Faber's asset-class trend following, as published.

The rule, from A Quantitative Approach to Tactical Asset Allocation (Faber,
2007): once a month, hold an asset if its monthly close is above its own
10-month simple moving average, otherwise hold the risk-free asset. Equal
weight across whatever is currently above its average. No stops, no sizing
rules, no discretion.

Two things about applying it here are worth stating plainly, because they
decide what a result can mean.

WHAT TRANSFERS. Faber's headline -- equity returns with bond volatility -- comes
from two sources: timing each asset, and holding five weakly correlated asset
classes. Only the first transfers to BTC/ETH/SOL, which move together. Expect a
drawdown reduction, not diversification.

THE RISK-FREE LEG IS NOT ZERO. Faber parks in Treasuries. Sitting in stablecoin
earns a real lending rate, so the cash leg compounds, and a timing rule has to
beat that rather than beat zero. That raises the bar, which is the point.

The published window is 10 months and that is what gets tested. Other windows
are computed only as a robustness check -- reported together, never selected
from. If one window works and its neighbours do not, that is fragility, and
picking the winner would manufacture an edge out of noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from app.core.numeric import ZERO, round_money, safe_div, to_decimal

# Faber's published lookback, in months. Not a tunable.
PUBLISHED_LOOKBACK_MONTHS = 10


@dataclass
class AllocationPeriod:
    """One month of the backtest, with everything needed to audit it."""

    month_end: datetime
    held: tuple[str, ...]
    cash_weight: float
    gross_return: Decimal
    costs: Decimal
    net_return: Decimal
    equity: Decimal


@dataclass
class TacticalResult:
    name: str
    lookback_months: int
    periods: list[AllocationPeriod] = field(default_factory=list)
    starting_equity: Decimal = Decimal("10000")

    @property
    def final_equity(self) -> Decimal:
        return self.periods[-1].equity if self.periods else self.starting_equity

    @property
    def total_return_pct(self) -> Decimal:
        return safe_div(self.final_equity - self.starting_equity, self.starting_equity)

    @property
    def months(self) -> int:
        return len(self.periods)

    @property
    def cagr(self) -> Decimal | None:
        """None, not zero, when the window is too short to annualise honestly."""
        if self.months < 12:
            return None
        years = self.months / 12.0
        ratio = float(safe_div(self.final_equity, self.starting_equity))
        if ratio <= 0:
            return None
        return to_decimal((ratio ** (1.0 / years)) - 1.0)

    @property
    def max_drawdown_pct(self) -> Decimal:
        """The number this strategy exists to improve."""
        peak = self.starting_equity
        worst = ZERO
        for period in self.periods:
            peak = max(peak, period.equity)
            draw = safe_div(period.equity - peak, peak)
            worst = min(worst, draw)
        return worst

    @property
    def months_in_market(self) -> int:
        return sum(1 for p in self.periods if p.held)

    @property
    def exposure_pct(self) -> Decimal:
        return safe_div(to_decimal(self.months_in_market), to_decimal(self.months))

    def volatility_pct(self) -> Decimal | None:
        """Annualised standard deviation of monthly net returns."""
        if self.months < 12:
            return None
        rets = [float(p.net_return) for p in self.periods]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return to_decimal((var ** 0.5) * (12 ** 0.5))

    def sharpe(self, risk_free_annual: Decimal) -> Decimal | None:
        """Excess return over the LENDING rate, not over zero.

        Measuring excess return against cash at 0% would credit this strategy
        for yield any stablecoin holder earns by doing nothing.
        """
        vol = self.volatility_pct()
        cagr = self.cagr
        if vol is None or cagr is None or vol == ZERO:
            return None
        return safe_div(cagr - risk_free_annual, vol)


def monthly_closes(daily: pd.DataFrame) -> pd.Series:
    """Last close of each calendar month.

    Faber's signal is monthly. Resampling daily data to month-end is not a
    detail: running the same rule on daily bars is a different, far more active
    strategy with far higher turnover.
    """
    if daily.empty:
        return pd.Series(dtype="float64")
    frame = daily.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    frame.index = pd.to_datetime(frame.index, utc=True)
    closes = frame["close"].resample("ME").last().dropna()

    # Drop a trailing partial month. Resampling labels each bucket with the
    # month's last calendar day, so a series ending on the 3rd still produces a
    # row dated the 31st -- and that row's "month-end close" is the 3rd's
    # price. Live, the current month is ALWAYS partial, so without this the
    # rule would decide every month on an incomplete one and compute its return
    # against a date that has not happened.
    if len(closes) and frame.index.max() < closes.index[-1]:
        closes = closes.iloc[:-1]
    return closes


def signals_from_closes(closes: pd.Series, lookback_months: int) -> pd.Series:
    """True where the month's close sits above its own trailing average.

    The average includes the current month, and the resulting position applies
    to the FOLLOWING month -- which is what makes this implementable. Using a
    signal to trade the same month it is computed from is look-ahead, and it is
    the most common way this strategy gets accidentally inflated.
    """
    sma = closes.rolling(window=lookback_months, min_periods=lookback_months).mean()
    return (closes > sma).where(sma.notna())


def run_tactical(
    prices: dict[str, pd.DataFrame],
    lookback_months: int = PUBLISHED_LOOKBACK_MONTHS,
    starting_equity: Decimal = Decimal("10000"),
    annual_yield: Decimal = Decimal("0.04"),
    round_trip_cost_pct: Decimal = Decimal("0.003"),
    name: str = "faber_tactical",
) -> TacticalResult:
    """Equal weight across assets above their average; the rest earns yield."""
    closes = {sym: monthly_closes(df) for sym, df in prices.items()}
    closes = {sym: s for sym, s in closes.items() if len(s) > lookback_months}
    if not closes:
        return TacticalResult(name=name, lookback_months=lookback_months,
                              starting_equity=starting_equity)

    signals = {sym: signals_from_closes(s, lookback_months) for sym, s in closes.items()}
    months = sorted(set().union(*(set(s.index) for s in closes.values())))

    monthly_yield = to_decimal((1.0 + float(annual_yield)) ** (1.0 / 12.0) - 1.0)
    result = TacticalResult(name=name, lookback_months=lookback_months,
                            starting_equity=starting_equity)
    equity = starting_equity
    previous: tuple[str, ...] = ()

    for index in range(len(months) - 1):
        decision_month, next_month = months[index], months[index + 1]

        # Decide using data through decision_month, earn over next_month.
        held = tuple(sorted(
            sym for sym, sig in signals.items()
            if decision_month in sig.index and sig.loc[decision_month] is True
            and next_month in closes[sym].index and decision_month in closes[sym].index
        ))
        if not held and not any(
            decision_month in sig.index and pd.notna(sig.loc[decision_month])
            for sig in signals.values()
        ):
            continue        # no asset has enough history yet

        weight_each = to_decimal(1.0 / len(held)) if held else ZERO
        gross = ZERO
        for sym in held:
            start, end = closes[sym].loc[decision_month], closes[sym].loc[next_month]
            if start <= 0:
                continue
            gross += weight_each * to_decimal(float(end) / float(start) - 1.0)
        cash_weight = ZERO if held else to_decimal(1.0)
        gross += cash_weight * monthly_yield

        # Turnover: only what actually changed hands pays a cost.
        changed = len(set(held).symmetric_difference(previous))
        traded = min(to_decimal(1.0), to_decimal(changed) * weight_each) if held or previous else ZERO
        costs = traded * round_trip_cost_pct
        net = gross - costs
        equity = round_money(equity * (to_decimal(1.0) + net), 2)

        result.periods.append(AllocationPeriod(
            month_end=next_month.to_pydatetime(), held=held,
            cash_weight=float(cash_weight), gross_return=gross,
            costs=costs, net_return=net, equity=equity))
        previous = held

    return result


def buy_and_hold(
    prices: dict[str, pd.DataFrame],
    starting_equity: Decimal = Decimal("10000"),
    lookback_months: int = PUBLISHED_LOOKBACK_MONTHS,
    round_trip_cost_pct: Decimal = Decimal("0.003"),
) -> TacticalResult:
    """Equal-weight, always invested, over the SAME months as the tactical run.

    Same window or the comparison is meaningless: a strategy that happened to
    start after a crash would look skilful against a benchmark that did not.
    """
    closes = {sym: monthly_closes(df) for sym, df in prices.items()}
    closes = {sym: s for sym, s in closes.items() if len(s) > lookback_months}
    if not closes:
        return TacticalResult(name="buy_and_hold", lookback_months=lookback_months,
                              starting_equity=starting_equity)

    signals = {sym: signals_from_closes(s, lookback_months) for sym, s in closes.items()}
    months = sorted(set().union(*(set(s.index) for s in closes.values())))
    result = TacticalResult(name="buy_and_hold", lookback_months=lookback_months,
                            starting_equity=starting_equity)
    equity = starting_equity
    first = True

    for index in range(len(months) - 1):
        decision_month, next_month = months[index], months[index + 1]
        available = tuple(sorted(
            sym for sym, sig in signals.items()
            if decision_month in sig.index and pd.notna(sig.loc[decision_month])
            and next_month in closes[sym].index
        ))
        if not available:
            continue
        weight_each = to_decimal(1.0 / len(available))
        gross = ZERO
        for sym in available:
            start, end = closes[sym].loc[decision_month], closes[sym].loc[next_month]
            if start > 0:
                gross += weight_each * to_decimal(float(end) / float(start) - 1.0)
        costs = round_trip_cost_pct / to_decimal(2.0) if first else ZERO
        first = False
        net = gross - costs
        equity = round_money(equity * (to_decimal(1.0) + net), 2)
        result.periods.append(AllocationPeriod(
            month_end=next_month.to_pydatetime(), held=available, cash_weight=0.0,
            gross_return=gross, costs=costs, net_return=net, equity=equity))

    return result


def robustness(
    prices: dict[str, pd.DataFrame],
    windows: tuple[int, ...] = (6, 8, 10, 12, 14),
    **kwargs,
) -> dict[int, TacticalResult]:
    """Every window, reported together and never selected from.

    Faber found 3-12 months all worked, which is what a real effect looks like.
    If one window here is profitable and its neighbours are not, the result is
    noise with a lucky parameter, and choosing it would be the overfitting this
    project keeps finding in its own work.
    """
    kwargs.pop("lookback_months", None)
    return {w: run_tactical(prices, lookback_months=w, **kwargs) for w in windows}
