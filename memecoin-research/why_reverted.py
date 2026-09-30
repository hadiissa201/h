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

import os
import sys
from collections import Counter

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.models import SimulatedExit, Token
from poc.sources import METHOD_RPC_SIM

# Signatures of a fault in OUR method rather than a property of the token.
OUR_FAULT_MARKERS = (
    ("insufficient funds", "the simulated holder did not have the tokens"),
    ("InsufficientFunds", "the simulated holder did not have the tokens"),
    ("AccountNotFound", "the holder's token account did not exist"),
    ("could not find account", "the holder's token account did not exist"),
    ("build failed", "Jupiter would not build the swap for that holder"),
    ("no swapTransaction", "Jupiter would not build the swap for that holder"),
    ("BlockhashNotFound", "a transient node error, not the token"),
)

# Signatures of a genuine restriction on selling.
REAL_MARKERS = (
    ("frozen", "the token account is FROZEN -- a real, deliberate block"),
    ("Frozen", "the token account is FROZEN -- a real, deliberate block"),
    ("transfer hook", "a transfer hook rejected the sell"),
    ("TransferHook", "a transfer hook rejected the sell"),
    ("0x11", "SPL token error 0x11 (owner mismatch / frozen)"),
    ("Slippage", "the route moved beyond tolerance before landing"),
    ("slippage", "the route moved beyond tolerance before landing"),
)


def classify(reason: str) -> tuple[str, str]:
    for marker, explanation in OUR_FAULT_MARKERS:
        if marker in reason:
            return "OURS", explanation
    for marker, explanation in REAL_MARKERS:
        if marker in reason:
            return "REAL", explanation
    return "UNKNOWN", "not recognised -- read the raw reason below"


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.")
        return 2
    engine = create_engine(url)
    verdicts: Counter[str] = Counter()

    with Session(engine) as session:
        rows = session.execute(
            select(SimulatedExit, Token.address)
            .join(Token, Token.id == SimulatedExit.token_id)
            .where(SimulatedExit.method == METHOD_RPC_SIM,
                   SimulatedExit.succeeded.is_(False))
            .order_by(SimulatedExit.simulated_ts.desc()).limit(40)).all()

        if not rows:
            print("No failed verifications recorded yet.")
            return 0

        print(f"{len(rows)} failed verification(s), newest first\n")
        for sim, address in rows:
            reason = sim.failure_reason or ""
            verdict, explanation = classify(reason)
            verdicts[verdict] += 1
            print(f"  {sim.simulated_ts:%Y-%m-%d %H:%M}  {address[:16]}...")
            print(f"    verdict:  {verdict} -- {explanation}")
            print(f"    kind:     {sim.failure_kind}")
            print(f"    reason:   {reason[:300]}")
            print()

    print("=" * 70)
    print(f"  attributable to a real restriction: {verdicts['REAL']}")
    print(f"  attributable to our own method:     {verdicts['OURS']}")
    print(f"  unclassified:                      {verdicts['UNKNOWN']}")
    if verdicts["OURS"] or verdicts["UNKNOWN"]:
        print("\n  Any count outside REAL means the verification rate in the audit")
        print("  is not yet a measurement of the market. Fix the method first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
