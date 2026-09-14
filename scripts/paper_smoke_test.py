#!/usr/bin/env python3
"""End-to-end paper smoke test against a running service.

Drives the same sequence the n8n workflows drive, over HTTP, and asserts the
properties that must hold in paper mode. It reports what it actually observed —
including "no trade was taken", which is a perfectly valid outcome and must never
be dressed up as a success.

    python scripts/paper_smoke_test.py --base-url http://localhost:8000 --api-key "$SERVICE_API_KEY"

Exit code 0 means every assertion held. Non-zero means something is wrong; read
the FAIL lines.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
INFO = "info"

_results: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    _results.append((status, label))
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return condition


def note(text: str) -> None:
    print(f"  [{INFO}] {text}")


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, body

    def get(self, path: str, **kwargs) -> tuple[int, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: dict[str, Any] | None = None, **kwargs):
        return self.request("POST", path, payload, **kwargs)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------- stages
def stage_health(client: Client) -> dict[str, Any]:
    section("1. Health and safety posture")
    status, health = client.get("/health")
    check("GET /health responds", status == 200, f"HTTP {status}")
    if status != 200:
        sys.exit(_summary())

    check("service reports paper mode", health["mode"] == "paper", health["mode"])
    check("live trading is NOT armed", health["live_trading_armed"] is False)
    check(
        "every live-mode blocker is listed",
        len(health.get("live_mode_blockers") or []) >= 1,
        f"{len(health.get('live_mode_blockers') or [])} blockers",
    )
    for component in health["components"]:
        check(
            f"component {component['name']} healthy",
            component["healthy"],
            component.get("detail", ""),
        )
    return health


def stage_auth(client: Client) -> None:
    section("2. Authentication")
    status, _ = client.get("/portfolio", auth=False)
    check("unauthenticated request is refused", status == 401, f"HTTP {status}")
    status, _ = client.get("/portfolio")
    check("authenticated request is accepted", status == 200, f"HTTP {status}")


def stage_market_data(client: Client, symbols: list[str], timeframes: list[str]) -> None:
    section("3. Market data collection")
    status, body = client.post(
        "/market/collect",
        {"symbols": symbols, "timeframes": timeframes, "limit": 400},
    )
    check("POST /market/collect succeeds", status == 200, f"HTTP {status}")
    if status != 200:
        return
    for entry in body["symbols"]:
        check(
            f"{entry['symbol']} data passes the quality gates",
            entry["data_ok"],
            ", ".join(entry.get("errors") or []) or "no issues",
        )
        for timeframe, stats in entry["timeframes"].items():
            note(
                f"{entry['symbol']} {timeframe}: {stats['bars']} bars, "
                f"staleness {stats['staleness_seconds']:.0f}s"
            )


def stage_pipeline(client: Client, symbols: list[str], timeframe: str) -> dict[str, Any]:
    section("4. Analysis -> risk -> execution pipeline")
    status, body = client.post(
        "/pipeline/run", {"symbols": symbols, "timeframe": timeframe}
    )
    check("POST /pipeline/run succeeds", status == 200, f"HTTP {status}")
    if status != 200:
        return {}
    check("pipeline stays in paper mode", body["mode"] == "paper", body["mode"])
    note(f"{body['traded']} of {len(body['outcomes'])} symbols traded")
    for outcome in body["outcomes"]:
        note(
            f"{outcome['symbol']}: stage={outcome['stage_reached']} "
            f"traded={outcome['traded']} reason={outcome.get('reason') or '-'}"
        )
    return body


def stage_risk_authority(client: Client) -> None:
    section("5. Risk engine authority")
    status, limits = client.get("/risk/limits")
    check("GET /risk/limits succeeds", status == 200, f"HTTP {status}")

    # An order without a risk approval must be refused outright.
    status, body = client.post(
        "/orders",
        {
            "risk_approval_id": "ra_forged_approval_id",
            "strategy": "smoke",
            "regime": "range",
        },
    )
    check(
        "execution refuses a forged risk approval",
        status >= 400,
        f"HTTP {status}: {_error_text(body)}",
    )

    # A proposal that risks far too much must be rejected by the risk engine.
    price = 100.0
    status, decision = client.post(
        "/risk/check",
        {
            "symbol": "BTC/USDT",
            "direction": "BUY",
            "entry": price,
            "stop_loss": price * 0.999,   # absurdly tight stop
            "take_profit": price * 1.001, # and no reward
            "confidence": 0.99,
            "strategy": "smoke",
        },
    )
    if status == 200:
        failed = [c["name"] for c in decision.get("checks", []) if not c["passed"]]
        check(
            "risk engine rejects a no-edge proposal",
            decision["decision"] != "APPROVED",
            f"{decision['decision']}; failed: {', '.join(failed)[:110]}",
        )
        check(
            "a rejected proposal is issued no approval",
            decision.get("approval_id") is None,
        )
        sizing = decision.get("sizing") or {}
        if sizing.get("effective_risk_pct") is not None:
            note(
                f"sizing: quantity {sizing['quantity']}, notional {sizing['notional']}, "
                f"risked {float(sizing['effective_risk_pct']) * 100:.4f}% of equity "
                f"(capped by: {', '.join(sizing.get('capped_by') or []) or 'nothing'})"
            )
    else:
        check(
            "risk engine rejects a no-edge proposal",
            status >= 400,
            f"HTTP {status}: {_error_text(decision)}",
        )


def stage_monitor_and_portfolio(client: Client) -> dict[str, Any]:
    section("6. Position monitoring and portfolio accounting")
    status, monitor = client.post("/positions/monitor", {})
    check("POST /positions/monitor succeeds", status == 200, f"HTTP {status}")
    if status == 200:
        note(
            f"checked {monitor.get('checked')} position(s): "
            f"{len(monitor.get('exits') or [])} exit(s), "
            f"{len(monitor.get('stop_updates') or [])} stop update(s), "
            f"{len(monitor.get('resting_fills') or [])} resting fill(s)"
        )
        check(
            "the monitor reported no errors",
            not (monitor.get("errors") or []),
            "; ".join(str(e) for e in (monitor.get("errors") or []))[:120],
        )
        for flagged in monitor.get("stale_symbols") or []:
            note(f"stale data flagged: {flagged}")
        for flagged in monitor.get("abnormal_symbols") or []:
            note(f"abnormal conditions flagged: {flagged}")
        note(
            f"monitor view of the account: equity {monitor.get('equity')}, "
            f"drawdown {monitor.get('drawdown_pct')}, status {monitor.get('bot_status')}"
        )

    status, portfolio = client.get("/portfolio")
    check("GET /portfolio succeeds", status == 200, f"HTTP {status}")
    if status != 200:
        return {}

    equity = float(portfolio["equity"])
    starting = float(portfolio["starting_equity"])
    realized = float(portfolio["realized_pnl"])
    unrealized = float(portfolio["unrealized_pnl"])
    fees = float(portfolio["fees_paid"])

    note(
        f"equity {equity:.2f} (start {starting:.2f}), realised {realized:+.2f}, "
        f"unrealised {unrealized:+.2f}, fees {fees:.2f}, "
        f"{portfolio['open_positions']} open"
    )
    check("equity equals cash plus position value",
          abs(equity - (float(portfolio["cash"]) + float(portfolio["positions_value"]))) < 1e-6)
    if portfolio["open_positions"] > 0:
        check("open positions have paid fees", fees > 0, f"{fees:.4f}")
    for position in portfolio["positions"]:
        check(
            f"{position['symbol']} position carries a stop loss",
            position["stop_loss"] is not None,
            str(position["stop_loss"]),
        )
        check(
            f"{position['symbol']} position cites a risk approval",
            bool(position["risk_approval_id"]),
            str(position["risk_approval_id"]),
        )
    return portfolio


def stage_kill_switch(client: Client) -> None:
    section("7. Kill switch drill")
    status, halted = client.post(
        "/bot/halt",
        {"reason": "MANUAL", "detail": "smoke test drill", "source": "smoke-test"},
    )
    check("POST /bot/halt succeeds", status == 200, f"HTTP {status}")
    status, state = client.get("/bot/state")
    check("bot reports HALTED", state.get("status") == "HALTED", str(state.get("status")))

    status, body = client.post("/pipeline/run", {"symbols": ["BTC/USDT"], "timeframe": "1h"})
    if status == 200:
        traded = body.get("traded", 0)
        check("no new entry is taken while halted", traded == 0, f"traded={traded}")
    else:
        check("no new entry is taken while halted", status >= 400, f"HTTP {status}")

    status, _ = client.post(
        "/bot/reset",
        {"confirmation": "RESET", "operator": "smoke-test", "note": "drill complete"},
    )
    check("POST /bot/reset succeeds", status == 200, f"HTTP {status}")
    status, state = client.get("/bot/state")
    check("bot is RUNNING again", state.get("status") == "RUNNING", str(state.get("status")))


def stage_reporting(client: Client) -> None:
    section("8. Reporting and analytics")
    for path in ("/performance", "/performance/equity-curve", "/performance/trades"):
        status, _ = client.get(path)
        check(f"GET {path} succeeds", status == 200, f"HTTP {status}")

    status, report = client.post("/reports/daily", {"include_ai_summary": False})
    check("POST /reports/daily succeeds", status == 200, f"HTTP {status}")
    if status == 200:
        account = report["facts"]["account"]
        check(
            "the daily report says the books reconcile",
            account.get("books_balance") is True,
            f"residual {account.get('reconciliation_error')}",
        )


def stage_backtest(client: Client, symbol: str, timeframe: str) -> None:
    section("9. Backtest on the shared code path")
    status, body = client.post(
        "/backtest/run",
        {
            "data": {"symbol": symbol, "timeframe": timeframe, "limit": 1500},
            "starting_balance": 10000,
            "label": "paper smoke test",
            "persist": True,
        },
    )
    check("POST /backtest/run succeeds", status == 200, f"HTTP {status}")
    if status != 200:
        note(f"response: {_error_text(body)}")
        return
    metrics = body.get("metrics", {})
    check("the backtest consumed bars", body.get("bars", 0) > 0, f"{body.get('bars')} bars")
    note(
        f"source={body.get('data_source')} {body.get('bars')} bars, "
        f"{metrics.get('trades', 0)} trades, net P&L "
        f"{float(metrics.get('net_pnl', 0)):+.2f}, "
        f"win rate {metrics.get('win_rate')}, max DD {metrics.get('max_drawdown_pct')}"
    )
    for warning in body.get("warnings") or []:
        note(f"warning: {warning}")
    note(
        "A backtest is a wiring and no-look-ahead check. On synthetic data the P&L "
        "figure carries no information about real profitability whatsoever."
    )


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)[:160]
    return str(body)[:160]


def _summary() -> int:
    failures = [label for status, label in _results if status == FAIL]
    print(f"\n{'=' * 70}")
    print(f"{len(_results) - len(failures)} passed, {len(failures)} failed")
    for label in failures:
        print(f"  FAILED: {label}")
    print("=" * 70)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--timeframes", default="5m,15m,1h,4h")
    parser.add_argument("--skip-backtest", action="store_true")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    client = Client(args.base_url, args.api_key)

    print(f"Paper smoke test against {args.base_url}")

    health = stage_health(client)
    if health["mode"] != "paper" or health["live_trading_armed"]:
        print("\nABORTING: this service is not in disarmed paper mode.")
        return _summary() or 1

    stage_auth(client)
    stage_market_data(client, symbols, timeframes)
    stage_pipeline(client, symbols, args.timeframe)
    stage_risk_authority(client)
    stage_monitor_and_portfolio(client)
    stage_kill_switch(client)
    stage_reporting(client)
    if not args.skip_backtest:
        stage_backtest(client, symbols[0], args.timeframe)

    return _summary()


if __name__ == "__main__":
    sys.exit(main())
