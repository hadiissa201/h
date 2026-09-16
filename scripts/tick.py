#!/usr/bin/env python3
"""One scheduled iteration of the trading loop, for setups without n8n.

Windows Task Scheduler (or cron) runs this every few minutes. It performs one
tick and exits, which is deliberate: a one-shot process cannot leak, wedge, or
drift, and the scheduler restarts it for free if it dies.

    python scripts/tick.py

It replaces workflows 01, 02, 06 and 07. What you give up versus n8n: the
visual execution history, the built-in retry/alert nodes, and the error-handler
workflow. Everything financially meaningful -- data gates, strategies, the risk
engine, execution, position monitoring, the kill switch -- lives in the Python
service and is identical either way.

The API key is read from .env, never passed as an argument: scheduled-task
command lines are visible to anyone who can open Task Scheduler.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = REPO_ROOT / "logs" / "tick.log"


def load_env_value(key: str) -> str:
    """Read one key from .env without needing python-dotenv."""
    for candidate in (REPO_ROOT / ".env", REPO_ROOT / "python-service" / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    return ""


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict | None = None):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
        )
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        except Exception as exc:  # connection refused, DNS, timeout
            return 0, str(exc)


def log(line: str) -> None:
    stamped = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {line}"
    print(stamped)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError:
        pass  # a failed log write must never stop the tick


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--timeframe", default="")
    parser.add_argument(
        "--daily-report",
        action="store_true",
        help="also build the daily report (schedule this one once a day)",
    )
    args = parser.parse_args()

    api_key = load_env_value("SERVICE_API_KEY")
    client = Client(args.base_url, api_key, args.timeout)

    # ---------------------------------------------------------------- health
    status, health = client.call("GET", "/health")
    if status != 200:
        log(f"SKIP  service unreachable at {args.base_url}: {health}")
        return 1

    mode = health.get("mode")
    if mode != "paper":
        # Not a refusal -- the service enforces its own mode. But an unattended
        # loop silently switching to real money is worth shouting about.
        log(f"WARNING  effective mode is {mode!r}, not paper")

    bot_status = health.get("bot_status")
    if bot_status != "RUNNING":
        log(f"HALTED  bot_status={bot_status} — no new entries until a manual reset")

    symbols = [s.strip() for s in (load_env_value("TRADING_SYMBOLS") or
                                   "BTC/USDT,ETH/USDT,SOL/USDT").split(",") if s.strip()]
    timeframes = [t.strip() for t in (load_env_value("TIMEFRAMES") or
                                      "5m,15m,1h,4h").split(",") if t.strip()]
    timeframe = args.timeframe or load_env_value("PRIMARY_TIMEFRAME") or "1h"

    # ------------------------------------------------------- 1. market data
    status, collected = client.call(
        "POST",
        "/market/collect",
        {"symbols": symbols, "timeframes": timeframes, "limit": 400},
    )
    if status != 200:
        log(f"FAIL  /market/collect -> {status}: {str(collected)[:200]}")
        return 1

    bad = [
        entry["symbol"]
        for entry in collected.get("symbols", [])
        if not entry.get("data_ok")
    ]
    ok_count = len(collected.get("symbols", [])) - len(bad)
    log(f"data     {ok_count}/{len(collected.get('symbols', []))} symbols clean"
        + (f"; FAILED GATES: {', '.join(bad)}" if bad else ""))

    # --------------------------------------------------- 2. analysis + trade
    # The bot being halted does not skip this: the risk engine refuses the entry
    # itself, and the attempt is worth recording.
    status, run = client.call(
        "POST", "/pipeline/run", {"symbols": symbols, "timeframe": timeframe}
    )
    if status != 200:
        log(f"FAIL  /pipeline/run -> {status}: {str(run)[:200]}")
        return 1

    for outcome in run.get("outcomes", []):
        log(
            f"  {outcome['symbol']:<10} stage={outcome.get('stage_reached'):<10}"
            f" traded={outcome.get('traded')}  {outcome.get('reason') or ''}"
        )
    log(f"pipeline {run.get('traded', 0)} trade(s) placed, mode={run.get('mode')}")

    # ---------------------------------------------------- 3. manage positions
    status, monitor = client.call("POST", "/positions/monitor", {})
    if status != 200:
        log(f"FAIL  /positions/monitor -> {status}: {str(monitor)[:200]}")
        return 1

    log(
        f"monitor  checked={monitor.get('checked')} exits={len(monitor.get('exits') or [])}"
        f" stop_moves={len(monitor.get('stop_updates') or [])}"
        f" equity={monitor.get('equity')} status={monitor.get('bot_status')}"
    )
    for trigger in monitor.get("emergency_triggers") or []:
        log(f"  EMERGENCY  {trigger}")
    for error in monitor.get("errors") or []:
        log(f"  monitor error: {error}")

    # -------------------------------------------------------- 4. daily report
    if args.daily_report:
        status, report = client.call(
            "POST", "/reports/daily", {"include_ai_summary": False}
        )
        if status == 200:
            account = report.get("facts", {}).get("account", {})
            log(
                f"report   equity={account.get('equity')} "
                f"books_balance={account.get('books_balance')}"
            )
            if not account.get("books_balance", True):
                log("  ACCOUNTING DOES NOT RECONCILE — investigate before trading on")
        else:
            log(f"FAIL  /reports/daily -> {status}: {str(report)[:200]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
