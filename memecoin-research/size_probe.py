"""Does a smaller order escape the exit cost? Live, read-only, no wallet.

The replay measured a median EXIT price impact of 96.4% on $100 orders, against
7.7% on entry. If that is a property of $100 rather than of the pools, a smaller
order changes everything; if the impact is just as bad at $10, these tokens
cannot be exited at any size and the question is closed.

Method: for each tracked token, ask Jupiter for a sell quote at several sizes in
immediate succession, so all sizes see the same pool at the same moment. The
comparison is therefore paired -- the usual objection, that the big order was
quoted during a worse minute, cannot apply.

Every size is computed from the CURRENT price, which was the subject of a real
bug: sizing from the detection price quoted a completely different trade and
produced impossible impacts.

Nothing here can move funds. It requests quotes, which is a public read.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, Token
from poc.sources import parse_jupiter_quote, says_no_route
from probe.constants import WSOL_MINT

SIZES = (100.0, 50.0, 20.0, 10.0, 5.0)
# Jupiter rejects amounts outside a sane range with a 400, and a 400 is not a
# statement about liquidity.
MIN_AMOUNT, MAX_AMOUNT = 1_000, 10 ** 18


@dataclass
class Quote:
    token_id: int
    address: str
    notional: float
    impact: float | None
    routed: bool | None
    detail: str = ""


def amount_for(notional: float, price: float, decimals: int) -> int | None:
    if price <= 0:
        return None
    raw = int((notional / price) * (10 ** decimals))
    return raw if MIN_AMOUNT <= raw <= MAX_AMOUNT else None


def quote_once(client: httpx.Client, url: str, mint: str, amount: int,
               notional: float, token_id: int, slippage_bps: int) -> Quote:
    try:
        resp = client.get(url, params={
            "inputMint": mint, "outputMint": WSOL_MINT,
            "amount": str(amount), "slippageBps": str(slippage_bps)}, timeout=20.0)
    except Exception as exc:
        return Quote(token_id, mint, notional, None, None, f"{type(exc).__name__}")

    if resp.status_code == 200:
        sim = parse_jupiter_quote(resp.text, notional)
        if sim.succeeded:
            impact = abs(float(sim.price_impact_pct or 0.0))
            return Quote(token_id, mint, notional, impact, True)
        return Quote(token_id, mint, notional, None, False, sim.failure_kind or "")
    if says_no_route(resp.text):
        return Quote(token_id, mint, notional, None, False, "no_route")
    # Our problem, not the market's: never counted as unsellable.
    return Quote(token_id, mint, notional, None, None, f"HTTP {resp.status_code}")


def summarise(quotes: list[Quote]) -> None:
    print("\n" + "=" * 74)
    print("EXIT COST BY ORDER SIZE -- same token, same moment, different size")
    print("=" * 74)
    by_size: dict[float, list[Quote]] = {size: [] for size in SIZES}
    for q in quotes:
        by_size.setdefault(q.notional, []).append(q)

    # "asked" is the only honest denominator: a quote we never got an answer
    # to is not a statement about the market, and dividing by all attempts
    # would let our own failures look like routing failures.
    print(f"  {'size':>8}{'tried':>9}{'answered':>9}{'routed':>9}"
          f"{'route %':>9}{'median impact':>16}{'no answer':>12}")
    for size in sorted(by_size, reverse=True):
        rows = by_size[size]
        asked = [q for q in rows if q.routed is not None]
        routed = [q for q in asked if q.routed]
        impacts = sorted(q.impact for q in routed if q.impact is not None)
        median = f"{impacts[len(impacts) // 2] * 100:.1f}%" if impacts else "-"
        unanswered = [q for q in rows if q.routed is None]
        share = f"{len(routed) / len(asked) * 100:.0f}%" if asked else "-"
        print(f"  ${size:>7,.0f}{len(rows):>9}{len(asked):>9}{len(routed):>9}"
              f"{share:>9}{median:>16}{len(unanswered):>12}")

    # The paired question: on tokens quoted at BOTH ends, does size help?
    big, small = max(SIZES), min(SIZES)
    at_big = {q.token_id: q.impact for q in by_size.get(big, []) if q.impact is not None}
    at_small = {q.token_id: q.impact for q in by_size.get(small, []) if q.impact is not None}
    shared = sorted(set(at_big) & set(at_small))
    print(f"\n  {len(shared)} tokens routed at BOTH ${big:,.0f} and ${small:,.0f}")
    if shared:
        diffs = [at_big[t] - at_small[t] for t in shared]
        diffs.sort()
        median_diff = diffs[len(diffs) // 2]
        helped = sum(1 for d in diffs if d > 0.05)
        print(f"    median impact at ${big:,.0f}: "
              f"{sorted(at_big[t] for t in shared)[len(shared) // 2] * 100:.1f}%")
        print(f"    median impact at ${small:,.0f}: "
              f"{sorted(at_small[t] for t in shared)[len(shared) // 2] * 100:.1f}%")
        print(f"    smaller order was cheaper by >5 points on {helped} of "
              f"{len(shared)} ({helped / len(shared) * 100:.0f}%)")
        big_median = sorted(at_big[t] for t in shared)[len(shared) // 2]
        if len(shared) < 20:
            print(f"\n  NO VERDICT: {len(shared)} paired tokens is far too few. "
                  f"Run longer\n  or widen --max-age-hours before reading "
                  f"anything into these numbers.")
        elif big_median < 0.10:
            # The level matters as much as the difference, and the first
            # version of this check ignored it entirely -- printing "cannot be
            # exited at any size" over a table showing 2.8% impact.
            print(f"\n  VERDICT: where a route EXISTS the exit is cheap "
                  f"({big_median * 100:.1f}% at\n  ${big:,.0f}), so impact is not "
                  f"the obstacle. Whether a route exists at all is.")
        elif median_diff < 0.05:
            print("\n  VERDICT: size is NOT the problem. The exit costs the same")
            print("  whether you are selling $100 or $5, so these pools cannot be")
            print("  exited at any size a retail position would use.")
        else:
            print("\n  VERDICT: size matters. A smaller position escapes a large")
            print("  part of the exit cost, so position sizing -- not the stop or")
            print("  the target -- is the lever worth pulling.")
    else:
        print("    not enough paired quotes to answer; run longer")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--max-age-hours", type=float, default=48.0,
                        help="only tokens seen recently enough to still trade")
    parser.add_argument("--out", default="size_probe.csv")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)
    deadline = time.monotonic() + args.minutes * 60
    quotes: list[Quote] = []
    # Jupiter's measured ceiling is ~120/min; stay well inside it.
    pause = 1.0 / 1.5

    with Session(engine) as session, httpx.Client() as client:
        cutoff = datetime.now(UTC) - timedelta(hours=args.max_age_hours)
        tokens = session.scalars(
            select(Token).where(Token.detected_ts >= cutoff)
            .order_by(Token.detected_ts.desc())).all()
        print(f"{len(tokens)} tokens detected in the last {args.max_age_hours:.0f}h")
        print(f"Quoting each at {', '.join(f'${s:,.0f}' for s in SIZES)} "
              f"for {args.minutes:.0f} minutes.\n")

        checked = 0
        for token in tokens:
            if time.monotonic() > deadline:
                break
            obs = session.scalars(
                select(Observation)
                .where(Observation.token_id == token.id,
                       Observation.price_usd.is_not(None))
                .order_by(Observation.observed_ts.desc()).limit(1)).first()
            if obs is None or not obs.price_usd or float(obs.price_usd) <= 0:
                continue
            price = float(obs.price_usd)
            decimals = token.decimals if token.decimals is not None else 6

            got = []
            for size in SIZES:
                if time.monotonic() > deadline:
                    break
                amount = amount_for(size, price, decimals)
                if amount is None:
                    continue
                got.append(quote_once(client, settings.jupiter_quote_url,
                                      token.address, amount, size, token.id,
                                      settings.exit_slippage_bps))
                time.sleep(pause)
            quotes.extend(got)
            checked += 1
            routed = [q for q in got if q.routed]
            refused = [q for q in got if q.routed is False]
            unanswered = [q for q in got if q.routed is None]
            if routed:
                worst = max(q.impact or 0 for q in routed)
                best = min(q.impact or 0 for q in routed)
                print(f"  [{checked}] {token.address[:12]}... "
                      f"{len(routed)}/{len(got)} routed, impact "
                      f"{best * 100:.1f}% at small -> {worst * 100:.1f}% at large")
            elif refused and not unanswered:
                # The market genuinely said no at every size.
                print(f"  [{checked}] {token.address[:12]}... NO ROUTE at any size")
            else:
                # Our request failed. Saying "no route" here would turn our own
                # outage into evidence about the token, which is the single
                # error this project exists to avoid.
                reasons = {q.detail for q in unanswered if q.detail}
                print(f"  [{checked}] {token.address[:12]}... NO ANSWER "
                      f"({len(unanswered)}/{len(got)} failed: "
                      f"{', '.join(sorted(reasons)[:3]) or 'unknown'})")

    if not quotes:
        print("\nNo quotes collected. Is the database populated and the network up?")
        return 1

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["token_id", "address", "notional_usd", "impact_pct",
                         "routed", "detail"])
        for q in quotes:
            writer.writerow([q.token_id, q.address, q.notional,
                             "" if q.impact is None else f"{q.impact:.6f}",
                             "" if q.routed is None else q.routed, q.detail])
    # Why we failed to get an answer decides whether this run means anything.
    from collections import Counter
    failures = Counter(q.detail for q in quotes if q.routed is None and q.detail)
    if failures:
        answered = sum(1 for q in quotes if q.routed is not None)
        print(f"\n  {sum(failures.values())} of {len(quotes)} quotes got NO "
              f"answer ({answered} answered). Reasons:")
        for reason, n in failures.most_common(6):
            print(f"    {n:>6}x  {reason}")
        if sum(failures.values()) > len(quotes) * 0.5:
            print("\n  More than half the quotes failed, so the table below rests")
            print("  on a small and possibly unrepresentative remainder.")

    summarise(quotes)
    print(f"\n  raw quotes written to {args.out}")
    print("  No wallet, no keys, no orders -- these are public price quotes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
