"""Download every daily bar the exchange has, and cache it to CSV.

Standalone on purpose: it talks to ccxt directly rather than through the
service's provider, so downloading data does not require the web service's
logging and settings stack to be installed.

Faber's rule needs a 10-month average before it says anything, so a 625-day
sample leaves about ten months of signal and one or two regime changes -- not a
test of a monthly strategy. This pages back to each pair's listing date
instead.

Writing CSV matters as much as the fetch. A backtest whose data silently
changes between runs cannot be checked, and "re-run it and see" stops being a
valid answer.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
DEFAULT_OUT = Path("data/history")

# Binance caps one OHLCV response at 1000 bars and returns fewer rather than
# erroring. Asking for 4000 in one call silently yields 1000 -- which is how a
# backtest ends up measuring a quarter of the window it claims.
PAGE = 1000
# Well before any crypto pair listed, so paging starts at the true beginning.
EPOCH_MS = int(datetime(2013, 1, 1, tzinfo=UTC).timestamp() * 1000)
MAX_PAGES = 60


def fetch_all(exchange, symbol: str, timeframe: str) -> list[list[float]]:
    """Page forward until the exchange stops returning new bars."""
    bar_ms = exchange.parse_timeframe(timeframe) * 1000
    cursor = EPOCH_MS
    rows: list[list[float]] = []
    seen: set[int] = set()

    for page in range(MAX_PAGES):
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe,
                                     since=cursor, limit=PAGE)
        if not batch:
            break
        fresh = [bar for bar in batch if bar[0] not in seen]
        for bar in fresh:
            seen.add(bar[0])
        rows.extend(fresh)
        if not fresh:
            break
        last = batch[-1][0]
        if last <= cursor:
            break                      # no forward progress; stop rather than spin
        cursor = last + bar_ms
        print(f"      page {page + 1}: {len(rows)} bars through "
              f"{datetime.fromtimestamp(last / 1000, tz=UTC):%Y-%m-%d}", end="\r")
        if cursor > int(time.time() * 1000):
            break

    rows.sort(key=lambda bar: bar[0])
    # The newest bar is still forming; its close is not a close.
    return rows[:-1] if rows else rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    try:
        import ccxt
    except ImportError:
        print("ccxt is not installed. Run: pip install ccxt pandas")
        return 2

    exchange = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Fetching every {args.timeframe} bar from {args.exchange}\n")
    failures = 0
    for symbol in args.symbols:
        print(f"  {symbol}")
        try:
            rows = fetch_all(exchange, symbol, args.timeframe)
        except Exception as exc:
            print(f"      FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        if not rows:
            print("      no data returned")
            failures += 1
            continue

        path = args.out / f"{symbol.replace('/', '-')}_{args.timeframe}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for bar in rows:
                stamp = datetime.fromtimestamp(bar[0] / 1000, tz=UTC)
                writer.writerow([stamp.isoformat(), *bar[1:]])

        start = datetime.fromtimestamp(rows[0][0] / 1000, tz=UTC)
        end = datetime.fromtimestamp(rows[-1][0] / 1000, tz=UTC)
        months = int((end - start).days / 30.44)
        verdict = ("enough for a 10-month rule" if months >= 36
                   else "TOO SHORT to conclude from")
        print(f"      {len(rows)} bars  {start:%Y-%m-%d} to {end:%Y-%m-%d}  "
              f"{(end - start).days / 365.25:.1f}y ({months} months)  {verdict}")
        print(f"      -> {path}")

    if failures:
        print(f"\n{failures} symbol(s) failed. A partial fetch makes the portfolio "
              f"backtest span a different window than intended.")
    return 1 if failures == len(args.symbols) else 0


if __name__ == "__main__":
    sys.exit(main())
