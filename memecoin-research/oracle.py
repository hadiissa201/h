"""The best possible result, to find out whether the game is winnable at all.

Every strategy tested has lost. Before trying a twelfth, this asks a different
question: with PERFECT FORESIGHT -- buy the exact low, sell the exact high --
what is the ceiling? And how much of that ceiling is taken away by the one
constraint we measured rather than assumed, namely that an exit has to exist?

Three numbers, and the gaps between them are the whole point:

  CHART ORACLE      perfect timing, ignoring whether anyone would buy.
                    This is what a naive backtest reports.
  SELLABLE ORACLE   perfect timing, but every exit must land on a moment we
                    actually verified was sellable.
  OUR BOT           what the strategies achieved.

chart minus sellable is the cost of unsellability. sellable minus our bot is the
part that skill could in principle close. If the SELLABLE oracle is negative,
no entry rule can win -- not a better filter, not three agents agreeing, not
anything -- because the oracle already picks the best possible entry.

LIMITATION, stated plainly: the oracle can only use prices we recorded. Real
highs between observations are invisible to it, so this UNDERSTATES the true
ceiling. A negative result here is therefore suggestive rather than final; a
positive one is necessary but not sufficient.

Read-only.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, SimulatedExit, Token
from collector.paper import IMPLAUSIBLE_MULTIPLE, MAX_POOL_FRACTION

ENTRY_WINDOW_S = 1800.0      # same window the strategies used
NOTIONAL = 100.0
ROUND_TRIP_COST = 0.01       # fees both legs


@dataclass
class Best:
    token_id: int
    multiple: float = 0.0
    net_pct: float = 0.0
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    traded: bool = False


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def best_trade(observations: list[tuple[datetime, float, float]],
               sellable_at: dict[datetime, float] | None,
               detected: datetime, notional: float,
               stale_budget_s: float = 600.0) -> Best:
    """Highest achievable net return for one token.

    Walks forward once, holding the cheapest valid ENTRY seen so far, and at
    each later observation asks what selling there would have paid. An entry is
    valid only inside the entry window and only if the order fits the pool; an
    exit is valid only if `sellable_at` offers a verified sale near that moment
    (or always, for the chart oracle).
    """
    best = Best(token_id=0)
    cheapest: tuple[datetime, float, float] | None = None

    for moment, price, liquidity in observations:
        age = (moment - detected).total_seconds()

        # ---- could we have sold here, using an entry from STRICTLY earlier?
        # Evaluated before the entry is updated. Doing it the other way round
        # meant that on a token which only falls, every bar became the new
        # cheapest entry and no exit was ever considered, so the oracle
        # recorded no trade at all and the forced case was under-reported.
        sellable_now = cheapest is not None and moment > cheapest[0]

        if sellable_now:
            pass    # evaluated below
        if (age <= ENTRY_WINDOW_S and price > 0 and liquidity > 0
                and notional <= liquidity * MAX_POOL_FRACTION
                and (cheapest is None or price < cheapest[1])):
            if not sellable_now:
                cheapest = (moment, price, liquidity)
                continue
            pending_entry = (moment, price, liquidity)
        else:
            pending_entry = None

        if not sellable_now:
            continue

        # ---- could we have sold here?
        exit_impact = 0.0
        if sellable_at is not None:
            match = None
            for sim_ts, impact in sellable_at.items():
                gap = (moment - sim_ts).total_seconds()
                if 0 <= gap <= stale_budget_s:
                    if match is None or gap < match[0]:
                        match = (gap, impact)
            if match is None:
                if pending_entry is not None:
                    cheapest = pending_entry
                continue
            exit_impact = min(1.0, abs(match[1]))

        multiple = price / cheapest[1]
        if multiple > IMPLAUSIBLE_MULTIPLE:
            if pending_entry is not None:
                cheapest = pending_entry
            continue
        gross = notional * (multiple - 1.0)
        costs = (notional * ROUND_TRIP_COST
                 + max(0.0, notional + gross) * exit_impact)
        net_pct = (gross - costs) / notional * 100.0
        # Keep the best pair even when it LOSES. Recording only profitable ones
        # would make every token look like a winner and inflate the ceiling --
        # the oracle's right to decline a trade is reported separately.
        if not best.traded or net_pct > best.net_pct:
            best = Best(token_id=0, multiple=multiple, net_pct=net_pct,
                        entry_ts=cheapest[0], exit_ts=moment, traded=True)
        if pending_entry is not None:
            cheapest = pending_entry
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--notional", type=float, default=NOTIONAL)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)

    with Session(engine) as session:
        query = select(Token).order_by(Token.detected_ts)
        if args.since:
            query = query.where(
                Token.detected_ts >= datetime.fromisoformat(args.since).replace(tzinfo=UTC))
        if args.limit:
            query = query.limit(args.limit)
        tokens = session.scalars(query).all()
        print(f"Oracle over {len(tokens)} tokens at ${args.notional:,.0f} per position\n")

        chart: list[Best] = []
        sellable: list[Best] = []
        no_entry = 0

        for token in tokens:
            rows = session.execute(
                select(Observation.observed_ts, Observation.price_usd,
                       Observation.liquidity_usd)
                .where(Observation.token_id == token.id,
                       Observation.price_usd.is_not(None))
                .order_by(Observation.observed_ts)).all()
            observations = [(_aware(ts), float(p), float(liq or 0.0))
                            for ts, p, liq in rows if p and float(p) > 0]
            if len(observations) < 2:
                no_entry += 1
                continue

            sims = session.execute(
                select(SimulatedExit.simulated_ts, SimulatedExit.price_impact_pct)
                .where(SimulatedExit.token_id == token.id,
                       SimulatedExit.succeeded.is_(True))).all()
            verified = {_aware(ts): float(impact or 0.0) for ts, impact in sims}

            detected = _aware(token.detected_ts)
            with_chart = best_trade(observations, None, detected, args.notional)
            with_exit = best_trade(observations, verified, detected, args.notional)
            if with_chart.traded:
                chart.append(with_chart)
            else:
                no_entry += 1
            if with_exit.traded:
                sellable.append(with_exit)

    def report(name: str, best: list[Best], total: int) -> float | None:
        """Two framings, because they answer different questions.

        FORCED: trade every token the oracle can trade, best timing available.
        Says whether timing alone is enough.

        SELECTIVE: trade only the ones that end up profitable. This is the true
        ceiling for any entry filter, since a filter's best case is picking
        exactly the winners -- and no filter can beat perfect selection.
        """
        if not best:
            print(f"  {name:<22}no tradable token at all")
            return None
        rets = sorted(b.net_pct for b in best)
        forced = sum(rets) / len(rets)
        winners = [r for r in rets if r > 0]
        selective = sum(winners) / len(winners) if winners else 0.0
        big = sum(1 for b in best if b.multiple >= 3.0)
        huge = sum(1 for b in best if b.multiple >= 10.0)
        print(f"  {name}")
        print(f"    tokens tradable        {len(best):>7} of {total}")
        print(f"    FORCED  (trade all)    {forced:>+7.1f}%  median "
              f"{rets[len(rets) // 2]:>+7.1f}%")
        print(f"    SELECTIVE (pick wins)  {selective:>+7.1f}%  on "
              f"{len(winners)} tokens ({len(winners) / len(best) * 100:.0f}% "
              f"of tradable)")
        print(f"    best case reached      >=3x on {big}, >=10x on {huge}")
        return selective

    print("THE CEILING\n")
    chart_sel = report("CHART ORACLE -- ignores whether anyone would buy",
                       chart, len(tokens))
    print()
    sell_sel = report("SELLABLE ORACLE -- every exit verified sellable",
                      sellable, len(tokens))
    print(f"\n  our best strategy        {-37.3:>+7.1f}%  (stop_10, replay)")
    print(f"  {no_entry} tokens never offered a valid entry at all")

    if chart and sellable and chart_sel is not None and sell_sel is not None:
        chart_mean, sell_mean = chart_sel, sell_sel
        print(f"\n  cost of unsellability   {sell_mean - chart_mean:>+8.1f} points")
        print(f"  gap left for skill      {sell_mean - (-37.3):>+8.1f} points")
        print()
        if sell_mean <= 0:
            print("  THE GAME IS NOT WINNABLE FROM HERE. The oracle buys the exact")
            print("  low and sells the highest VERIFIED-SELLABLE high, and still")
            print("  loses. No entry rule can beat that, because the oracle is the")
            print("  best entry rule there is -- not a better filter, not three")
            print("  agents agreeing, not anything.")
        else:
            print("  A winnable game EXISTS at this ceiling. The gap above is what")
            print("  skill would have to close, and no strategy can exceed it.")
            print("  Note the oracle only sees prices we recorded, so the true")
            print("  ceiling is higher -- but so is the difficulty of reaching it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
