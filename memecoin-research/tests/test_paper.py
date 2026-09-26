"""Paper trading honesty.

Every test here guards a way a memecoin backtest can flatter itself. The one
that matters most: a position must not close at a price nobody could have sold
at. That single shortcut turns an unsellable rug into a clean +200% win.
"""

from __future__ import annotations

import itertools

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from collector.models import Base, Observation, PaperPosition, Token
from collector.paper import (
    Strategy,
    consider_entry,
    exit_available,
    manage_position,
    summary,
)
from poc.store import insert_simulated_exit, upsert_token

TS = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
STRAT = Strategy(name="t", max_age_s=600, min_liquidity_usd=5_000,
                 min_buys_5m=5, take_profit_multiple=3.0,
                 stop_loss_multiple=0.5, time_stop_s=3600,
                 notional_usd=100.0, round_trip_cost_pct=0.01)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_MINT_SEQ = itertools.count()


def make_token(session, detected=TS, address=None) -> Token:
    # Distinct addresses by default: upsert_token returns the EXISTING row for a
    # repeated address, so a shared one silently makes two "different" tokens
    # the same token and quietly breaks any test comparing them.
    address = address or f"MINT{next(_MINT_SEQ):04d}"
    token_id = upsert_token(session, chain="solana", address=address,
                            detected_ts=detected, detection_source="ws")
    session.commit()
    return session.get(Token, token_id)


def observe(session, token, at, price, liquidity=50_000.0, buys=30):
    session.add(Observation(token_id=token.id, observed_ts=at,
                            source="dexscreener", price_usd=price,
                            liquidity_usd=liquidity, buys_5m=buys))
    session.flush()


def can_sell(session, token, at, impact=0.02):
    insert_simulated_exit(session, token_id=token.id, simulated_ts=at,
                          method="quote", notional_usd=100.0, succeeded=True,
                          price_impact_pct=impact)


def cannot_sell(session, token, at):
    insert_simulated_exit(session, token_id=token.id, simulated_ts=at,
                          method="quote", notional_usd=100.0, succeeded=False,
                          failure_kind="no_route", failure_reason="No routes")


def unknown_sell(session, token, at):
    insert_simulated_exit(session, token_id=token.id, simulated_ts=at,
                          method="quote", notional_usd=100.0, succeeded=None,
                          failure_kind="transport", failure_reason="HTTP 429")


# --------------------------------------------------------------------- entry
def test_a_token_meeting_every_rule_is_entered(session):
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=30), 0.001)
    can_sell(session, token, TS + timedelta(seconds=30))
    assert consider_entry(session, token, STRAT) is True


def test_a_token_we_have_never_been_able_to_sell_is_not_bought(session):
    """Buying something with no demonstrated exit is not a strategy."""
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=30), 0.001)
    cannot_sell(session, token, TS + timedelta(seconds=30))
    assert consider_entry(session, token, STRAT) is False


def test_an_unknown_exit_does_not_count_as_a_proven_one(session):
    """Our own rate limit is not evidence the token is tradable."""
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=30), 0.001)
    unknown_sell(session, token, TS + timedelta(seconds=30))
    assert consider_entry(session, token, STRAT) is False


def test_a_thin_pool_is_skipped(session):
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=30), 0.001, liquidity=200.0)
    can_sell(session, token, TS + timedelta(seconds=30))
    assert consider_entry(session, token, STRAT) is False


def test_a_token_older_than_the_window_is_skipped(session):
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=5000), 0.001)
    can_sell(session, token, TS + timedelta(seconds=5000))
    assert consider_entry(session, token, STRAT) is False


def test_the_same_token_is_not_entered_twice(session):
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=30), 0.001)
    can_sell(session, token, TS + timedelta(seconds=30))
    assert consider_entry(session, token, STRAT) is True
    session.commit()
    assert consider_entry(session, token, STRAT) is False


def test_entry_is_priced_after_detection_never_before(session):
    token = make_token(session)
    observe(session, token, TS + timedelta(seconds=45), 0.002)
    can_sell(session, token, TS + timedelta(seconds=45))
    consider_entry(session, token, STRAT)
    session.commit()
    position = session.scalar(select(PaperPosition))
    assert position.opened_ts.replace(tzinfo=UTC) > TS
    assert float(position.token_age_at_entry_s) == pytest.approx(45.0)


# ---------------------------------------------------------------------- exit
def open_position(session, token, entry=0.001) -> PaperPosition:
    observe(session, token, TS + timedelta(seconds=30), entry)
    can_sell(session, token, TS + timedelta(seconds=30))
    consider_entry(session, token, STRAT)
    session.commit()
    return session.scalar(select(PaperPosition))


