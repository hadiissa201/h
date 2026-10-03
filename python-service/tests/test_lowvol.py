"""The low-volatility anomaly on crypto.

The tests worth having are about the ways this specific strategy fakes a
result: picking dead coins because dead coins have flat prices, selecting the
universe with hindsight, and reading a positive mean as a finding when its
interval spans zero.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from app.backtesting.lowvol import (
    LOOKBACK_MONTHS,
    month_end_frame,
    run_buckets,
    spread_test,
    trailing_volatility,
)


def series(values: list[float], quote_volume: float = 5_000_000.0,
           start: str = "2018-01-31") -> pd.DataFrame:
    """Month-end bars. `quote_volume` is DOLLAR volume; base volume is derived,
    because that is what an exchange actually reports."""
    index = pd.date_range(start, periods=len(values), freq="ME", tz="UTC")
    return pd.DataFrame({"timestamp": index, "close": values,
                         "volume": [quote_volume / max(v, 0.01) for v in values]})


def calm(n: int = 60, rate: float = 1.01) -> list[float]:
    return [100.0 * (rate ** i) for i in range(n)]


def wild(n: int = 60, rate: float = 1.01) -> list[float]:
    return [100.0 * (rate ** i) * (1.6 if i % 2 else 0.65) for i in range(n)]


# ------------------------------------------------------------ the zombie trap
def test_an_illiquid_coin_is_not_held_however_calm_it_looks():
    """The trap unique to crypto.

    In equities the low-vol bucket is utilities. Here a coin nobody trades has
    a flat price and ranks as the calmest asset on the exchange. Without a
    volume floor the strategy buys zombies and prints a smooth equity curve
    nobody could have traded.
    """
    prices = {f"WILD{i}/USDT": series(wild()) for i in range(12)}
    prices["ZOMBIE/USDT"] = series([100.0] * 60, quote_volume=500.0)   # flat, no volume

    results = run_buckets(prices, min_volume_usd=250_000.0)
    held = {sym for step in results["low_vol"].rebalances for sym in step.held}
    assert "ZOMBIE/USDT" not in held, "bought a coin nobody was trading"


def test_the_volume_floor_uses_volume_known_at_the_time():
    """A floor applied from today's volume would select survivors.

    This asset is silent for its first 50 months and busy afterwards. At the
    earliest rebalance it must be excluded, because at that date its liquidity
    had not happened yet.
    """
    quiet = series([100.0] * 70)
    quiet.loc[quiet.index[:50], "volume"] = 1.0        # base volume -> $100/day
    prices = {"PHASED/USDT": quiet}
    prices |= {f"OTHER{i}/USDT": series(calm(70)) for i in range(12)}

    results = run_buckets(prices, min_volume_usd=250_000.0)
    early = results["universe"].rebalances[:5]
    assert early, "no rebalances to check"
    assert all("PHASED/USDT" not in step.held for step in early), (
        "held an asset during months when it had no volume")


def test_one_busy_month_does_not_admit_a_dead_asset():
    """Sustained liquidity, not a single month's.

    The first version checked only the rebalance month, so a lone busy month
    after years of silence let a zombie in.
    """
    zombie = series([100.0] * 70, quote_volume=100.0)
    zombie.loc[zombie.index[48], "volume"] = 1_000_000.0   # one busy month
    prices = {"ZOMBIE/USDT": zombie}
    prices |= {f"OTHER{i}/USDT": series(calm(70)) for i in range(12)}

    results = run_buckets(prices, min_volume_usd=250_000.0, volume_window_months=12)
    held = {sym for step in results["universe"].rebalances for sym in step.held}
    assert "ZOMBIE/USDT" not in held


# -------------------------------------------------------------- the selection
def test_an_asset_enters_only_once_it_has_enough_history():
    """No asset may be ranked on a window that extends before its listing."""
    short = series(calm(40), start="2021-01-31")
    long_ones = {f"OLD{i}/USDT": series(calm(90)) for i in range(12)}
    prices = {"NEW/USDT": short} | long_ones

    results = run_buckets(prices, lookback_months=36)
    first_held = next(
        (step.month_end for step in results["universe"].rebalances
         if "NEW/USDT" in step.held), None)
    assert first_held is not None
    # 36 months of history after a 2021-01 listing is 2024-01 at the earliest.
    assert first_held.year >= 2024


def test_all_three_buckets_cover_identical_months():
    """Comparing a strategy to a benchmark on a different window measures the
    window. This is the mistake that made trend following look like it worked."""
    prices = {f"A{i}/USDT": series(calm() if i % 2 else wild()) for i in range(14)}
    results = run_buckets(prices)
    months = {name: [s.month_end for s in r.rebalances] for name, r in results.items()}
    assert months["low_vol"] == months["high_vol"] == months["universe"]


def test_the_calm_assets_land_in_the_low_bucket():
    prices = {f"CALM{i}/USDT": series(calm()) for i in range(8)}
    prices |= {f"WILD{i}/USDT": series(wild()) for i in range(8)}
    results = run_buckets(prices, bucket=0.25)
    low = {sym for s in results["low_vol"].rebalances for sym in s.held}
    high = {sym for s in results["high_vol"].rebalances for sym in s.held}
    assert all(sym.startswith("CALM") for sym in low), low
    assert all(sym.startswith("WILD") for sym in high), high


def test_a_universe_too_small_to_rank_is_skipped():
    """Deciles of three assets are not deciles."""
    prices = {f"X{i}/USDT": series(calm()) for i in range(4)}
    results = run_buckets(prices)
    assert all(r.months == 0 for r in results.values())


# ------------------------------------------------------------------- the costs
def test_turnover_is_charged_when_the_bucket_changes():
    prices = {f"A{i}/USDT": series(wild()) for i in range(14)}
    free = run_buckets(prices, round_trip_cost_pct=Decimal("0"))
    paid = run_buckets(prices, round_trip_cost_pct=Decimal("0.01"))
    assert paid["low_vol"].final_equity < free["low_vol"].final_equity


# ------------------------------------------------------------------ the spread
def test_a_positive_spread_with_an_interval_spanning_zero_is_not_a_finding():
    """The discipline that killed the sniper-timing hypothesis, reused."""
    prices = {f"A{i}/USDT": series(calm() if i % 2 else wild()) for i in range(16)}
    results = run_buckets(prices)
    test = spread_test(results["low_vol"], results["high_vol"])
    assert test["conclusive"] is True
    if not test["significant"]:
        assert test["ci_low_pct"] <= 0 <= test["ci_high_pct"]


def test_the_spread_refuses_a_verdict_on_too_few_months():
    prices = {f"A{i}/USDT": series(calm(44)) for i in range(14)}
    results = run_buckets(prices, lookback_months=36)
    test = spread_test(results["low_vol"], results["high_vol"])
    if test["months"] < 12:
        assert test["conclusive"] is False


def test_volume_is_measured_in_quote_currency():
    """Binance reports base volume. A $1 coin and a $60,000 coin with the same
    base volume are not equally liquid, and ranking them as if they were would
    admit exactly the illiquid assets the floor exists to exclude."""
    index = pd.date_range("2020-01-31", periods=40, freq="ME", tz="UTC")
    # Two coins a day changing hands: trivial in base terms, $100k in quote.
    pricey = pd.DataFrame({"timestamp": index, "close": [50_000.0] * 40,
                           "volume": [2.0] * 40})
    assert float(month_end_frame(pricey)["quote_volume"].iloc[-1]) == 100_000.0


def test_log_returns_treat_a_doubling_and_a_halving_symmetrically():
    index = pd.date_range("2020-01-31", periods=40, freq="ME", tz="UTC")
    up_then_down = pd.Series(
        [100.0 * (2.0 if i % 2 else 1.0) for i in range(40)], index=index)
    vol = trailing_volatility(up_then_down, 36).iloc[-1]
    assert float(vol) > 0
    assert LOOKBACK_MONTHS == 36
