"""Does graduation separate the winners, and could it be seen at entry?

The on-chain run turned up 11 tokens whose bonding curve was zeroed, which
means they graduated: they survived the curve and migrated to an AMM. The
collector had been discarding those as read failures, and they are the closest
thing to winners this dataset holds.

Two questions follow, and ONLY THE SECOND ONE CAN MAKE MONEY.

WHAT DID GRADUATES RETURN? Interesting, and worthless on its own. Selecting
tokens by what they went on to do is survivorship bias in its purest form --
the same error that made the Faber trend result look like +2,271% until the
basket was rebuilt point-in-time. Reported here only as a ceiling: what a
perfect oracle for graduation would have been worth.

IS GRADUATION VISIBLE AT ENTRY? This is the tradable question. If some signal
observable in the first few minutes separates the tokens that graduate from
the ones that do not, that is an edge. If the graduation rate is flat across
every bucket of every early feature, then the ceiling above is unreachable and
knowing it changes nothing.

Read-only. No trading, no recommendations.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import UTC, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, Pool, Token
from collector.verify import wilson_interval

# Below this there is no test to run. A handful of graduates cannot support a
# claim about which early features predict graduation, and reporting bucket
# rates on five tokens would invite exactly the reading the header warns about.
MIN_GRADUATES = 20

# Features a trader could actually see shortly after launch. Deliberately
# excludes anything that is a consequence of surviving, such as peak price or
# observation count -- those would leak the answer into the predictor.
EARLY_FEATURES = (
    ("liquidity_usd", "liquidity"),
    ("market_cap_usd", "market cap"),
    ("volume_5m", "5m volume"),
    ("buys_5m", "5m buys"),
)


def graduation_markers(session: Session) -> tuple[set[int], Counter]:
    """Token ids that reached a non-pump.fun pool, plus what the dex column said.

    The counter is returned so the labelling can be checked rather than
    trusted. Every wrong conclusion in this project so far came from a number
    whose provenance was not printed next to it.
    """
    graduated: set[int] = set()
    seen: Counter = Counter()
    for pool in session.scalars(select(Pool)).all():
        label = (pool.dex or "unknown").lower()
        seen[label] += 1
        if "pump" not in label:
            graduated.add(pool.token_id)
    return graduated, seen


def early_features(session: Session, token: Token,
                   window_s: float) -> dict[str, float]:
    """The first non-null value of each feature inside the entry window.

    Anything after the window is hindsight. A token's liquidity an hour in is
    partly a result of it surviving, so using it to predict survival would
    measure nothing but itself.
    """
    cutoff = token.detected_ts
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    cutoff = cutoff + timedelta(seconds=window_s)
    rows = session.scalars(
        select(Observation)
        .where(Observation.token_id == token.id)
        .order_by(Observation.observed_ts.asc())).all()
    out: dict[str, float] = {}
    for row in rows:
        observed = row.observed_ts
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if observed > cutoff:
            break
        for field, _ in EARLY_FEATURES:
            value = getattr(row, field)
            if field not in out and value is not None:
                out[field] = float(value)
    return out


def tercile_rates(values: list[tuple[float, bool]]) -> list[dict]:
    """Graduation rate in each third of a feature's range, with intervals.

    A rate without an interval on a sample this size is a coin flip dressed as
    a finding.
    """
    ordered = sorted(values, key=lambda pair: pair[0])
    n = len(ordered)
    if n < 3:
        return []
    cuts = [ordered[: n // 3], ordered[n // 3: 2 * n // 3], ordered[2 * n // 3:]]
    out = []
    for name, bucket in zip(("bottom", "middle", "top"), cuts, strict=True):
        if not bucket:
            continue
        hits = sum(1 for _, graduated in bucket if graduated)
        low, high = wilson_interval(hits, len(bucket))
        out.append({"bucket": name, "n": len(bucket), "graduated": hits,
                    "rate": hits / len(bucket), "low": low, "high": high,
                    "min": bucket[0][0], "max": bucket[-1][0]})
    return out


def separates(rates: list[dict]) -> bool:
    """True only when the top and bottom intervals do not overlap.

    Overlapping intervals mean the apparent gap is within what this sample
    size can produce by chance, and calling that a signal is how a backtest
    starts lying.
    """
    if len(rates) < 2:
        return False
    bottom = next((r for r in rates if r["bucket"] == "bottom"), None)
    top = next((r for r in rates if r["bucket"] == "top"), None)
    if not bottom or not top:
        return False
    return bottom["high"] < top["low"] or top["high"] < bottom["low"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-window", type=float, default=300.0,
                        help="seconds after detection that a feature must be "
                             "observed within to count as visible at entry")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        tokens = session.scalars(select(Token)).all()
        graduated, dex_counts = graduation_markers(session)

        print("=" * 70)
        print("HOW GRADUATION IS LABELLED")
        print("=" * 70)
        print("  A token counts as graduated if it has a pool on a dex that is")
        print("  not pump.fun, which is what migration off the curve creates.")
        print("  The dex column, so the label can be checked:")
        for label, count in dex_counts.most_common(8):
            mark = "  graduated" if "pump" not in label else ""
            print(f"    {count:>6}  {label}{mark}")
        if not dex_counts:
            print("    no pools recorded at all -- nothing can be labelled")

        n = len(tokens)
        hits = sum(1 for t in tokens if t.id in graduated)
        low, high = wilson_interval(hits, n) if n else (0.0, 0.0)
        print("\n" + "=" * 70)
        print("BASE RATE")
        print("=" * 70)
        print(f"  {hits} of {n} tokens graduated "
              f"({hits / n * 100:.2f}%)" if n else "  no tokens")
        if n:
            print(f"  95% interval {low * 100:.2f}% to {high * 100:.2f}%")
            print("\n  This is the number any entry signal has to beat. A rule")
            print("  that picks graduates at the base rate has found nothing.")

        if hits < MIN_GRADUATES:
            print("\n" + "=" * 70)
            print("VERDICT")
            print("=" * 70)
            print(f"  UNDERPOWERED. {hits} graduates is below the {MIN_GRADUATES}")
            print("  needed to say anything about which early features predict")
            print("  graduation. Bucket rates on a sample this small would be")
            print("  noise, and noise presented as a signal is how this")
            print("  project has gone wrong before.")
            print("\n  What would fix it: the collector running again, with the")
            print("  on-chain reader wired in so the curve's own complete flag")
            print("  labels graduation directly instead of inferring it from a")
            print("  pool we may never have seen.")
            return 0

        print("\n" + "=" * 70)
        print(f"IS GRADUATION VISIBLE WITHIN {args.entry_window:.0f}s OF LAUNCH?")
        print("=" * 70)
        buckets: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        for token in tokens:
            feats = early_features(session, token, args.entry_window)
            for field, _ in EARLY_FEATURES:
                if field in feats:
                    buckets[field].append((feats[field], token.id in graduated))

        any_signal = False
        for field, label in EARLY_FEATURES:
            rows = buckets.get(field, [])
            print(f"\n  {label}  ({len(rows)} tokens had it recorded in time)")
            if len(rows) < 3:
                print("    too few to bucket")
                continue
            rates = tercile_rates(rows)
            for row in rates:
                print(f"    {row['bucket']:>6}  n={row['n']:<5} "
                      f"graduated {row['graduated']:>4}  "
                      f"{row['rate'] * 100:>6.2f}%  "
                      f"[{row['low'] * 100:.2f}%, {row['high'] * 100:.2f}%]")
            if separates(rates):
                any_signal = True
                print("    SEPARATES: the top and bottom intervals do not overlap")
            else:
                print("    no separation: the intervals overlap, so the gap is")
                print("    within what this sample can produce by chance")

        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)
        if any_signal:
            print("  At least one early feature separates graduates from the")
            print("  rest. That is a candidate edge and NOT yet a strategy:")
            print("  it needs a held-out period before it is traded, because a")
            print("  threshold chosen on the same data that suggested it will")
            print("  flatter itself.")
        else:
            print("  NO early feature separates graduates from the rest. Every")
            print("  bucket's interval overlaps every other's, so nothing")
            print("  observable in the first few minutes tells the two apart.")
            print("\n  What that means for the returns question: whatever")
            print("  graduates went on to earn is unreachable, because at entry")
            print("  they are indistinguishable from the ones that died. The")
            print("  graduated cohort is a ceiling, not an opportunity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
