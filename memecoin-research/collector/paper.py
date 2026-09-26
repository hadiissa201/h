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
    min_age_s: float = 0.0               # wait at least this long before buying
    max_age_s: float = 600.0             # only tokens younger than this
    min_liquidity_usd: float = 5_000.0   # a pool too thin to exit is not a trade
    max_liquidity_usd: float = 2_000_000.0
    min_buys_5m: int = 5                 # some sign of life, not a dead listing
    require_proven_exit: bool = True     # a route must have existed BEFORE entry
    notional_usd: float = 100.0

    # ---- structural filters: facts about the DEPLOYER, not the price.
    # Everything above is on every screen in the market, which is consistent
    # with what we measured -- the unfiltered control beat every filtered
    # strategy. These are the only inputs that are not already public in the
    # way liquidity is.
    require_freeze_revoked: bool = False   # they cannot freeze your account
    require_mint_revoked: bool = False     # they cannot print more supply
    max_creator_death_rate: float | None = None   # None = do not look
    min_creator_prior_tokens: int = 0      # history needed before judging

    # ---- exit
    take_profit_multiple: float = 3.0    # +200%
    stop_loss_multiple: float = 0.5      # -50%
    time_stop_s: float = 3600.0          # give up after an hour
    # An exit verdict goes stale: liquidity can be pulled in a single block, so
    # an old success is weak evidence. But this floor cannot be tighter than the
    # rate we actually simulate exits at, or every verdict is stale by
    # construction and NO position can ever close. That happened: 44 of 44 open
    # positions were permanently stuck because exits are simulated every 30-120
    # minutes for older tokens while this was pinned at 300 seconds.
    min_verdict_age_s: float = 300.0
    # Multiple of the current tier's exit-simulation interval to tolerate.
    verdict_age_cadence_multiple: float = 2.5

    # ---- costs, charged on both legs
    round_trip_cost_pct: float = 0.01    # priority fees + DEX fees + tips


# Ordinary and unoptimised on purpose. Tuning these before the base rate is
# known is how a curve gets fitted to noise.
#
# The thresholds were loosened once, after 8 hours produced ZERO positions: a
# $5,000 liquidity floor excludes most tokens at the age these rules want to
# buy them. That change widens the net to obtain a sample at all -- it is not
# tuning for returns, and the distinction matters. Adjusting filters because
# the P&L looks bad would be curve fitting; adjusting them because there is no
# data to judge is just making the experiment runnable.
DEFAULT_STRATEGIES = (
    # THE CONTROL. Buys anything with a demonstrated exit and no other opinion.
    # Every filtered strategy has to beat this, or its filters are decoration.
    # Same role the cash and buy-and-hold baselines play in the trading system:
    # without it, any positive number looks like skill.
    Strategy(name="control_any", max_age_s=1800, min_liquidity_usd=0.0,
             min_buys_5m=0, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=3600),
    Strategy(name="early_200", max_age_s=600, min_liquidity_usd=1_500,
             min_buys_5m=3, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=3600),
    Strategy(name="patient_200", max_age_s=1800, min_liquidity_usd=10_000,
             min_buys_5m=10, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=10800),
    Strategy(name="quick_50", max_age_s=600, min_liquidity_usd=1_500,
             min_buys_5m=3, take_profit_multiple=1.5, stop_loss_multiple=0.7,
             time_stop_s=900),
)

# ---------------------------------------------------- the sniping experiment
#
# A MATCHED PAIR. Every parameter is identical except WHEN they buy, so any
# difference in outcome is attributable to entry timing and nothing else. That
# is the whole point: a sniper bot's claimed edge is being early, and this is
# the cheapest honest way to ask whether earliness is worth anything.
#
# What this CAN answer: does buying as soon as we can see a token beat buying
# the same token five minutes later, after costs.
#
# What it CANNOT answer: whether block-zero sniping works. Our detection is
# seconds behind the chain, and a real sniper competes in milliseconds. So this
# tests earliness across SECONDS, not milliseconds. If earliness does not pay
# across seconds, the case for chasing milliseconds gets much weaker -- but a
# null result here is not proof that true sniping fails.
_SNIPE_COMMON = dict(
    min_liquidity_usd=0.0, min_buys_5m=0, take_profit_multiple=3.0,
    stop_loss_multiple=0.5, time_stop_s=3600, notional_usd=100.0,
)

SNIPER_STRATEGIES = (
    # Buys at the first observation we ever get. As close to sniping as our
    # data allows.
    Strategy(name="snipe_asap", min_age_s=0.0, max_age_s=90.0, **_SNIPE_COMMON),
    # Same token, same rules, five minutes later.
    Strategy(name="wait_5m", min_age_s=300.0, max_age_s=600.0, **_SNIPE_COMMON),
)


