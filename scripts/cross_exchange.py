"""Is there a cross-exchange spread worth capturing at retail size? Read-only.

docs/FINDINGS.md names four structural reasons an edge could exist, and says
any future idea must arrive with a reason why it should work. Cross-exchange
arbitrage has one: the same asset priced differently on two venues is a real
dislocation, not a pattern drawn on a chart. Two of the four have never been
tested. This tests the cheaper one.

Public endpoints only. No API keys, no orders, no wallet. The script reads
order books and does arithmetic.

Three things it refuses to get wrong, each of which this project has paid for
once already:

TOP OF BOOK IS A QUOTE, NOT A FILL. The best bid is for whatever size happens
to sit there. Capturing $1,000 means walking down the book and paying a worse
average price, exactly as a memecoin's quoted price came from virtual reserves
while the payout came from real ones. The edge here is computed on the volume
weighted price for the actual notional.

THE RETURN IS ON CAPITAL, NOT NOTIONAL. Capturing a spread without waiting for
a blockchain transfer means holding the asset on one venue and the quote
currency on the other, so a single $1,000 capture ties up roughly $2,000. The
funding-carry result looked like 4% until the same correction took it to 3%.

A SPREAD YOU CANNOT REACH IN TIME IS NOT AN EDGE. Between seeing both books
and both orders landing, the price moves. Sampling repeatedly shows whether a
positive edge is still positive moments later, which is the difference between
an opportunity and a screenshot.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass

from collector_stats import wilson

# Published taker fees at the lowest VIP tier, spot, no token discounts. These
# are the honest numbers for a retail account; a lower tier is not available
# without volume nobody starting out has.
DEFAULT_TAKER_FEES = {
    "binance": 0.0010,
    "kraken": 0.0026,
    "coinbase": 0.0060,
    "bybit": 0.0010,
    "okx": 0.0010,
}
FALLBACK_TAKER_FEE = 0.0026

# Below this there is no distribution to describe, only anecdotes.
MIN_SAMPLES = 30


@dataclass(frozen=True)
class Fill:
    """The price actually paid or received for a given notional."""
    vwap: float
    filled_notional: float
    levels_used: int
    complete: bool


def walk_book(levels: list[list[float]], notional: float) -> Fill | None:
    """Volume-weighted price for `notional`, walking down the book.

    levels is [[price, amount], ...] best first. Returns None if the book is
    empty. `complete` is False when the book cannot absorb the size, which is
    a finding rather than a reason to pretend the rest filled at the last
    price -- the memecoin run had a $100 order against a $1 pool reported as a
    clean fill.
    """
    if not levels:
        return None
    spent = 0.0
    base = 0.0
    used = 0
    for price, amount in levels:
        price, amount = float(price), float(amount)
        if price <= 0 or amount <= 0:
            continue
        used += 1
        room = notional - spent
        level_value = price * amount
        if level_value >= room:
            base += room / price
            spent = notional
            break
        spent += level_value
        base += amount
    if base <= 0:
        return None
    return Fill(vwap=spent / base, filled_notional=spent,
                levels_used=used, complete=spent >= notional * 0.999)


def net_edge(buy: Fill, buy_fee: float, sell: Fill, sell_fee: float) -> float:
    """Profit as a fraction of the notional traded, after both taker fees.

    Buy the base on one venue, sell it on the other. Both legs are takers:
    crossing the spread is what makes the capture immediate, and a maker order
    that may not fill is a different strategy with different risk.
    """
    if buy.vwap <= 0 or sell.vwap <= 0:
        return 0.0
    gross = (sell.vwap - buy.vwap) / buy.vwap
    return gross - buy_fee - sell_fee


def capital_multiple(legs: int = 2) -> float:
    """Capital tied up per unit of notional captured.

    Avoiding transfer latency means pre-positioning both sides: the base asset
    where you will sell it, the quote where you will buy. One $1,000 capture
    therefore needs about $2,000 standing ready.
    """
    return float(legs)


def summarise(edges: list[float], notional: float, fees: dict) -> dict:
    """Describe the distribution, or say it cannot be described."""
    if len(edges) < MIN_SAMPLES:
        return {"enough": False, "n": len(edges)}
    positive = sum(1 for e in edges if e > 0)
    on_capital = [e / capital_multiple() for e in edges]
    return {
        "enough": True,
        "n": len(edges),
        "positive": positive,
        "median": statistics.median(edges),
        "best": max(edges),
        "worst": min(edges),
        "median_on_capital": statistics.median(on_capital),
        "fee_hurdle": sum(fees.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchanges", nargs=2, default=["binance", "kraken"])
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between samples. Also the window over "
                             "which a positive edge must persist to be real")
    args = parser.parse_args()

    try:
        import ccxt
    except ImportError:
        print("ccxt is required: pip install ccxt")
        return 1

    venues = {}
    fees = {}
    for name in args.exchanges:
        if not hasattr(ccxt, name):
            print(f"ccxt has no exchange named {name}")
            return 1
        venues[name] = getattr(ccxt, name)({"enableRateLimit": True})
        fees[name] = DEFAULT_TAKER_FEES.get(name, FALLBACK_TAKER_FEE)

    a, b = args.exchanges
    print("=" * 70)
    print("CROSS-EXCHANGE SPREAD, NET OF WHAT IT COSTS TO TAKE IT")
    print("=" * 70)
    print(f"  {args.symbol} on {a} and {b}, ${args.notional:,.0f} per capture")
    print(f"  taker fees: {a} {fees[a] * 100:.3f}%, {b} {fees[b] * 100:.3f}%")
    print(f"  round-trip fee hurdle: {sum(fees.values()) * 100:.3f}%")
    print(f"  sampling {args.samples} times, {args.interval:.0f}s apart\n")

    edges: list[float] = []
    incomplete = 0
    errors = 0
    previous_positive = None
    persisted = 0
    chances = 0

    for i in range(args.samples):
        try:
            book_a = venues[a].fetch_order_book(args.symbol, limit=50)
            book_b = venues[b].fetch_order_book(args.symbol, limit=50)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors <= 3:
                print(f"  sample {i + 1}: {type(exc).__name__}: {exc}")
            time.sleep(args.interval)
            continue

        # Both directions: buy where it is cheap, sell where it is dear.
        best = None
        for buy_venue, sell_venue in ((a, b), (b, a)):
            buy_book = book_a if buy_venue == a else book_b
            sell_book = book_a if sell_venue == a else book_b
            buy = walk_book(buy_book.get("asks") or [], args.notional)
            sell = walk_book(sell_book.get("bids") or [], args.notional)
            if buy is None or sell is None:
                continue
            if not (buy.complete and sell.complete):
                incomplete += 1
                continue
            edge = net_edge(buy, fees[buy_venue], sell, fees[sell_venue])
            if best is None or edge > best[0]:
                best = (edge, buy_venue, sell_venue)
        if best is None:
            continue
        edge = best[0]
        edges.append(edge)

        if previous_positive is not None:
            chances += 1
            if previous_positive and edge > 0:
                persisted += 1
        previous_positive = edge > 0

        if (i + 1) % 10 == 0 or edge > 0:
            flag = "  <- POSITIVE" if edge > 0 else ""
            print(f"  sample {i + 1:>3}: best {edge * 100:+.4f}% "
                  f"(buy {best[1]}, sell {best[2]}){flag}")
        time.sleep(args.interval)

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)
    stats = summarise(edges, args.notional, fees)
    if not stats["enough"]:
        print(f"  Only {stats['n']} usable samples, below the {MIN_SAMPLES} "
              f"needed.")
        print("  Not a result either way. Run longer.")
        return 0

    low, high = wilson(stats["positive"], stats["n"])
    print(f"  {stats['n']} samples, {stats['positive']} with a positive net edge")
    print(f"  {stats['positive'] / stats['n'] * 100:.1f}% "
          f"[{low * 100:.1f}%, {high * 100:.1f}%]")
    print(f"\n  median net edge on notional  {stats['median'] * 100:+.4f}%")
    print(f"  median net edge on CAPITAL   "
          f"{stats['median_on_capital'] * 100:+.4f}%   "
          f"(both sides pre-positioned)")
    print(f"  best observed                {stats['best'] * 100:+.4f}%")
    if incomplete:
        print(f"\n  {incomplete} book sides could not absorb ${args.notional:,.0f}")
        print("  and were skipped rather than filled at the last price.")
    if chances:
        print(f"\n  a positive edge was still positive {args.interval:.0f}s later "
              f"{persisted}/{chances} times")
        if persisted == 0:
            print("  Never. Anything seen was gone before it could be reached.")

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    if high <= 0.0:
        print("  NO EDGE. The net spread was never positive, so there is")
        print("  nothing to capture at this size on these venues.")
    elif stats["median"] > 0:
        print("  CANDIDATE. The median capture is positive after fees, which")
        print("  is the first spot result in this project that is. It is not")
        print("  yet a strategy: latency, partial fills, and the exchange")
        print("  risk of holding inventory on two venues are all unmodelled,")
        print("  and the persistence figure above is what decides whether a")
        print("  real order could have reached it.")
    else:
        print("  NOT WORTH TAKING. Positive edges occur but the median")
        print("  capture loses money, so a rule that trades whenever the")
        print("  spread looks positive pays for the misses with the hits.")
        print("  Only a rule that can tell them apart in advance would work,")
        print("  and nothing here is evidence such a rule exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
