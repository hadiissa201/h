"""Storage guarantees, exercised against a real SQL engine (SQLite in-memory).

Idempotency is the property that matters most here. A websocket that
redelivers, a retry after a timeout, or a restart mid-batch will all replay the
same fact. A dataset that double-counts those replays is as wrong as one that
drops them -- both stop matching reality, and Phase 2 cannot tell either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from poc.models import Base, CollectionGap, Event, Observation, SimulatedExit, Token
from poc.store import (
    close_gap,
    insert_event,
    insert_observation,
    insert_simulated_exit,
    open_gap,
    read_raw,
    store_raw,
    upsert_token,
)

TS = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_token(session, address="MINT1") -> int:
    return upsert_token(session, chain="solana", address=address,
                        detected_ts=TS, detection_source="websocket")


# ----------------------------------------------------------------- tokens
def test_the_same_token_twice_is_one_row(session):
    first = make_token(session)
    second = make_token(session)
    assert first == second
    assert session.scalar(select(func.count()).select_from(Token)) == 1


def test_a_later_sighting_does_not_move_detected_ts_forward(session):
    """Overwriting detection time would silently improve every Phase 2 entry."""
    token_id = make_token(session)
    upsert_token(session, chain="solana", address="MINT1",
                 detected_ts=TS + timedelta(hours=5), detection_source="reconciler")
    token = session.get(Token, token_id)
    assert token.detected_ts.replace(tzinfo=UTC) == TS
    assert token.detection_source == "websocket"


def test_the_same_address_on_another_chain_is_a_different_token(session):
    """Chain-agnostic keys: the schema must not collide across chains."""
    a = upsert_token(session, chain="solana", address="SAME",
                     detected_ts=TS, detection_source="ws")
    b = upsert_token(session, chain="base", address="SAME",
                     detected_ts=TS, detection_source="ws")
    assert a != b
    assert session.scalar(select(func.count()).select_from(Token)) == 2


# ----------------------------------------------------------- observations
def test_a_replayed_observation_is_not_counted_twice(session):
    token_id = make_token(session)
    fields = dict(token_id=token_id, observed_ts=TS, source="dexscreener",
                  price_usd=0.001, liquidity_usd=5000.0)
    assert insert_observation(session, **fields) is True
    assert insert_observation(session, **fields) is False
    assert session.scalar(select(func.count()).select_from(Observation)) == 1


def test_a_later_observation_is_a_new_row(session):
    token_id = make_token(session)
    insert_observation(session, token_id=token_id, observed_ts=TS,
                       source="dexscreener", price_usd=0.001)
    insert_observation(session, token_id=token_id, observed_ts=TS + timedelta(seconds=5),
                       source="dexscreener", price_usd=0.002)
    assert session.scalar(select(func.count()).select_from(Observation)) == 2


def test_two_sources_at_the_same_instant_both_survive(session):
    """Cross-checking sources is how we measure our own accuracy."""
    token_id = make_token(session)
    insert_observation(session, token_id=token_id, observed_ts=TS,
                       source="dexscreener", price_usd=0.001)
    insert_observation(session, token_id=token_id, observed_ts=TS,
                       source="birdeye", price_usd=0.0011)
    assert session.scalar(select(func.count()).select_from(Observation)) == 2


def test_null_liquidity_is_preserved_as_null(session):
    """'No data' must never be stored as zero."""
    token_id = make_token(session)
    insert_observation(session, token_id=token_id, observed_ts=TS,
                       source="dexscreener", price_usd=None, liquidity_usd=None)
    row = session.scalar(select(Observation))
    assert row.liquidity_usd is None
    assert row.price_usd is None


# -------------------------------------------------------- simulated exits
def test_a_failed_exit_is_stored_not_discarded(session):
    """The unsellable moment is the most valuable row in the database."""
    token_id = make_token(session)
    insert_simulated_exit(session, token_id=token_id, simulated_ts=TS,
                          method="quote", notional_usd=100.0,
                          succeeded=False, failure_reason="No routes found")
    row = session.scalar(select(SimulatedExit))
    assert row.succeeded is False
    assert row.failure_reason == "No routes found"


def test_the_same_size_replayed_is_one_row_but_a_different_size_is_another(session):
    token_id = make_token(session)
    base = dict(token_id=token_id, simulated_ts=TS, method="quote", succeeded=True)
    assert insert_simulated_exit(session, **base, notional_usd=100.0) is True
    assert insert_simulated_exit(session, **base, notional_usd=100.0) is False
    assert insert_simulated_exit(session, **base, notional_usd=1000.0) is True
    assert session.scalar(select(func.count()).select_from(SimulatedExit)) == 2


def test_quote_and_rpc_sim_are_recorded_separately(session):
    """They fail differently; collapsing them would hide the disagreement."""
    token_id = make_token(session)
    base = dict(token_id=token_id, simulated_ts=TS, notional_usd=100.0)
    insert_simulated_exit(session, **base, method="quote", succeeded=True)
    insert_simulated_exit(session, **base, method="rpc_sim", succeeded=False,
                          failure_reason="AccountFrozen")
    assert session.scalar(select(func.count()).select_from(SimulatedExit)) == 2


# ------------------------------------------------------- events, gaps, raw
def test_a_replayed_event_is_not_duplicated(session):
    token_id = make_token(session)
    fields = dict(token_id=token_id, event_ts=TS, kind="first_seen", detail={"a": 1})
    assert insert_event(session, **fields) is True
    assert insert_event(session, **fields) is False
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_downtime_is_recorded_as_a_gap(session):
    """Without this, downtime silently becomes 'no launches happened'."""
    gap_id = open_gap(session, gap_start=TS, cause="websocket_disconnect",
                      affected_sources="launchpad")
    row = session.get(CollectionGap, gap_id)
    assert row.gap_end is None            # an open gap means we are still blind
    close_gap(session, gap_id, TS + timedelta(minutes=7))
    assert session.get(CollectionGap, gap_id).gap_end is not None


def test_a_raw_payload_round_trips_through_gzip(session):
    body = '{"pairs": [{"priceUsd": "0.0001"}]}'
    payload_id = store_raw(session, source="dexscreener", endpoint="http://x",
                           http_status=200, body=body, fetched_ts=TS)
    assert read_raw(session, payload_id) == body


def test_no_body_stores_no_payload(session):
    assert store_raw(session, source="x", endpoint="y", http_status=None,
                     body=None, fetched_ts=TS) is None


def test_an_unknown_exit_is_stored_as_null_not_false(session):
    """A transport failure must never be counted as 'could not sell'.

    Phase 2 counts only rows where succeeded IS NOT NULL. If an outage wrote
    False instead, the measured unsellable rate would include our own downtime.
    """
    token_id = make_token(session)
    insert_simulated_exit(session, token_id=token_id, simulated_ts=TS,
                          method="quote", notional_usd=100.0,
                          succeeded=None, failure_kind="transport",
                          failure_reason="HTTP 429")
    row = session.scalar(select(SimulatedExit))
    assert row.succeeded is None
    assert row.failure_kind == "transport"

    usable = session.scalar(
        select(func.count()).select_from(SimulatedExit)
        .where(SimulatedExit.succeeded.is_not(None))
    )
    assert usable == 0, "an unknown result must not count as evidence"