# ------------------------------------------------ the structural experiment
#
# The genuinely untested hypothesis: that facts about the DEPLOYER predict
# something the price does not. Each arm shares control_any's entry window and
# exits, so the comparison against control_any isolates the structural filter.
#
# It has to beat a measured base rate of about -22%, which is a large hole.
# These make the question answerable; they do not make it likely.
STRUCTURAL_STRATEGIES = (
    # Cannot freeze your account, cannot print more supply.
    Strategy(name="safe_authorities", max_age_s=1800, min_liquidity_usd=0.0,
             min_buys_5m=0, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=3600, require_freeze_revoked=True,
             require_mint_revoked=True),
    # Deployer has launched before and most of those tokens are not dead.
    Strategy(name="clean_deployer", max_age_s=1800, min_liquidity_usd=0.0,
             min_buys_5m=0, take_profit_multiple=3.0, stop_loss_multiple=0.5,
             time_stop_s=3600, max_creator_death_rate=0.5,
             min_creator_prior_tokens=2),
)

ALL_STRATEGIES = DEFAULT_STRATEGIES + SNIPER_STRATEGIES + STRUCTURAL_STRATEGIES


def latest_observation(session: Session, token_id: int) -> Observation | None:
    return session.scalars(
        select(Observation).where(Observation.token_id == token_id)
        .order_by(Observation.observed_ts.desc()).limit(1)).first()


# A price move beyond this within the tracked window is treated as a DATA
# ERROR, not a windfall. A 35,000x observation came from a near-zero
# denominator on a thin pool, and taking it at face value recorded a $3.5m
# gross gain on a $100 position. The dangerous direction is the optimistic one:
# with a smaller quoted impact that would have booked as an enormous fake win.
IMPLAUSIBLE_MULTIPLE = 1_000.0


def verdict_age_budget(settings, strategy: "Strategy", age_s: float) -> float:  # noqa: ANN001
    """How old an exit verdict may be before it stops authorising a sale.

    Derived from the cadence we actually simulate at, never tighter than it.
    A bound below the sampling rate does not make the test conservative -- it
    makes it impossible, which is a different thing and much worse, because it
    looks like a finding.
    """
    from collector.schedule import WorkKind, interval_for

    interval = interval_for(settings, WorkKind.EXIT, age_s)
    if interval is None:
        interval = 0.0
    return max(strategy.min_verdict_age_s,
               interval * strategy.verdict_age_cadence_multiple)


def exit_reason_unavailable(session: Session, token_id: int, moment: datetime,
                            max_age_s: float) -> str:
    """WHY we cannot sell. Three answers, and conflating them is a false finding.

    'no_route'  -- we asked and the market said there is no way out. A fact.
    'stale'     -- the last verdict was a success but too old to rely on. Our
                   sampling rate, not the market's liquidity.
    'unknown'   -- our request failed. Says nothing about the token.
    'available' -- an exit existed.

    Reporting 'stale' and 'unknown' alongside 'no_route' would overstate how
    unsellable this market is, and the throttling we are currently under makes
    both much more common.
    """
    row = session.scalars(
        select(SimulatedExit)
        .where(SimulatedExit.token_id == token_id,
               SimulatedExit.simulated_ts <= moment)
        .order_by(SimulatedExit.simulated_ts.desc()).limit(1)).first()
    if row is None:
        return "unknown"
    if row.succeeded is None:
        return "unknown"
    if not row.succeeded:
        return "no_route"
    simulated = row.simulated_ts
    if simulated.tzinfo is None:
        simulated = simulated.replace(tzinfo=UTC)
    if (moment - simulated).total_seconds() > max_age_s:
        return "stale"
    return "available"


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


def _age_s_from_open(position: PaperPosition, moment: datetime) -> float:
    opened = position.opened_ts
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    return max(0.0, (moment - opened).total_seconds())


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
    if age > strategy.max_age_s or age < strategy.min_age_s:
        return False
    liquidity = float(obs.liquidity_usd or 0.0)
    if not (strategy.min_liquidity_usd <= liquidity <= strategy.max_liquidity_usd):
        return False
    if (obs.buys_5m or 0) < strategy.min_buys_5m:
        return False
    # Buying something we have never been able to sell is not a strategy.
    if strategy.require_proven_exit and exit_available(
            session, token.id, moment, strategy.min_verdict_age_s) is None:
        return False

    if not _structural_ok(session, token, strategy):
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


