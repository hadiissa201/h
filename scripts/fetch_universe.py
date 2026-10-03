"""Fetch daily bars for every liquid USDT spot pair, not a hand-picked few.

The Faber test died because the symbol list was three coins that had already
won. A ranking strategy needs breadth anyway -- deciles of 1000 stocks means
100 names per bucket, and you cannot form a decile from three assets -- so this
takes the whole exchange and lets the backtest decide what to hold at each
date.

One bias cannot be removed with this data source and is stated rather than
hidden: ccxt lists pairs that exist TODAY, so coins Binance has already
delisted are invisible. Any long-only result from this universe is therefore
optimistic. The long-short spread is far more robust to it, because both legs
are drawn from the same surviving set.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PAGE = 1000
EPOCH_MS = int(datetime(2013, 1, 1, tzinfo=UTC).timestamp() * 1000)
MAX_PAGES = 60

# Pairs whose price is pinned by design. A volatility ranking would put every
# one of them in the lowest bucket and measure nothing.
STABLE = {
    "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "PAX", "EUR", "GBP", "AUD",
    "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "VAI", "USDS",
    "SUSD", "USTC", "UST", "PYUSD", "EURI", "AEUR", "XUSD",
}
# Leveraged tokens and wrapped duplicates: not separate assets.
SKIP_SUFFIX = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")


def wanted(base: str) -> bool:
    if base in STABLE:
        return False
    return not any(base.endswith(s) for s in SKIP_SUFFIX)


def fetch_all(exchange, symbol: str, timeframe: str) -> list[list[float]]:
    bar_ms = exchange.parse_timeframe(timeframe) * 1000
    cursor, rows, seen = EPOCH_MS, [], set()
    for _ in range(MAX_PAGES):
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe,
                                     since=cursor, limit=PAGE)
        if not batch:
            break
        fresh = [bar for bar in batch if bar[0] not in seen]
        if not fresh:
            break
        seen.update(bar[0] for bar in fresh)
        rows.extend(fresh)
        last = batch[-1][0]
        if last <= cursor:
            break
        cursor = last + bar_ms
        if cursor > int(time.time() * 1000):
            break
    rows.sort(key=lambda bar: bar[0])
    return rows[:-1] if rows else rows          # drop the forming candle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--min-bars", type=int, default=400,
                        help="skip pairs too short to rank on trailing vol")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many pairs (0 = all)")
    parser.add_argument("--out", type=Path, default=Path("data/universe"))
    args = parser.parse_args()

    try:
        import ccxt
    except ImportError:
        print("ccxt is not installed. Run: pip install ccxt pandas")
        return 2

    exchange = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    markets = exchange.load_markets()

    symbols = sorted(
        sym for sym, m in markets.items()
        if m.get("spot") and m.get("active")
        and m.get("quote") == args.quote and wanted(m.get("base", ""))
    )
    if args.limit:
        symbols = symbols[: args.limit]

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(symbols)} active {args.quote} spot pairs after excluding "
          f"stablecoins and leveraged tokens\n")

    kept = skipped = failed = 0
    for index, symbol in enumerate(symbols, 1):
        try:
            rows = fetch_all(exchange, symbol, args.timeframe)
        except Exception as exc:
            print(f"  [{index}/{len(symbols)}] {symbol:<16} FAILED "
                  f"{type(exc).__name__}")
            failed += 1
            continue

        if len(rows) < args.min_bars:
            skipped += 1
            print(f"  [{index}/{len(symbols)}] {symbol:<16} only {len(rows)} "
                  f"bars, skipped", end="\r")
            continue

        path = args.out / f"{symbol.replace('/', '-')}_{args.timeframe}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for bar in rows:
                stamp = datetime.fromtimestamp(bar[0] / 1000, tz=UTC)
                writer.writerow([stamp.isoformat(), *bar[1:]])
        kept += 1
        start = datetime.fromtimestamp(rows[0][0] / 1000, tz=UTC)
        print(f"  [{index}/{len(symbols)}] {symbol:<16} {len(rows):>5} bars "
              f"from {start:%Y-%m}  (kept {kept})")

    print(f"\nkept {kept}, skipped {skipped} as too short, {failed} failed")
    print(f"-> {args.out}")
    if kept < 30:
        print(f"\nWARNING: {kept} assets is too few to rank into buckets. "
              f"Lower --min-bars or check the quote currency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
