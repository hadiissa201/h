"""Replaying a strategy over collected history.

The danger in a replay is not that it fails; it is that it quietly decides
differently from the live trader and then reports a strategy result that is
really a result about the replay. These tests mostly pin the agreement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.models import Base, Observation, Token
from collector.paper import (
    Strategy,
    consider_entry,
    exit_trigger,
    manage_position,
    trend_broken_from_prices,
)
from poc.store import insert_simulated_exit
from replay import replay_token

TS = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class Settings:
    exit_interval_0_5m = 60.0
    exit_interval_5_60m = 300.0
    exit_interval_1_6h = 1800.0
    exit_interval_6_24h = 7200.0
    exit_interval_1_7d = 43200.0


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_token(session, address="ReplayMint1111111111111111111111111111111") -> Token:
    token = Token(chain="solana", address=address, detected_ts=TS,
                  detection_source="ws:pumpfun-bonding-curve", decimals=6)
    session.add(token)
    session.commit()
    return token


def observe(session, token, at, price, liquidity=50_000.0, buys=30):
    session.add(Observation(token_id=token.id, observed_ts=at, price_usd=price,
                            liquidity_usd=liquidity, buys_5m=buys,
                            source="dexscreener"))


def can_sell(session, token, at, impact=0.01):
    insert_simulated_exit(session, token_id=token.id, simulated_ts=at,
                          method="quote", notional_usd=100.0, succeeded=True,
                          price_impact_pct=impact)


STRAT = Strategy(name="trend_5m", max_age_s=1800, min_liquidity_usd=0.0,
                 min_buys_5m=0, take_profit_multiple=3.0,
                 stop_loss_multiple=0.5, time_stop_s=36_000,
                 notional_usd=100.0, round_trip_cost_pct=0.01,
                 trend_window_s=300.0, trend_grace_s=60.0, trend_min_points=3)


# ------------------------------------------------- the agreement requirement
def test_the_replay_shares_the_live_exit_ladder():
    """Not a similar ladder. The same function, called by both."""
    import inspect

    from collector import paper
    source = inspect.getsource(paper.manage_position)
    assert "exit_trigger(" in source, (
        "the live trader stopped using the shared ladder; the replay would drift")


def test_the_replay_and_the_live_trader_close_on_the_same_bar(session):
    """The test that matters. Same stored data, same strategy, same decision.

    If these diverge, any replay result is a statement about the replay.
    """
    token = make_token(session)
    timeline = [(30, 0.001), (90, 0.0014), (150, 0.0016), (210, 0.0015),
                (270, 0.0009)]
    for offset, price in timeline:
        observe(session, token, TS + timedelta(seconds=offset), price)
        can_sell(session, token, TS + timedelta(seconds=offset))
    session.commit()

    replayed, entered = replay_token(session, token, STRAT, Settings())
    assert entered and replayed is not None and replayed.exit_ts is not None

    # Now drive the LIVE trader over the same data, observation by observation.
    live_token = make_token(session, "LiveMint111111111111111111111111111111111")
    live_token.detected_ts = TS
    session.commit()
    closed_at = None
    for offset, price in timeline:
        at = TS + timedelta(seconds=offset)
        observe(session, live_token, at, price)
        can_sell(session, live_token, at)
        session.commit()
        from collector.models import PaperPosition
        from sqlalchemy import select as sa_select
        position = session.scalar(
            sa_select(PaperPosition).where(PaperPosition.token_id == live_token.id))
        if position is None:
            consider_entry(session, live_token, STRAT)
            session.commit()
            continue
        if position.is_open and manage_position(session, position, STRAT, Settings()):
            closed_at = at
            break

    assert closed_at is not None, "the live trader never closed"
    assert replayed.exit_ts == closed_at, (
        f"replay closed at {replayed.exit_ts}, live closed at {closed_at}")
    assert replayed.reason == "trend_exit"


# -------------------------------------------------------------- look-ahead
def test_the_replay_cannot_see_a_later_price(session):
    """A replay that reads the whole series at once is not a backtest."""
    token = make_token(session)
    # Flat, then a huge spike at the very end.
    for offset in range(30, 400, 30):
        observe(session, token, TS + timedelta(seconds=offset), 0.001)
        can_sell(session, token, TS + timedelta(seconds=offset))
    observe(session, token, TS + timedelta(seconds=430), 0.010)   # x10 at the end
    can_sell(session, token, TS + timedelta(seconds=430))
    session.commit()

    replayed, _ = replay_token(session, token, STRAT, Settings())
    assert replayed is not None
    # Entry must be at the flat price, not anywhere near the spike.
    assert replayed.entry_price == pytest.approx(0.001)


def test_a_sale_needs_a_verdict_from_during_the_hold(session):
    """Same rule as live: the entry's own verdict does not authorise an exit."""
    token = make_token(session)
    for offset, price in ((30, 0.001), (90, 0.0014), (150, 0.0016),
                          (210, 0.0015), (270, 0.0009)):
        observe(session, token, TS + timedelta(seconds=offset), price)
    can_sell(session, token, TS + timedelta(seconds=30))     # only before entry
    session.commit()

    replayed, entered = replay_token(session, token, STRAT, Settings())
    assert entered
    assert replayed.exit_ts is None, "sold on a pre-entry verdict"
    assert replayed.blocked_our_fault >= 1


