"""Download as much daily history as the exchange will give, and cache it.

Faber's rule needs a 10-month average before it can say anything, so a 625-day
sample leaves roughly ten months of signal and one or two regime changes. That
is not a test of a monthly strategy. This fetches years instead, and writes CSV
so every later run reads the same bars -- a backtest whose data moves between
runs cannot be checked.

Binance history starts when each pair listed, not when the asset existed:
BTC/USDT and ETH/USDT around August 2017, SOL/USDT around August 2020. The
script reports what it actually got per symbol rather than assuming.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
DEFAULT_OUT = Path("data/history")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--days", type=int, default=4000,
                        help="bars to request; the exchange returns fewer if "
                             "the history does not exist")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python-service"))
    from app.data.providers.ccxt_provider import CcxtMarketDataProvider

    provider = CcxtMarketDataProvider(exchange_id=args.exchange)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {args.timeframe} bars from {args.exchange}\n")
    failures = 0
    for symbol in args.symbols:
        try:
            frame = provider.fetch_ohlcv(symbol, args.timeframe, limit=args.days)
        except Exception as exc:
            print(f"  {symbol:<10} FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        if frame.empty:
            print(f"  {symbol:<10} no data returned")
            failures += 1
            continue

        path = args.out / f"{symbol.replace('/', '-')}_{args.timeframe}.csv"
        frame.to_csv(path, index=False)
        start, end = frame["timestamp"].iloc[0], frame["timestamp"].iloc[-1]
        years = (end - start).days / 365.25
        months = int((end - start).days / 30.44)
        verdict = "enough for a 10-month rule" if months >= 36 else "TOO SHORT"
        print(f"  {symbol:<10} {len(frame):>5} bars  "
              f"{start:%Y-%m-%d} to {end:%Y-%m-%d}  "
              f"{years:4.1f}y ({months} months)  {verdict}")
        print(f"             -> {path}")

    if failures:
        print(f"\n{failures} symbol(s) failed. A partial fetch makes a portfolio "
              f"backtest span a different window than intended.")
    return 1 if failures == len(args.symbols) else 0


if __name__ == "__main__":
    sys.exit(main())
