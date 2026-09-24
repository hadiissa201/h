"""Paper trading against the live feed. No wallet, no orders, no money.

This exists to answer one question honestly while the dataset builds: would
these rules have made money? Three properties make the answer trustworthy, and
each one exists because its absence is how memecoin backtests lie.

1. NO LOOK-AHEAD. A decision uses only observations at or before that moment.
   Entry is priced at the first observation AFTER detection, never at launch.

2. AN EXIT MUST HAVE EXISTED. A position closes only at a moment when the exit
   simulation actually found a route. If the token became unsellable while we
   held it, the position STAYS OPEN -- exactly as real money would be stuck.
   Closing at the last quoted price instead is the most common lie in this
   space, and it is precisely the risk being measured.

3. COSTS ARE REAL. Round-trip fees plus the measured price impact at the size
   traded. A memecoin pool's impact at $100 is not a rounding error.

The default rules below are deliberately ordinary. They are a starting point to
be measured, not a recommendation, and nothing here is evidence of an edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import Observation, PaperPosition, SimulatedExit, Token

log = logging.getLogger("collector.paper")


@dataclass(frozen=True)
class Strategy:
    """A rule set. Plain thresholds so the result can be attributed to a rule."""

    name: str

    # ---- entry
    max_age_s: float = 600.0             # only tokens younger than this
    min_liquidity_usd: float = 5_000.0   # a pool too thin to exit is not a trade
    max_liquidity_usd: float = 2_000_000.0
    min_buys_5m: int = 5                 # some sign of life, not a dead listing
    require_proven_exit: bool = True     # a route must have existed BEFORE entry
    notional_usd: float = 100.0

    # ---- exit
    take_profit_multiple: float = 3.0    # +200%
    stop_loss_multiple: float = 0.5      # -50%
    time_stop_s: float = 3600.0          # give up after an hour
    # An exit verdict goes stale. Liquidity can be pulled in a single block, so
    # a success from ten minutes ago is not permission to sell now.
    max_verdict_age_s: float = 300.0

    # ---- costs, charged on both legs
    round_trip_cost_pct: float = 0.01    # priority fees + DEX fees + tips


# Ordinary and unoptimised on purpose. Tuning these before the base rate is
# known is how a curve gets fitted to noise.
DEFAULT_STRATEGIES = (
    Strategy(name="early_200", max_age_s=600, min_liquidity_usd=5_000,
             take_profit_multiple=3.0, stop_loss_multiple=0.5, time_stop_s=3600),
    Strategy(name="patient_200", max_age_s=1800, min_liquidity_usd=20_000,
             min_buys_5m=15, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=10800),
    Strategy(name="quick_50", max_age_s=600, min_liquidity_usd=5_000,
             take_profit_multiple=1.5, stop_loss_multiple=0.7, time_stop_s=900),
)


def latest_observation(session: Session, token_id: int) -> Observation | None:
    return session.scalars(
        select(Observation).where(Observation.token_id == token_id)
        .order_by(Observation.observed_ts.desc()).limit(1)).first()


def exit_available(session: Session, token_id: int, moment: datetime,
                   max_age_s: float = 300.0) -> SimulatedExit | None:
    """Could this position have been sold at `moment`? Conservative by design.

    Three rules, and each rejects a way of flattering the result:

    - The MOST RECENT attempt decides, including one that returned no verdict.
      Skipping over an unknown to reach an older success assumes our failure to
      get an answer is unrelated to the token -- but a pool that just vanished
      is exactly the sort of thing that makes a quote fail.
    - A stale verdict expires. Liquidity can be pulled in one block, so a
      success from ten minutes ago is not permission to sell now.
    - No attempt at all means no.

    Erring toward "stuck" understates returns; erring toward "sold" overstates
    them. Only one of those errors can cost real money.
    """
    row = session.scalars(
        select(SimulatedExit)
        .where(SimulatedExit.token_id == token_id,
               SimulatedExit.simulated_ts <= moment)
        .order_by(SimulatedExit.simulated_ts.desc()).limit(1)).first()
    if row is None or not row.succeeded:
        return None
    simulated = row.simulated_ts
    if simulated.tzinfo is None:
        simulated = simulated.replace(tzinfo=UTC)
    if (moment - simulated).total_seconds() > max_age_s:
        return None
    return row


def _age_s(token: Token, moment: datetime) -> float:
    detected = token.detected_ts
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - detected).total_seconds()


def consider_entry(session: Session, token: Token, strategy: Strategy) -> bool:
    """Open a hypothetical position if the rules allow it right now."""
    existing = session.scalar(
        select(PaperPosition.id).where(PaperPosition.token_id == token.id,
                                       PaperPosition.strategy == strategy.name))
    if existing is not None:
        return False

    obs = latest_observation(session, token.id)
    if obs is None or not obs.price_usd or obs.price_usd <= 0:
        return False

    moment = obs.observed_ts
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    age = _age_s(token, moment)
    if age > strategy.max_age_s:
        return False
    liquidity = float(obs.liquidity_usd or 0.0)
    if not (strategy.min_liquidity_usd <= liquidity <= strategy.max_liquidity_usd):
        return False
    if (obs.buys_5m or 0) < strategy.min_buys_5m:
        return False
    # Buying something we have never been able to sell is not a strategy.
    if strategy.require_proven_exit and exit_available(
            session, token.id, moment, strategy.max_verdict_age_s) is None:
        return False

    session.add(PaperPosition(
        token_id=token.id, strategy=strategy.name, opened_ts=moment,
        entry_price_usd=float(obs.price_usd), notional_usd=strategy.notional_usd,
        entry_liquidity_usd=liquidity, token_age_at_entry_s=age,
        peak_price_usd=float(obs.price_usd), peak_multiple=1.0,
        unrealisable_peak_multiple=1.0, is_open=True, blocked_exits=0,
    ))
    log.info("paper[%s] OPEN %s @ %.10f (age %.0fs, liq $%.0f)",
             strategy.name, token.address[:12], float(obs.price_usd), age, liquidity)
    return True


def manage_position(session: Session, position: PaperPosition,
                    strategy: Strategy) -> str | None:
    """Update a live position; close it only if an exit genuinely existed."""
    obs = latest_observation(session, position.token_id)
    if obs is None or not obs.price_usd or obs.price_usd <= 0:
        return None

    moment = obs.observed_ts
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    price = float(obs.price_usd)
    entry = float(position.entry_price_usd)
    multiple = price / entry if entry else 0.0

    # Track the peak both ways: what we could have taken, and what merely
    # appeared on a chart. The gap between them IS the cost of unsellability.
    if position.unrealisable_peak_multiple is None or \
            multiple > float(position.unrealisable_peak_multiple):
        position.unrealisable_peak_multiple = multiple

    sellable = exit_available(session, position.token_id, moment,
                              strategy.max_verdict_age_s)
    if sellable is not None and (position.peak_multiple is None
                                 or multiple > float(position.peak_multiple)):
        position.peak_multiple = multiple
        position.peak_price_usd = price

    opened = position.opened_ts
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    held_s = (moment - opened).total_seconds()

    reason = None
    if multiple >= strategy.take_profit_multiple:
        reason = "take_profit"
    elif multiple <= strategy.stop_loss_multiple:
        reason = "stop_loss"
    elif held_s >= strategy.time_stop_s:
        reason = "time_stop"
    if reason is None:
        return None

    if sellable is None:
        # The rule fired but there was no way out. This is the finding, not an
        # inconvenience: the position stays open and keeps trying.
        position.blocked_exits = (position.blocked_exits or 0) + 1
        log.info("paper[%s] BLOCKED %s wanted %s at x%.2f -- no exit route",
                 strategy.name, position.token_id, reason, multiple)
        return None

    _close(position, strategy, price, moment, reason, sellable)
    return reason


def _close(position: PaperPosition, strategy: Strategy, price: float,
           moment: datetime, reason: str, sellable: SimulatedExit) -> None:
    entry = float(position.entry_price_usd)
    notional = float(position.notional_usd)
    gross = notional * ((price / entry) - 1.0) if entry else -notional

    # Both legs pay the flat cost; the exit leg also pays the measured price
    # impact for this size. On a thin pool that impact dwarfs the fee.
    impact = abs(float(sellable.price_impact_pct or 0.0))
    costs = notional * strategy.round_trip_cost_pct + (notional + gross) * impact

    position.is_open = False
    position.closed_ts = moment
    position.exit_price_usd = price
    position.exit_reason = reason
    position.gross_pnl_usd = gross
    position.costs_usd = costs
    position.net_pnl_usd = gross - costs
    position.price_impact_at_exit_pct = impact
    log.info("paper[%s] CLOSE %s %s x%.2f net $%+.2f",
             strategy.name, position.token_id, reason, price / entry if entry else 0,
             gross - costs)


def run_once(session: Session, strategies=DEFAULT_STRATEGIES) -> dict[str, int]:
    """One sweep: manage open positions, then look for new entries."""
    counts = {"opened": 0, "closed": 0, "blocked": 0}
    by_name = {s.name: s for s in strategies}

    for position in session.scalars(
            select(PaperPosition).where(PaperPosition.is_open.is_(True))).all():
        strategy = by_name.get(position.strategy)
        if strategy is None:
            continue
        before = position.blocked_exits or 0
        if manage_position(session, position, strategy):
            counts["closed"] += 1
        elif (position.blocked_exits or 0) > before:
            counts["blocked"] += 1

    # Only tokens young enough for any strategy to care about.
    now = datetime.now(UTC)
    oldest = max(s.max_age_s for s in strategies)
    for token in session.scalars(select(Token).where(Token.is_tracked.is_(True))).all():
        if _age_s(token, now) > oldest:
            continue
        for strategy in strategies:
            if consider_entry(session, token, strategy):
                counts["opened"] += 1
    session.commit()
    return counts


def summary(session: Session, strategies=DEFAULT_STRATEGIES) -> dict:
    """Running paper P&L per strategy. Honest about what is still unresolved."""
    out: dict[str, dict] = {}
    for strategy in strategies:
        rows = session.scalars(
            select(PaperPosition).where(
                PaperPosition.strategy == strategy.name)).all()
        closed = [r for r in rows if not r.is_open]
        open_rows = [r for r in rows if r.is_open]
        net = sum(float(r.net_pnl_usd or 0.0) for r in closed)
        wins = [r for r in closed if float(r.net_pnl_usd or 0.0) > 0]
        deployed = sum(float(r.notional_usd) for r in closed) or 1.0
        stuck = [r for r in open_rows if (r.blocked_exits or 0) > 0]
        out[strategy.name] = {
            "positions_opened": len(rows),
            "closed": len(closed),
            "still_open": len(open_rows),
            # Positions the rules wanted to exit but could not. Real money would
            # still be in these, and they are NOT counted as profit.
            "stuck_no_exit": len(stuck),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 4) if closed else None,
            "net_pnl_usd": round(net, 2),
            "return_on_deployed_pct": round(net / deployed * 100, 3) if closed else None,
            "total_costs_usd": round(
                sum(float(r.costs_usd or 0.0) for r in closed), 2),
        }
    return out
