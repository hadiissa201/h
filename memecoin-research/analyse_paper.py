"""Read the paper trading results honestly, with error bars.

Two jobs, both about not fooling ourselves.

First, EXCLUDE CONTAMINATED POSITIONS. Positions opened before the cost fix
carry impossible values (one recorded $3.5m of costs on a $100 stake). Averaging
them in poisons every aggregate, so they are filtered out by default and the
count of what was dropped is printed rather than hidden.

Second, PUT ERROR BARS ON THE DIFFERENCE. Memecoin position returns have
enormous spread, so two strategies can differ by several percentage points
through luck alone. A difference reported without a confidence interval is an
invitation to conclude something the data does not support -- which is exactly
how a sniper bot gets sold.

    python analyse_paper.py
    python analyse_paper.py --since 2026-09-25T18:00:00
"""

from __future__ import annotations

import argparse
import math
import statistics
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import PaperPosition

# A position whose costs exceed this multiple of its stake is arithmetically
# impossible and predates the fix. Kept as a filter rather than a delete: the
# rows are evidence of a bug, and deleting evidence is how bugs come back.
MAX_PLAUSIBLE_COST_MULTIPLE = 3.0


def returns_for(rows: list[PaperPosition]) -> list[float]:
    """Per-position return as a fraction of the stake."""
    out = []
    for row in rows:
        notional = float(row.notional_usd or 0.0)
        if notional <= 0 or row.net_pnl_usd is None:
            continue
        out.append(float(row.net_pnl_usd) / notional)
    return out


def contaminated(row: PaperPosition) -> bool:
    notional = float(row.notional_usd or 0.0)
    if notional <= 0:
        return True
    costs = float(row.costs_usd or 0.0)
    return costs > notional * MAX_PLAUSIBLE_COST_MULTIPLE


def describe(name: str, rows: list[PaperPosition]) -> dict | None:
    closed = [r for r in rows if not r.is_open]
    rets = returns_for(closed)
    if not rets:
        return None
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    stderr = sd / math.sqrt(len(rets)) if rets else 0.0
    wins = sum(1 for r in rets if r > 0)
    stuck = sum(1 for r in rows if r.is_open and (r.blocked_exits or 0) > 0)
    return {
        "name": name, "closed": len(rets), "open": len(rows) - len(closed),
        "stuck": stuck, "wins": wins, "win_rate": wins / len(rets),
        "mean_return": mean, "sd": sd, "stderr": stderr,
        # 95% interval on the MEAN, which is what we are actually comparing.
        "ci_low": mean - 1.96 * stderr, "ci_high": mean + 1.96 * stderr,
        "best": max(rets), "worst": min(rets),
    }


def compare(a: dict, b: dict) -> str:
    """Welch's t-test on the difference in mean return. Unequal variances."""
    diff = a["mean_return"] - b["mean_return"]
    se = math.sqrt(a["stderr"] ** 2 + b["stderr"] ** 2)
    if se == 0:
        return "cannot compare: no variance"
    t = diff / se
    lo, hi = diff - 1.96 * se, diff + 1.96 * se
    verdict = ("DIFFERENT" if abs(t) > 1.96 else
               "INDISTINGUISHABLE -- the difference is within noise")
    return (f"  {a['name']} - {b['name']} = {diff * 100:+.2f} pp\n"
            f"  95% CI [{lo * 100:+.2f}, {hi * 100:+.2f}] pp   t = {t:+.2f}\n"
            f"  -> {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None,
                        help="only positions opened at/after this ISO timestamp")
    parser.add_argument("--include-contaminated", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None

    with Session(engine) as session:
        everything = session.scalars(select(PaperPosition)).all()

    dropped_bad = 0
    dropped_old = 0
    kept: list[PaperPosition] = []
    for row in everything:
        if since is not None:
            opened = row.opened_ts
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if opened < since:
                dropped_old += 1
                continue
        if not args.include_contaminated and contaminated(row):
            dropped_bad += 1
            continue
        kept.append(row)

    print("=" * 74)
    print("PAPER TRADING -- hypothetical only, no money was ever at risk")
    print("=" * 74)
    print(f"positions: {len(everything)} total, {len(kept)} analysed")
    if dropped_bad:
        print(f"  EXCLUDED {dropped_bad} with impossible costs (pre-fix bug)")
    if dropped_old:
        print(f"  EXCLUDED {dropped_old} opened before --since")

    by_name: dict[str, list[PaperPosition]] = {}
    for row in kept:
        by_name.setdefault(row.strategy, []).append(row)

    stats = {}
    print(f"\n{'strategy':<14}{'closed':>7}{'open':>6}{'stuck':>7}{'wins':>6}"
          f"{'win%':>7}{'mean ret':>10}{'95% CI':>22}")
    print("-" * 80)
    for name in sorted(by_name):
        info = describe(name, by_name[name])
        if info is None:
            print(f"{name:<14}  no closed positions yet")
            continue
        stats[name] = info
        print(f"{name:<14}{info['closed']:>7}{info['open']:>6}{info['stuck']:>7}"
              f"{info['wins']:>6}{info['win_rate'] * 100:>6.1f}%"
              f"{info['mean_return'] * 100:>9.2f}%"
              f"   [{info['ci_low'] * 100:>+7.2f},{info['ci_high'] * 100:>+7.2f}]")

    if "snipe_asap" in stats and "wait_5m" in stats:
        print("\n" + "=" * 74)
        print("THE SNIPER QUESTION: does buying earlier pay?")
        print("=" * 74)
        print("Matched pair -- identical rules, only the entry window differs.\n")
        print(compare(stats["snipe_asap"], stats["wait_5m"]))
        print("\nScope: this compares earliness across SECONDS. A real sniper")
        print("competes in the launch block, in milliseconds. A null result here")
        print("weakens the case for chasing that; it does not disprove it.")

    if stats:
        print("\nEvery strategy's mean return is a small difference between large")
        print("numbers. Check the CI before believing any ranking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
