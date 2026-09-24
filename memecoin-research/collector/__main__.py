"""Run the Phase 1 collector.

    python -m collector --workers 4
    python -m collector --plan        # show capacity and exit, change nothing

Observation only. No private key exists in this package, no transaction can be
signed or sent, and tests fail the build if anyone adds a path that could.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from collector.config import load_settings
from collector.runner import run
from collector.schedule import capacity


def preflight(settings) -> int:  # noqa: ANN001
    """Check everything the collector needs before it runs for weeks.

    Separate from --plan because a wrong answer here is expensive in a way a
    wrong capacity number is not: a missing key means silently under-counted
    detection, and an unreachable database means the run dies at 3am having
    collected nothing recoverable.
    """
    from sqlalchemy import create_engine, text

    problems: list[str] = []
    print("\n--- preflight ---")

    if settings.using_fallback:
        problems.append(
            "No Helius key. The public RPC was MEASURED dropping ~30% of launch "
            "messages without erroring, so detection silently under-counts.")
        print("  [FAIL] helius key        not set (checked MEMECOIN_HELIUS_API_KEY "
              "and .env)")
    else:
        tail = settings.helius_api_key[-4:]
        print(f"  [ OK ] helius key        loaded (...{tail})")

    try:
        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"  [ OK ] database          reachable ({engine.url.render_as_string()})")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Database unreachable: {type(exc).__name__}: {exc}")
        print(f"  [FAIL] database          {type(exc).__name__}: {str(exc)[:120]}")

    print(f"  [ OK ] sample rate       {settings.sample_rate} "
          f"({plan_note(settings)})")

    if problems:
        print("\nNOT READY:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nReady. Start with:  python -m collector --workers 4")
    return 0


def plan_note(settings) -> str:  # noqa: ANN001
    admitted = capacity(settings)["intake_tokens_per_day"]
    return f"~{admitted:,.0f} tokens/day admitted at capacity"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sample-rate", type=float, default=None,
                        help="override MEMECOIN_SAMPLE_RATE for this run")
    parser.add_argument("--plan", action="store_true",
                        help="print capacity and exit without collecting")
    parser.add_argument("--check", action="store_true",
                        help="verify config, credentials and database, then exit. "
                             "Collects nothing and writes nothing.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    settings = load_settings()
    if args.sample_rate is not None:
        settings.sample_rate = args.sample_rate

    plan = capacity(settings)
    print(json.dumps(plan, indent=2))
    if args.plan:
        return 0
    if args.check:
        return preflight(settings)

    if settings.using_fallback:
        print("\nWARNING: no MEMECOIN_HELIUS_API_KEY set.\n"
              "The public RPC was measured dropping ~30% of launch messages "
              "without reporting any error, so detection will silently\n"
              "under-count. Set the key before collecting data you intend to "
              "trust.\n")

    print(f"\nCollecting at sample_rate={settings.sample_rate}. "
          f"Status: http://127.0.0.1:{settings.status_port}/status")
    print("Ctrl+C to stop. Everything is recorded, including the gaps.\n")
    return run(settings, workers=args.workers)


if __name__ == "__main__":
    sys.exit(main())
