"""The low-volatility anomaly, tested on crypto.

Rank assets by trailing realised volatility, hold the least volatile bucket,
rebalance monthly. From Quantpedia's writeup of the low-vol literature (Clarke
de Silva and Thorley; Baker Bradley and Wurgler; Blitz and van Vliet).

Three things decide whether a result here means anything, and all three are
enforced in code rather than noted in a comment.

THE MECHANISM MOSTLY DOES NOT TRANSFER. The literature explains the anomaly by
leverage constraints -- investors who want high returns but cannot borrow buy
high-beta instead -- and by benchmark-driven managers tilting to high beta to
beat an index. Neither applies to crypto: leverage is one click away and there
are no index-tracking crypto managers at scale. What remains is the behavioural
leg, lottery-ticket preference, which is plausibly stronger here. So a positive
result would have one supporting mechanism out of three.

LOW VOLATILITY IN CRYPTO OFTEN MEANS DEAD. In equities the low-vol bucket is
utilities and staples. Here a coin nobody trades has a flat price and ranks as
the calmest asset on the exchange. Without a liquidity floor the strategy loads
up on zombies and prints a smooth equity curve nobody could have traded. The
floor is applied from trailing volume known at the rebalance date, never from
today's.

THE UNIVERSE IS CHOSEN AS AT EACH DATE. An asset enters only once it has enough
history at that date, so nothing is selected on information from the future.
The one bias left is delisting: the data source lists pairs that exist today.
That inflates the long-only result, which is why the long-short SPREAD is the
headline -- both legs are drawn from the same surviving set, so the bias largely
cancels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from app.core.numeric import ZERO, round_money, safe_div, to_decimal

# Trailing window for the volatility ranking, in months. The literature uses
# three years of history; crypto pairs rarely have much more, so this is the
# published spirit at the longest length the data supports.
LOOKBACK_MONTHS = 36
# Fraction of the universe held. A decile needs ~100 names per bucket; crypto
# gives a few hundred pairs at most, so quintiles keep buckets usable.
BUCKET = 0.20
# Minimum median daily quote volume for an asset to be investable, measured
# over a trailing window. Below this the price is an artefact of nobody trading
# rather than a calm market.
MIN_DAILY_VOLUME_USD = 250_000.0
# Months of SUSTAINED liquidity required. A single month's volume is not
# enough: one busy month after years of silence would admit exactly the dead
# asset the floor exists to exclude, and a test caught the earlier version
# doing that.
VOLUME_WINDOW_MONTHS = 12


@dataclass
class Rebalance:
    month_end: datetime
    universe_size: int
    held: tuple[str, ...]
    gross_return: Decimal
    costs: Decimal
    net_return: Decimal
    equity: Decimal


@dataclass
class BucketResult:
    name: str
    rebalances: list[Rebalance] = field(default_factory=list)
    starting_equity: Decimal = Decimal("10000")

    @property
    def months(self) -> int:
        return len(self.rebalances)

    @property
    def final_equity(self) -> Decimal:
        return self.rebalances[-1].equity if self.rebalances else self.starting_equity

    @property
    def total_return_pct(self) -> Decimal:
        return safe_div(self.final_equity - self.starting_equity, self.starting_equity)

    @property
    def cagr(self) -> Decimal | None:
        if self.months < 12:
            return None
        ratio = float(safe_div(self.final_equity, self.starting_equity))
        if ratio <= 0:
            return None
        return to_decimal(ratio ** (12.0 / self.months) - 1.0)

    @property
    def max_drawdown_pct(self) -> Decimal:
        peak, worst = self.starting_equity, ZERO
        for step in self.rebalances:
            peak = max(peak, step.equity)
            worst = min(worst, safe_div(step.equity - peak, peak))
        return worst

    def volatility_pct(self) -> Decimal | None:
        if self.months < 12:
            return None
        rets = [float(r.net_return) for r in self.rebalances]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return to_decimal((var ** 0.5) * (12 ** 0.5))

    def sharpe(self, risk_free_annual: Decimal) -> Decimal | None:
        vol, cagr = self.volatility_pct(), self.cagr
        if vol is None or cagr is None or vol == ZERO:
            return None
        return safe_div(cagr - risk_free_annual, vol)

    def monthly_returns(self) -> list[float]:
        return [float(r.net_return) for r in self.rebalances]


def month_end_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Month-end close plus the month's median daily volume.

    Volume is carried because the liquidity floor must be applied from what was
    known at the rebalance date. A filter using today's volume would quietly
    select assets that survived.
    """
    frame = daily.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    frame.index = pd.to_datetime(frame.index, utc=True)
    closes = frame["close"].resample("ME").last()
    # Quote volume: base volume times price. Binance's volume column is base.
    quote = (frame["close"] * frame["volume"]).resample("ME").median()
    out = pd.DataFrame({"close": closes, "quote_volume": quote}).dropna()
    # Drop a trailing partial month, as the current month always is.
    if len(out) and frame.index.max() < out.index[-1]:
        out = out.iloc[:-1]
    return out


