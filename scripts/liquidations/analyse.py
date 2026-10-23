"""Does price revert after a forced liquidation? Read-only.

Implements docs/LIQUIDATION_PLAN.md, pre-registered 2026-10-12. Written
before any return in this dataset had been looked at, which is the only time
it can be written honestly: every choice below -- horizons reported, costs
charged, what counts as a pass -- was fixed by that document, not by what the
numbers turned out to be.

What it does, in the plan's own order:

  1. Checks the four preconditions. Failing one means "not yet", which is a
     result rather than a delay, and no verdict is printed.
  2. Computes net return per cascade trade at EVERY horizon, after fees and
     both real spreads. All four are reported. Picking the best afterwards
     and showing it alone is the fitting the plan exists to prevent.
  3. Does the same for the random-entry control, and reports the difference
     with a bootstrap interval. The control is the point: crypto drifts, and
     a window that happened to rise makes every long look clever.

    python scripts/liquidations/analyse.py
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HORIZONS_S = (30.0, 60.0, 300.0, 900.0)

# Bybit linear perpetual taker fee, each way. The plan tabled Binance's
# 0.050% because that was the intended venue; collection runs on Bybit, whose
# taker fee is higher. Using the venue we actually sampled, and the higher
# number, is the conservative of the two -- charging Binance's fee to Bybit's
# data would flatter the result by 0.01% a round trip.
TAKER_FEE = 0.00055
ROUND_TRIP_FEES = 2 * TAKER_FEE

# Preconditions, restated from the plan so the arithmetic is visible.
CASCADES_NEEDED = 100
DAYS_NEEDED = 20
QUOTE_COVERAGE_NEEDED = 0.90
# Added 2026-10-23. The plan requires a control but set no size for it, and
# the first run computed a difference -- with a narrow-looking interval --
# against three control entries, because the control sampler had been live
# for forty minutes. This is a hurdle the plan omitted rather than one it
# set, and it makes the test harder to pass, never easier.
CONTROLS_NEEDED = 100
MAX_MEDIAN_LATENCY_S = 2.0

# Cascades cluster: the first live burst was three inside 140 seconds, one
# market move counted three times. Intervals are bootstrapped over EPISODES
# rather than cascades, because resampling correlated observations as if they
# were independent is what makes an interval too narrow.
EPISODE_GAP_S = 600.0

BOOTSTRAP_SAMPLES = 10_000
# Fixed so a rerun gives the same interval. A seed chosen after seeing which
# one produced a nicer answer would be fitting with extra steps.
SEED = 20261012


class Trade:
    """One entry, its exit at a horizon, and what it cost to take."""

    __slots__ = ("direction", "entry", "entry_spread", "exit", "symbol", "ts")

    def __init__(self, ts: float, symbol: str, direction: str,
                 entry: tuple[float, float], exit_: tuple[float, float]):
        self.ts = ts
        self.symbol = symbol
        self.direction = direction
        self.entry = entry
        self.exit = exit_
        bid, ask = entry
        self.entry_spread = (ask - bid) / ((ask + bid) / 2.0)

    def net_return(self, fees: float = ROUND_TRIP_FEES) -> float:
        """Return after crossing both spreads and paying both fees.

        A buy pays the ask going in and receives the bid coming out; a sell
        is the mirror. The spread is therefore already inside this number and
        must NOT be subtracted again -- doing so would charge it twice and
        is the easiest way to manufacture a loss, just as assuming a normal
        spread during a cascade is the easiest way to manufacture a profit.
        """
        entry_bid, entry_ask = self.entry
        exit_bid, exit_ask = self.exit
        if self.direction == "BUY":
            gross = (exit_bid - entry_ask) / entry_ask
        else:
            gross = (entry_bid - exit_ask) / entry_bid
        return gross - fees


def trade_direction(liquidation_side: str) -> str:
    """A liquidation's side, turned into the trade it argues for.

    Bybit reports the side of the position that was liquidated. A liquidated
    LONG is closed by SELLING, so the forced flow is selling and the
    candidate trade is to buy into it.
    """
    return "BUY" if liquidation_side.upper() == "SELL" else "SELL"


def quote_series(conn: sqlite3.Connection, column: str
                 ) -> dict[int, dict[float, tuple[float, float]]]:
    """Every quote, grouped by series id then by offset."""
    out: dict[int, dict[float, tuple[float, float]]] = defaultdict(dict)
    rows = conn.execute(
        f"SELECT {column}, offset_s, bid, ask FROM quotes "
        f"WHERE {column} IS NOT NULL AND bid IS NOT NULL AND ask IS NOT NULL"
    ).fetchall()
    for series_id, offset, bid, ask in rows:
        if bid > 0 and ask > 0:
            out[series_id][float(offset)] = (float(bid), float(ask))
    return out


def episodes(timestamps: list[float], gap_s: float = EPISODE_GAP_S
             ) -> list[int]:
    """Label each timestamp with an episode, collapsing bursts within gap_s."""
    labels: list[int] = []
    current = -1
    previous = None
    for ts in timestamps:
        if previous is None or ts - previous > gap_s:
            current += 1
        labels.append(current)
        previous = ts
    return labels


def cascade_trades(conn: sqlite3.Connection, horizon: float
                   ) -> tuple[list[Trade], list[int], int]:
    """Cascade trades at one horizon, their episode labels, and how many
    cascades had no usable quote pair."""
    series = quote_series(conn, "cascade_id")
    rows = conn.execute(
        "SELECT id, symbol, side, detected_ts FROM cascades "
        "ORDER BY detected_ts").fetchall()
    trades: list[Trade] = []
    stamps: list[float] = []
    missing = 0
    for cid, symbol, side, detected in rows:
        quotes = series.get(cid, {})
        entry, exit_ = quotes.get(0.0), quotes.get(horizon)
        if entry is None or exit_ is None:
            # Dropped, never counted as a zero return. A cascade whose
            # follow-up never arrived is one we cannot price, and scoring it
            # flat would pull every mean toward zero.
            missing += 1
            continue
        trades.append(Trade(float(detected), symbol,
                            trade_direction(side), entry, exit_))
        stamps.append(float(detected))
    return trades, episodes(stamps), missing


def control_direction(control_id: int, buy_fraction: float) -> str:
    """A control entry's direction, stable across every horizon.

    Drawn from the control's own id rather than a shared stream, so the same
    entry is the same trade at 30s and at 900s. With a shared generator the
    horizons would each get a fresh coin flip, and a difference between them
    could be the assignment rather than the market.
    """
    return ("BUY" if random.Random(SEED ^ (control_id * 2654435761)).random()
            < buy_fraction else "SELL")


def control_trades(conn: sqlite3.Connection, horizon: float,
                   buy_fraction: float) -> tuple[list[Trade], int]:
    """Random-time entries at one horizon.

    A control entry has no liquidation and so no direction of its own. Taking
    both ways and averaging would be worse than useless: a long's return is
    the negative of a short's before costs, so the average is exactly minus
    the fees, and the drift the control exists to measure cancels out. Each
    entry is therefore assigned a direction drawn from the cascades' own mix,
    which preserves the drift and the variance.
    """
    series = quote_series(conn, "control_id")
    rows = conn.execute(
        "SELECT id, symbol, started_ts FROM controls ORDER BY started_ts"
    ).fetchall()
    trades: list[Trade] = []
    missing = 0
    for ctl_id, symbol, started in rows:
        quotes = series.get(ctl_id, {})
        entry, exit_ = quotes.get(0.0), quotes.get(horizon)
        if entry is None or exit_ is None:
            missing += 1
            continue
        trades.append(Trade(float(started), symbol,
                            control_direction(ctl_id, buy_fraction),
                            entry, exit_))
    return trades, missing


def weighted_mean(by_symbol: dict[str, list[float]],
                  weights: dict[str, float]) -> float | None:
    """Mean over symbols, weighted to match the cascade arm's symbol mix.

    The control draws symbols uniformly; cascades do not arrive uniformly. An
    unweighted comparison would partly be a comparison of symbol mixes.
    """
    total = num = 0.0
    for symbol, weight in weights.items():
        values = by_symbol.get(symbol)
        if values:
            total += weight * statistics.fmean(values)
            num += weight
    return total / num if num else None


def group(trades: list[Trade]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        out[t.symbol].append(t.net_return())
    return out


def bootstrap_difference(cascades: list[Trade], labels: list[int],
                         controls: list[Trade], weights: dict[str, float],
                         rng: random.Random,
                         samples: int = BOOTSTRAP_SAMPLES
                         ) -> tuple[float, float] | None:
    """95% interval for cascade mean minus control mean.

    Cascades are resampled by EPISODE, keeping each cluster whole, because
    three cascades from one market move are one observation and resampling
    them separately would narrow the interval by pretending otherwise.
    """
    if not cascades or not controls:
        return None
    clusters: dict[int, list[Trade]] = defaultdict(list)
    for trade, label in zip(cascades, labels):
        clusters[label].append(trade)
    keys = list(clusters)
    control_by_symbol = group(controls)

    diffs: list[float] = []
    for _ in range(samples):
        drawn: list[Trade] = []
        for _ in range(len(keys)):
            drawn.extend(clusters[rng.choice(keys)])
        c_mean = weighted_mean(group(drawn), weights)
        resampled = {
            sym: [rng.choice(vals) for _ in vals]
            for sym, vals in control_by_symbol.items() if vals
        }
        k_mean = weighted_mean(resampled, weights)
        if c_mean is not None and k_mean is not None:
            diffs.append(c_mean - k_mean)
    if not diffs:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))]
    return lo, hi


def distinct_days(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT detected_ts FROM cascades").fetchall()
    return len({datetime.fromtimestamp(r[0], timezone.utc).date()
                for r in rows})


def median_latency(conn: sqlite3.Connection) -> float | None:
    rows = conn.execute(
        "SELECT received_ts - event_ts FROM liquidations").fetchall()
    gaps = [r[0] for r in rows if r[0] is not None]
    return statistics.median(gaps) if gaps else None


def check_preconditions(conn: sqlite3.Connection, total_cascades: int,
                        coverage: dict[float, float],
                        total_controls: int) -> list[tuple]:
    """The plan's four, each as (name, pass, detail)."""
    days = distinct_days(conn)
    latency = median_latency(conn)
    worst_cov = min(coverage.values()) if coverage else 0.0
    return [
        ("at least 100 cascades", total_cascades >= CASCADES_NEEDED,
         f"{total_cascades} of {CASCADES_NEEDED}"),
        ("at least 20 distinct days", days >= DAYS_NEEDED,
         f"{days} of {DAYS_NEEDED}"),
        ("follow-up quotes on 90% of cascades",
         worst_cov >= QUOTE_COVERAGE_NEEDED,
         f"worst horizon {worst_cov:.1%} of {QUOTE_COVERAGE_NEEDED:.0%}"),
        ("at least 100 control entries", total_controls >= CONTROLS_NEEDED,
         f"{total_controls} of {CONTROLS_NEEDED}"),
        ("median latency under 2s",
         latency is not None and latency < MAX_MEDIAN_LATENCY_S,
         "no liquidations" if latency is None else f"{latency:.2f}s"),
    ]


