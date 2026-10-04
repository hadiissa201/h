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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import Observation, PaperPosition, SimulatedExit, Token
from poc.sources import METHOD_QUOTE

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

    # ---- exit: trend following, applied at memecoin timescale
    # Hold while price is above its own trailing average; exit when it drops
    # below. An adaptive exit rather than a fixed threshold, so a run is not
    # capped at the take-profit multiple.
    #
    # The honest objection, recorded because it may well be the answer: a
    # moving average is lagging by construction, and the characteristic
    # memecoin failure is liquidity removed in a SINGLE block. No average can
    # exit ahead of that; it can only report it afterwards. This should help
    # against a slow bleed and do nothing at all against a rug.
    #
    # The window is in SECONDS, not in observations. Our cadence runs from 10s
    # early to 4h late, so a fixed count of observations would span minutes for
    # one position and days for another, and the rule would not be the same
    # rule across the sample.
    trend_window_s: float = 0.0          # 0 = do not use a trend exit
    trend_min_points: int = 3            # too few points is not an average
    trend_grace_s: float = 60.0          # ignore the launch-minute whipsaw

    # ---- trailing exit: let a winner run, then sell on the pullback
    # The fixed take-profit caps gains at the multiple, while the stop cannot
    # cap losses at all -- measured median stop exit was -77% on a -50% stop.
    # That shape loses by construction. A trailing stop inverts it: the loss is
    # cut tight, and the gain is bounded by how far the token runs rather than
    # by a number chosen in advance.
    #
    # trailing_stop_pct is the fall FROM THE PEAK that triggers a sale, and it
    # only arms once the position is above trail_after_multiple -- otherwise
    # launch noise would trip it in the first minute, and the tight initial
    # stop is what covers that period.
    #
    # The same gap limitation applies: a trail cannot beat liquidity pulled in
    # one block. What it can do is sell into a decline that happens over
    # several observations, which is the case a fixed take-profit never sees
    # because it has already sold at the cap.
    trailing_stop_pct: float = 0.0       # 0 = no trailing exit
    trail_after_multiple: float = 1.0    # arm only once above this multiple

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
    # The verdict authorising a SALE must come from during the hold, not from
    # before entry. An audit trace showed positions closing 20 seconds after
    # entry, with zero exit checks in between, on the same verdict that let
    # them in -- one taken before a 62% collapse. A liquidity check from
    # before the crash is not evidence you could sell after it.
    require_verdict_after_entry: bool = True

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

# --------------------------------------------- the trend-exit experiment
#
# A MATCHED TRIPLET. Identical entry, identical take-profit, stop and time
# stop. The ONLY difference is whether the position also exits when price falls
# below its own trailing average, and over what window. Any difference in
# outcome is attributable to the exit rule and nothing else.
#
# This is trend following applied at memecoin timescale, which is NOT Faber's
# anomaly: there are no multi-year regimes in a token that lives for hours,
# only a launch, a pump and a terminal event. What it does test is whether an
# ADAPTIVE exit beats a fixed threshold, which every strategy above uses. An
# adaptive exit can ride a run past +200% instead of capping there, and can
# leave a slow bleed before the -50% stop.
#
# The reason to expect nothing: a moving average lags by construction and the
# characteristic failure here is liquidity pulled in one block. No average
# exits ahead of that. Against a gradual decline it may help; against a rug it
# cannot. Recorded in advance so a null result is not reinterpreted later.
#
# And it cannot help with the finding that actually dominates: positions up
# 6.02x with no exit route. A better signal for WHEN to sell is worth nothing
# when there is no WAY to sell.
_TREND_COMMON = dict(
    max_age_s=1800, min_liquidity_usd=0.0, min_buys_5m=0,
    take_profit_multiple=3.0, stop_loss_multiple=0.5, time_stop_s=3600,
    notional_usd=100.0,
)

TREND_STRATEGIES = (
    # The control for this triplet: fixed exits only, no trend rule. Separate
    # from control_any so the comparison is not contaminated by control_any's
    # different entry window.
    Strategy(name="fixed_only", trend_window_s=0.0, **_TREND_COMMON),
    # Five minutes of trailing average: fast enough to react within the window
    # where most of these tokens live and die.
    Strategy(name="trend_5m", trend_window_s=300.0, **_TREND_COMMON),
    # Thirty minutes: slower, so it rides further but gives back more.
    Strategy(name="trend_30m", trend_window_s=1800.0, **_TREND_COMMON),
)

