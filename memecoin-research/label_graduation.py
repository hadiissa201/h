"""Label every token's curve state from the chain. Read-only, resumable.

The pool table cannot answer "did this token graduate". It is censored both
ways: 88 tokens have a pumpswap pool with no pump.fun pool recorded, so their
curve phase was never seen, and any token that migrated after the collector
stopped watching it looks like it never did. That gave a 1.14% graduation rate
on a population whose true rate is clearly higher, and a base rate that wrong
makes every entry-signal test meaningless.

The curve's own `complete` flag settles it. One getAccountInfo call per token,
valid whenever it is made, independent of what we happened to be watching.

    python label_graduation.py --rpc https://api.mainnet-beta.solana.com

Resumable: tokens already labelled are skipped, so interrupting it loses
nothing. The public RPC rate-limits hard, so expect this to take a while and
run it in chunks with --limit if you prefer.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Base, CurveState, Token
from collector.onchain import _redact, curve_address, read_curve
from collector.pacing import Pacer


# Answers the node actually gave about the account, as opposed to our failing
# to ask it. Only these may be written down as "this token has no curve".
_DEFINITIVE = ("account does not exist or is empty",
               "is not a BondingCurve",
               "not a bool",
               "need at least")


def is_definitive(detail: str) -> bool:
    """Did the chain answer, or did we just fail to reach it?

    This distinction is the whole correctness of the pass. A refused
    connection recorded as "no curve" would quietly shrink the eligible
    population and bias the base rate downward -- a network error written
    down as a fact about the token. The smoke test did exactly that on six
    tokens before this existed.
    """
    return any(marker in detail for marker in _DEFINITIVE)


def label_one(client: httpx.Client, rpc_url: str, token: Token,
              pacer: Pacer) -> CurveState | None:
    """One token, one RPC call. None means unanswered -- retry, do not record."""
    now = datetime.now(UTC)
    address, note = curve_address(token.address)
    if address is None:
        # A mint we cannot decode is a definitive answer about the mint.
        return CurveState(token_id=token.id, curve_address=None,
                          account_exists=False, complete=None,
                          checked_ts=now, note=note[:256])
    pacer.wait()
    curve, detail = read_curve(client, rpc_url, address, pacer=pacer)
    if curve is None:
        if not is_definitive(detail):
            return None
        return CurveState(token_id=token.id, curve_address=address,
                          account_exists=False, complete=None,
                          checked_ts=now, note=detail[:256])
    return CurveState(
        token_id=token.id, curve_address=address, account_exists=True,
        complete=curve.complete,
        virtual_sol_reserves=curve.virtual_sol_reserves,
        real_sol_reserves=curve.real_sol_reserves,
        account_bytes=curve.raw_len, checked_ts=now, note="ok")


def summarise(states: list[CurveState]) -> str:
    """Cross-tabulate the complete flag against zeroed reserves.

    Printed rather than assumed. The whole labelling rests on migration
    setting `complete`, and that belief has not been checked against anything
    -- the earlier run saw 11 curves with zeroed reserves but never looked at
    their flag, because the price helper returned None first.
    """
    live = [s for s in states if s.account_exists]
    if not live:
        return ("  No curve was read, so there is nothing to cross-check. "
                "That is\n  not agreement.")
    table: Counter = Counter()
    for state in live:
        drained = not state.real_sol_reserves and not state.virtual_sol_reserves
        table[(bool(state.complete), drained)] += 1
    lines = ["  complete  reserves    count",
             "  --------  ----------  -----"]
    for (complete, drained), count in sorted(table.items(), reverse=True):
        lines.append(f"  {str(complete):<8}  "
                     f"{'zeroed' if drained else 'present':<10}  {count:>5}")
    lines.append("")
    inconsistent = table[(False, True)] + table[(True, False)]
    if inconsistent:
        lines.append(f"  {inconsistent} curves disagree with the assumption that")
        lines.append("  migration zeroes the reserves AND sets the flag. Where")
        lines.append("  they disagree, trust the flag: it is what the program")
        lines.append("  writes on completion, while reserves can be drained by")
        lines.append("  ordinary selling.")
    else:
        lines.append("  The flag and the reserves agree on every curve, so the")
        lines.append("  label rests on two signals that happen to concur.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="label at most this many unlabelled tokens")
    parser.add_argument("--recheck", action="store_true",
                        help="re-read tokens already labelled. A token on the "
                             "curve today may graduate tomorrow, so an "
                             "on-curve label is provisional while a complete "
                             "one is final")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    settings = load_settings()
    rpc_url = args.rpc or settings.rpc_url
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)  # only ever adds the new table

    with Session(engine) as session, httpx.Client() as client:
        print(f"RPC endpoint: {_redact(rpc_url)}")
        done = {row for row in session.scalars(select(CurveState.token_id))}
        if args.recheck:
            # Keep tokens already known to have completed: that is terminal.
            done = {row for row in session.scalars(
                select(CurveState.token_id).where(CurveState.complete.is_(True)))}
        pending = [t for t in session.scalars(select(Token)).all()
                   if t.id not in done]
        if args.limit:
            pending = pending[: args.limit]
        print(f"{len(done)} already labelled, {len(pending)} to go\n")

        pacer = Pacer()
        written, unanswered = 0, 0
        for i, token in enumerate(pending, start=1):
            state = label_one(client, rpc_url, token, pacer)
            if state is None:
                # The node did not answer. Leaving the row absent means the
                # next run retries it, rather than baking a network failure
                # into the base rate.
                unanswered += 1
                continue
            existing = session.scalars(
                select(CurveState)
                .where(CurveState.token_id == token.id)).first()
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(state)
            written += 1
            if i % args.progress_every == 0 or unanswered and i == len(pending):
                # Commit as we go: a run this long will be interrupted, and
                # losing an hour of reads to a lost connection would be its
                # own kind of avoidable.
                session.commit()
                print(f"  {i}/{len(pending)}  pace {pacer.delay:.2f}s  "
                      f"{pacer.throttles} rate limits")
        session.commit()

        states = session.scalars(select(CurveState)).all()
        exists = [s for s in states if s.account_exists]
        graduated = [s for s in exists if s.complete]
        print("\n" + "=" * 70)
        print("CURVE STATE, FROM THE CHAIN")
        print("=" * 70)
        print(f"  {len(states)} tokens labelled this run and before")
        print(f"  {len(exists)} have a pump.fun bonding curve")
        print(f"  {len(graduated)} of those have completed (graduated)")
        print(f"  {len(states) - len(exists)} have no curve: not a pump.fun launch")
        if unanswered:
            print(f"\n  {unanswered} tokens went unanswered this run and were "
                  f"NOT recorded,")
            print("  so they are retried next time. A refused connection is not")
            print("  evidence a token has no curve.")
        if exists:
            rate = len(graduated) / len(exists) * 100
            print(f"\n  graduation rate among pump.fun launches: {rate:.2f}%")
        print("\n  Why the reserves are stored next to the flag:")
        print(summarise(states))

        reasons = Counter(s.note or "" for s in states if not s.account_exists)
        if reasons:
            print("\n  tokens with no curve, by reason:")
            for reason, count in reasons.most_common(5):
                print(f"    {count:>5}  {reason[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
