"""Paper trading honesty.

Every test here guards a way a memecoin backtest can flatter itself. The one
that matters most: a position must not close at a price nobody could have sold
at. That single shortcut turns an unsellable rug into a clean +200% win.
"""

from __future__ import annotations

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


def make_token(session, detected=TS) -> Token:
    token_id = upsert_token(session, chain="solana", address="MINTAAA",
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