def test_an_order_too_big_for_the_pool_never_enters(session):
    token = make_token(session)
    for offset, price in ((30, 0.001), (90, 0.0012)):
        observe(session, token, TS + timedelta(seconds=offset), price,
                liquidity=1.0)
        can_sell(session, token, TS + timedelta(seconds=offset))
    session.commit()
    _, entered = replay_token(session, token, STRAT, Settings())
    assert entered is False


# ------------------------------------------------------------- the accounting
def test_both_legs_pay_price_impact(session):
    token = make_token(session)
    for offset, price in ((30, 0.001), (90, 0.0014), (150, 0.0016),
                          (210, 0.0015), (270, 0.0009)):
        observe(session, token, TS + timedelta(seconds=offset), price)
        can_sell(session, token, TS + timedelta(seconds=offset), impact=0.05)
    session.commit()

    replayed, _ = replay_token(session, token, STRAT, Settings())
    assert replayed is not None and replayed.exit_ts is not None
    fees_only = 100.0 * STRAT.round_trip_cost_pct
    assert replayed.costs > fees_only * 2, "impact was not charged on both legs"
    assert replayed.net == pytest.approx(replayed.gross - replayed.costs)


def test_a_loss_can_never_exceed_the_stake(session):
    token = make_token(session)
    for offset, price in ((30, 0.001), (90, 0.0009), (150, 0.000001)):
        observe(session, token, TS + timedelta(seconds=offset), price)
        can_sell(session, token, TS + timedelta(seconds=offset), impact=0.9)
    session.commit()
    replayed, _ = replay_token(session, token, STRAT, Settings())
    assert replayed is not None and replayed.exit_ts is not None
    assert replayed.net >= -STRAT.notional_usd - 1e-6


# ------------------------------------------------------------ shared helpers
def test_the_trend_helper_is_the_one_the_live_path_uses():
    import inspect

    from collector import paper
    source = inspect.getsource(paper._trend_broken)
    assert "trend_broken_from_prices(" in source


def test_the_shared_ladder_orders_rules_deterministically():
    """Take-profit beats the trend rule when both fire on the same bar."""
    assert exit_trigger(STRAT, multiple=5.0, held_s=100, trend_broken=True) == "take_profit"
    assert exit_trigger(STRAT, multiple=0.3, held_s=100, trend_broken=True) == "stop_loss"
    assert exit_trigger(STRAT, multiple=1.1, held_s=100, trend_broken=True) == "trend_exit"
    assert exit_trigger(STRAT, multiple=1.1, held_s=99_999, trend_broken=False) == "time_stop"
    assert exit_trigger(STRAT, multiple=1.1, held_s=100, trend_broken=False) is None


def test_the_grace_period_applies_in_replay_too():
    assert trend_broken_from_prices(STRAT, [1.0, 1.0, 1.0], 0.5, held_s=10) is False
    assert trend_broken_from_prices(STRAT, [1.0, 1.0, 1.0], 0.5, held_s=100) is True