def test_take_profit_closes_when_an_exit_exists(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.004)      # x4
    can_sell(session, token, later)
    assert manage_position(session, position, STRAT) == "take_profit"
    assert position.is_open is False
    assert float(position.net_pnl_usd) > 0


def test_a_position_cannot_close_when_no_exit_exists(session):
    """THE test. Closing at a price nobody could sell at is the core lie.

    Without this, a rug that printed +300% on a chart before the liquidity was
    pulled records as a clean winner.
    """
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.004)      # x4, take-profit territory
    cannot_sell(session, token, later)         # but there is no way out

    assert manage_position(session, position, STRAT) is None
    assert position.is_open is True, "money would still be stuck in this"
    assert position.blocked_exits == 1
    assert position.net_pnl_usd is None, "an unrealised gain is not profit"


def test_an_unknown_verdict_does_not_permit_an_exit(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.004)
    unknown_sell(session, token, later)
    assert manage_position(session, position, STRAT) is None
    assert position.is_open is True


def test_the_chart_peak_and_the_sellable_peak_are_tracked_separately(session):
    """The gap between them is the measured cost of unsellability."""
    token = make_token(session)
    position = open_position(session, token)
    blocked = TS + timedelta(minutes=5)
    observe(session, token, blocked, 0.010)    # x10 on the chart
    cannot_sell(session, token, blocked)
    manage_position(session, position, STRAT)

    assert float(position.unrealisable_peak_multiple) == pytest.approx(10.0)
    assert float(position.peak_multiple) == pytest.approx(1.0), \
        "no sellable moment ever reached x10"


def test_stop_loss_and_time_stop_both_fire(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.0004)     # -60%
    can_sell(session, token, later)
    assert manage_position(session, position, STRAT) == "stop_loss"
    assert float(position.net_pnl_usd) < 0


def test_a_time_stop_closes_a_flat_position(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(hours=2)
    observe(session, token, later, 0.001)      # unchanged
    can_sell(session, token, later)
    assert manage_position(session, position, STRAT) == "time_stop"


# --------------------------------------------------------------------- costs
def test_costs_include_price_impact_not_just_fees(session):
    """On a thin pool the impact dwarfs the fee, so charging fees alone lies."""
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.004)
    can_sell(session, token, later, impact=0.25)   # 25% impact
    manage_position(session, position, STRAT)

    flat_fee_only = 100.0 * STRAT.round_trip_cost_pct
    assert float(position.costs_usd) > flat_fee_only * 5
    assert float(position.net_pnl_usd) < float(position.gross_pnl_usd)


def test_a_win_on_paper_can_be_a_loss_after_costs(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.0011)     # +10%
    can_sell(session, token, later, impact=0.30)
    manage_position(session, position, STRAT)
    if position.exit_reason:
        assert float(position.gross_pnl_usd) > 0
        assert float(position.net_pnl_usd) < 0


# ------------------------------------------------------------------ summary
def test_stuck_positions_are_reported_and_not_counted_as_profit(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.010)
    cannot_sell(session, token, later)
    manage_position(session, position, STRAT)
    session.commit()

    report = summary(session, [STRAT])["t"]
    assert report["still_open"] == 1
    assert report["stuck_no_exit"] == 1
    assert report["closed"] == 0
    assert report["net_pnl_usd"] == 0.0, "unrealised gains are never profit"


def test_exit_available_returns_none_when_the_last_verdict_was_no_route(session):
    token = make_token(session)
    can_sell(session, token, TS)
    cannot_sell(session, token, TS + timedelta(minutes=1))
    session.commit()
    assert exit_available(session, token.id, TS + timedelta(minutes=2)) is None
    assert exit_available(session, token.id, TS + timedelta(seconds=30)) is not None


def test_a_stale_success_does_not_authorise_a_sale(session):
    """Liquidity can be pulled in one block; a ten-minute-old yes means little."""
    token = make_token(session)
    can_sell(session, token, TS)
    session.commit()
    assert exit_available(session, token.id, TS + timedelta(seconds=60),
                          max_age_s=300) is not None
    assert exit_available(session, token.id, TS + timedelta(minutes=30),
                          max_age_s=300) is None


def test_a_fresh_unknown_overrides_an_older_success(session):
    """Skipping an unknown to reach an older yes assumes our failure is
    unrelated to the token -- but a vanished pool is exactly what breaks quotes."""
    token = make_token(session)
    can_sell(session, token, TS)
    unknown_sell(session, token, TS + timedelta(seconds=60))
    session.commit()
    assert exit_available(session, token.id, TS + timedelta(seconds=90)) is None


