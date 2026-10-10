"""How much of the 14x is left after the rule has waited for its signal?

signal_test.py established that entering when the first 5-minute buy count is
at least 10 graduates 5.80% of the time out of sample, against 0.41% below the
threshold. At that win rate, and against the measured -77% median realised
loss, break-even needs +1,251% on every winner.

Graduation carries a curve from roughly $5k to $69k, about 14x, which clears
it -- but only for a buyer who entered at the very start. This rule waits 300
seconds for the buying it keys on, and that buying is what moved the price.
This script measures what is actually left.

ENTRY IS FIXED BY THE RULE, NOT CHOSEN WITH HINDSIGHT. oracle.py answers a
different question: it holds the cheapest entry it has seen, which is a
ceiling nobody can trade. Here the entry is the first observation that trips
the threshold, at whatever price that was, because that is the price the rule
pays.

EXIT IS STILL OPTIMISTIC, AND DELIBERATELY. It takes the best exit available
anywhere after entry, so the result is an UPPER BOUND on the rule. Two
versions are reported: the best chart price, which nobody can necessarily
sell into, and the best price where a sale was actually verified. The gap
between them is the exit problem the oracle run already measured once, where
outcomes above 10x fell from 16 to 2.

Read-only.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import UTC

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, SimulatedExit, Token
from collector.paper import MAX_POOL_FRACTION
from graduated import chain_labels
from oracle import ROUND_TRIP_COST
from signal_test import MEDIAN_REALISED_LOSS

# The rule signal_test.py fitted on the earlier half and confirmed on the
# later half. Not re-fitted here: refitting it on the graduates it is being
# measured against would be the survivorship error one layer down.
BUYS_THRESHOLD = 10.0
ENTRY_WINDOW_S = 300.0
OUT_OF_SAMPLE_WIN_RATE = 0.0580
STALE_BUDGET_S = 600.0


def entry_at_rule(rows, detected, notional: float):
    """(ts, price) of the first observation that trips the rule, or None.

    No search, no choosing. The rule fires once and pays whatever is quoted.
    """
    for row in rows:
        moment = row.observed_ts
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if (moment - detected).total_seconds() > ENTRY_WINDOW_S:
            return None
        if row.buys_5m is None or float(row.buys_5m) < BUYS_THRESHOLD:
            continue
        price = float(row.price_usd) if row.price_usd else 0.0
        liquidity = float(row.liquidity_usd) if row.liquidity_usd else 0.0
        if price <= 0:
            continue
        if liquidity > 0 and notional > liquidity * MAX_POOL_FRACTION:
            # The order would not fit. Counted as no entry rather than as a
            # free fill, which is the mistake that produced 100% impact on a
            # $20k pool earlier in this project.
            return None
        return moment, price
    return None


def best_after(rows, entry_ts, entry_price: float,
               sellable: dict | None, notional: float):
    """Best net return after entry. sellable=None means chart prices."""
    best = None
    for row in rows:
        moment = row.observed_ts
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if moment <= entry_ts or not row.price_usd:
            continue
        price = float(row.price_usd)
        impact = 0.0
        if sellable is not None:
            match = None
            for sim_ts, sim_impact in sellable.items():
                gap = (moment - sim_ts).total_seconds()
                if 0 <= gap <= STALE_BUDGET_S and (match is None or gap < match[0]):
                    match = (gap, sim_impact)
            if match is None:
                continue
            impact = min(1.0, abs(match[1]))
        multiple = price / entry_price
        gross = notional * (multiple - 1.0)
        costs = notional * ROUND_TRIP_COST + max(0.0, notional + gross) * impact
        net = (gross - costs) / notional
        if best is None or net > best[1]:
            best = (multiple, net)
    return best


def expectancy(win_rate: float, gain: float,
               loss: float = MEDIAN_REALISED_LOSS) -> float:
    """Per-trade expectancy as a fraction of the stake."""
    return win_rate * gain - (1 - win_rate) * loss


def describe(label: str, nets: list[float], multiples: list[float]) -> None:
    print(f"\n  {label}: {len(nets)} graduates with a rule entry")
    if not nets:
        print("    none, so there is nothing to measure")
        return
    print(f"    median multiple from entry  {statistics.median(multiples):>8.2f}x")
    print(f"    best multiple               {max(multiples):>8.2f}x")
    print(f"    median net                  {statistics.median(nets) * 100:>8.1f}%")
    print(f"    mean net                    "
          f"{sum(nets) / len(nets) * 100:>8.1f}%")
    above = sum(1 for n in nets if n >= 12.51)
    print(f"    {above} of {len(nets)} cleared the +1,251% break-even")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notional", type=float, default=100.0)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        graduated, eligible, _ = chain_labels(session)
        if not graduated:
            print("No graduated tokens labelled. Run label_graduation.py first.")
            return 1

        tokens = [t for t in session.scalars(select(Token)).all()
                  if t.id in graduated]
        print("=" * 70)
        print("WHAT THE RULE ACTUALLY CAPTURES ON THE WINNERS")
        print("=" * 70)
        print(f"  {len(tokens)} graduated tokens, entered at the first 5m buy")
        print(f"  count >= {BUYS_THRESHOLD:.0f} inside {ENTRY_WINDOW_S:.0f}s, "
              f"${args.notional:,.0f} per position")
        print("\n  Entry is whatever price the rule pays. Exit is the best")
        print("  available afterwards, so both figures below are UPPER BOUNDS.")

        chart_nets, chart_mults = [], []
        sell_nets, sell_mults = [], []
        no_entry = 0
        reasons: dict[str, int] = defaultdict(int)
        for token in tokens:
            detected = token.detected_ts
            if detected.tzinfo is None:
                detected = detected.replace(tzinfo=UTC)
            rows = session.scalars(
                select(Observation)
                .where(Observation.token_id == token.id)
                .order_by(Observation.observed_ts.asc())).all()
            entry = entry_at_rule(rows, detected, args.notional)
            if entry is None:
                no_entry += 1
                reasons["never tripped the rule inside the window"] += 1
                continue
            entry_ts, entry_price = entry

            verified = {
                ts if ts.tzinfo else ts.replace(tzinfo=UTC): float(impact or 0.0)
                for ts, impact in session.execute(
                    select(SimulatedExit.simulated_ts,
                           SimulatedExit.price_impact_pct)
                    .where(SimulatedExit.token_id == token.id,
                           SimulatedExit.succeeded.is_(True))).all()}

            chart = best_after(rows, entry_ts, entry_price, None, args.notional)
            if chart:
                chart_mults.append(chart[0])
                chart_nets.append(chart[1])
            sell = best_after(rows, entry_ts, entry_price, verified,
                              args.notional) if verified else None
            if sell:
                sell_mults.append(sell[0])
                sell_nets.append(sell[1])

        describe("CHART prices (cannot necessarily be sold into)",
                 chart_nets, chart_mults)
        describe("VERIFIED sellable prices", sell_nets, sell_mults)
        if no_entry:
            print(f"\n  {no_entry} graduates never tripped the rule, so the rule")
            print("  would not have bought them at all. They are not losses, but")
            print("  they are not the winners it would have caught either.")

        print("\n" + "=" * 70)
        print("EXPECTANCY AT THE OUT-OF-SAMPLE WIN RATE")
        print("=" * 70)
        print(f"  win rate above the threshold  {OUT_OF_SAMPLE_WIN_RATE * 100:.2f}%"
              f"   (held out, never fitted)")
        print(f"  median realised loss          "
              f"-{MEDIAN_REALISED_LOSS * 100:.0f}%     (measured, 227 closes)")
        print("\n  Expectancy takes the MEAN winner, not the median. A")
        print("  memecoin payoff is a power law: almost all of the return is")
        print("  in a few outcomes, so the median winner understates what the")
        print("  strategy actually collects. The first version of this script")
        print("  used the median and so was unfair to the rule.")
        for label, nets in (("chart", chart_nets), ("sellable", sell_nets)):
            if not nets:
                print(f"\n  {label}: no data")
                continue
            mean_gain = sum(nets) / len(nets)
            value = expectancy(OUT_OF_SAMPLE_WIN_RATE, mean_gain)
            print(f"\n  {label}, MEAN winner +{mean_gain * 100:,.0f}%:")
            print(f"    expectancy per trade  {value * 100:>+8.2f}%   "
                  f"{'PROFITABLE' if value > 0 else 'STILL LOSES'}")

            # How much of that rests on one token. With 34 winners a single
            # 217x outcome can carry the whole mean, and a strategy whose
            # edge is one trade has not been measured, it has been witnessed.
            trimmed = sorted(nets)[:-1]
            if trimmed:
                trimmed_gain = sum(trimmed) / len(trimmed)
                trimmed_value = expectancy(OUT_OF_SAMPLE_WIN_RATE, trimmed_gain)
                share = ((mean_gain - trimmed_gain) / mean_gain * 100
                         if mean_gain else 0.0)
                print(f"    drop the single best winner: mean falls to "
                      f"+{trimmed_gain * 100:,.0f}%")
                print(f"    expectancy then       {trimmed_value * 100:>+8.2f}%"
                      f"   ({share:.0f}% of the mean was that one token)")
            median_gain = statistics.median(nets)
            print(f"    for reference, median winner +{median_gain * 100:,.0f}%"
                  f" -> {expectancy(OUT_OF_SAMPLE_WIN_RATE, median_gain) * 100:+.2f}%")
        print("\n  A negative number here is close to decisive. The exit was")
        print("  chosen with full hindsight and the entry was the real one, so")
        print("  no exit rule can do better than this. Only a higher win rate")
        print("  or a smaller loss per loser can change it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
