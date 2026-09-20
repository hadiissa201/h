"""Idempotent writes.

Every function here can be called twice with the same fact and leave the
database in the same state. That is not an optimisation -- a websocket that
redelivers, a retry after a timeout, or a restart mid-batch will all replay
facts, and a dataset that double-counts them is as wrong as one that drops
them.

The guarantee lives in the UNIQUE constraints in models.py; these helpers just
use ON CONFLICT so the collector never has to reason about it.
"""

from __future__ import annotations

import gzip
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from poc.models import CollectionGap, Event, Observation, Pool, RawPayload, SimulatedExit, Token


def _dialect_is_postgres(session: Session) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def store_raw(session: Session, *, source: str, endpoint: str,
              http_status: int | None, body: str | None,
              fetched_ts: datetime) -> int | None:
    """Archive a response verbatim so parsing can be redone later."""
    if body is None:
        return None
    payload = RawPayload(
        fetched_ts=fetched_ts, source=source, endpoint=endpoint,
        http_status=http_status, body_gz=gzip.compress(body.encode("utf-8")),
    )
    session.add(payload)
    session.flush()
    return payload.id


def read_raw(session: Session, payload_id: int) -> str | None:
    payload = session.get(RawPayload, payload_id)
    if payload is None or payload.body_gz is None:
        return None
    return gzip.decompress(payload.body_gz).decode("utf-8")


def upsert_token(session: Session, **fields: Any) -> int:
    """Insert a token, or return the existing id. Launch facts are never overwritten.

    A token seen a second time keeps its ORIGINAL detected_ts. Overwriting it
    with a later sighting would quietly move our entry point forward in time and
    flatter every Phase 2 result.
    """
    chain, address = fields["chain"], fields["address"]
    existing = session.scalar(
        select(Token.id).where(Token.chain == chain, Token.address == address)
    )
    if existing is not None:
        return existing
    if _dialect_is_postgres(session):
        stmt = (
            pg_insert(Token).values(**fields)
            .on_conflict_do_nothing(index_elements=["chain", "address"])
            .returning(Token.id)
        )
        returned = session.execute(stmt).scalar()
        if returned is not None:
            return returned
        return session.scalar(
            select(Token.id).where(Token.chain == chain, Token.address == address)
        )
    token = Token(**fields)
    session.add(token)
    session.flush()
    return token.id


def upsert_pool(session: Session, **fields: Any) -> int:
    chain, pair = fields["chain"], fields["pair_address"]
    existing = session.scalar(
        select(Pool.id).where(Pool.chain == chain, Pool.pair_address == pair)
    )
    if existing is not None:
        return existing
    pool = Pool(**fields)
    session.add(pool)
    session.flush()
    return pool.id


def insert_observation(session: Session, **fields: Any) -> bool:
    """Returns True if a new row was written, False if it was a duplicate."""
    existing = session.scalar(
        select(Observation.id).where(
            Observation.token_id == fields["token_id"],
            Observation.observed_ts == fields["observed_ts"],
            Observation.source == fields["source"],
        )
    )
    if existing is not None:
        return False
    session.add(Observation(**fields))
    session.flush()
    return True


def insert_simulated_exit(session: Session, **fields: Any) -> bool:
    existing = session.scalar(
        select(SimulatedExit.id).where(
            SimulatedExit.token_id == fields["token_id"],
            SimulatedExit.simulated_ts == fields["simulated_ts"],
            SimulatedExit.method == fields["method"],
            SimulatedExit.notional_usd == fields["notional_usd"],
        )
    )
    if existing is not None:
        return False
    session.add(SimulatedExit(**fields))
    session.flush()
    return True


def insert_event(session: Session, **fields: Any) -> bool:
    existing = session.scalar(
        select(Event.id).where(
            Event.token_id == fields["token_id"],
            Event.event_ts == fields["event_ts"],
            Event.kind == fields["kind"],
        )
    )
    if existing is not None:
        return False
    session.add(Event(**fields))
    session.flush()
    return True


def open_gap(session: Session, *, gap_start: datetime, cause: str,
             affected_sources: str | None = None) -> int:
    gap = CollectionGap(gap_start=gap_start, cause=cause,
                        affected_sources=affected_sources)
    session.add(gap)
    session.flush()
    return gap.id


def close_gap(session: Session, gap_id: int, gap_end: datetime) -> None:
    gap = session.get(CollectionGap, gap_id)
    if gap is not None:
        gap.gap_end = gap_end
        session.flush()
