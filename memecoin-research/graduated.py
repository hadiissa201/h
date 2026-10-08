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
from collector.models import CurveState, Observation, Pool, Token
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


def chain_labels(session: Session) -> tuple[set[int], set[int], int]:
    """(graduated, eligible, labelled) from the curve's own complete flag.

    Preferred over the pool table whenever it exists, because it is not
    censored by what the collector happened to be watching. Returns an empty
    eligible set when label_graduation.py has not been run.
    """
    graduated, eligible = set(), set()
    rows = session.scalars(select(CurveState)).all()
    for row in rows:
        if not row.account_exists:
            continue
        eligible.add(row.token_id)
        if row.complete:
            graduated.add(row.token_id)
    return graduated, eligible, len(rows)


def graduation_markers(session: Session) -> tuple[set[int], set[int], Counter]:
    """Tokens that LEFT the pump.fun curve, the ones eligible to, and the dexes.

    Returns (graduated, eligible, dex_counts).

    Graduation is a transition WITHIN one token: it launched on the pump.fun
    curve and later appeared on another venue. The first version asked instead
    whether a pool's dex name contained "pump", and got the answer exactly
    backwards in both directions. It discarded pumpswap -- pump.fun's own AMM,
    which is where a graduating token goes -- and it counted Meteora DBC and
    Bags tokens as graduated pump.fun coins when those are separate
    LAUNCHPADS that were never on a pump.fun curve at all.

    That mistake manufactured a signal. 156 "graduates" were mostly foreign
    launchpads, so the market-cap separation it found was a launchpad detector
    dressed as a graduation predictor.
    """
    pools_by_token: dict[int, set[str]] = defaultdict(set)
    seen: Counter = Counter()
    for pool in session.scalars(select(Pool)).all():
        label = (pool.dex or "unknown").lower()
        seen[label] += 1
        pools_by_token[pool.token_id].add(label)

    eligible, graduated = set(), set()
    for token_id, dexes in pools_by_token.items():
        if not any(_is_pumpfun_curve(d) for d in dexes):
            # Never on the curve, so it cannot graduate off one.
            continue
        eligible.add(token_id)
        if any(not _is_pumpfun_curve(d) for d in dexes):
            graduated.add(token_id)
    return graduated, eligible, seen


def _is_pumpfun_curve(dex: str) -> bool:
    """The launch venue itself, not pump.fun's AMM.

    pumpswap is the destination, so matching on "pump" alone would treat
    arriving as never having left.
    """
    return "pumpfun" in dex or dex in {"pump.fun", "pump"}


