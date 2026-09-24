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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sample-rate", type=float, default=None,
                        help="override MEMECOIN_SAMPLE_RATE for this run")
    parser.add_argument("--plan", action="store_true",
                        help="print capacity and exit without collecting")
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
