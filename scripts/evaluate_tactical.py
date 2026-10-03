"""Run Faber's tactical allocation on cached history and report it honestly.

Reports the published 10-month window as the result, every neighbouring window
as a robustness check, and the lending baseline as the bar to clear. Nothing is
selected on performance.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd


def pct(value) -> str:
    if value is None:
        return "    n/a"
    return f"{float(value) * 100:+7.2f}%"


def load(directory: Path, timeframe: str) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(directory.glob(f"*_{timeframe}.csv")):
        symbol = path.stem.rsplit("_", 1)[0].replace("-", "/")
        frame = pd.read_csv(path, parse_dates=["timestamp"])
        frames[symbol] = frame
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/history"))
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--equity", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--lending-apy", type=Decimal, default=Decimal("0.04"),
                        help="the risk-free leg, and the bar to clear")
    parser.add_argument("--cost-pct", type=Decimal, default=Decimal("0.003"),
                        help="round-trip taker fee plus slippage")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python-service"))
    from app.backtesting.tactical import (
        PUBLISHED_LOOKBACK_MONTHS,
        buy_and_hold,
        monthly_closes,
        robustness,
        run_tactical,
    )

    prices = load(args.data, args.timeframe)
    if not prices:
        print(f"No CSVs in {args.data}. Run scripts/fetch_history.py first.")
        return 2

    print("HISTORY AVAILABLE")
    for symbol, frame in prices.items():
        closes = monthly_closes(frame)
        print(f"  {symbol:<10} {len(closes):>3} complete months  "
              f"{closes.index[0]:%Y-%m} to {closes.index[-1]:%Y-%m}")
    shortest = min(len(monthly_closes(f)) for f in prices.values())
    if shortest <= PUBLISHED_LOOKBACK_MONTHS + 12:
        print(f"\n  WARNING: the shortest series has {shortest} months. A "
              f"{PUBLISHED_LOOKBACK_MONTHS}-month rule leaves "
              f"{shortest - PUBLISHED_LOOKBACK_MONTHS} months of signal, which "
              f"is too few regime changes to conclude anything.")

    kwargs = {"starting_equity": args.equity, "annual_yield": args.lending_apy,
              "round_trip_cost_pct": args.cost_pct}

    timed = run_tactical(prices, lookback_months=PUBLISHED_LOOKBACK_MONTHS, **kwargs)
    held = buy_and_hold(prices, lookback_months=PUBLISHED_LOOKBACK_MONTHS,
                        starting_equity=args.equity, round_trip_cost_pct=args.cost_pct)

    print(f"\nTHE PUBLISHED RULE ({PUBLISHED_LOOKBACK_MONTHS}-month SMA, "
          f"{timed.months} months tested)")
    head = f"\n{'':<16}{'TOTAL':>10}{'CAGR':>10}{'VOL':>10}{'MAXDD':>10}{'SHARPE':>9}{'IN MKT':>9}"
    print(head)
    intervals = {}
    for label, run in (("faber timed", timed), ("buy & hold", held)):
        sharpe = run.sharpe(args.lending_apy)
        intervals[label] = run.sharpe_interval(args.lending_apy)
        print(f"{label:<16}{pct(run.total_return_pct):>10}{pct(run.cagr):>10}"
              f"{pct(run.volatility_pct()):>10}{pct(run.max_drawdown_pct):>10}"
              f"{('   n/a' if sharpe is None else f'{float(sharpe):+8.2f}'):>9}"
              f"{pct(run.exposure_pct):>9}")

    # The bar: lending the same capital for the same window, doing nothing.
    years = timed.months / 12.0
    lend_total = Decimal(str((1.0 + float(args.lending_apy)) ** years - 1.0))
    print(f"{'lend only':<16}{pct(lend_total):>10}{pct(args.lending_apy):>10}"
          f"{'   0.00%':>10}{'   0.00%':>10}{'     n/a':>9}{'   0.00%':>9}")

    # A Sharpe without its interval invites more confidence than the sample
    # supports, and these two overlap heavily on any crypto history available.
    timed_ci, held_ci = intervals.get("faber timed"), intervals.get("buy & hold")
    if timed_ci and held_ci:
        print(f"\n  Sharpe 95% intervals over {timed.months / 12:.1f} years:")
        print(f"    faber timed  [{float(timed_ci[0]):+.2f}, {float(timed_ci[1]):+.2f}]")
        print(f"    buy & hold   [{float(held_ci[0]):+.2f}, {float(held_ci[1]):+.2f}]")
        if float(timed_ci[0]) < float(held_ci[1]):
            print("    These OVERLAP: the return advantage is not distinguishable")
            print("    from luck at this sample length. The drawdown reduction is the")
            print("    more credible claim -- it is structural, not a return forecast.")

    print("\nROBUSTNESS -- every window, none selected")
    print(f"{'window':<16}{'TOTAL':>10}{'CAGR':>10}{'MAXDD':>10}{'IN MKT':>9}")
    results = robustness(prices, **kwargs)
    for window, run in sorted(results.items()):
        marker = "  <- published" if window == PUBLISHED_LOOKBACK_MONTHS else ""
        print(f"{f'{window} months':<16}{pct(run.total_return_pct):>10}"
              f"{pct(run.cagr):>10}{pct(run.max_drawdown_pct):>10}"
              f"{pct(run.exposure_pct):>9}{marker}")

    beat = sum(1 for r in results.values() if r.total_return_pct > lend_total)
    print(f"\n  {beat} of {len(results)} windows beat lending at "
          f"{float(args.lending_apy):.1%}.")
    if beat and beat < len(results):
        print("  A result that holds at some windows and not their neighbours is")
        print("  fragile. Faber found 3-12 months all worked; that is what a real")
        print("  effect looks like.")

    print("\n  Drawdown is the claim this strategy actually makes. Compare MAXDD")
    print("  before total return: 'equity returns with bond volatility' is a")
    print("  statement about risk, not about beating the market.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