def trailing_volatility(closes: pd.Series, months: int) -> pd.Series:
    """Standard deviation of monthly log returns over the trailing window.

    Log returns because a ranking on simple returns penalises assets that have
    risen -- a +100% month and a -50% month are the same move in opposite
    directions, and only logs treat them symmetrically.
    """
    prices = closes.astype("float64")
    ratios = prices / prices.shift(1)
    ratios = ratios.where(ratios > 0)       # a zero or negative print is bad data
    log_returns = np.log(ratios)
    return log_returns.rolling(window=months, min_periods=months).std()


def run_buckets(
    prices: dict[str, pd.DataFrame],
    lookback_months: int = LOOKBACK_MONTHS,
    bucket: float = BUCKET,
    min_volume_usd: float = MIN_DAILY_VOLUME_USD,
    volume_window_months: int = VOLUME_WINDOW_MONTHS,
    starting_equity: Decimal = Decimal("10000"),
    round_trip_cost_pct: Decimal = Decimal("0.003"),
) -> dict[str, BucketResult]:
    """Low-vol bucket, high-vol bucket, and the whole investable universe.

    All three run over identical months and identical universes, so the only
    difference between them is the ranking. Comparing a strategy against a
    benchmark built from a different set is how a universe choice gets mistaken
    for a strategy.
    """
    frames = {sym: month_end_frame(df) for sym, df in prices.items()}
    frames = {s: f for s, f in frames.items() if len(f) > lookback_months + 1}
    if not frames:
        return {}

    vols = {sym: trailing_volatility(f["close"], lookback_months)
            for sym, f in frames.items()}
    # Trailing median of monthly median volume: liquidity that held up, using
    # only months already past at the rebalance date.
    liquidity = {
        sym: f["quote_volume"].rolling(
            window=min(volume_window_months, lookback_months), min_periods=1).median()
        for sym, f in frames.items()
    }
    months = sorted(set().union(*(set(f.index) for f in frames.values())))

    results = {
        "low_vol": BucketResult("low_vol", starting_equity=starting_equity),
        "high_vol": BucketResult("high_vol", starting_equity=starting_equity),
        "universe": BucketResult("universe", starting_equity=starting_equity),
    }
    equity = {name: starting_equity for name in results}
    previous: dict[str, tuple[str, ...]] = {name: () for name in results}

    for index in range(len(months) - 1):
        decide, earn = months[index], months[index + 1]

        # Investable as at `decide`: enough history for a ranking, a volume
        # floor met on trailing data, and a price to earn into next month.
        candidates: list[tuple[float, str]] = []
        for sym, frame in frames.items():
            if decide not in frame.index or earn not in frame.index:
                continue
            vol = vols[sym].get(decide)
            if vol is None or pd.isna(vol):
                continue
            sustained = liquidity[sym].get(decide)
            if sustained is None or pd.isna(sustained) or float(sustained) < min_volume_usd:
                continue
            candidates.append((float(vol), sym))

        if len(candidates) < 10:
            continue            # too thin to form buckets; skip the month

        candidates.sort()
        size = max(1, int(len(candidates) * bucket))
        picks = {
            "low_vol": tuple(sorted(sym for _, sym in candidates[:size])),
            "high_vol": tuple(sorted(sym for _, sym in candidates[-size:])),
            "universe": tuple(sorted(sym for _, sym in candidates)),
        }

        for name, held in picks.items():
            weight = to_decimal(1.0 / len(held))
            gross = ZERO
            for sym in held:
                start = float(frames[sym].loc[decide, "close"])
                end = float(frames[sym].loc[earn, "close"])
                if start > 0:
                    gross += weight * to_decimal(end / start - 1.0)
            changed = len(set(held).symmetric_difference(previous[name]))
            traded = min(to_decimal(1.0), to_decimal(changed) * weight)
            costs = traded * round_trip_cost_pct
            net = gross - costs
            equity[name] = round_money(equity[name] * (to_decimal(1.0) + net), 2)
            results[name].rebalances.append(Rebalance(
                month_end=earn.to_pydatetime(), universe_size=len(candidates),
                held=held, gross_return=gross, costs=costs, net_return=net,
                equity=equity[name]))
            previous[name] = held

    return results


def spread_test(low: BucketResult, high: BucketResult) -> dict:
    """Welch's t-test on monthly returns, low-vol minus high-vol.

    This is the headline rather than the long-only return, because both buckets
    come from the same surviving universe. Delisting bias inflates them both, so
    it largely cancels in the difference -- which is exactly the weakness that
    killed the trend-following test.
    """
    a, b = low.monthly_returns(), high.monthly_returns()
    n_a, n_b = len(a), len(b)
    if n_a < 12 or n_b < 12:
        return {"months": min(n_a, n_b), "conclusive": False,
                "reason": "fewer than 12 paired months"}

    mean_a, mean_b = sum(a) / n_a, sum(b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)
    se = math.sqrt(var_a / n_a + var_b / n_b)
    diff = mean_a - mean_b
    if se == 0:
        return {"months": n_a, "conclusive": False, "reason": "zero variance"}

    t = diff / se
    half = 1.96 * se
    return {
        "months": n_a,
        "monthly_diff_pct": diff,
        "annualised_diff_pct": diff * 12,
        "ci_low_pct": (diff - half) * 12,
        "ci_high_pct": (diff + half) * 12,
        "t_stat": t,
        # The interval excluding zero is the whole question. A positive mean
        # with an interval spanning zero is not a finding.
        "significant": (diff - half) > 0 or (diff + half) < 0,
        "conclusive": True,
    }
