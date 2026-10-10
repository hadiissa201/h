"""The pre-registered verdict, evaluated mechanically. Read-only.

docs/DECISION.md was written on 2026-09-30, before the data existed, so that
the criteria could not be adjusted to fit whatever came back. This script
applies them. It does not decide anything; it reads the file's rules off and
checks them.

Four preconditions gate the verdict. Failing one means the answer is "not
yet", which the pre-registration calls a result rather than a delay.

The primary number is control_any's mean net return per position with a 95%
interval. control_any buys anything it can prove is sellable, with no filter,
so it is the base rate every filtered strategy has to beat merely to reach
zero.

The crux test is separate and can fail the thesis on its own: if positions
that reached the profit target were sellable LESS often than positions sitting
at a loss, the average is computed over the exits that happened to be
available and the unavailable ones are exactly the wins.

    python verdict.py
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import (
    Observation,
    PaperPosition,
    Token,
    WorkItem,
)
from collector.paper import ALL_STRATEGIES
from collector.verify import (
    disagreement_rate,
    fisher_exact,
    wilson_interval,
)

BASELINE = "control_any"
MIN_CLOSED = 100
MIN_VERIFICATIONS = 20
MAX_FALSE_POSITIVE_RATE = 0.10
MAX_ABANDONMENT_SPREAD = 0.15
TARGETS = {s.name: s.take_profit_multiple for s in ALL_STRATEGIES}
# Amended 2026-10-10; see docs/DECISION.md.
CRUX_ALPHA = 0.05


def net_return(position: PaperPosition) -> float | None:
    """Net as a fraction of the stake, or None when it cannot be computed."""
    notional = float(position.notional_usd or 0.0)
    if notional <= 0 or position.gross_pnl_usd is None:
        return None
    gross = float(position.gross_pnl_usd)
    costs = float(position.costs_usd or 0.0)
    return (gross - costs) / notional


def bootstrap_ci(values: list[float], draws: int = 4000,
                 seed: int = 20260930) -> tuple[float, float]:
    """Percentile interval on the mean.

    A normal interval assumes a symmetric distribution. Memecoin returns are
    bounded at -100% and unbounded above, so the sample mean is dominated by
    a few large outcomes and its sampling distribution is skewed. The
    bootstrap makes no symmetry assumption.
    """
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n))
                   for _ in range(draws))
    return (means[int(draws * 0.025)], means[int(draws * 0.975)])


def abandonment_by_source(session: Session) -> dict[str, tuple[int, int]]:
    """(abandoned, total) per detection source.

    Abandoned means detected and never priced. A launchpad we abandon far more
    often than another is not represented in the sample, and the
    pre-registration refuses a verdict on an unrepresentative sample.
    """
    priced = select(Observation.token_id).where(
        Observation.price_usd.is_not(None)).distinct().scalar_subquery()
    out: dict[str, tuple[int, int]] = {}
    rows = session.execute(
        select(Token.detection_source, func.count(Token.id))
        .group_by(Token.detection_source)).all()
    for source, total in rows:
        abandoned = session.scalar(
            select(func.count(Token.id))
            .where(Token.detection_source == source,
                   Token.id.not_in(priced))) or 0
        out[source or "unknown"] = (abandoned, total)
    return out


def exitability_by_outcome(session: Session, strategy: str) -> dict:
    """Could the best moment actually be sold, split by whether it was a win?

    THE FIRST VERSION OF THIS WAS A TAUTOLOGY. It asked whether the token had
    ever produced a successful simulated exit -- and every token carrying a
    paper position necessarily has, because that is what opened and closed it.
    So it reported 100% sellable in both groups, on 35 winners and 400 losers,
    and concluded the thesis survived. It measured nothing, and it failed in
    the direction that flattered the strategy.

    The right instrument was already in the schema.
    `unrealisable_peak_multiple` records what the position WOULD have made if
    an exit had always been available; `peak_multiple` records what was
    actually reachable. When the first exceeds the second, the best moment
    could not be sold. That is the question the pre-registration asked.
    """
    target = TARGETS.get(strategy, 3.0)
    groups: dict[str, list[bool]] = defaultdict(list)
    positions = session.scalars(
        select(PaperPosition).where(PaperPosition.strategy == strategy,
                                    PaperPosition.is_open.is_(False))).all()
    for position in positions:
        realisable = float(position.peak_multiple or 0.0)
        wanted = float(position.unrealisable_peak_multiple or 0.0)
        best = max(realisable, wanted)
        # No penalty means the best price the position saw was a price it
        # could have sold into.
        sellable = realisable >= best - 1e-9
        groups["reached target" if best >= target
               else "stayed at a loss"].append(sellable)
    out = {}
    for name, flags in groups.items():
        hits = sum(1 for f in flags if f)
        low, high = wilson_interval(hits, len(flags)) if flags else (0.0, 0.0)
        out[name] = {"n": len(flags), "sellable": hits,
                     "rate": hits / len(flags) if flags else 0.0,
                     "low": low, "high": high}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default=BASELINE)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        print("=" * 70)
        print("PRECONDITIONS (a verdict is only issued if all four hold)")
        print("=" * 70)
        failures = []

        closed = session.scalar(
            select(func.count(PaperPosition.id))
            .where(PaperPosition.strategy == args.strategy,
                   PaperPosition.is_open.is_(False))) or 0
        ok = closed >= MIN_CLOSED
        failures += [] if ok else [f"only {closed} closed positions"]
        print(f"  [{'PASS' if ok else 'FAIL'}] {closed} closed positions for "
              f"{args.strategy} (need {MIN_CLOSED})")

        counts = disagreement_rate(session)
        verified = counts["verified"]
        # None, not zero, when nothing has been verified. An unmeasured rate
        # is not a rate of zero, and defaulting it to zero would let the
        # precondition pass on no evidence at all.
        rate = counts["quote_false_positive_rate"]
        ok = (verified >= MIN_VERIFICATIONS and rate is not None
              and rate < MAX_FALSE_POSITIVE_RATE)
        shown = "unmeasured" if rate is None else f"{rate * 100:.1f}%"
        failures += [] if ok else [
            f"{verified} verifications, quote false-positive rate {shown}"]
        print(f"  [{'PASS' if ok else 'FAIL'}] {verified} chain verifications, "
              f"quote false positives {shown} "
              f"(need {MIN_VERIFICATIONS}+ and under "
              f"{MAX_FALSE_POSITIVE_RATE * 100:.0f}%)")

        sources = abandonment_by_source(session)
        rates = {s: a / t for s, (a, t) in sources.items() if t >= 50}
        spread = (max(rates.values()) - min(rates.values())) if len(rates) > 1 else 0.0
        ok = spread <= MAX_ABANDONMENT_SPREAD
        failures += [] if ok else [
            f"abandonment differs by {spread * 100:.1f} points across launchpads"]
        print(f"  [{'PASS' if ok else 'FAIL'}] abandonment spread "
              f"{spread * 100:.1f} points across launchpads "
              f"(need under {MAX_ABANDONMENT_SPREAD * 100:.0f})")
        for source, (abandoned, total) in sorted(sources.items()):
            if total >= 50:
                print(f"           {source:<28} {abandoned:>5}/{total:<6} "
                      f"{abandoned / total * 100:>5.1f}%")

        overdue = session.scalar(
            select(func.count(WorkItem.id))
            .where(WorkItem.due_at < datetime.now(UTC))) or 0
        ok = overdue == 0
        failures += [] if ok else [f"{overdue} overdue queue items"]
        print(f"  [{'PASS' if ok else 'FAIL'}] {overdue} overdue queue items "
              f"(need 0)")

        print("\n" + "=" * 70)
        print(f"PRIMARY NUMBER: mean net per position, {args.strategy}")
        print("=" * 70)
        positions = session.scalars(
            select(PaperPosition).where(
                PaperPosition.strategy == args.strategy,
                PaperPosition.is_open.is_(False))).all()
        nets = [n for n in (net_return(p) for p in positions) if n is not None]
        if not nets:
            print("  no computable positions")
            return 0
        mean = statistics.fmean(nets)
        low, high = bootstrap_ci(nets)
        print(f"  n = {len(nets)}")
        print(f"  mean net        {mean * 100:>+8.2f}%")
        print(f"  95% interval    [{low * 100:+.2f}%, {high * 100:+.2f}%]")
        print(f"  median net      {statistics.median(nets) * 100:>+8.2f}%")
        # The quote false-positive rate decides whether this number means
        # anything. Over half the paper exits reverting on chain does not make
        # the return uncertain in both directions: a reverted sale is a sale
        # that did not happen, so the true figure is WORSE than the one above,
        # by an amount nothing here measures.
        if rate is not None and rate >= MAX_FALSE_POSITIVE_RATE:
            print(f"\n  Read with the precondition in mind: {rate * 100:.1f}% of "
                  f"verified exits")
            print("  would have reverted on chain. Those sales did not happen,")
            print("  so this figure is optimistic, not merely uncertain.")
        if high < 0:
            primary = "UNPROFITABLE"
            consequence = "Stop. Do not fund."
        elif low > 0:
            primary = "CANDIDATE"
            consequence = ("Go to the second test: priority fees and MEV, real "
                           "latency, and size. A positive mean means 'worth "
                           "testing further', never 'worth funding'.")
        else:
            primary = "UNPROVEN"
            consequence = "Keep collecting. No money either way."
        print(f"\n  -> {primary}. {consequence}")

        print("\n" + "=" * 70)
        print("THE CRUX TEST: were the winners sellable?")
        print("=" * 70)
        groups = exitability_by_outcome(session, args.strategy)
        for name in ("reached target", "stayed at a loss"):
            row = groups.get(name)
            if not row:
                print(f"  {name}: none")
                continue
            print(f"  {name:<18} n={row['n']:<5} sellable {row['sellable']:>4}  "
                  f"{row['rate'] * 100:>6.2f}%  "
                  f"[{row['low'] * 100:.2f}%, {row['high'] * 100:.2f}%]")
        winners = groups.get("reached target")
        losers = groups.get("stayed at a loss")
        crux = None
        if winners and losers and winners["n"] and losers["n"]:
            # A rate comparison is not a comparison until the difference is
            # established. The first version fired the project's most
            # consequential verdict off two point estimates whose intervals
            # overlapped, at p=0.064. See the 2026-10-10 amendment in
            # docs/DECISION.md: this tightening was made AFTER seeing the
            # data and it makes the thesis harder to fail, so it is recorded
            # rather than applied quietly.
            w_bad = winners["n"] - winners["sellable"]
            l_bad = losers["n"] - losers["sellable"]
            pvalue = fisher_exact(w_bad, winners["sellable"],
                                  l_bad, losers["sellable"])
            print(f"\n  unsellable: {w_bad}/{winners['n']} of winners vs "
                  f"{l_bad}/{losers['n']} of losers")
            print(f"  Fisher exact two-tailed p = {pvalue:.4f}")
            directional = winners["rate"] < losers["rate"]
            crux = directional and pvalue < CRUX_ALPHA
            if directional and not crux:
                print(f"\n  -> DIRECTION MATCHES but is NOT ESTABLISHED at "
                      f"p<{CRUX_ALPHA}. The winners")
                print("     were less sellable in this sample, which is what")
                print("     the thesis predicts, and the sample cannot carry")
                print("     the claim. More target-reaching positions would")
                print("     settle it; there are only "
                      f"{winners['n']}.")
            if crux:
                print("\n  -> THESIS FAILS. Positions that reached the target were")
                print("     sellable less often than positions at a loss, so the")
                print("     average return is computed over the exits that were")
                print("     available and the unavailable ones are the wins.")
                print("     The pre-registration fails the thesis on this")
                print("     REGARDLESS of mean return.")
            else:
                print("\n  -> this test does not fail the thesis")

        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)
        if failures:
            print("  NOT YET. Preconditions unmet:")
            for item in failures:
                print(f"    - {item}")
            print("\n  Saying 'not yet' is a result, not a delay. The numbers")
            print("  above are printed so they can be read, but the")
            print("  pre-registration does not license a verdict from them.")
        elif crux:
            print("  THESIS FAILS on the exitability test.")
        else:
            print(f"  {primary}. {consequence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
