"""Reclassify verification rows that recorded our own failure as the market's.

The first live verification run chose a PDA as the fee payer. Solana rejected
those transactions with InvalidAccountForFee before the token was ever involved,
and they were stored as succeeded=False -- "this could not be sold". That is the
one error this project most needs not to make.

Nothing is deleted. The rows move from False to NULL, which is what they always
should have been: we tried to ask and learned nothing. The original reason is
preserved so the mistake stays auditable.

Run with --apply to write; the default is a dry run.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import SimulatedExit
from poc.sources import METHOD_RPC_SIM
from why_reverted import classify

OUR_FAULT_KIND = "our_fee_payer"


def main() -> int:
    apply = "--apply" in sys.argv
    engine = create_engine(load_settings().database_url, future=True)

    with Session(engine) as session:
        rows = session.scalars(
            select(SimulatedExit)
            .where(SimulatedExit.method == METHOD_RPC_SIM,
                   SimulatedExit.succeeded.is_(False))).all()

        mistaken = [r for r in rows if classify(r.failure_reason or "")[0] == "OURS"]
        genuine = len(rows) - len(mistaken)

        print(f"  {len(rows)} failed verification rows")
        print(f"    our own fault:        {len(mistaken)}  -> to be reclassified NULL")
        print(f"    genuine restrictions: {genuine}  -> left untouched")

        if not mistaken:
            print("\n  Nothing to repair.")
            return 0

        for row in mistaken:
            if apply:
                row.succeeded = None
                row.failure_kind = OUR_FAULT_KIND
                row.failure_reason = (
                    f"reclassified: our fee payer could not pay fees. "
                    f"original: {row.failure_reason}")[:500]

        if apply:
            session.commit()
            print(f"\n  Reclassified {len(mistaken)} rows to unknown (NULL).")
            print("  They no longer count as evidence about sellability.")
        else:
            print(f"\n  DRY RUN. Re-run with --apply to reclassify {len(mistaken)} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