def _structural_ok(session: Session, token: Token, strategy: Strategy) -> bool:
    """Apply the deployer-based filters, if this strategy uses any.

    Unknown is NOT treated as clean. A token whose authorities we failed to
    read is skipped by a strategy that requires them revoked -- scoring a
    missing fact as a pass would quietly let through exactly the tokens we
    could not check.
    """
    if strategy.require_freeze_revoked and token.freeze_authority is not None:
        return False
    if strategy.require_mint_revoked and token.mint_authority is not None:
        return False
    if strategy.require_freeze_revoked and token.freeze_authority is None \
            and token.creator_address is None:
        # Nothing was read at all; we cannot claim the authority is revoked.
        return False

    if strategy.max_creator_death_rate is None:
        return True
    if not token.creator_address:
        return False

    from collector.enrich import creator_history

    history = creator_history(session, token.chain, token.creator_address,
                              before_token_id=token.id)
    if history.prior_tokens < strategy.min_creator_prior_tokens:
        # Not enough history to judge. A first-time deployer is unknown, not
        # innocent, and this strategy is specifically about known behaviour.
        return False
    rate = history.death_rate
    return rate is not None and rate <= strategy.max_creator_death_rate


def manage_position(session: Session, position: PaperPosition,
                    strategy: Strategy, settings=None) -> str | None:  # noqa: ANN001
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

    # A move this large on a thin pool is a bad reading, not a windfall.
    # Skipping the observation is the conservative choice: acting on it would
    # book a spectacular fake gain, and ignoring it only delays a real one to
    # the next observation.
    if multiple > IMPLAUSIBLE_MULTIPLE or multiple < 0:
        log.warning("paper[%s] ignoring implausible x%.1f on token %s "
                    "(price %.12g vs entry %.12g) -- treated as a data error",
                    strategy.name, multiple, position.token_id, price, entry)
        return None

    # Track the peak both ways: what we could have taken, and what merely
    # appeared on a chart. The gap between them IS the cost of unsellability.
    if position.unrealisable_peak_multiple is None or \
            multiple > float(position.unrealisable_peak_multiple):
        position.unrealisable_peak_multiple = multiple

    age = _age_s_from_open(position, moment)
    budget = (verdict_age_budget(settings, strategy, age) if settings is not None
              else strategy.min_verdict_age_s)
    sellable = exit_available(session, position.token_id, moment, budget)
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
        # The rule fired but we could not act. WHY matters: only 'no_route' is
        # a fact about the market. 'stale' and 'unknown' are facts about us.
        why = exit_reason_unavailable(session, position.token_id, moment, budget)
        position.blocked_exits = (position.blocked_exits or 0) + 1
        if why == "no_route":
            position.blocked_no_route = (position.blocked_no_route or 0) + 1
        else:
            position.blocked_our_fault = (position.blocked_our_fault or 0) + 1
        log.info("paper[%s] BLOCKED %s wanted %s at x%.2f -- %s",
                 strategy.name, position.token_id, reason, multiple,
                 {"no_route": "NO EXIT ROUTE (market)",
                  "stale": "verdict too old (our sampling rate)",
                  "unknown": "could not check (our request failed)"}[why])
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
    #
    # Impact is clamped to 1.0: a quote can report an impact above 100%, which
    # means "you get essentially nothing back", not "you owe more than you
    # staked". Uncapped, a single such quote produced $3.5m of costs on a $100
    # position and made every strategy's total meaningless.
    impact = min(1.0, abs(float(sellable.price_impact_pct or 0.0)))
    costs = notional * strategy.round_trip_cost_pct + max(0.0, notional + gross) * impact

    # A long spot position cannot lose more than it staked. Whatever the
    # arithmetic says, the floor is -notional.
    if gross - costs < -notional:
        costs = gross + notional

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


def run_once(session: Session, strategies=ALL_STRATEGIES,
             settings=None) -> dict[str, int]:  # noqa: ANN001
    """One sweep: manage open positions, then look for new entries."""
    counts = {"opened": 0, "closed": 0, "blocked": 0}
    by_name = {s.name: s for s in strategies}

    for position in session.scalars(
            select(PaperPosition).where(PaperPosition.is_open.is_(True))).all():
        strategy = by_name.get(position.strategy)
        if strategy is None:
            continue
        before = position.blocked_exits or 0
        if manage_position(session, position, strategy, settings):
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


def summary(session: Session, strategies=ALL_STRATEGIES) -> dict:
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
        # Split the stuck ones by cause, so "this market is unsellable" is never
        # claimed on the back of our own throttling.
        stuck_market = [r for r in stuck if (r.blocked_no_route or 0) > 0]
        stuck_ours = [r for r in stuck if (r.blocked_no_route or 0) == 0
                      and (r.blocked_our_fault or 0) > 0]
        out[strategy.name] = {
            "positions_opened": len(rows),
            "closed": len(closed),
            "still_open": len(open_rows),
            # Positions the rules wanted to exit but could not. Real money would
            # still be in these, and they are NOT counted as profit.
            "stuck_no_exit": len(stuck),
            "stuck_market_no_route": len(stuck_market),
            "stuck_our_fault": len(stuck_ours),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 4) if closed else None,
            "net_pnl_usd": round(net, 2),
            "return_on_deployed_pct": round(net / deployed * 100, 3) if closed else None,
            "total_costs_usd": round(
                sum(float(r.costs_usd or 0.0) for r in closed), 2),
        }
    return out
