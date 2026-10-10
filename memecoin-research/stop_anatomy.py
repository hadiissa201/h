"""Where does a -50% stop lose the other 27 points? Read-only.

Stops are configured at -50% and the median realised loss on a closed
position is -77%, with 29% of closes below -99%. stop_reality.py established
the size of that gap. It never said what the gap is MADE of, and the fix
depends entirely on that, because a threshold can only help with one of the
two possible answers.

GAP RISK. The price was already past the stop the first time we saw it. The
collector observes at intervals; a token that falls 90% between two
observations triggers the stop at -90%, not at -50%. Nothing about the
threshold changes this: a -10% stop and a -50% stop both exit at -90%. The
only lever is observing faster, and if the fall happens inside one block, not
even that.

EXECUTION WAIT. The price was near the stop when we saw it, and fell further
while we waited for an exit to be possible. Here the threshold DOES help --
triggering earlier means noticing higher up -- and so does anything that
shortens the wait.

This splits the realised loss into those two parts and says which dominates.
It is the difference between "tighten the stop" and "tightening the stop is
decoration", and the project has already measured the suspicious fact that
stop_10 realised -37.3% against stop_50's -43.5%: six points of improvement
for a five-fold tighter threshold, which is what gap risk looks like.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from datetime import UTC

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, PaperPosition
from collector.paper import ALL_STRATEGIES

STOPS = {s.name: s.stop_loss_multiple for s in ALL_STRATEGIES}

# Below this there is no anatomy to report, only anecdotes.
MIN_CLOSES = 20


def _aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def anatomy(entry: float, stop_multiple: float, exit_price: float,
            path: list[tuple[float, float]]) -> dict | None:
    """Split one position's loss into gap risk and execution wait.

    path is [(seconds_since_open, price)] strictly after the entry.

    Returns fractions OF THE ENTRY, all non-negative:
      gap   -- how far below the stop the price already was when first seen
      wait  -- how much further it fell between that sighting and the exit
      total -- the realised loss
    """
    if entry <= 0 or not path:
        return None
    threshold = entry * stop_multiple
    trigger = next(((t, p) for t, p in path if p <= threshold), None)
    if trigger is None:
        # The stop never fired on an observed price. Not a stop failure, and
        # counting it as one would blame the threshold for a time exit.
        return None
    trigger_s, trigger_price = trigger
    return {
        "gap": max(0.0, (threshold - trigger_price) / entry),
        "wait": max(0.0, (trigger_price - exit_price) / entry),
        "total": max(0.0, (entry - exit_price) / entry),
        "trigger_s": trigger_s,
        "gapped": (threshold - trigger_price) > (trigger_price - exit_price),
    }


def intervals(path: list[tuple[float, float]]) -> list[float]:
    """Seconds between consecutive observations. Gap risk scales with this."""
    return [b[0] - a[0] for a, b in zip(path, path[1:], strict=False)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default=None,
                        help="restrict to one strategy; default is all")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        query = select(PaperPosition).where(
            PaperPosition.is_open.is_(False),
            PaperPosition.exit_price_usd.is_not(None))
        if args.strategy:
            query = query.where(PaperPosition.strategy == args.strategy)
        positions = session.scalars(query).all()

        rows, skipped = [], Counter()
        all_intervals: list[float] = []
        for position in positions:
            stop = STOPS.get(position.strategy)
            if stop is None:
                skipped["unknown strategy"] += 1
                continue
            entry = float(position.entry_price_usd or 0.0)
            exit_price = float(position.exit_price_usd or 0.0)
            opened = _aware(position.opened_ts)
            closed = _aware(position.closed_ts) if position.closed_ts else None
            if not closed:
                skipped["no close time"] += 1
                continue
            observations = session.execute(
                select(Observation.observed_ts, Observation.price_usd)
                .where(Observation.token_id == position.token_id,
                       Observation.price_usd.is_not(None))
                .order_by(Observation.observed_ts.asc())).all()
            path = [((_aware(ts) - opened).total_seconds(), float(price))
                    for ts, price in observations
                    if opened < _aware(ts) <= closed]
            all_intervals.extend(intervals(path))
            result = anatomy(entry, stop, exit_price, path)
            if result is None:
                skipped["stop never fired on an observed price"] += 1
                continue
            result["strategy"] = position.strategy
            rows.append(result)

        print("=" * 70)
        print("WHAT THE REALISED LOSS IS MADE OF")
        print("=" * 70)
        print(f"  {len(positions)} closed positions, {len(rows)} where the stop "
              f"fired on an observed price")
        for reason, count in skipped.most_common():
            print(f"    {count:>5} skipped: {reason}")
        if len(rows) < MIN_CLOSES:
            print(f"\n  FEWER THAN {MIN_CLOSES}. Not enough to split the loss;")
            print("  what follows would be anecdote.")
            return 0

        gaps = [r["gap"] for r in rows]
        waits = [r["wait"] for r in rows]
        totals = [r["total"] for r in rows]
        print(f"\n  median realised loss          "
              f"-{statistics.median(totals) * 100:.1f}%")
        print(f"  median gap component          "
              f"-{statistics.median(gaps) * 100:.1f}%   "
              f"(already past the stop when first seen)")
        print(f"  median wait component         "
              f"-{statistics.median(waits) * 100:.1f}%   "
              f"(fell further before the exit was possible)")
        gap_sum, wait_sum = sum(gaps), sum(waits)
        if gap_sum + wait_sum > 0:
            share = gap_sum / (gap_sum + wait_sum) * 100
            print(f"\n  gap risk is {share:.0f}% of the avoidable loss, "
                  f"execution wait {100 - share:.0f}%")
        gapped = sum(1 for r in rows if r["gapped"])
        print(f"  {gapped} of {len(rows)} positions were already further past the")
        print("  stop than they fell afterwards")

        if all_intervals:
            print(f"\n  median gap between observations "
                  f"{statistics.median(all_intervals):.0f}s")
            print("  Gap risk scales with this directly: a token cannot be")
            print("  stopped at a price nobody looked at.")

        print("\n" + "=" * 70)
        print("WHICH LEVER")
        print("=" * 70)
        if gap_sum > wait_sum:
            print("  GAP RISK DOMINATES. Tightening the threshold cannot fix")
            print("  this, and the stop ladder measured exactly that: five")
            print("  times tighter bought six points. What would help is")
            print("  observing faster, and the ceiling on that is how fast a")
            print("  memecoin can be drained -- often one block, which no")
            print("  polling interval can beat.")
            print("\n  The honest conclusion if this holds: a stop loss is not")
            print("  a risk control on these assets. Position size is. A loss")
            print("  that cannot be bounded by an exit can only be bounded by")
            print("  how much was put in.")
        else:
            print("  EXECUTION WAIT DOMINATES. The price was near the stop when")
            print("  we saw it and fell while we waited, so the threshold does")
            print("  real work here and so does shortening the wait. A tighter")
            print("  stop should show up as a proportionally smaller loss, and")
            print("  the stop ladder is worth re-running to confirm it does.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
