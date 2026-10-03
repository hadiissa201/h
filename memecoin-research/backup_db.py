"""Dump the research database, reading credentials from .env.

Exists because the Postgres password is in .env and not in anyone's memory. It
parses the connection URL the collector already uses, hands the password to
pg_dump through the environment rather than the command line -- a password in
argv is visible to anything that can list processes -- and never prints it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from collector.config import load_settings

# Where EDB's installer puts it on Windows, newest first.
WINDOWS_HINTS = [Path(f"C:/Program Files/PostgreSQL/{v}/bin/pg_dump.exe")
                 for v in (18, 17, 16, 15, 14)]


def find_pg_dump() -> str | None:
    found = shutil.which("pg_dump")
    if found:
        return found
    return next((str(p) for p in WINDOWS_HINTS if p.exists()), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    url = urlparse(load_settings().database_url.replace("postgresql+psycopg", "postgresql"))
    if url.scheme != "postgresql":
        print(f"not a Postgres database ({url.scheme}); nothing to dump")
        return 1

    database = (url.path or "").lstrip("/")
    user = unquote(url.username or "postgres")
    password = unquote(url.password or "")
    host = url.hostname or "localhost"
    port = str(url.port or 5432)

    out = args.out or (Path.home() / "Desktop" /
                       f"memecoin_research_{datetime.now(UTC):%Y%m%d}.sql")
    binary = find_pg_dump()
    if binary is None:
        print("pg_dump not found. Give its full path with PATH or install the "
              "PostgreSQL client tools.")
        return 2

    print(f"  database {database} on {host}:{port} as {user}")
    print(f"  password {'from .env' if password else 'NOT in .env -- will prompt'}")
    print(f"  writing  {out}\n")

    env = dict(os.environ)
    if password:
        # Through the environment, never argv: a password in a command line is
        # readable by any process that can list processes.
        env["PGPASSWORD"] = password

    result = subprocess.run(
        [binary, "-U", user, "-h", host, "-p", port, "-d", database,
         "-f", str(out), "--no-password" if password else "--password"],
        env=env, check=False)

    if result.returncode != 0:
        print(f"\npg_dump failed with code {result.returncode}.")
        print("If it says authentication failed, the password in .env is not "
              "the one Postgres expects for that user.")
        return result.returncode

    size = out.stat().st_size / 1_048_576 if out.exists() else 0.0
    print(f"\n  done: {out} ({size:,.1f} MB)")
    print("  Contains prices, sellability verdicts and paper trades. No keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