# ------------------------------------------------- the stop-level experiment
#
# A MATCHED LADDER. Identical in every respect except where the stop sits.
#
# The hypothesis, and it is a good one: when a memecoin starts falling it
# frequently never recovers, so a -50% stop rides a decline that a -15% stop
# would have left early. The measured median stop exit was -77%, far below the
# -50% it was set at, which looks like an argument for cutting sooner.
#
# The counter-argument, which is why this needs measuring rather than assuming.
# A tighter stop helps only on a GRADUAL decline, where a price between -15%
# and -50% was actually observed. It does nothing when the price gaps: if a
# token falls by a factor of 100,000 between two observations, a -15% stop and
# a -50% stop both execute at the same near-zero price. And a tight stop on an
# asset this volatile will exit positions that would have recovered -- the
# whipsaw cost, paid in winners never held.
#
# So the question is empirical and has two sides: how much does a tighter stop
# save on the losers, and how many winners does it cost? Comparing the
# take_profit counts across this ladder answers the second.
_STOP_COMMON = dict(
    max_age_s=1800, min_liquidity_usd=0.0, min_buys_5m=0,
    take_profit_multiple=3.0, time_stop_s=3600, notional_usd=100.0,
)

STOP_STRATEGIES = (
    Strategy(name="stop_10", stop_loss_multiple=0.90, **_STOP_COMMON),
    Strategy(name="stop_15", stop_loss_multiple=0.85, **_STOP_COMMON),
    Strategy(name="stop_20", stop_loss_multiple=0.80, **_STOP_COMMON),
    Strategy(name="stop_35", stop_loss_multiple=0.65, **_STOP_COMMON),
    Strategy(name="stop_50", stop_loss_multiple=0.50, **_STOP_COMMON),
)

# ------------------------------------------------ the trailing exit ladder
#
# The shape the structural finding points to: cut losses tight, and do not cap
# gains at all. Memecoins that work do not stop at +200%; capping there while
# losses run to -77% is the losing structure measured across 227 stop exits.
#
# take_profit is set absurdly high rather than removed, so these arms are the
# same code path as every other strategy and the only live exits are the tight
# stop and the trail.
#
# What would make this fail, recorded in advance: the winners are exactly the
# positions that cannot be sold. 26% of 17,113 exit checks found no route, and
# the blocked exits logged include x6.02, x4.34 and x3.03. A trail that fires at
# +400% is worth nothing if nothing will buy. These arms will show that as
# `stuck` rather than as returns.
_TRAIL_COMMON = dict(
    max_age_s=1800, min_liquidity_usd=0.0, min_buys_5m=0,
    take_profit_multiple=1_000_000.0,     # effectively no cap on the upside
    time_stop_s=21_600,                   # six hours: let a runner run
    notional_usd=100.0,
)

TRAILING_STRATEGIES = (
    # Tight initial stop, then trail 20% below the peak once up 50%.
    Strategy(name="trail_7_20", stop_loss_multiple=0.93,
             trailing_stop_pct=0.20, trail_after_multiple=1.5, **_TRAIL_COMMON),
    # Same, but a looser trail that gives a volatile runner more room.
    Strategy(name="trail_7_35", stop_loss_multiple=0.93,
             trailing_stop_pct=0.35, trail_after_multiple=1.5, **_TRAIL_COMMON),
    # A 10% initial stop with a tight 20% trail.
    Strategy(name="trail_10_20", stop_loss_multiple=0.90,
             trailing_stop_pct=0.20, trail_after_multiple=1.5, **_TRAIL_COMMON),
    # Arms later: only starts trailing once the position has tripled, so small
    # winners are held through noise instead of being trailed out early.
    Strategy(name="trail_10_late", stop_loss_multiple=0.90,
             trailing_stop_pct=0.30, trail_after_multiple=3.0, **_TRAIL_COMMON),
)

ALL_STRATEGIES = (DEFAULT_STRATEGIES + SNIPER_STRATEGIES + STRUCTURAL_STRATEGIES
                  + TREND_STRATEGIES + STOP_STRATEGIES + TRAILING_STRATEGIES)


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

