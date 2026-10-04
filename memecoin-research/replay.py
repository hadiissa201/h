"""Replay any strategy over history already collected. Read-only.

Why this exists: adding a strategy to the live collector means waiting days for
positions to accumulate. But the observations and sellability verdicts for
every token we have ever tracked are already on disk, timestamped. A strategy
can be judged against all of them in seconds.

The correctness requirement is that the replay decides exactly as the live
trader does. It does not carry its own copy of the rules -- it calls the same
`exit_trigger` and `trend_broken_from_prices` the running collector calls, and
a test asserts the two agree on identical input. A replay with its own
interpretation of a strategy measures the replay.

Discipline that makes a replay honest rather than flattering:

  - At every decision point, only rows timestamped at or before that moment are
    visible. That is enforced by walking forward through the observations, not
    by filtering after the fact.
  - A sale still requires a sellability verdict from DURING the hold, under the
    same staleness budget as live. An exit nobody could have taken is not an
    exit, which is the entire premise of this project.
  - Price impact is charged on both legs and the order must fit the pool, as
    live. Without that the replay reproduces the bugs the audit removed.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Observation, Token
from collector.paper import (
    IMPLAUSIBLE_MULTIPLE,
    MAX_POOL_FRACTION,
    Strategy,
    exit_available,
    exit_trigger,
    trend_broken_from_prices,
    verdict_age_budget,
)


@dataclass
class ReplayPosition:
    token_id: int
    strategy: str
    opened_ts: datetime
    entry_price: float
    entry_impact: float
    exit_ts: datetime | None = None
    exit_price: float | None = None
    reason: str | None = None
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    blocked_no_route: int = 0
    blocked_our_fault: int = 0
    peak_multiple: float = 1.0
    exit_impact: float = 0.0
    costs_gross: float = 0.0
    floored: bool = False


@dataclass
class ReplayResult:
    strategy: str
    closed: list[ReplayPosition] = field(default_factory=list)
    still_open: int = 0
    never_entered: int = 0
    # Which gate turned each token away. 8 entries from 241 tokens is either a
    # strict filter or a broken one, and the counts say which.
    rejected: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def wins(self) -> int:
        return sum(1 for p in self.closed if p.net > 0)

    @property
    def mean_return_pct(self) -> float | None:
        if not self.closed:
            return None
        return sum(p.net / 100.0 for p in self.closed) / len(self.closed)

    def confidence_interval(self) -> tuple[float, float] | None:
        """95% interval on the mean. A mean without one is not a result."""
        n = len(self.closed)
        if n < 2:
            return None
        rets = [p.net / 100.0 for p in self.closed]
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        se = (var / n) ** 0.5
        return (mean - 1.96 * se, mean + 1.96 * se)

    @property
    def floored(self) -> int:
        return sum(1 for p in self.closed if p.floored)

    def impact_summary(self) -> str:
        """Entry and exit impact actually used, so an absurd figure is visible.

        A $100 order in a funded pool cannot move the price 100%. When it says
        it did, the quote behind it was for a different trade -- and the audit
        found 1,121 stored quotes claiming exactly that.
        """
        if not self.closed:
            return "no closed positions"
        entries = sorted(p.entry_impact for p in self.closed)
        exits = sorted(p.exit_impact for p in self.closed)
        mid = len(entries) // 2
        return (f"entry impact median {entries[mid]:.1%} (max {entries[-1]:.1%}), "
                f"exit impact median {exits[mid]:.1%} (max {exits[-1]:.1%})")

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for position in self.closed:
            counts[position.reason or "?"] += 1
        return dict(counts)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def replay_token(session: Session, token: Token, strategy: Strategy,
                 settings,  # noqa: ANN001
                 gates: dict[str, int] | None = None,
                 ) -> tuple[ReplayPosition | None, bool]:
    """Walk one token's observations once, as the live trader would have.

    Returns (position, entered). A position with no exit_ts never found a
    sellable moment while its exit rule was firing -- which is a result, not a
    gap, and is counted rather than dropped.
    """
    gates = gates if gates is not None else defaultdict(int)
    rows = session.scalars(
        select(Observation)
        .where(Observation.token_id == token.id, Observation.price_usd.is_not(None))
        .order_by(Observation.observed_ts)).all()
    if len(rows) < 2:
        gates["too_few_observations"] += 1
        return None, False

    detected = _aware(token.detected_ts)
    position: ReplayPosition | None = None
    prices: list[tuple[datetime, float]] = []

    for obs in rows:
        moment = _aware(obs.observed_ts)
        price = float(obs.price_usd)
        if price <= 0:
            continue
        prices.append((moment, price))
        liquidity = float(obs.liquidity_usd or 0.0)
        age = (moment - detected).total_seconds()

        if position is None:
            # ---- entry, using only what was visible at `moment`
            if age > strategy.max_age_s or age < strategy.min_age_s:
                gates["age"] += 1
                continue
            if not (strategy.min_liquidity_usd <= liquidity
                    <= strategy.max_liquidity_usd):
                gates["liquidity_band"] += 1
                continue
            if liquidity <= 0 or strategy.notional_usd > liquidity * MAX_POOL_FRACTION:
                gates["order_too_big_for_pool"] += 1
                continue
            if (obs.buys_5m or 0) < strategy.min_buys_5m:
                gates["too_few_buys"] += 1
                continue
            budget = verdict_age_budget(settings, strategy, age)
            proven = exit_available(session, token.id, moment, budget)
            if strategy.require_proven_exit and proven is None:
                gates["no_proven_exit"] += 1
                continue
            impact = (min(1.0, abs(float(proven.price_impact_pct or 0.0)))
                      if proven is not None else 0.0)
            position = ReplayPosition(
                token_id=token.id, strategy=strategy.name, opened_ts=moment,
                entry_price=price, entry_impact=impact)
            continue

        # ---- management
        multiple = price / position.entry_price if position.entry_price else 0.0
        if multiple > IMPLAUSIBLE_MULTIPLE or multiple < 0:
            continue
        position.peak_multiple = max(position.peak_multiple, multiple)
        held_s = (moment - position.opened_ts).total_seconds()

        window_start = moment - timedelta(seconds=strategy.trend_window_s)
        window = [p for ts, p in prices if window_start <= ts <= moment]
        reason = exit_trigger(
            strategy, multiple, held_s,
            trend_broken=trend_broken_from_prices(strategy, window, price, held_s),
            peak_multiple=position.peak_multiple)
        if reason is None:
            continue

        budget = verdict_age_budget(settings, strategy, held_s)
        sellable = exit_available(session, token.id, moment, budget)
        if (sellable is not None and strategy.require_verdict_after_entry
                and _aware(sellable.simulated_ts) <= position.opened_ts):
            sellable = None
        if sellable is None:
            position.blocked_our_fault += 1
            continue

        exit_impact = min(1.0, abs(float(sellable.price_impact_pct or 0.0)))
        notional = strategy.notional_usd
        gross = notional * (multiple - 1.0)
        fees = notional * strategy.round_trip_cost_pct
        costs = (fees + notional * position.entry_impact
                 + max(0.0, notional + gross) * exit_impact)
        position.exit_impact = exit_impact
        position.costs_gross = costs
        if gross - costs < -notional:
            # The stake is the most that can be lost. Hitting this means the
            # modelled costs exceeded the position -- which is a statement
            # about the cost inputs, not about the trade.
            costs = gross + notional
            position.floored = True
        position.exit_ts, position.exit_price, position.reason = moment, price, reason
        position.gross, position.costs = gross, costs
        position.net = gross - costs
        return position, True

    return position, position is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None,
                        help="only tokens detected on/after this date "
                             "(YYYY-MM-DD), to exclude pre-fix history")
    parser.add_argument("--strategies", nargs="+", default=None,
                        help="names to replay; default is the trend triplet")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url, future=True)

    from collector.paper import ALL_STRATEGIES
    wanted = (set(args.strategies) if args.strategies
              else {"fixed_only", "trend_5m", "trend_30m", "control_any"})
    chosen = [s for s in ALL_STRATEGIES if s.name in wanted]
    missing = wanted - {s.name for s in chosen}
    if missing:
        print(f"unknown strategies: {sorted(missing)}")
        return 2

    with Session(engine) as session:
        query = select(Token).order_by(Token.detected_ts)
        if args.since:
            cutoff = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
            query = query.where(Token.detected_ts >= cutoff)
        if args.limit:
            query = query.limit(args.limit)
        tokens = session.scalars(query).all()

        print(f"Replaying {len(chosen)} strategies over {len(tokens)} tokens"
              + (f" detected since {args.since}" if args.since else "")
              + "\n")

        results = {}
        for strategy in chosen:
            result = ReplayResult(strategy=strategy.name)
            for token in tokens:
                position, entered = replay_token(session, token, strategy,
                                                 settings, result.rejected)
                if not entered or position is None:
                    result.never_entered += 1
                elif position.exit_ts is None:
                    result.still_open += 1
                else:
                    result.closed.append(position)
            results[strategy.name] = result

    print(f"{'strategy':<14}{'closed':>8}{'stuck':>7}{'wins':>6}{'floored':>9}"
          f"{'mean':>10}{'95% CI':>22}")
    for name, result in results.items():
        mean = result.mean_return_pct
        ci = result.confidence_interval()
        mean_txt = "    n/a" if mean is None else f"{mean * 100:+8.2f}%"
        ci_txt = ("                 n/a" if ci is None
                  else f"[{ci[0] * 100:+7.2f}%, {ci[1] * 100:+7.2f}%]")
        print(f"{name:<14}{len(result.closed):>8}{result.still_open:>7}"
              f"{result.wins:>6}{result.floored:>9}{mean_txt:>10}{ci_txt:>22}")

    # A mean of exactly -100% with no spread is the floor, not a return.
    saturated = [n for n, r in results.items()
                 if r.closed and r.floored == len(r.closed)]
    if saturated:
        print(f"\n  NOT A RESULT: every closed position hit the loss floor for "
              f"{', '.join(saturated)}.")
        print("  A mean of -100% with a zero-width interval means the modelled")
        print("  costs exceeded the stake on every trade, so the floor set the")
        print("  answer and the strategy never did. Read the impacts below: a")
        print("  $100 order cannot move a funded pool 100%, and when the stored")
        print("  quote says it did, that quote was for a different trade.")

    print("\ncost inputs")
    for name, result in results.items():
        print(f"  {name:<14}{result.impact_summary()}")

    print("\nexit reasons")
    for name, result in results.items():
        print(f"  {name:<14}{result.reasons()}")

    # Entry rate matters: a strategy that almost never trades has not been
    # tested, however good the few trades look.
    print("\nwhy tokens never produced a position (observation-level counts)")
    control = results[next(iter(results))]
    total = len(control.closed) + control.still_open + control.never_entered
    entered = len(control.closed) + control.still_open
    print(f"  entered {entered} of {total} tokens "
          f"({entered / total * 100:.1f}%)" if total else "  no tokens")
    for gate, n in sorted(control.rejected.items(), key=lambda kv: -kv[1]):
        print(f"    {gate:<28}{n:>8}")

    print("\n  A mean whose interval spans zero is not a result. The sniper")
    print("  test showed two IDENTICAL distributions producing a 9.5pp gap at")
    print("  these sample sizes, so compare intervals, not point estimates.")
    print("\n  Replay shares its decision code with the live trader, but it")
    print("  cannot recover what was never recorded: a token's sellability was")
    print("  only sampled at the cadence running at the time, so a replay sees")
    print("  the same gaps the live trader saw.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
