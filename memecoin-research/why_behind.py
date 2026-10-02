"""Why is the work queue behind?

The audit says how many items are past due. It does not say why, and the two
plausible causes call for opposite fixes: if the workers are saturated, collect
less; if the work is FAILING and backing off, collecting less changes nothing.

Guessing between them is how a wrong fix gets shipped twice. Read-only.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import WorkItem


def main() -> int:
    engine = create_engine(load_settings().database_url, future=True)
    now = datetime.now(UTC)

    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(WorkItem))
        rows = session.execute(
            select(WorkItem.kind, WorkItem.attempts, WorkItem.last_error,
                   WorkItem.due_at)).all()

        overdue = []
        for kind, attempts, last_error, due_at in rows:
            due = due_at.replace(tzinfo=UTC) if due_at and not due_at.tzinfo else due_at
            if due is not None and due < now:
                overdue.append((kind, attempts, last_error, (now - due).total_seconds()))

        print(f"  {total} work items, {len(overdue)} past due\n")
        if not overdue:
            print("  Queue is healthy.")
            return 0

        by_kind = Counter(k for k, _, _, _ in overdue)
        print("  Overdue by kind:")
        for kind, n in by_kind.most_common():
            print(f"    {kind:10} {n:>6}")

        # The decisive split. Attempts at zero means the item has never been
        # tried: the workers simply have not reached it, so there is too much
        # work. Attempts above zero means it is being tried and failing, and
        # the error says what is actually broken.
        never_tried = sum(1 for _, a, _, _ in overdue if not a)
        failing = len(overdue) - never_tried
        print(f"\n  never attempted (workers behind):  {never_tried:>6}")
        print(f"  attempted and failing:             {failing:>6}")

        if failing:
            print("\n  Errors on failing items, most common first:")
            errors = Counter((e or "")[:110] for _, a, e, _ in overdue if a and e)
            for err, n in errors.most_common(8):
                print(f"    {n:>6}x  {err}")
            attempts = Counter(a for _, a, _, _ in overdue if a)
            print("\n  Attempt counts:")
            for a, n in sorted(attempts.items())[:10]:
                print(f"    {a:>3} attempts: {n:>6} items")

        lateness = sorted(late for _, _, _, late in overdue)
        print(f"\n  Lateness: median {lateness[len(lateness)//2]/60:,.1f} min, "
              f"worst {lateness[-1]/3600:,.1f} h")

        print("\n  " + "-" * 66)
        if never_tried > failing:
            print("  VERDICT: too much work for the workers. Lower sample_rate or")
            print("  raise the worker count. Collecting less will help.")
        else:
            print("  VERDICT: the work is FAILING, not queued. Lowering sample_rate")
            print("  would not fix this -- read the errors above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
