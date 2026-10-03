"""Does a stop loss actually stop the loss? Read-only.

A position was found exiting at 0.0009% of its entry price on a stop set at
-50%. The stop did not fail to fire; it fired at the first price observed after
the collapse, and that price was effectively zero. A -50% stop only limits a
loss if the price can be seen and acted on somewhere between -50% and the
bottom, and for liquidity pulled in a single block no such moment exists.

This measures the gap between the stop a strategy specifies and the price its
exits actually realise. If that gap is large, every threshold in every strategy
here is decoration, and tuning them is pointless.

Also counts quotes claiming 100% price impact by day, to see whether the
order-sizing fix removed them or they are still arriving.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import PaperPosition, SimulatedExit
from collector.paper import ALL_STRATEGIES

STOPS = {s.name: s.stop_loss_multiple for s in ALL_STRATEGIES}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    engine = create_engine(load_settings().database_url, future=True)
    with Session(engine) as session:
        rows = session.execute(
            select(PaperPosition.strategy, PaperPosition.entry_price_usd,
                   PaperPosition.exit_price_usd, PaperPosition.exit_reason)
            .where(PaperPosition.is_open.is_(False),
                   PaperPosition.exit_reason == "stop_loss",
                   PaperPosition.exit_price_usd.is_not(None))).all()

        print("=" * 76)
        print("DID THE STOP LOSS STOP THE LOSS?")
        print("=" * 76)
        if not rows:
            print("  no closed stop-loss positions yet")
        else:
            by_strategy: dict[str, list[float]] = defaultdict(list)
            for strategy, entry, exit_price, _ in rows:
                if not entry or float(entry) <= 0:
                    continue
                by_strategy[strategy].append(float(exit_price) / float(entry))

            print(f"  {'strategy':<18}{'stop set at':>12}{'median exit':>14}"
                  f"{'worst exit':>13}{'n':>6}")
            overshoot_total = []
            for strategy, multiples in sorted(by_strategy.items()):
                multiples.sort()
                stop = STOPS.get(strategy)
                median = multiples[len(multiples) // 2]
                worst = multiples[0]
                overshoot_total.extend(
                    m for m in multiples if stop is not None and m < stop * 0.5)
                stop_txt = f"x{stop:.2f}" if stop is not None else "?"
                print(f"  {strategy:<18}{stop_txt:>12}{f'x{median:.5f}':>14}"
                      f"{f'x{worst:.7f}':>13}{len(multiples):>6}")

            everything = sorted(m for ms in by_strategy.values() for m in ms)
            far_below = sum(1 for m in everything if m < 0.25)
            near_zero = sum(1 for m in everything if m < 0.01)
            print(f"\n  {len(everything)} stop-loss exits in total")
            print(f"    exited below x0.25 (half the stop or worse): "
                  f"{far_below} ({far_below / len(everything) * 100:.0f}%)")
            print(f"    exited below x0.01 (a near-total loss):      "
                  f"{near_zero} ({near_zero / len(everything) * 100:.0f}%)")
            if near_zero > len(everything) * 0.25:
                print("\n  The stop is not limiting anything. It fires at the first")
                print("  price observed after the collapse, which is near zero, so")
                print("  the realised loss is the whole stake regardless of where")
                print("  the stop was set. Tuning stop levels cannot change this;")
                print("  only observing faster could, and a one-block rug outruns")
                print("  any observation rate.")

        print("\n" + "=" * 76)
        print("QUOTES CLAIMING 100% PRICE IMPACT, BY DAY")
        print("=" * 76)
        print("  A $100 order cannot move a funded pool 100%. These inflate costs")
        print("  and, when they fail to route, inflate the unsellable rate.")
        impossible = session.execute(
            select(func.date(SimulatedExit.simulated_ts), func.count())
            .where(SimulatedExit.price_impact_pct >= 1.0)
            .group_by(func.date(SimulatedExit.simulated_ts))
            .order_by(func.date(SimulatedExit.simulated_ts).desc())
            .limit(args.days)).all()
        total = session.execute(
            select(func.date(SimulatedExit.simulated_ts), func.count())
            .group_by(func.date(SimulatedExit.simulated_ts))
            .order_by(func.date(SimulatedExit.simulated_ts).desc())
            .limit(args.days)).all()
        totals = dict(total)
        if not impossible:
            print("\n  none recorded -- the sizing fix is holding")
        else:
            print(f"\n  {'day':<14}{'impact>=100%':>14}{'all quotes':>12}{'share':>9}")
            for day, count in impossible:
                whole = totals.get(day, 0)
                share = f"{count / whole * 100:.1f}%" if whole else "-"
                print(f"  {str(day):<14}{count:>14,}{whole:>12,}{share:>9}")
            print("\n  If the most recent days still show these, order sizing is")
            print("  still wrong for some tokens -- most likely ones whose price")
            print("  moved far between the observation we size from and the quote.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
