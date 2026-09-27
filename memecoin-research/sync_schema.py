"""Add columns the models declare but the database is missing.

SQLAlchemy's create_all() creates missing TABLES and silently ignores missing
COLUMNS on tables that already exist. Every column added to a model after the
database was first created is therefore absent, and every write touching one
fails -- quietly, because the collector catches exceptions per task and
requeues rather than crashing. That is the right behaviour for a network
blip and exactly the wrong way to find out about a schema drift.

This compares the models against the live database and issues the ALTER TABLE
statements needed to close the gap. It only ever ADDS: no column is dropped,
no type is changed, no data is touched.

    python sync_schema.py --dry-run     # show what is missing, change nothing
    python sync_schema.py
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateColumn

from collector.config import load_settings
from collector.models import Base


def missing_columns(engine) -> list[tuple[str, str, str]]:  # noqa: ANN001
    """(table, column, DDL) for every column the models have and the DB lacks."""
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    gaps: list[tuple[str, str, str]] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue  # create_all handles whole missing tables
        live = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in live:
                continue
            ddl = str(CreateColumn(column).compile(engine))
            # A NOT NULL column cannot be added to a populated table without a
            # default, so existing rows get the model's default or NULL.
            ddl = ddl.replace(" NOT NULL", "")
            gaps.append((table.name, column.name, ddl))
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(load_settings().database_url, future=True)
    Base.metadata.create_all(engine)          # whole missing tables first
    gaps = missing_columns(engine)

    if not gaps:
        print("Schema is in sync: every model column exists in the database.")
        return 0

    print(f"{len(gaps)} column(s) missing from the database:\n")
    for table, column, ddl in gaps:
        print(f"  {table}.{column}")
    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        return 0

    with engine.begin() as conn:
        for table, column, ddl in gaps:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))
            print(f"  added {table}.{column}")

    remaining = missing_columns(engine)
    if remaining:
        print(f"\nSTILL MISSING {len(remaining)}: {remaining}")
        return 1
    print("\nSchema is now in sync. Restart the collector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