# ------------------------------------------------------------------- control
def test_the_control_strategy_buys_without_an_opinion(session):
    """It exists so a filtered strategy has something to beat.

    Without a control, any positive P&L reads as skill when it may just be the
    base rate of the market during that window -- the same reason the trading
    system reports cash and buy-and-hold baselines.
    """
    from collector.paper import DEFAULT_STRATEGIES

    control = next(s for s in DEFAULT_STRATEGIES if s.name == "control_any")
    assert control.min_liquidity_usd == 0.0
    assert control.min_buys_5m == 0
    # Still may not buy what was never sellable -- that is honesty, not a filter.
    assert control.require_proven_exit is True


def test_the_control_enters_a_token_the_filters_would_reject(session):
    from collector.paper import DEFAULT_STRATEGIES

    control = next(s for s in DEFAULT_STRATEGIES if s.name == "control_any")
    filtered = next(s for s in DEFAULT_STRATEGIES if s.name == "patient_200")

    token = make_token(session)
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001, liquidity=300.0, buys=1)  # thin and quiet
    can_sell(session, token, at)
    session.commit()

    assert consider_entry(session, token, filtered) is False
    assert consider_entry(session, token, control) is True


def test_even_the_control_will_not_buy_something_unsellable(session):
    from collector.paper import DEFAULT_STRATEGIES

    control = next(s for s in DEFAULT_STRATEGIES if s.name == "control_any")
    token = make_token(session)
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001, liquidity=300.0, buys=1)
    cannot_sell(session, token, at)
    session.commit()
    assert consider_entry(session, token, control) is False


# ---------------------------------------------- bugs found in live running
def test_the_staleness_bound_never_undercuts_the_simulation_cadence():
    """A bound tighter than the sampling rate makes EVERY verdict stale.

    That is not conservative, it is impossible -- and it looked like a finding:
    44 of 44 open positions were permanently stuck, which reads as "the market
    is unsellable" when it actually meant "we can never satisfy our own rule".
    """
    from collector.config import CollectorSettings
    from collector.paper import verdict_age_budget
    from collector.schedule import WorkKind, interval_for

    settings = CollectorSettings()
    for age in (60.0, 1800.0, 12_000.0, 60_000.0, 300_000.0):
        budget = verdict_age_budget(settings, STRAT, age)
        cadence = interval_for(settings, WorkKind.EXIT, age) or 0.0
        assert budget >= cadence, f"age {age}: budget {budget} < cadence {cadence}"
        assert budget >= STRAT.min_verdict_age_s


def test_an_old_position_can_still_close(session):
    """The deadlock, end to end: exits are simulated every 2h for a token this
    old, so a 300s staleness bound could never be met."""
    from collector.config import CollectorSettings

    settings = CollectorSettings()
    token = make_token(session)
    position = open_position(session, token)

    later = TS + timedelta(hours=8)
    observe(session, token, later, 0.004)
    can_sell(session, token, later - timedelta(minutes=20))   # last sim, 20m old
    session.commit()

    assert manage_position(session, position, STRAT, settings) == "take_profit"
    assert position.is_open is False


def test_a_long_position_cannot_lose_more_than_it_staked(session):
    """A quote reported >100% impact and produced $3.5m of costs on $100."""
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.0004)
    can_sell(session, token, later, impact=35_000.0)   # absurd quoted impact
    manage_position(session, position, STRAT)

    assert float(position.net_pnl_usd) >= -float(position.notional_usd)
    assert float(position.costs_usd) <= float(position.notional_usd) * 2


def test_an_implausible_price_is_ignored_rather_than_booked(session):
    """A 35,000x reading is a near-zero denominator, not a windfall.

    The dangerous direction is optimistic: with a smaller quoted impact this
    would have recorded a spectacular fake win.
    """
    token = make_token(session)
    position = open_position(session, token, entry=0.001)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 35.0)        # x35,000
    can_sell(session, token, later)

    assert manage_position(session, position, STRAT) is None
    assert position.is_open is True
    assert position.net_pnl_usd is None
    assert float(position.unrealisable_peak_multiple) == pytest.approx(1.0), \
        "an implausible reading must not even set the peak"


def test_a_large_but_credible_move_is_still_taken(session):
    """The guard must not silently discard genuine winners."""
    token = make_token(session)
    position = open_position(session, token, entry=0.001)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.05)        # x50 -- big, but credible
    can_sell(session, token, later)
    assert manage_position(session, position, STRAT) == "take_profit"
    assert float(position.net_pnl_usd) > 0