# The largest share of a pool a single paper order may represent. Above this the
# quoted price is not a price we could have transacted at, so the position is
# not recorded at all -- the token stays tracked, it simply is not bought.
# Deliberately generous: the aim is to exclude the impossible, not to impose a
# view about what is wise.
MAX_POOL_FRACTION = 0.10


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
               SimulatedExit.simulated_ts <= moment,
               # Routing quotes only. RPC verification runs on a small sample
               # and is a check ON this measurement, not part of it -- letting
               # its rows in here would change which positions close and shift
               # the headline unsellability rate, so the two never mix.
               SimulatedExit.method == METHOD_QUOTE)
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
               SimulatedExit.simulated_ts <= moment,
               # Routing quotes only. RPC verification runs on a small sample
               # and is a check ON this measurement, not part of it -- letting
               # its rows in here would change which positions close and shift
               # the headline unsellability rate, so the two never mix.
               SimulatedExit.method == METHOD_QUOTE)
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


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


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
    # A feasibility gate, not a strategy filter, so it applies to every
    # strategy including the unfiltered baseline. An audit trace showed a $100
    # position opened against a pool holding $1 -- one hundred times the entire
    # pool -- which then reported a tidy -14% loss. That number is fiction: the
    # trade could not have happened at the quoted price, or at any price. A
    # strategy is free to choose a thin pool; it is not free to place an order
    # the pool could not absorb.
    if liquidity <= 0 or strategy.notional_usd > liquidity * MAX_POOL_FRACTION:
        return False
    if (obs.buys_5m or 0) < strategy.min_buys_5m:
        return False
    # Buying something we have never been able to sell is not a strategy.
    if strategy.require_proven_exit and exit_available(
            session, token.id, moment, strategy.min_verdict_age_s) is None:
        return False

    if not _structural_ok(session, token, strategy):
        return False

    # The proven exit quote at entry is our best estimate of what BUYING this
    # size costs in impact. Symmetric and conservative: it is the same pool.
    proven = exit_available(session, token.id, moment, strategy.min_verdict_age_s)
    entry_impact = (min(1.0, abs(float(proven.price_impact_pct or 0.0)))
                    if proven is not None else 0.0)

    session.add(PaperPosition(
        token_id=token.id, strategy=strategy.name, opened_ts=moment,
        entry_price_usd=float(obs.price_usd), notional_usd=strategy.notional_usd,
        entry_liquidity_usd=liquidity, token_age_at_entry_s=age,
        entry_price_impact_pct=entry_impact,
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


def exit_trigger(strategy: Strategy, multiple: float, held_s: float,
                 trend_broken: bool, peak_multiple: float | None = None) -> str | None:
    """Which exit rule fires, if any. Pure: no session, no clock, no I/O.

    Extracted so the live trader and any historical replay decide identically.
    A replay carrying its own copy of this ladder would drift from the running
    rule, and then it would be measuring itself rather than the strategy.

    Order matters and is deliberate. Take-profit and stop-loss come first
    because they are price levels that were already crossed; the trend rule and
    the time stop are weaker conditions that should not pre-empt them.
    """
    if multiple >= strategy.take_profit_multiple:
        return "take_profit"
    if multiple <= strategy.stop_loss_multiple:
        return "stop_loss"
    if (strategy.trailing_stop_pct > 0 and peak_multiple is not None
            and peak_multiple >= strategy.trail_after_multiple
            and multiple <= peak_multiple * (1.0 - strategy.trailing_stop_pct)):
        return "trailing_stop"
    if trend_broken:
        return "trend_exit"
    if held_s >= strategy.time_stop_s:
        return "time_stop"
    return None


def trend_broken_from_prices(strategy: Strategy, prices: list[float],
                             price: float, held_s: float) -> bool:
    """The trend test over an explicit price list, for replay.

    Shares its rules with the live path by construction: the live version reads
    the window from the database and then calls this.
    """
    if strategy.trend_window_s <= 0 or held_s < strategy.trend_grace_s:
        return False
    usable = [p for p in prices if p and p > 0]
    if len(usable) < strategy.trend_min_points:
        return False
    average = sum(usable) / len(usable)
    return average > 0 and price < average


def _trend_broken(session: Session, position: PaperPosition, strategy: Strategy,
                  moment: datetime, price: float, held_s: float) -> bool:
    """Has price fallen below its own trailing average over the window?

    Uses only observations at or before `moment`, so the rule never sees a
    price it could not have seen. The grace period exists because the first
    seconds after launch whipsaw hard enough to trip any average immediately,
    which would make this a test of the grace period rather than of the trend.
    """
    if strategy.trend_window_s <= 0:
        return False
    if held_s < strategy.trend_grace_s:
        return False

    since = moment - timedelta(seconds=strategy.trend_window_s)
    prices = session.scalars(
        select(Observation.price_usd)
        .where(Observation.token_id == position.token_id,
               Observation.observed_ts <= moment,
               Observation.observed_ts >= since,
               Observation.price_usd.is_not(None))).all()
    # Not enough history inside the window returns False, keeping the position
    # open on OUR sampling gap rather than inventing a signal from two points.
    return trend_broken_from_prices(
        strategy, [float(p) for p in prices if p], price, held_s)


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
    if (sellable is not None and strategy.require_verdict_after_entry
            and _aware(sellable.simulated_ts) <= _aware(position.opened_ts)):
        # Proven sellable at or before the moment we bought, never re-checked
        # since. That is permission to enter, not permission to exit: you
        # cannot buy and sell on one liquidity check.
        sellable = None
    if sellable is not None and (position.peak_multiple is None
                                 or multiple > float(position.peak_multiple)):
        position.peak_multiple = multiple
        position.peak_price_usd = price

    opened = position.opened_ts
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    held_s = (moment - opened).total_seconds()

    # The peak a trailing stop trails from is the one that was OBSERVED, which
    # is what a trader watching the chart would react to. Whether that peak was
    # sellable is a separate question, answered below by the exit check.
    observed_peak = float(position.unrealisable_peak_multiple or multiple)
    reason = exit_trigger(
        strategy, multiple, held_s,
        trend_broken=_trend_broken(session, position, strategy, moment,
                                   price, held_s),
        peak_multiple=observed_peak)
    if reason is None:
        return None

    if sellable is None:
        # The rule fired but we could not act. WHY matters: only 'no_route' is
        # a fact about the market. 'stale' and 'unknown' are facts about us.
        why = exit_reason_unavailable(session, position.token_id, moment, budget)
        if why == "available":
            # A verdict exists but it predates entry. Our sampling rate, not
            # the market's liquidity.
            why = "stale"
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
    exit_impact = min(1.0, abs(float(sellable.price_impact_pct or 0.0)))
    # BOTH legs pay impact. Charging only the exit understated costs on every
    # trade: buying $100 of a thin pool moves the price just as selling does.
    entry_impact = min(1.0, abs(float(position.entry_price_impact_pct or 0.0)))

    fees = notional * strategy.round_trip_cost_pct
    costs_gross = (fees
                   + notional * entry_impact
                   + max(0.0, notional + gross) * exit_impact)

    # A long spot position cannot lose more than it staked. The floor caps what
    # can be CHARGED; costs_gross keeps what the costs actually were, so the
    # totals stay meaningful instead of being rewritten to fit the floor.
    costs = costs_gross
    capped = False
    if gross - costs < -notional:
        costs = gross + notional
        capped = True

    position.is_open = False
    position.closed_ts = moment
    position.exit_price_usd = price
    position.exit_reason = reason
    position.gross_pnl_usd = gross
    position.costs_gross_usd = costs_gross
    position.costs_usd = costs
    position.costs_capped_by_floor = capped
    position.net_pnl_usd = gross - costs
    position.price_impact_at_exit_pct = exit_impact
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
            # Charged (floor-capped) and actual, because a position that lost
            # everything has its charged costs rewritten by the floor.
            "total_costs_charged_usd": round(
                sum(float(r.costs_usd or 0.0) for r in closed), 2),
            "total_costs_gross_usd": round(
                sum(float(r.costs_gross_usd or r.costs_usd or 0.0)
                    for r in closed), 2),
            "positions_capped_by_floor": sum(
                1 for r in closed if r.costs_capped_by_floor),
            "total_notional_deployed_usd": round(deployed, 2),
        }
    return out
