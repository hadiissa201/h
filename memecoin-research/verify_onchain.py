"""Check the on-chain reader against DexScreener before trusting it. Read-only.

Two questions, and the second is the point of the whole exercise.

IS THE LAYOUT RIGHT? For tokens where DexScreener gave us a price, the on-chain
price should match it closely. A systematic offset means the struct is being
read wrong; scattered disagreement means one of the two sources is stale. The
discriminator check already rules out reading a different account entirely, so
this is about the fields inside it.

HOW MUCH MORE CAN WE SEE? The oracle found 1,282 tokens with no liquidity
figure at all. If the chain gives a price for most of those, coverage goes from
7.6% of detected tokens to nearly all of them -- and every measurement this
project has made so far was taken on the indexed minority.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.pacing import Pacer
from collector.models import Observation, Token
from collector.onchain import (
    _redact,
    curve_address,
    find_bonding_curve,
    read_curve,
    sol_price_usd,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--hours", type=float, default=48.0)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)

    with Session(engine) as session, httpx.Client() as client:
        print(f"RPC endpoint: {_redact(settings.rpc_url)}")
        sol_usd = sol_price_usd(client, settings.jupiter_quote_url)
        if sol_usd is None:
            print("Could not price SOL; dollar comparisons will be skipped.\n")
        else:
            print(f"SOL = ${sol_usd:,.2f}\n")

        cutoff = datetime.now(UTC) - timedelta(hours=args.hours)
        tokens = session.scalars(
            select(Token).where(Token.detected_ts >= cutoff)
            .order_by(Token.detected_ts.desc()).limit(args.limit)).all()

        pacer = Pacer()
        agreed, disagreed, chain_only, unreadable = [], [], 0, []
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
            curve, detail = read_curve(client, settings.rpc_url, address,
                                       pacer=pacer)
            if curve is None and "does not exist" in detail:
                # Not at the derived address -- graduated, or not a pump.fun
                # curve. Fall back to the expensive lookup for just these.
                pacer.wait()
                fallback, note = find_bonding_curve(client, settings.rpc_url,
                                                    token.address, pacer=pacer)
                if fallback:
                    pacer.wait()
                    curve, detail = read_curve(client, settings.rpc_url,
                                               fallback, pacer=pacer)
            if curve is None:
                unreadable.append((token.address, detail))
                continue

            decimals = token.decimals if token.decimals is not None else 6
            price_sol = curve.price_sol(decimals)
            if price_sol is None:
                unreadable.append((token.address, "curve has no usable reserves"))
                continue
            chain_usd = price_sol * sol_usd if sol_usd else None

            if dex_price is None:
                chain_only += 1
                extra = (f"  ${chain_usd:.3e}" if chain_usd else "")
                print(f"  {token.address[:12]}... CHAIN ONLY{extra}  "
                      f"{curve.extractable_sol():.2f} SOL extractable"
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
    total = len(agreed) + len(disagreed)
    print("\n" + "=" * 70)
    print("DOES THE LAYOUT READ CORRECTLY?")
    print("=" * 70)
    if not total:
        print("  No token had both a DexScreener price and a readable curve.")
    else:
        print(f"  {len(agreed)} of {total} within 25% of DexScreener "
              f"({len(agreed) / total * 100:.0f}%)")
        if disagreed:
            ratios = sorted(r[3] for r in disagreed)
            print(f"  {len(disagreed)} disagreed, ratios {ratios[0]:.2f} "
                  f"to {ratios[-1]:.2f}")
            # A consistent multiple is a decimals or field-order error; scatter
            # is staleness in one source or the other.
            if ratios and max(ratios) / max(min(ratios), 1e-9) < 1.5:
                print("  They are clustered around one multiple, which points at a")
                print("  decimals or field-order error rather than stale data.")
        if len(agreed) >= total * 0.8:
            print("\n  LAYOUT CONFIRMED. The on-chain price can be trusted.")
        else:
            print("\n  LAYOUT NOT CONFIRMED. Do not wire this in yet.")

    print("\n" + "=" * 70)
    print("HOW MUCH MORE CAN WE SEE?")
    print("=" * 70)
    readable = total + chain_only
    sampled = readable + len(unreadable)
    print(f"  readable on chain      {readable:>4} of {sampled}")
    print(f"  of which DexScreener   {total:>4}")
    print(f"  CHAIN ONLY             {chain_only:>4}  <- invisible to us today")
    if unreadable:
        print(f"\n  {len(unreadable)} could not be read on chain either:")
        from collections import Counter
        for reason, n in Counter(r for _, r in unreadable).most_common(4):
            print(f"    {n:>4}  {reason[:60]}")
    if chain_only > total:
        print("\n  The chain shows more tokens than the aggregator does, which is")
        print("  the whole point: every measurement so far was taken on the")
        print("  indexed minority.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