# ------------------------------------------------- the sniping experiment
def test_the_sniper_pair_differs_only_in_timing():
    """A controlled comparison, or it measures nothing.

    A sniper bot's whole claim is that being early pays. If the two arms
    differed in liquidity floors or exits too, a difference in outcome could
    not be attributed to earliness.
    """
    from collector.paper import SNIPER_STRATEGIES

    early, late = SNIPER_STRATEGIES
    differing = [f for f in early.__dataclass_fields__
                 if getattr(early, f) != getattr(late, f)]
    assert set(differing) == {"name", "min_age_s", "max_age_s"}


def test_the_early_arm_buys_immediately_and_the_late_arm_waits():
    from collector.paper import SNIPER_STRATEGIES

    early, late = SNIPER_STRATEGIES
    assert early.min_age_s == 0.0
    assert late.min_age_s >= 300.0
    assert early.max_age_s <= late.min_age_s, "the windows must not overlap"


def test_a_token_too_young_for_the_late_arm_is_rejected(session):
    from collector.paper import SNIPER_STRATEGIES

    early, late = SNIPER_STRATEGIES
    token = make_token(session)
    at = TS + timedelta(seconds=20)           # 20 seconds old
    observe(session, token, at, 0.001, liquidity=500.0, buys=1)
    can_sell(session, token, at)
    session.commit()

    assert consider_entry(session, token, early) is True
    assert consider_entry(session, token, late) is False


def test_a_token_too_old_for_the_early_arm_is_rejected(session):
    from collector.paper import SNIPER_STRATEGIES

    early, late = SNIPER_STRATEGIES
    token = make_token(session)
    at = TS + timedelta(seconds=400)          # ~7 minutes old
    observe(session, token, at, 0.001, liquidity=500.0, buys=1)
    can_sell(session, token, at)
    session.commit()

    assert consider_entry(session, token, early) is False
    assert consider_entry(session, token, late) is True


def test_min_age_defaults_to_zero_so_existing_strategies_are_unchanged():
    from collector.paper import DEFAULT_STRATEGIES

    assert all(s.min_age_s == 0.0 for s in DEFAULT_STRATEGIES)


# -------------------------------------------- the structural experiment
def test_a_live_freeze_authority_is_rejected(session):
    """A live freeze authority means they can stop YOU from selling."""
    from collector.paper import STRUCTURAL_STRATEGIES

    safe = next(s for s in STRUCTURAL_STRATEGIES if s.name == "safe_authorities")
    token = make_token(session)
    token.freeze_authority = "SomeAuthorityAddress"
    token.mint_authority = None
    token.creator_address = "Deployer1"
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001)
    can_sell(session, token, at)
    session.commit()
    assert consider_entry(session, token, safe) is False


def test_revoked_authorities_pass(session):
    from collector.paper import STRUCTURAL_STRATEGIES

    safe = next(s for s in STRUCTURAL_STRATEGIES if s.name == "safe_authorities")
    token = make_token(session)
    token.freeze_authority = None
    token.mint_authority = None
    token.creator_address = "Deployer1"
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001)
    can_sell(session, token, at)
    session.commit()
    assert consider_entry(session, token, safe) is True


def test_an_unread_token_is_not_treated_as_safe(session):
    """Unknown must never score as clean.

    Passing a token whose authorities we failed to read would quietly admit
    exactly the ones we could not check.
    """
    from collector.paper import STRUCTURAL_STRATEGIES

    safe = next(s for s in STRUCTURAL_STRATEGIES if s.name == "safe_authorities")
    token = make_token(session)           # nothing enriched: all NULL
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001)
    can_sell(session, token, at)
    session.commit()
    assert consider_entry(session, token, safe) is False


def test_a_first_time_deployer_is_unknown_not_innocent(session):
    from collector.paper import STRUCTURAL_STRATEGIES

    clean = next(s for s in STRUCTURAL_STRATEGIES if s.name == "clean_deployer")
    token = make_token(session)
    token.creator_address = "BrandNewWallet"
    at = TS + timedelta(seconds=30)
    observe(session, token, at, 0.001)
    can_sell(session, token, at)
    session.commit()
    assert consider_entry(session, token, clean) is False


def test_creator_history_excludes_the_token_being_judged(session):
    """Counting the current token's own fate would be look-ahead of the worst
    kind: the filter would 'predict' a rug using the rug it is predicting."""
    from collector.enrich import creator_history
    from collector.models import TokenStatus
    from poc.store import upsert_token

    ids = []
    for i in range(3):
        tid = upsert_token(session, chain="solana", address=f"TOK{i}",
                           detected_ts=TS, detection_source="ws")
        token = session.get(Token, tid)
        token.creator_address = "SerialDeployer"
        session.add(TokenStatus(token_id=tid, retired_ts=TS))   # all died
        ids.append(tid)
    session.commit()

    from collector.enrich import record_creator
    for _ in range(3):
        record_creator(session, "solana", "SerialDeployer")
    session.commit()

    # Judging the LAST token sees only the two before it.
    history = creator_history(session, "solana", "SerialDeployer",
                              before_token_id=ids[-1])
    assert history.prior_tokens == 2
    assert history.prior_dead == 2
    assert history.death_rate == pytest.approx(1.0)


