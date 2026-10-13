"""How often does a cascade actually happen, at each threshold? Read-only.

The pre-registration fixed the cascade threshold at $250,000 in 10 seconds and
requires 100 cascades before any verdict. The first live run recorded 40
liquidations in six minutes and zero cascades, at sizes of $662 to $3,056. If
that rate holds, 100 cascades is months away.

So the threshold may have to change, and changing a pre-registered constant is
exactly what docs/LIQUIDATION_PLAN.md exists to make difficult. There is a
legitimate way and an illegitimate way:

LEGITIMATE. Choose the threshold on EVENT FREQUENCY alone, before any return
is computed, so the choice cannot be influenced by which threshold pays. That
is a power calculation: it answers "how long until there is enough to test",
which has nothing to do with the answer being tested.

ILLEGITIMATE. Compute returns at several thresholds and keep the best. That is
fitting, and it is how a backtest starts lying.

This script therefore reports FREQUENCY ONLY. It does not touch prices, it
cannot compute a return, and that is deliberate -- the separation is enforced
by the script not having the capability rather than by remembering not to.

    python scripts/liquidations/rate.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

CANDIDATE_THRESHOLDS = (25_000.0, 50_000.0, 100_000.0, 250_000.0, 500_000.0)
# A quiet market and a dropped connection both look like no liquidations.
# At the observed ~35/hour, seeing none for half an hour is vanishingly
# unlikely, so a gap this long is overwhelmingly an outage. Shorter gaps are
# left in, because calling a quiet stretch an outage would inflate the rate.
OUTAGE_GAP_S = 1_800.0
# The pre-registration's requirement, restated so the arithmetic is visible.
CASCADES_NEEDED = 100
DAYS_NEEDED = 20


def rolling_cascades(events: list[tuple[float, float]], threshold: float,
                     window_s: float) -> int:
    """Count non-overlapping windows whose liquidations sum past the threshold.

    Non-overlapping on purpose. A sliding count would report one burst many
    times over -- fifty windows all containing the same spike -- and inflate
    the sample with duplicates of a single event.
    """
    count = 0
    start = None
    total = 0.0
    for ts, notional in sorted(events):
        if start is None or ts - start > window_s:
            start, total = ts, 0.0
        total += notional
        if total >= threshold:
            count += 1
            start, total = None, 0.0
    return count


def find_gaps(timestamps: list[float],
              gap_s: float = OUTAGE_GAP_S) -> list[tuple[float, float]]:
    """Stretches with no liquidation at all, long enough to be an outage.

    Dividing by elapsed time was wrong by 4x on the first real run: 22 hours
    of wall clock contained a 17-hour hole where the websocket could not
    reconnect, so 176 liquidations read as 8/hour when the truth while
    connected was 35/hour.

    The error has a direction, which is why it matters rather than merely
    being imprecise: it makes cascades look rarer than they are, and would
    argue for lowering the pre-registered threshold further than the data
    requires.
    """
    gaps = []
    for earlier, later in pairwise(timestamps):
        if later - earlier >= gap_s:
            gaps.append((earlier, later))
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/liquidations.sqlite")
    parser.add_argument("--window", type=float, default=10.0)
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"No database at {path}. Run the collector first.")
        return 1

    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT symbol, side, event_ts, notional_usd FROM liquidations"
        " ORDER BY event_ts").fetchall()
    if not rows:
        print("No liquidations recorded yet.")
        return 0

    span_s = rows[-1][2] - rows[0][2]
    gaps = find_gaps([r[2] for r in rows])
    gap_s = sum(later - earlier for earlier, later in gaps)
    # Connected time, not elapsed time. See find_gaps.
    live_s = max(1.0, span_s - gap_s)
    hours = live_s / 3600.0
    days = live_s / 86400.0
    sizes = sorted(r[3] for r in rows)

    print("=" * 70)
    print("WHAT HAS BEEN COLLECTED")
    print("=" * 70)
    print(f"  {len(rows):,} liquidations")
    print(f"  elapsed        {span_s / 3600.0:>8.2f} hours")
    print(f"  lost to gaps   {gap_s / 3600.0:>8.2f} hours   "
          f"({len(gaps)} gap{'s' if len(gaps) != 1 else ''} over "
          f"{OUTAGE_GAP_S / 60:.0f}min)")
    print(f"  CONNECTED      {hours:>8.2f} hours   <- the denominator")
    if span_s > 0:
        print(f"  uptime         {live_s / span_s * 100:>8.1f}%")
    print(f"\n  {len(rows) / hours:,.1f} per hour while connected")
    if gaps:
        print(f"  counting the gaps as quiet market would say "
              f"{len(rows) / (span_s / 3600.0):,.1f}/hour,")
        print("  which understates the rate and argues for a lower threshold")
        print("  than the data needs")
    print(f"  total notional ${sum(sizes):,.0f}")
    print(f"\n  size: median ${sizes[len(sizes) // 2]:,.0f}, "
          f"p90 ${sizes[int(len(sizes) * 0.9)]:,.0f}, "
          f"max ${sizes[-1]:,.0f}")

    by_symbol: dict[str, list] = defaultdict(list)
    for symbol, side, ts, notional in rows:
        by_symbol[(symbol, side)].append((ts, notional))

    print("\n" + "=" * 70)
    print("CASCADES PER DAY, BY THRESHOLD")
    print("=" * 70)
    print(f"  window {args.window:.0f}s, counted non-overlapping so one burst")
    print("  is one event rather than fifty")
    print(f"\n  {'threshold':>12}  {'cascades':>8}  {'per day':>8}  "
          f"{'days for 100':>13}")
    for threshold in CANDIDATE_THRESHOLDS:
        total = sum(rolling_cascades(events, threshold, args.window)
                    for events in by_symbol.values())
        per_day = total / days if days > 0 else 0.0
        eta = (f"{CASCADES_NEEDED / per_day:,.0f}" if per_day > 0 else "never")
        marker = "  <- pre-registered" if threshold == 250_000.0 else ""
        print(f"  ${threshold:>11,.0f}  {total:>8}  {per_day:>8.1f}  "
              f"{eta:>13}{marker}")

    print("\n" + "=" * 70)
    print("WHAT THIS DOES AND DOES NOT LICENSE")
    print("=" * 70)
    print("  These are counts of events. No price was read and no return was")
    print("  computed -- this script cannot do either, which is the point.")
    print("  Choosing a threshold so that 100 cascades arrive in a workable")
    print(f"  time, over at least {DAYS_NEEDED} distinct days, is a power")
    print("  calculation and is legitimate. Choosing one because it paid")
    print("  better is fitting. If the threshold changes, the change belongs")
    print("  in docs/LIQUIDATION_PLAN.md with today's date and the reason,")
    print("  the way the crux-test amendment was recorded.")
    if gaps and live_s / span_s < 0.8:
        print(f"\n  UPTIME {live_s / span_s * 100:.0f}%. Gaps are not neutral:")
        print("  liquidations cluster in volatility, so an outage is likelier")
        print("  to have swallowed a cascade than a quiet stretch is. Every")
        print("  figure above is conditional on the hours actually seen.")
    if days < 0.04:
        print("\n  CAUTION: this is well under an hour of data. Liquidations")
        print("  cluster in volatility, so a short quiet sample understates")
        print("  the rate and a short violent one overstates it. Collect at")
        print("  least a full day before setting anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
