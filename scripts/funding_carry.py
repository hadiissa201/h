#!/usr/bin/env python3
"""What would a delta-neutral funding carry actually have yielded?

Not a strategy. Arithmetic on published data.

On perpetual futures, when more traders want to be long than short, longs pay
shorts a funding fee -- typically every 8 hours. Hold the spot asset and short
the perp against it and the price moves cancel: you are flat the market and
collect (or pay) funding. There is a mechanical reason the money arrives, which
is what separates this from every chart pattern this repository has tested and
rejected.

Binance publishes every historical funding payment, so this needs no strategy
logic and no assumptions about entries. It reads what was actually paid.

    python scripts/funding_carry.py --days 625

What it does NOT model, and you must not forget:

* **Liquidation of the short leg.** Delta-neutral on paper is not delta-neutral
  in an account. A sharp rally moves the perp against you; if the futures wallet
  is thin, you are liquidated and left long spot into the move. This is the way
  carry trades actually die, and no backtest of funding rates can show it.
* **Exchange and counterparty risk.** Both legs sit on one venue.
* **Rate changes.** Past funding says nothing about future funding. Yields
  compress when the trade gets crowded -- which is exactly what happens when a
  trade is well known, and this one is.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
PERIODS_PER_DAY = 3  # Binance funds every 8 hours
PAGE_LIMIT = 1000

# Round-trip cost of establishing and unwinding BOTH legs, in basis points.
# Spot buy + spot sell at taker, perp open + perp close at futures taker.
# Deliberately on the pessimistic side.
ROUND_TRIP_COST_BPS = 30.0

# Short-leg leverage choices to report against.
#
# Funding accrues on NOTIONAL, but the trade consumes spot capital plus futures
# margin, so return on the money actually tied up is always lower than the
# headline. Reporting the notional figure alone overstates the trade, which is
# the specific way carry gets oversold.
#
# The leverage choice is a straight trade between capital efficiency and
# survival: 2x leaves roughly 50% of adverse room before liquidation, 10x
# leaves ~10%. Bitcoin has moved 10% in a day more than once. Higher leverage
# reads better in this table and is closer to losing the position outright --
# and a liquidated short leaves you long spot into the rally that killed it.
LEVERAGE_CHOICES = (2.0, 3.0, 5.0, 10.0)


@dataclass(frozen=True)
class FundingStats:
    symbol: str
    periods: int
    first: datetime
    last: datetime
    days: float
    total_rate: float          # sum of funding rates over the window
    mean_rate: float
    positive_share: float      # fraction of periods a short would have been PAID
    worst_stretch: float       # deepest cumulative drawdown in collected funding
    annualised_gross: float    # on notional, before costs
    annualised_net: float      # after the one-off round-trip cost, amortised


def fetch_funding(symbol: str, days: int, timeout: float = 30.0) -> list[dict]:
    """Page backwards through Binance's published funding history."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows: list[dict] = []
    cursor = start_ms

    while True:
        url = (
            f"{FUNDING_URL}?symbol={symbol}&startTime={cursor}"
            f"&endTime={end_ms}&limit={PAGE_LIMIT}"
        )
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                page = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{symbol}: HTTP {exc.code} from Binance -- {exc.read()[:200]!r}")
        except Exception as exc:
            raise SystemExit(f"{symbol}: could not reach Binance ({exc})")

        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        nxt = int(page[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.2)  # be polite; this endpoint is public and unauthenticated

    # Deduplicate by funding time, which pages can repeat at the boundary.
    unique = {int(row["fundingTime"]): row for row in rows}
    return [unique[key] for key in sorted(unique)]


def analyse(symbol: str, rows: list[dict]) -> FundingStats | None:
    """Pure maths on published rates, so it can be tested without a network."""
    if len(rows) < 2:
        return None

    rates = [float(row["fundingRate"]) for row in rows]
    times = [
        datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=timezone.utc)
        for row in rows
    ]

    total = sum(rates)
    days = (times[-1] - times[0]).total_seconds() / 86_400.0
    if days <= 0:
        return None

    # Deepest peak-to-trough in CUMULATIVE funding. A positive average hides
    # stretches where a short pays for weeks; that is the drawdown you would have
    # had to sit through, and the reason people abandon the trade at the worst
    # moment.
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for rate in rates:
        cumulative += rate
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)

    annualised_gross = total / days * 365.0
    # The round trip is paid once, so amortise it over the window actually held.
    cost = ROUND_TRIP_COST_BPS / 10_000.0
    annualised_net = (total - cost) / days * 365.0

    return FundingStats(
        symbol=symbol,
        periods=len(rates),
        first=times[0],
        last=times[-1],
        days=days,
        total_rate=total,
        mean_rate=total / len(rates),
        positive_share=sum(1 for rate in rates if rate > 0) / len(rates),
        worst_stretch=worst,
        annualised_gross=annualised_gross,
        annualised_net=annualised_net,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--days", type=int, default=625, help="lookback (default ~20 months)")
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print("Delta-neutral funding carry: long spot, short perp.")
    print("Binance published funding history -- no strategy, no assumed entries.\n")

    results: list[FundingStats] = []
    for symbol in symbols:
        print(f"  fetching {symbol} ...", flush=True)
        stats = analyse(symbol, fetch_funding(symbol, args.days))
        if stats is None:
            print(f"    no usable funding history for {symbol}")
            continue
        results.append(stats)

    if not results:
        print("\nNo data. Nothing to conclude.")
        return 1

    print(f"\n{'symbol':<10}{'periods':>9}{'days':>7}{'paid%':>8}"
          f"{'total':>9}{'gross/yr':>10}{'net/yr':>9}{'worst':>9}")
    print("-" * 71)
    for stats in results:
        print(
            f"{stats.symbol:<10}{stats.periods:>9}{stats.days:>7.0f}"
            f"{stats.positive_share * 100:>7.1f}%"
            f"{stats.total_rate * 100:>8.2f}%"
            f"{stats.annualised_gross * 100:>9.2f}%"
            f"{stats.annualised_net * 100:>8.2f}%"
            f"{stats.worst_stretch * 100:>8.2f}%"
        )

    print("\npaid%    = share of 8h periods a short was PAID rather than charged")
    print("total    = funding collected over the whole window, on notional")
    print("gross/yr = annualised, before costs   net/yr = after a 30bps round trip")
    print("worst    = deepest run of paying instead of collecting")

    # ------------------------------------------------ return on deployed capital
    print("\nRETURN ON DEPLOYED CAPITAL (what the money actually earns)")
    print("Funding accrues on notional, but the trade ties up spot AND futures")
    print("margin. Per 10,000 of notional:\n")
    header = f"{'symbol':<10}{'on notional':>13}"
    for leverage in LEVERAGE_CHOICES:
        header += f"{f'{leverage:g}x short':>12}"
    print(header)
    print("-" * len(header))
    for stats in results:
        line = f"{stats.symbol:<10}{stats.annualised_net * 100:>12.2f}%"
        for leverage in LEVERAGE_CHOICES:
            capital = 1.0 + 1.0 / leverage  # spot + margin, per unit of notional
            line += f"{stats.annualised_net / capital * 100:>11.2f}%"
        print(line)
    print("\nHigher leverage on the short looks better here and is closer to being")
    print("liquidated. At 10x a ~10% rally wipes the short leg and leaves you long")
    print("spot into it. Bitcoin has moved 10% in a day more than once.")

    print(f"\nCash on {args.capital:,.0f} of notional over the window:")
    for stats in results:
        at_3x = stats.annualised_net / (1.0 + 1.0 / 3.0)
        print(
            f"  {stats.symbol:<10} {args.capital * stats.total_rate:>10,.0f} "
            f"collected  ({stats.annualised_net * 100:+.2f}%/yr on notional, "
            f"{at_3x * 100:+.2f}%/yr on capital at 3x)"
        )

    best = max(results, key=lambda s: s.annualised_net)
    print("\nRead:")
    if best.annualised_net <= 0:
        print("  Funding did not pay a short over this window. The carry trade was")
        print("  not a trade. That is the answer.")
    else:
        on_capital = best.annualised_net / (1.0 + 1.0 / 3.0)
        print(f"  Best was {best.symbol} at {best.annualised_net * 100:.2f}% a year net")
        print(f"  on notional -- but {on_capital * 100:.2f}% on the capital actually")
        print(f"  deployed at 3x. Paid in {best.positive_share * 100:.0f}% of periods,")
        print(f"  worst stretch {best.worst_stretch * 100:.2f}%.")
        print("\n  COMPARE THAT TO A SAVINGS ACCOUNT OR SHORT-DATED GOVERNMENT BONDS")
        print("  before going further. If risk-free pays about the same, this trade")
        print("  earns the same return while adding exchange counterparty risk,")
        print("  liquidation risk and daily management. That is a worse trade, not a")
        print("  better one, however consistent the funding looks.")
        print("\n  Also understand what it is NOT:")
        print("  * it needs the short leg margined well enough to survive a rally,")
        print("    and liquidation there is how this trade actually kills people;")
        print("  * it is one venue, so it carries that venue's counterparty risk;")
        print("  * a known, crowded trade compresses. Past funding does not")
        print("    predict future funding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