def missingness_confound(subsample_hits: int, subsample_n: int,
                         base_hits: int, base_n: int) -> bool:
    """True when HAVING the feature predicts the outcome better than its value.

    The run that prompted this: only 162 of 1,764 tokens had liquidity
    recorded within 300s, and 70.4% of those graduated against an 8.8% base
    rate. Whether the field exists is an eight-fold stronger signal than any
    value it takes, so terciles of that field measure our own coverage. The
    tell was the direction -- LESS early liquidity appeared to predict MORE
    graduation, which no market story explains.
    """
    if not subsample_n or not base_n:
        return False
    sub_low, sub_high = wilson_interval(subsample_hits, subsample_n)
    base_low, base_high = wilson_interval(base_hits, base_n)
    return sub_high < base_low or base_high < sub_low


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
        all_tokens = session.scalars(select(Token)).all()
        chain_grad, chain_elig, labelled = chain_labels(session)
        graduated, eligible, dex_counts = graduation_markers(session)

        print("=" * 70)
        print("HOW GRADUATION IS LABELLED")
        print("=" * 70)
        from_chain = bool(chain_elig)
        if from_chain:
            graduated, eligible = chain_grad, chain_elig
            print(f"  FROM THE CHAIN. {labelled} tokens have had their bonding")
            print("  curve read directly, and the curve's own complete flag")
            print("  says whether it graduated. That is not censored by what")
            print("  the collector happened to be watching.")
            if labelled < len(all_tokens):
                print(f"\n  INCOMPLETE: {len(all_tokens) - labelled} of "
                      f"{len(all_tokens)} tokens are not labelled")
                print("  yet, so every figure below covers the labelled subset")
                print("  only. Finish the pass before reading the base rate as")
                print("  the truth:")
                print("    python label_graduation.py --rpc "
                      "https://api.mainnet-beta.solana.com")
        else:
            print("  FROM THE POOL TABLE, WHICH IS CENSORED. Tokens whose curve")
            print("  phase we missed and tokens that migrated after we stopped")
            print("  watching are both invisible to it, so the rate below is a")
            print("  floor, not an estimate. Fix it with:")
            print("    python label_graduation.py --rpc "
                  "https://api.mainnet-beta.solana.com")
            print("\n  Meanwhile: graduation is a transition within ONE token,")
            print("  launched on the pump.fun curve and later on another venue.")
            print("  pumpswap is pump.fun's own AMM, so arriving there IS")
            print("  leaving the curve. Meteora DBC and Bags are separate")
            print("  launchpads, never on a pump.fun curve to graduate off.")

        print("\n  The dex column, for reference:")
        for label, count in dex_counts.most_common(10):
            role = "the curve" if _is_pumpfun_curve(label) else "off-curve venue"
            print(f"    {count:>6}  {label:<14} {role}")
        if not dex_counts:
            print("    no pools recorded at all")

        tokens = [t for t in all_tokens if t.id in eligible]
        n = len(tokens)
        hits = sum(1 for t in tokens if t.id in graduated)
        print("\n" + "=" * 70)
        print("BASE RATE")
        print("=" * 70)
        print(f"  {len(all_tokens)} tokens collected")
        print(f"  {n} are pump.fun launches, so only these could graduate")
        print(f"  {len(all_tokens) - n} are not, or are not labelled yet, "
              f"and are excluded")
        if not n:
            print("\n  Nothing eligible. No test to run.")
            return 0
        low, high = wilson_interval(hits, n)
        print(f"\n  {hits} of {n} eligible tokens graduated ({hits / n * 100:.2f}%)")
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
        confounded = []
        for field, label in EARLY_FEATURES:
            rows = buckets.get(field, [])
            sub_hits = sum(1 for _, graduated_flag in rows if graduated_flag)
            coverage = len(rows) / n * 100 if n else 0.0
            print(f"\n  {label}  (recorded in time for {len(rows)} of {n} "
                  f"eligible, {coverage:.0f}%)")
            if len(rows) < 3:
                print("    too few to bucket")
                continue

            # Checked BEFORE the terciles, because a field whose presence
            # predicts the outcome makes every bucket of it uninterpretable.
            if missingness_confound(sub_hits, len(rows), hits, n):
                sub_rate = sub_hits / len(rows) * 100
                confounded.append(label)
                print(f"    CONFOUNDED BY MISSINGNESS. {sub_rate:.1f}% of the "
                      f"tokens that have")
                print(f"    this field graduated, against a {hits / n * 100:.1f}% "
                      f"base rate. Whether we")
                print("    recorded it is a stronger signal than any value it")
                print("    takes, so terciles of it measure our own coverage.")
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
                if field == "market_cap_usd":
                    # Graduation happens at a market cap threshold (~$69k on
                    # pump.fun), so a token already high at 300s is partway
                    # there by definition. The separation is real and
                    # observable, but it is momentum, not foresight.
                    print("    CAUTION: graduation IS crossing a market-cap")
                    print("    threshold, so a token already high at 300s is")
                    print("    partway there by definition. This is momentum")
                    print("    measured early, not a hidden property -- and")
                    print("    the oracle run already showed the exits are")
                    print("    where this strategy dies, not the entries.")
            else:
                print("    no separation: the intervals overlap, so the gap is")
                print("    within what this sample can produce by chance")

        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)
        if confounded:
            print(f"  {len(confounded)} feature(s) unusable: "
                  f"{', '.join(confounded)}.")
            print("  Their presence predicts graduation better than their")
            print("  value does, which is a fact about our collection, not")
            print("  about the market.\n")
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
