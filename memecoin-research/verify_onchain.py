"""Check the on-chain reader against DexScreener before trusting it. Read-only.

Two questions, and the second is the point of the whole exercise.

IS THE LAYOUT RIGHT? Checked two ways, because the first run of this script
found no token with both a DexScreener price and a readable curve and so proved
nothing. The primary check is now internal: pump.fun seeds every curve with the
same fictional reserves, so virtual_sol - real_sol is one constant on every
live token, and a field read at the wrong offset cannot hold constant across
tokens that differ in every other way. Where DexScreener does have a price, it
is used as a second, independent confirmation.

HOW MUCH MORE CAN WE SEE? The oracle found 1,282 tokens with no liquidity
figure at all. If the chain gives a price for most of those, coverage goes from
7.6% of detected tokens to nearly all of them -- and every measurement this
project has made so far was taken on the indexed minority.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.pacing import Pacer
from collector.models import Observation, Token
from collector.onchain import (
    MIN_SELLABLE_SOL,
    WSOL_DECIMALS,
    anomalies,
    traded_curves,
    _redact,
    curve_address,
    find_bonding_curve,
    layout_evidence,
    read_curve,
    sol_price_usd,
)


def report(curves, agreed, disagreed, unreadable,
           chain_only_funded, chain_only_empty, migrated=0) -> int:
    """Print the conclusions. One function owns the verdict.

    The previous version printed a verdict inside the DexScreener section and
    another at the end, which could disagree -- and separately printed "the
    chain shows more tokens, which is the whole point" on a sample of three
    tokens holding no SOL at all. Both are the same failure: a conclusion
    drawn wider than the evidence under it.
    """
    total = len(agreed) + len(disagreed)

    print("\n" + "=" * 70)
    print("DOES THE LAYOUT READ CORRECTLY? (from the curves themselves)")
    print("=" * 70)
    if curves:
        print("  raw fields, as parsed:")
        for c in curves[:6]:
            print(f"    vSOL {c.virtual_sol_reserves / 10 ** WSOL_DECIMALS:>11,.4f}"
                  f"  rSOL {c.real_sol_reserves / 10 ** WSOL_DECIMALS:>9,.4f}"
                  f"  vTok {c.virtual_token_reserves / 10 ** 6:>15,.0f}"
                  f"  rTok {c.real_token_reserves / 10 ** 6:>15,.0f}"
                  f"  supply {c.token_total_supply / 10 ** 6:>13,.0f}"
                  f"  {c.raw_len}B"
                  f"  complete {c.complete}")
        print()
    evidence = layout_evidence(curves)
    for check in evidence:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}")
        print(f"         {check.detail}")
    internally_sound = bool(curves) and all(c.passed for c in evidence)

    odd = anomalies(curves)
    if odd:
        print("\n  ANOMALIES (open questions about those tokens, not about")
        print("  the parse -- the checks above already settled that):")
        for line in odd[:5]:
            print(f"    {line}")

    print("\n" + "=" * 70)
    print("DOES IT AGREE WITH DEXSCREENER? (independent second source)")
    print("=" * 70)
    if not total:
        print("  Unavailable: no token had both a DexScreener price and a")
        print("  readable curve. It needs a token the aggregator indexed AND")
        print("  whose curve has not graduated, which is rare in a small recent")
        print("  sample because indexing lags the launch. Absence of this check")
        print("  is not a failure of the layout, and not a pass either.")
    else:
        print(f"  {len(agreed)} of {total} within 25% of DexScreener "
              f"({len(agreed) / total * 100:.0f}%)")
        if disagreed:
            ratios = sorted(r[3] for r in disagreed)
            print(f"  {len(disagreed)} disagreed, ratios {ratios[0]:.2f} "
                  f"to {ratios[-1]:.2f}")
            # A consistent multiple is a decimals or field-order error; scatter
            # is staleness in one source or the other.
            if max(ratios) / max(min(ratios), 1e-9) < 1.5:
                print("  They cluster around one multiple, which points at a")
                print("  decimals or field-order error rather than stale data.")

    print("\n" + "=" * 70)
    print("HOW MUCH MORE CAN WE SEE?")
    print("=" * 70)
    chain_only = chain_only_funded + chain_only_empty
    readable = total + chain_only
    # The graduated curves were read successfully, so leaving them out of the
    # denominator understated the sample and made the ratio flattering: it
    # printed "5 of 29" for a run that had looked at 40 tokens.
    sampled = readable + migrated + len(unreadable)
    print(f"  readable on chain              {readable:>4} of {sampled}")
    print(f"  of which DexScreener had too   {total:>4}")
    print(f"  CHAIN ONLY, over {MIN_SELLABLE_SOL} SOL     {chain_only_funded:>4}  "
          f"<- genuinely new coverage")
    print(f"  CHAIN ONLY, dust or empty      {chain_only_empty:>4}  "
          f"<- visible, but nothing to sell into")
    if migrated:
        print(f"  curve exists but is zeroed     {migrated:>4}  "
              f"<- pump.fun tokens that LEFT the curve")
        print("\n  A zeroed curve is a graduated token: it is a pump.fun")
        print("  launch, so this reader found it, but its price now lives in")
        print("  an AMM pool. Reading those needs a second reader, and they")
        print("  are the survivors, so they are worth having.")
    if unreadable:
        print(f"\n  {len(unreadable)} could not be read on chain either:")
        for reason, n in Counter(r for _, r in unreadable).most_common(4):
            print(f"    {n:>4}  {reason[:200]}")
        if any("does not exist" in r for _, r in unreadable):
            print("\n  'account does not exist' at the derived address means")
            print("  the token is not a pump.fun launch -- a Raydium pool, or")
            print("  another launchpad. Those need a different reader.")

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    if not internally_sound:
        print("  LAYOUT NOT CONFIRMED. Do not wire this into the collector.")
    elif total and len(agreed) < total * 0.8:
        print("  LAYOUT NOT CONFIRMED. The invariants hold but DexScreener")
        print("  disagrees, and two sources disagreeing is unresolved, not a")
        print("  pass. Find out which is wrong first.")
    else:
        print("  LAYOUT CONFIRMED against pump.fun's documented launch state"
              + (" and by DexScreener." if total else ","))
        if not total:
            print("  with no second source.")
        print("\n  Two things that does NOT establish.")
        # Derived from the same predicate the seed check uses. These two
        # sections used to decide it separately and printed a passing check
        # about "1 traded curves" above a verdict saying none existed.
        with_sol = traded_curves(curves)
        print("\n  One: that real_sol_reserves reads correctly in anger. Its")
        print("  position is pinned by the launch state, but only "
              f"{len(with_sol)} of the")
        print(f"  {len(curves)} curves read held more than {MIN_SELLABLE_SOL} "
              f"SOL, so the field that caps")
        print("  a real exit has barely been observed carrying anything.")
        print("\n  Two: that the coverage gain is tradable. A curve we can")
        print(f"  read is not a curve we could sell into. Of the {chain_only} tokens")
        print(f"  only the chain sees, {chain_only_funded} hold more than "
              f"{MIN_SELLABLE_SOL} SOL. The rest")
        print("  become visible but stay unsellable, so they cannot rescue a")
        print("  return -- they can only make the loss rate honest, which is")
        print("  still worth having.")
    return 0


def pick_tokens(session, limit: int, hours: float | None) -> list:
    """Choose which tokens to read, in two halves and for two reasons.

    Ordered by the most liquidity we ever recorded, not by how recently the
    token was detected. Recency was the wrong key twice over: the collector is
    stopped, so "recent" means days old, and a token detected recently is most
    often one nobody bought. Peak liquidity selects for curves that took real
    SOL, and an abandoned curve keeps whatever its holders never sold -- which
    is the differing real_sol the seed invariant needs and has never had.

    The second half is tokens DexScreener never priced. Those answer the
    coverage question and must stay unfiltered, since they are the population
    the whole exercise is about.
    """
    window = []
    if hours:
        window.append(Token.detected_ts
                      >= datetime.now(UTC) - timedelta(hours=hours))

    half = max(1, limit // 2)
    peak = (select(Observation.token_id.label("token_id"),
                   func.max(Observation.liquidity_usd).label("peak"))
            .where(Observation.liquidity_usd.is_not(None))
            .group_by(Observation.token_id).subquery())
    traded = session.scalars(
        select(Token).join(peak, peak.c.token_id == Token.id)
        .where(*window).order_by(peak.c.peak.desc()).limit(half)).all()

    priced_ids = select(Observation.token_id).where(
        Observation.price_usd.is_not(None)).distinct().scalar_subquery()
    unpriced = session.scalars(
        select(Token).where(*window, Token.id.not_in(priced_ids))
        .order_by(Token.detected_ts.desc())
        .limit(limit - len(traded))).all()
    print(f"sampling {len(traded)} tokens by peak recorded liquidity and "
          f"{len(unpriced)} DexScreener never priced\n")
    return list(traded) + list(unpriced)


def check_mints(mints: list[str], rpc_url: str) -> int:
    """Confirm the layout against mints named on the command line.

    Sampling our own database cannot answer this question right now. The
    collector is stopped, so the newest tokens it holds are days old, and a
    week-old pump.fun curve has either graduated or been drained -- real_sol is
    0 either way, which is exactly the state the invariant cannot be tested in.

    The distinction that took three failed runs to see: the COVERAGE question
    needs a sample with no selection bias, because it is a claim about the
    population. The LAYOUT question does not. It is a claim about a struct, so
    any five curves somebody has traded will settle it, however they were
    chosen. Insisting on an unbiased sample for both is what kept this
    unverified.
    """
    print(f"RPC endpoint: {_redact(rpc_url)}")
    print(f"reading {len(mints)} mints named on the command line\n")
    curves, unreadable = [], []
    pacer = Pacer()
    with httpx.Client() as client:
        for mint in mints:
            address, note = curve_address(mint)
            if address is None:
                unreadable.append((mint, note))
                continue
            pacer.wait()
            curve, detail = read_curve(client, rpc_url, address, pacer=pacer)
            if curve is None:
                unreadable.append((mint, detail))
                print(f"  {mint[:12]}... unreadable: {detail[:120]}")
                continue
            curves.append(curve)
            print(f"  {mint[:12]}... {curve.extractable_sol():>9,.4f} SOL "
                  f"extractable"
                  f"{'  [graduated]' if curve.complete else ''}")
    print(f"\n  pacing ended at {pacer.delay:.2f}s after {pacer.throttles} "
          f"rate limits")
    return report(curves, [], [], unreadable, 0, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--hours", type=float, default=None,
                        help="only consider tokens detected in the last N hours. "
                             "Default is all of history, because a recent "
                             "window of a stopped collector holds nothing")
    parser.add_argument("--rpc", default=None,
                        help="override the RPC endpoint. Use "
                             "https://api.mainnet-beta.solana.com when the "
                             "paid key is out of credits -- it is slower and "
                             "drops requests, but verification needs only a "
                             "few dozen cheap reads")
    parser.add_argument("--mints", default=None,
                        help="comma-separated mint addresses to check instead "
                             "of sampling the database. Use this for the "
                             "layout check: it needs curves somebody has "
                             "traded, which a stopped collector cannot "
                             "supply, and the layout question does not care "
                             "how the mints were chosen")
    args = parser.parse_args()

    settings = load_settings()
    rpc_url = args.rpc or settings.rpc_url
    if args.mints:
        mints = [m.strip() for m in args.mints.split(",") if m.strip()]
        if not mints:
            print("--mints was empty")
            return 1
        return check_mints(mints, rpc_url)

    engine = create_engine(settings.database_url, future=True)

    with Session(engine) as session, httpx.Client() as client:
        print(f"RPC endpoint: {_redact(rpc_url)}")
        sol_usd = sol_price_usd(client, settings.jupiter_quote_url)
        if sol_usd is None:
            print("Could not price SOL; dollar comparisons will be skipped.")
            print("If the RPC is also refusing, the key is likely out of "
                  "credits rather than\nbeing hit too fast -- pacing cannot "
                  "fix a spent budget.\n")
        else:
            print(f"SOL = ${sol_usd:,.2f}\n")

        tokens = pick_tokens(session, args.limit, args.hours)

        pacer = Pacer()
        agreed, disagreed, unreadable = [], [], []
        curves = []
        chain_only_funded, chain_only_empty, migrated = 0, 0, 0
        for token in tokens:
            obs = session.scalars(
                select(Observation)
                .where(Observation.token_id == token.id,
                       Observation.price_usd.is_not(None))
                .order_by(Observation.observed_ts.desc()).limit(1)).first()
            dex_price = (float(obs.price_usd)
                         if obs and obs.price_usd and float(obs.price_usd) > 0 else None)

            # Derived locally: no RPC call, so one token costs one request.
            address, note = curve_address(token.address)
            if address is None:
                unreadable.append((token.address, note))
                continue
            pacer.wait()
            curve, detail = read_curve(client, rpc_url, address,
                                       pacer=pacer)
            if curve is None and "does not exist" in detail:
                # Not at the derived address -- graduated, or not a pump.fun
                # curve. Fall back to the expensive lookup for just these.
                pacer.wait()
                fallback, note = find_bonding_curve(client, rpc_url,
                                                    token.address, pacer=pacer)
                if fallback:
                    pacer.wait()
                    curve, detail = read_curve(client, rpc_url,
                                               fallback, pacer=pacer)
            if curve is None:
                unreadable.append((token.address, detail))
                continue

            decimals = token.decimals if token.decimals is not None else 6
            price_sol = curve.price_sol(decimals)
            if price_sol is None:
                # The account parsed but its reserves are zeroed, which is what
                # graduation leaves behind. Lumping this in with "unreadable"
                # discarded the single most useful group in the sample: the
                # tokens that survived long enough to leave the curve.
                migrated += 1
                continue
            chain_usd = price_sol * sol_usd if sol_usd else None

            curves.append(curve)
            if dex_price is None:
                if curve.extractable_sol() >= MIN_SELLABLE_SOL:
                    chain_only_funded += 1
                else:
                    chain_only_empty += 1
                extra = (f"  ${chain_usd:.3e}" if chain_usd else "")
                print(f"  {token.address[:12]}... CHAIN ONLY{extra}  "
                      f"{curve.extractable_sol():.4f} SOL extractable"
                      f"{'  [graduated]' if curve.complete else ''}")
                continue

            if chain_usd is None:
                continue
            ratio = chain_usd / dex_price
            row = (token.address, dex_price, chain_usd, ratio,
                   curve.extractable_sol(), curve.complete)
            (agreed if 0.8 <= ratio <= 1.25 else disagreed).append(row)
            flag = "ok" if 0.8 <= ratio <= 1.25 else "MISMATCH"
            print(f"  {token.address[:12]}... dex {dex_price:.3e}  "
                  f"chain {chain_usd:.3e}  ratio {ratio:6.2f}  {flag}"
                  f"{'  [graduated]' if curve.complete else ''}")

    print(f"\n  pacing ended at {pacer.delay:.2f}s after {pacer.throttles} "
          f"rate limits")
    return report(curves, agreed, disagreed, unreadable,
                  chain_only_funded, chain_only_empty, migrated)


    return 0


if __name__ == "__main__":
    sys.exit(main())