def verdict(interval: tuple[float, float] | None) -> tuple[str, str]:
    """The plan's table, applied mechanically."""
    if interval is None:
        return "NO DATA", "nothing to conclude"
    lo, hi = interval
    if lo > 0:
        return "CANDIDATE", "interval entirely above 0 -- go to the second test"
    if hi < 0:
        return "DEAD", "interval entirely below 0 -- stop"
    return "UNPROVEN", "interval straddles 0 -- keep collecting, no money either way"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/liquidations.sqlite")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(path)
    rng = random.Random(SEED)

    total_cascades = conn.execute(
        "SELECT count(*) FROM cascades").fetchone()[0]
    if not total_cascades:
        print("No cascades recorded yet.")
        return 0

    sides = conn.execute("SELECT side FROM cascades").fetchall()
    buys = sum(1 for (s,) in sides if trade_direction(s) == "BUY")
    buy_fraction = buys / len(sides)

    per_horizon = {}
    coverage = {}
    for horizon in HORIZONS_S:
        trades, labels, missing = cascade_trades(conn, horizon)
        coverage[horizon] = (len(trades) / total_cascades
                             if total_cascades else 0.0)
        controls, c_missing = control_trades(conn, horizon, buy_fraction)
        per_horizon[horizon] = (trades, labels, missing, controls, c_missing)

    print("=" * 70)
    print("PRECONDITIONS (docs/LIQUIDATION_PLAN.md)")
    print("=" * 70)
    total_controls = conn.execute(
        "SELECT count(*) FROM controls").fetchone()[0]
    checks = check_preconditions(conn, total_cascades, coverage,
                                 total_controls)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name}: {detail}")
    all_ok = all(ok for _, ok, _ in checks)

    weights = defaultdict(float)
    for (symbol,) in conn.execute("SELECT symbol FROM cascades"):
        weights[symbol] += 1.0 / total_cascades

    print("\n" + "=" * 70)
    print("NET RETURN PER TRADE, ALL HORIZONS")
    print("=" * 70)
    print(f"  costs: {ROUND_TRIP_FEES:.3%} fees round trip, plus both")
    print("  spreads, paid by entering at the ask and exiting at the bid")
    print(f"  cascade direction mix: {buy_fraction:.0%} buy, "
          f"{1 - buy_fraction:.0%} sell\n")
    print(f"  {'horizon':>8}  {'casc n':>6}  {'ctrl n':>6}  {'cascade':>9}  "
          f"{'control':>9}  {'difference':>10}  {'95% interval':>22}")

    intervals = {}
    for horizon in HORIZONS_S:
        trades, labels, missing, controls, _ = per_horizon[horizon]
        c_mean = weighted_mean(group(trades), weights)
        k_mean = weighted_mean(group(controls), weights)
        interval = bootstrap_difference(trades, labels, controls, weights, rng)
        intervals[horizon] = interval
        if c_mean is None or k_mean is None:
            print(f"  {horizon:>7.0f}s  {len(trades):>6}  {len(controls):>6}  "
                  f"{'--':>9}  {'--':>9}  {'--':>10}  {'--':>22}")
            continue
        span = (f"[{interval[0]:+.3%}, {interval[1]:+.3%}]"
                if interval else "--")
        print(f"  {horizon:>7.0f}s  {len(trades):>6}  {len(controls):>6}  "
              f"{c_mean:>+9.3%}  {k_mean:>+9.3%}  {c_mean - k_mean:>+10.3%}  "
              f"{span:>22}")

    print("\n" + "=" * 70)
    print("WHAT THE SPREAD ACTUALLY COST")
    print("=" * 70)
    print("  The plan left this to measurement: a cascade is exactly when")
    print("  liquidity is worst, so assuming a normal spread would be the")
    print("  single most flattering error available.")
    print("  Split by symbol, because tick size dominates the relative")
    print("  spread: BTC's $0.10 tick near $81,000 is 0.0001%, SOL's $0.01")
    print("  near $104 is 0.0096%. Pooled, a cascade mix weighted toward BTC")
    print("  against controls drawn uniformly reads as cascades having far")
    print("  TIGHTER spreads -- a comparison of symbols dressed as one of")
    print("  liquidity, and in the flattering direction.\n")
    entry_trades = per_horizon[HORIZONS_S[0]][0]
    control_entry = per_horizon[HORIZONS_S[0]][3]
    by_symbol: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"cascade": [], "control": []})
    for t in entry_trades:
        by_symbol[t.symbol]["cascade"].append(t.entry_spread)
    for t in control_entry:
        by_symbol[t.symbol]["control"].append(t.entry_spread)
    print(f"    {'symbol':<10}{'cascade':>22}{'random':>22}")
    for symbol in sorted(by_symbol):
        cells = []
        for arm in ("cascade", "control"):
            vals = by_symbol[symbol][arm]
            cells.append(f"{statistics.median(vals):.4%} (n={len(vals)})"
                         if vals else "--")
        print(f"    {symbol:<10}{cells[0]:>22}{cells[1]:>22}")

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    if not all_ok:
        print("  NOT YET. A precondition fails, so the plan gives no verdict.")
        print("  That is a result, not a delay. The numbers above are shown")
        print("  so collection can be steered, and must not be read as an")
        print("  answer -- reading them as one is what the preconditions")
        print("  exist to stop.")
        return 0
    for horizon in HORIZONS_S:
        name, why = verdict(intervals[horizon])
        print(f"  {horizon:>7.0f}s  {name:<10} {why}")
    print("\n  All four horizons are reported above. The plan forbids")
    print("  selecting one afterwards and presenting it alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
