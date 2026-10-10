"""Why did a chain-state verification fail?

Run this before believing any number from section 4 of the audit. A revert tells
us a sell would not have landed; it does not say whose fault that is. Three very
different causes look identical in the summary:

  - A real restriction on the token (frozen account, transfer hook). A finding.
  - The holder we simulated as could not have sold anyway. Our method's fault.
  - The swap could not be built at all. Also ours.

Read-only.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import SimulatedExit, Token
from collector.verify import (
    classify_failure,
    custom_error_code,
    disagreement_rate,
    failing_instruction,
    wilson_interval,
)
from poc.sources import METHOD_RPC_SIM

# The classifier now lives in collector/verify.py so the collector applies it
# at write time instead of the analysis repairing rows afterwards. Re-exported
# here under its old name, which repair_verifications.py imports.
classify = classify_failure


def main() -> int:
    # Same settings path as audit.py, so .env is honoured rather than
    # requiring the URL in the shell.
    engine = create_engine(load_settings().database_url, future=True)
    verdicts: Counter[str] = Counter()
    where: dict[int, set[int]] = defaultdict(set)

    with Session(engine) as session:
        rows = session.execute(
            select(SimulatedExit, Token.address)
            .join(Token, Token.id == SimulatedExit.token_id)
            .where(SimulatedExit.method == METHOD_RPC_SIM,
                   SimulatedExit.succeeded.is_(False))
            .order_by(SimulatedExit.simulated_ts.desc()).limit(500)).all()

        if not rows:
            print("No failed verifications recorded yet.")
            return 0

        print(f"{len(rows)} failed verification(s), newest first\n")
        for sim, address in rows:
            reason = sim.failure_reason or ""
            verdict, explanation = classify(reason)
            verdicts[verdict] += 1
            code = custom_error_code(reason)
            index = failing_instruction(reason)
            if code is not None and index is not None:
                where[code].add(index)
            print(f"  {sim.simulated_ts:%Y-%m-%d %H:%M}  {address[:16]}...")
            print(f"    verdict:  {verdict} -- {explanation}")
            print(f"    kind:     {sim.failure_kind}")
            print(f"    reason:   {reason[:300]}")
            print()

        counts = disagreement_rate(session)

    print("=" * 70)
    print(f"  attributable to a real restriction: {verdicts['REAL']}")
    print(f"  attributable to our own method:     {verdicts['OURS']}")
    print(f"  unclassified:                      {verdicts['UNKNOWN']}")

    if len(where) > 1:
        print("\n  Which instruction raised each code, as a cross-check on the")
        print("  classification that does not depend on any documentation:")
        for code, indexes in sorted(where.items()):
            print(f"    code {code}: instruction {sorted(indexes)}")
        print("\n  The route is the later instruction, and the route is where a")
        print("  price can move against you. A code raised only BEFORE it")
        print("  cannot be the market refusing the trade -- it is the")
        print("  transaction failing to assemble, which is ours.")

    answered = counts["verified"]
    reverts = counts["would_revert"]
    ours = verdicts["OURS"]
    print("\n" + "=" * 70)
    print("THE RATE THAT GATES EVERY OTHER NUMBER")
    print("=" * 70)
    if not answered:
        print("  Nothing answered, so there is no rate.")
        return 0
    raw = reverts / answered
    low, high = wilson_interval(reverts, answered)
    print(f"  as recorded      {reverts}/{answered} = {raw * 100:.1f}%  "
          f"[{low * 100:.1f}%, {high * 100:.1f}%]")

    # Our own broken transactions are not answers. Removing them from the
    # numerator AND the denominator is the correct correction: a transaction
    # Solana rejected before the token was involved tells us nothing either
    # way, which is exactly what succeeded=None means.
    net_answered = answered - ours
    net_reverts = max(0, reverts - ours)
    if net_answered <= 0:
        print("\n  Every answered check was our own fault. There is no")
        print("  measurement of the market here at all.")
        return 0
    corrected = net_reverts / net_answered
    clow, chigh = wilson_interval(net_reverts, net_answered)
    print(f"  excluding ours   {net_reverts}/{net_answered} = "
          f"{corrected * 100:.1f}%  [{clow * 100:.1f}%, {chigh * 100:.1f}%]")
    print("\n  Unclassified reverts are left IN the corrected numerator. An")
    print("  unrecognised reason could be either, and calling it ours would")
    print("  lower this rate every time a new error string appeared.")

    bar = 0.10
    if chigh < bar:
        print(f"\n  -> PASSES the {bar * 100:.0f}% precondition once our own")
        print("     faults are excluded. The paper exits can be trusted.")
    elif clow >= bar:
        print(f"\n  -> STILL FAILS the {bar * 100:.0f}% precondition. The quotes")
        print("     are wrong about the market often enough that every return")
        print("     figure in the project is overstated.")
    else:
        print(f"\n  -> UNDECIDED against the {bar * 100:.0f}% bar: the interval")
        print("     spans it. More verifications are needed, not a different")
        print("     reading of these ones.")
    if ours:
        print(f"\n  {ours} rows are our fault and still stored as False. The")
        print("  collector now writes these as NULL at the point of discovery;")
        print("  for the rows already in the database:")
        print("    python repair_verifications.py          # dry run")
        print("    python repair_verifications.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
