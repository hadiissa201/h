"""Is the early-buys signal tradable, or only real? Read-only.

graduated.py found that tokens in the top third of 5-minute buy counts
graduate at 6.49% against a 2.68% base rate. That is a genuine 2.4x lift and
it is not yet a strategy, for three reasons this script tests directly.

ONE SIGNAL, COUNTED THREE TIMES. 5m buys, 5m volume and market cap at 300s
all measure "this token is already moving". Reporting three separations makes
the finding look three times as confirmed as it is, so the entry rule here
uses buys alone.

THE THRESHOLD WAS CHOSEN ON THE SAME DATA IT WAS TESTED ON. Terciles cut
where the data happens to split, which flatters itself. Here the cut comes
from tokens detected in the EARLIER half and is evaluated on the later half,
which the rule has never seen.

A LIFT IS NOT AN EDGE. At 6.49% wins, break-even needs +1,109% on every
winner, because the measured median realised loss is -77%. Graduation runs a
curve from roughly $5k to $69k, about 14x, but only for a buyer who entered
at the start -- and this rule enters after 300 seconds of buying has already
moved the price. The replay settles it in money rather than in rates, using
the same sellability model as every other result here.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import UTC, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, Token
from collector.verify import wilson_interval
from graduated import chain_labels
from replay import replay_token

# The measured median realised loss on a closed memecoin position. Not an
# assumption: 227 closes, stop set at -50%, median outcome -77%.
MEDIAN_REALISED_LOSS = 0.77


def early_buys(session: Session, token: Token,
               window_s: float) -> float | None:
    """First 5m buy count observed within the window, or None."""
    start = token.detected_ts
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    cutoff = start + timedelta(seconds=window_s)
    rows = session.scalars(
        select(Observation)
        .where(Observation.token_id == token.id,
               Observation.buys_5m.is_not(None))
        .order_by(Observation.observed_ts.asc())).all()
    for row in rows:
        observed = row.observed_ts
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if observed > cutoff:
            return None
        return float(row.buys_5m)
    return None


def breakeven_payoff(win_rate: float,
                     loss: float = MEDIAN_REALISED_LOSS) -> float | None:
    """Gain per winner needed to break even at this win rate.

    The arithmetic a lift cannot escape: losers give back `loss` of the stake,
    so the winners have to cover all of them.
    """
    if win_rate <= 0:
        return None
    return (1 - win_rate) * loss / win_rate


def split_rate(tokens, feature: dict, graduated: set[int],
               threshold: float) -> dict:
    """Graduation above and below a threshold, with intervals."""
    above = [t for t in tokens if feature.get(t.id, -1) >= threshold]
    below = [t for t in tokens if 0 <= feature.get(t.id, -1) < threshold]
    out = {}
    for name, group in (("above", above), ("below", below)):
        hits = sum(1 for t in group if t.id in graduated)
        low, high = wilson_interval(hits, len(group)) if group else (0.0, 0.0)
        out[name] = {"n": len(group), "hits": hits,
                     "rate": hits / len(group) if group else 0.0,
                     "low": low, "high": high}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-window", type=float, default=300.0)
    parser.add_argument("--strategy", default="stop_10",
                        help="the least-bad exit rule found so far")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        graduated, eligible, labelled = chain_labels(session)
        if not eligible:
            print("No chain labels. Run label_graduation.py first.")
            return 1

        tokens = [t for t in session.scalars(
            select(Token).order_by(Token.detected_ts)).all()
            if t.id in eligible]
        feature = {}
        for token in tokens:
            value = early_buys(session, token, args.entry_window)
            if value is not None:
                feature[token.id] = value
        usable = [t for t in tokens if t.id in feature]

        # Split by time, not at random. A random split would let a token's
        # own market conditions leak across the boundary, and the question is
        # whether a rule fitted in the past works later.
        half = len(usable) // 2
        train, test = usable[:half], usable[half:]
        if not train or not test:
            print("Not enough tokens with an early buy count to split.")
            return 1

        values = sorted(feature[t.id] for t in train)
        threshold = values[int(len(values) * 2 / 3)]
        print("=" * 70)
        print("THE RULE, FITTED ON THE EARLIER HALF ONLY")
        print("=" * 70)
        print(f"  {len(usable)} eligible tokens have a 5m buy count inside "
              f"{args.entry_window:.0f}s")
        print(f"  train: {len(train)} tokens, detected "
              f"{train[0].detected_ts:%Y-%m-%d} to {train[-1].detected_ts:%Y-%m-%d}")
        print(f"  test:  {len(test)} tokens, detected "
              f"{test[0].detected_ts:%Y-%m-%d} to {test[-1].detected_ts:%Y-%m-%d}")
        print(f"\n  rule: enter when the first 5m buy count >= {threshold:.0f}")
        print("  (the top-third cut of the TRAIN half, never of the test half)")

        print("\n" + "=" * 70)
        print("DOES IT HOLD OUT OF SAMPLE?")
        print("=" * 70)
        for name, group in (("TRAIN (fitted here)", train),
                            ("TEST (never seen)", test)):
            rates = split_rate(group, feature, graduated, threshold)
            print(f"\n  {name}")
            for side in ("above", "below"):
                row = rates[side]
                print(f"    {side:>5} threshold  n={row['n']:<5} "
                      f"graduated {row['hits']:>3}  {row['rate'] * 100:>6.2f}%  "
                      f"[{row['low'] * 100:.2f}%, {row['high'] * 100:.2f}%]")
            a, b = rates["above"], rates["below"]
            held = a["low"] > b["high"]
            print(f"    {'SEPARATES' if held else 'no separation'}: "
                  f"the intervals {'do not ' if held else ''}overlap")
            if name.startswith("TEST") and not held:
                print("    The lift did not survive the split. A threshold")
                print("    chosen on one period and failing on the next is")
                print("    the definition of a fitted artefact.")

        print("\n" + "=" * 70)
        print("WHAT THE RULE WOULD HAVE TO PAY")
        print("=" * 70)
        rates = split_rate(test, feature, graduated, threshold)
        win = rates["above"]["rate"]
        need = breakeven_payoff(win)
        print(f"  out-of-sample win rate above the threshold: {win * 100:.2f}%")
        if need is None:
            print("  No winners out of sample, so no payoff can rescue it.")
        else:
            print(f"  median realised loss on a closed position: "
                  f"-{MEDIAN_REALISED_LOSS * 100:.0f}%  (measured, 227 closes)")
            print(f"\n  break-even needs +{need * 100:,.0f}% on every winner")
            print("\n  Graduation carries a curve from roughly $5k to $69k, about")
            print("  14x, so +1,300% -- but only for a buyer who entered at the")
            print("  start. This rule waits 300 seconds for the buying it keys")
            print("  on, and that buying is what moved the price. Whatever is")
            print("  left of the 14x after the entry is the actual payoff.")

        print("\n" + "=" * 70)
        print("THE REPLAY, IN MONEY RATHER THAN RATES")
        print("=" * 70)
        from collector.paper import ALL_STRATEGIES
        strategy = next((s for s in ALL_STRATEGIES
                         if s.name == args.strategy), None)
        if strategy is None:
            print(f"  unknown strategy {args.strategy}")
            return 2
        for name, group in (
                ("all eligible tokens", test),
                (f"only buys >= {threshold:.0f}",
                 [t for t in test if feature.get(t.id, -1) >= threshold])):
            gates: dict[str, int] = defaultdict(int)
            nets, floored = [], 0
            for token in group:
                position, entered = replay_token(session, token, strategy,
                                                 settings, gates)
                if not entered or position is None or position.exit_ts is None:
                    continue
                nets.append(position.net)
                floored += 1 if position.floored else 0
            print(f"\n  {name}: {len(nets)} closed positions")
            if not nets:
                print("    none closed, so there is nothing to compare")
                continue
            mean = sum(nets) / len(nets)
            print(f"    mean net   {mean * 100:>8.2f}%")
            print(f"    median net {statistics.median(nets) * 100:>8.2f}%")
            print(f"    winners    {sum(1 for x in nets if x > 0):>8} "
                  f"of {len(nets)}")
            if floored:
                print(f"    {floored} hit the -100% floor, so the mean is a "
                      f"floor too, not a return")
    return 0


if __name__ == "__main__":
    sys.exit(main())