def test_no_history_gives_none_not_zero():
    """None is not a clean record. Scoring it as 0.0 would pass every new
    deployer through a filter designed to catch known behaviour."""
    from collector.enrich import CreatorHistory

    assert CreatorHistory(address="X").death_rate is None
    assert CreatorHistory(address="X", prior_tokens=4, prior_dead=1).death_rate == 0.25


def test_the_structural_arms_match_the_control_except_for_the_filter():
    """So a difference is attributable to the structural fact, not the window."""
    from collector.paper import DEFAULT_STRATEGIES, STRUCTURAL_STRATEGIES

    control = next(s for s in DEFAULT_STRATEGIES if s.name == "control_any")
    for arm in STRUCTURAL_STRATEGIES:
        assert arm.max_age_s == control.max_age_s
        assert arm.min_liquidity_usd == control.min_liquidity_usd
        assert arm.take_profit_multiple == control.take_profit_multiple
        assert arm.stop_loss_multiple == control.stop_loss_multiple
        assert arm.time_stop_s == control.time_stop_s


# ------------------------------------ separating market facts from our faults
def test_a_bad_request_is_not_evidence_that_a_token_cannot_be_sold():
    """HTTP 400 was being parsed as 'no route'.

    That turned every malformed quote WE sent into a data point claiming the
    market was unsellable -- contaminating the single most important number in
    this project.
    """
    from poc.sources import says_no_route

    assert says_no_route('{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}') is True
    assert says_no_route('{"error":"No routes found"}') is True
    # A generic bad request says nothing about routing.
    assert says_no_route('{"error":"Invalid amount"}') is False
    assert says_no_route("Bad Request") is False
    assert says_no_route(None) is False


def test_the_three_causes_of_a_blocked_exit_are_distinguished(session):
    """Only one of them is a fact about the market."""
    from collector.paper import exit_reason_unavailable

    token = make_token(session)
    assert exit_reason_unavailable(session, token.id, TS, 300.0) == "unknown"

    cannot_sell(session, token, TS)
    session.commit()
    assert exit_reason_unavailable(session, token.id, TS + timedelta(seconds=10),
                                   300.0) == "no_route"

    token2 = make_token(session)
    can_sell(session, token2, TS)
    session.commit()
    assert exit_reason_unavailable(session, token2.id, TS + timedelta(seconds=10),
                                   300.0) == "available"
    # Same success, consulted much later: our sampling rate, not the market.
    assert exit_reason_unavailable(session, token2.id, TS + timedelta(hours=2),
                                   300.0) == "stale"


def test_an_unknown_verdict_is_not_counted_as_market_evidence(session):
    """Our rate limit must not appear in the unsellable statistic."""
    from collector.paper import exit_reason_unavailable

    token = make_token(session)
    unknown_sell(session, token, TS)
    session.commit()
    assert exit_reason_unavailable(session, token.id, TS + timedelta(seconds=10),
                                   300.0) == "unknown"


def test_a_blocked_position_records_which_side_was_at_fault(session):
    token = make_token(session)
    position = open_position(session, token)
    later = TS + timedelta(minutes=5)
    observe(session, token, later, 0.004)
    cannot_sell(session, token, later)          # the market says no
    manage_position(session, position, STRAT)

    assert position.blocked_no_route == 1
    assert position.blocked_our_fault == 0


def test_a_stale_block_is_charged_to_us_not_the_market(session):
    """The gap must exceed the cadence-derived budget, which is generous:
    exits are simulated every 12h for a token this old, so the tolerance is
    ~30h. Anything shorter is legitimately NOT stale."""
    from collector.config import CollectorSettings
    from collector.paper import verdict_age_budget

    settings = CollectorSettings()
    token = make_token(session)
    position = open_position(session, token)

    budget = verdict_age_budget(settings, STRAT, 200_000.0)
    later = TS + timedelta(seconds=budget * 1.5 + 3600)
    observe(session, token, later, 0.004)     # take-profit territory
    session.commit()
    manage_position(session, position, STRAT, settings)

    assert position.blocked_no_route == 0, "the market never said no"
    assert position.blocked_our_fault == 1
