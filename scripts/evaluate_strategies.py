#!/usr/bin/env python3
"""Backtest and walk-forward every configured symbol, then report honestly.

This answers one question: does the deterministic strategy layer have an edge
big enough to survive its own costs? It is deliberately willing to answer "no".

    python scripts/evaluate_strategies.py --api-key "$SERVICE_API_KEY"

Run it against a service on MARKET_DATA_PROVIDER=ccxt. It refuses to pretend a
synthetic run means anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

BAR_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float = 900.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
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
        except urllib.error.URLError as exc:
            return 0, f"could not reach {self.base_url}: {exc.reason}"


def fmt(value: Any, spec: str = ".3f", dash: str = "n/a") -> str:
    if value is None:
        return dash
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def cost_hurdle_r(config: dict[str, Any], risk_per_trade: float, position_pct: float) -> float:
    """R per round trip eaten by costs.

    Costs scale with POSITION size; risk scales with the stop distance. So the
    hurdle is (round-trip cost x notional) / (risk fraction x equity). Expressed
    in R, because that is the unit expectancy is measured in.
    """
    one_way_bps = (
        float(config.get("taker_fee_bps", 10))
        + float(config.get("slippage_bps", 5))
        + float(config.get("spread_bps", 4)) / 2.0
    )
    round_trip = 2.0 * one_way_bps / 10_000.0
    if risk_per_trade <= 0:
        return float("nan")
    return round_trip * position_pct / risk_per_trade


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=5000, help="bars of history")
    parser.add_argument("--skip-walkforward", action="store_true")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="run even on the synthetic fixture (results mean nothing)",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    client = Client(args.base_url, args.api_key)

    # ---------------------------------------------------------------- provider
    status, health = client.call("GET", "/health")
    if status != 200:
        print(f"Cannot reach the service: {health}")
        return 2

    provider = next(
        (c for c in health.get("components", []) if c["name"] == "market_data"), {}
    )
    detail = provider.get("detail", "")
    synthetic = "synthetic" in detail.lower()

    print(f"Service : {args.base_url}   mode={health.get('mode')}")
    print(f"Data    : {detail}")
    if synthetic and not args.allow_synthetic:
        print(
            "\nREFUSING TO RUN. This service is on the synthetic fixture, and a\n"
            "backtest against it measures the random number generator, not a market.\n"
            "Set MARKET_DATA_PROVIDER=ccxt and restart, or pass --allow-synthetic if\n"
            "you only want to check the plumbing."
        )
        return 2
    if synthetic:
        print("\n*** SYNTHETIC DATA — every number below is meaningless. ***")

    days = args.limit * BAR_SECONDS.get(args.timeframe, 3600) / 86400.0
    print(f"Window  : {args.limit} x {args.timeframe} bars requested (~{days:.0f} days)\n")

    # --------------------------------------------------------------- backtests
    rows: list[dict[str, Any]] = []
    hurdle: float | None = None

    for symbol in symbols:
        print(f"  backtesting {symbol} ...", flush=True)
        status, result = client.call(
            "POST",
            "/backtest/run",
            {
                "data": {
                    "symbol": symbol,
                    "timeframe": args.timeframe,
                    "source": "exchange",
                    "limit": args.limit,
                },
                "starting_balance": 10000,
                "label": f"eval {symbol} {args.timeframe}",
                "persist": True,
            },
        )
        if status != 200:
            print(f"    FAILED ({status}): {result}")
            rows.append({"symbol": symbol, "error": str(result)[:80]})
            continue

        metrics = result.get("metrics", {})
        benchmark = result.get("benchmark") or {}
        delivered = result.get("bars") or 0
        if delivered < args.limit * 0.9:
            print(
                f"    NOTE: asked for {args.limit} bars, got {delivered}"
                f" (~{delivered * BAR_SECONDS.get(args.timeframe, 3600) / 86400.0:.0f}"
                " days). The exchange has no more history for this symbol."
            )
        rows.append(
            {
                "symbol": symbol,
                "bars": result.get("bars"),
                "trades": metrics.get("trades"),
                "win_rate": metrics.get("win_rate"),
                "expectancy_r": metrics.get("expectancy_r"),
                "profit_factor": metrics.get("profit_factor"),
                "net_pnl": metrics.get("net_pnl"),
                "max_dd": metrics.get("max_drawdown_pct"),
                "hold_pct": benchmark.get("return_pct"),
                "hold_dd": benchmark.get("max_drawdown_pct"),
            }
        )
        if hurdle is None:
            config = result.get("config", {})
            status_l, limits = client.call("GET", "/risk/limits")
            position_pct = float((limits or {}).get("max_position_pct_equity", 0.20))
            hurdle = cost_hurdle_r(
                config, float(config.get("risk_per_trade", 0.005)), position_pct
            )

    # ------------------------------------------------------------------- table
    print(f"\n{'symbol':<12}{'trades':>8}{'win%':>7}{'exp_R':>9}{'PF':>7}"
          f"{'strat%':>9}{'maxDD%':>8}  |{'HOLD%':>9}{'maxDD%':>8}{'verdict':>10}")
    print("-" * 88)
    for row in rows:
        if "error" in row:
            print(f"{row['symbol']:<12}  ERROR: {row['error']}")
            continue
        win = row["win_rate"]
        strategy_pct = (
            float(row["net_pnl"]) / 10_000.0 * 100.0 if row["net_pnl"] is not None else None
        )
        hold_pct = float(row["hold_pct"]) * 100.0 if row.get("hold_pct") is not None else None
        if strategy_pct is None or hold_pct is None:
            verdict = "n/a"
        elif strategy_pct > hold_pct:
            verdict = "BEATS"
            row["beats_hold"] = True
        else:
            verdict = "loses to"
        print(
            f"{row['symbol']:<12}{row['trades'] or 0:>8}"
            f"{(fmt(win * 100, '.1f') if win is not None else 'n/a'):>7}"
            f"{fmt(row['expectancy_r']):>9}{fmt(row['profit_factor'], '.2f'):>7}"
            f"{fmt(strategy_pct, '+.2f'):>9}"
            f"{fmt(row['max_dd'] and float(row['max_dd']) * 100, '.2f'):>8}  |"
            f"{fmt(hold_pct, '+.2f'):>9}"
            f"{fmt(row.get('hold_dd') and float(row['hold_dd']) * 100, '.2f'):>8}"
            f"{verdict:>10}"
        )
    print("\nHOLD = buying at the start of the window and doing nothing, same costs.")
    print("A strategy that loses to HOLD took risk and paid fees to finish behind"
          " sitting still.")

    # ------------------------------------------------------------------ verdict
    print()
    if hurdle is not None and hurdle == hurdle:  # not NaN
        print(f"Cost hurdle: expectancy must exceed +{hurdle:.3f} R to break even.")
        print("(round-trip fees + slippage + half-spread, scaled by position size / risk)")

    scored = [r for r in rows if r.get("expectancy_r") is not None]
    thin = [r for r in rows if (r.get("trades") or 0) < 30]

    print("\nRead:")
    if not scored:
        print("  No symbol produced a usable result. Fix the errors above first.")
    else:
        beat_hold = [r for r in rows if r.get("beats_hold")]
        if not beat_hold:
            print("  NOT ONE symbol beat buy-and-hold. Whatever else is true, trading")
            print("  this actively destroyed value against doing nothing at all.")

        beat = [r for r in scored if hurdle is not None and float(r["expectancy_r"]) > hurdle]
        if not beat:
            print("  NO symbol clears its own costs. On this evidence the strategies")
            print("  do not have an edge. Do not fund this. That is a real answer,")
            print("  arrived at for free.")
        else:
            names = ", ".join(r["symbol"] for r in beat)
            print(f"  Clears the cost hurdle: {names}")
            print("  This is ONE window on ONE timeframe. It is not yet evidence of an")
            print("  edge -- run the walk-forward below, then paper trade for months.")
    if thin:
        names = ", ".join(f"{r['symbol']}({r.get('trades') or 0})" for r in thin)
        print(f"  Too few trades to mean anything: {names}. Under ~30 trades a result")
        print("  is indistinguishable from luck, and 30 is a floor, not a bar.")

    # ------------------------------------------------------------ walk-forward
    if args.skip_walkforward:
        return 0

    print("\n" + "=" * 72)
    print("WALK-FORWARD (tuned on early data, scored only on data it never saw)")
    print("=" * 72)

    for symbol in symbols:
        print(f"\n  {symbol} ...", flush=True)
        status, result = client.call(
            "POST",
            "/backtest/walkforward",
            {
                "data": {
                    "symbol": symbol,
                    "timeframe": args.timeframe,
                    "source": "exchange",
                    "limit": args.limit,
                },
                "train_bars": 1000,
                "validate_bars": 300,
                "test_bars": 300,
                "starting_balance": 10000,
                "label": f"wf {symbol} {args.timeframe}",
            },
        )
        if status != 200:
            print(f"    FAILED ({status}): {str(result)[:160]}")
            continue

        out_of_sample = result.get("aggregate_test_metrics", {})
        print(f"    windows      : {len(result.get('windows', []))}")
        print(f"    OOS trades   : {out_of_sample.get('trades')}")
        print(f"    OOS expect_R : {fmt(out_of_sample.get('expectancy_r'))}")
        print(f"    OOS profit f.: {fmt(out_of_sample.get('profit_factor'), '.2f')}")
        comparison = result.get("in_sample_vs_out_of_sample", {})
        if comparison:
            print(
                f"    train mean   : {fmt(comparison.get('train_mean'))}   "
                f"validate mean: {fmt(comparison.get('validate_mean'))}"
            )
            print(
                f"    pooled OOS   : {fmt(comparison.get('pooled_test_expectancy_r'))}"
                f"   (pooled, not an average of windows -- one lucky small window"
                f" must not carry the verdict)"
            )
        print(f"    VERDICT      : {result.get('verdict')}")

    print(
        "\nA big gap between in-sample and out-of-sample is the classic signature of\n"
        "curve fitting. Out-of-sample is the only column that counts."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
