"""Test the low-volatility anomaly on a broad crypto universe.

The long-short SPREAD is the headline, not the long-only return. The data
source only lists pairs that exist today, so delisted coins are invisible and
any long-only number is optimistic. Both buckets are drawn from the same
surviving set, so that bias largely cancels in the difference -- which is
precisely the weakness that killed the trend-following test.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd


def pct(value) -> str:
    return "    n/a" if value is None else f"{float(value) * 100:+7.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/universe"))
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--lookback", type=int, default=36)
    parser.add_argument("--bucket", type=float, default=0.20)
    parser.add_argument("--min-volume", type=float, default=250_000.0)
    parser.add_argument("--lending-apy", type=Decimal, default=Decimal("0.04"))
    parser.add_argument("--cost-pct", type=Decimal, default=Decimal("0.003"))
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python-service"))
    from app.backtesting.lowvol import run_buckets, spread_test

    prices = {}
    for path in sorted(args.data.glob(f"*_{args.timeframe}.csv")):
        symbol = path.stem.rsplit("_", 1)[0].replace("-", "/")
        prices[symbol] = pd.read_csv(path, parse_dates=["timestamp"])
    if not prices:
        print(f"No CSVs in {args.data}. Run scripts/fetch_universe.py first.")
        return 2

    print(f"UNIVERSE: {len(prices)} assets on disk")
    results = run_buckets(
        prices, lookback_months=args.lookback, bucket=args.bucket,
        min_volume_usd=args.min_volume, round_trip_cost_pct=args.cost_pct)
    if not results or not results["low_vol"].rebalances:
        print("\nNot enough overlapping history to form buckets. Either the "
              "lookback is too long for this data or too few assets qualify.")
        return 1

    low, high, univ = results["low_vol"], results["high_vol"], results["universe"]
    sizes = [s.universe_size for s in univ.rebalances]
    print(f"  investable at each rebalance: {min(sizes)}-{max(sizes)} assets, "
          f"holding the {args.bucket:.0%} tails")
    print(f"  months tested: {low.months} "
          f"({low.rebalances[0].month_end:%Y-%m} to "
          f"{low.rebalances[-1].month_end:%Y-%m})")

    print(f"\n{'':<12}{'TOTAL':>11}{'CAGR':>10}{'VOL':>10}{'MAXDD':>10}{'SHARPE':>9}")
    for label, run in (("low vol", low), ("high vol", high), ("universe", univ)):
        sharpe = run.sharpe(args.lending_apy)
        print(f"{label:<12}{pct(run.total_return_pct):>11}{pct(run.cagr):>10}"
              f"{pct(run.volatility_pct()):>10}{pct(run.max_drawdown_pct):>10}"
              f"{('   n/a' if sharpe is None else f'{float(sharpe):+8.2f}'):>9}")
    years = low.months / 12.0
    lend = (1.0 + float(args.lending_apy)) ** years - 1.0
    print(f"{'lend only':<12}{pct(lend):>11}{pct(args.lending_apy):>10}"
          f"{'   0.00%':>10}{'   0.00%':>10}{'     n/a':>9}")

    print("\nTHE ANOMALY ITSELF -- low vol minus high vol")
    test = spread_test(low, high)
    if not test["conclusive"]:
        print(f"  inconclusive: {test['reason']}")
        return 0

    print(f"  annualised difference  {test['annualised_diff_pct'] * 100:+7.2f}%")
    print(f"  95% CI                 [{test['ci_low_pct'] * 100:+.2f}%, "
          f"{test['ci_high_pct'] * 100:+.2f}%]")
    print(f"  t-statistic            {test['t_stat']:+7.2f}")
    print(f"  months                 {test['months']}")

    if test["significant"]:
        direction = "LOWER" if test["annualised_diff_pct"] > 0 else "HIGHER"
        print(f"\n  SIGNIFICANT: {direction}-volatility assets outperformed, and "
              f"the\n  interval excludes zero. This is the first result in this "
              f"project to\n  survive its own test. Treat it as a reason to keep "
              f"testing, not as\n  a reason to deploy capital.")
    else:
        print("\n  NOT SIGNIFICANT: the interval spans zero, so the sign of this")
        print("  difference is not established. A positive mean with an interval")
        print("  like that is what noise looks like.")

    print("\n  Remaining bias, which no run from this data can remove: delisted")
    print("  pairs are invisible, so both buckets exclude assets that died")
    print("  outright. The spread cancels most of it; the long-only numbers")
    print("  above are optimistic and should not be read as achievable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
