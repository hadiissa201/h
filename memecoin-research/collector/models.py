"""The Phase 1 schema. One source of truth -- the proof of concept imports it
too, so the pipeline that was verified and the collector that runs for weeks
can never drift apart.

Design commitments, each of which cost something to learn:

  - Chain-agnostic keys from the first row, so adding Base later is not a
    migration of every primary key.
  - Price and liquidity share one observations row: they arrive in one response
    at one instant, and splitting them would duplicate every timestamp.
  - Writes are idempotent by UNIQUE constraint, not by convention.
  - A field the source omitted is NULL, never 0.
  - simulated_exits.succeeded is NULLABLE: "we could not ask" is not "it could
    not be sold".

Idempotency is structural, not conventional. Every table that can receive the
same fact twice carries a UNIQUE constraint covering that fact, so a replayed
message or a restarted collector cannot double-count. Losing data and
silently duplicating it are the same class of bug: the dataset stops matching
reality.

Money and prices are NUMERIC, never float.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Prices span many orders of magnitude on new tokens: a memecoin can quote at
# 1e-11 and a quote leg at 1e2. Wide precision, generous scale, no floats.
# SQLite autoincrements only INTEGER PRIMARY KEY, so the variant keeps
# BIGSERIAL in Postgres while letting tests build the schema in memory.
PK = BigInteger().with_variant(Integer, "sqlite")
FK_TYPE = BigInteger().with_variant(Integer, "sqlite")

Price = Numeric(40, 20)
Money = Numeric(30, 10)


class Token(Base):
    """One row per token. Launch facts, written once.

    `chain` is present from the first row even though Phase 1 is Solana-only --
    adding a chain later must not require a migration of the primary key.

    Both timestamps are mandatory by design. `launch_ts` is when the token was
    created on-chain; `detected_ts` is when WE saw it. Phase 2 must price a
    hypothetical entry at detected_ts, because that is the first moment the
    opportunity actually existed for us. Storing only one of them would make
    that rule unenforceable and would let a backtest buy in the past.
    """

    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_token_chain_address"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32), default="solana", index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    symbol: Mapped[str | None] = mapped_column(String(64))
    decimals: Mapped[int | None] = mapped_column(Integer)

    launch_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    detection_latency_ms: Mapped[int | None] = mapped_column(Integer)
    detection_source: Mapped[str] = mapped_column(String(64))
    first_seen_slot: Mapped[int | None] = mapped_column(BigInteger)

    # Sampling is recorded per token so Phase 2 can weight correctly. Without
    # this, a sampled dataset is indistinguishable from a biased one.
    sample_score: Mapped[float | None] = mapped_column(Numeric(12, 10))
    sample_rate_at_detection: Mapped[float | None] = mapped_column(Numeric(10, 6))
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    creator_address: Mapped[str | None] = mapped_column(String(128), index=True)
    mint_authority: Mapped[str | None] = mapped_column(String(128))
    freeze_authority: Mapped[str | None] = mapped_column(String(128))
    token_program: Mapped[str | None] = mapped_column(String(128))
    is_token2022: Mapped[bool | None] = mapped_column(Boolean)

    observations: Mapped[list["Observation"]] = relationship(back_populates="token")
    exits: Mapped[list["SimulatedExit"]] = relationship(back_populates="token")


class Pool(Base):
    __tablename__ = "pools"
    __table_args__ = (UniqueConstraint("chain", "pair_address", name="uq_pool_pair"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    chain: Mapped[str] = mapped_column(String(32), default="solana")
    pair_address: Mapped[str] = mapped_column(String(128))
    dex: Mapped[str | None] = mapped_column(String(64))
    quote_mint: Mapped[str | None] = mapped_column(String(128))
    created_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initial_liquidity_usd: Mapped[float | None] = mapped_column(Money)
    initial_price_usd: Mapped[float | None] = mapped_column(Price)


class Observation(Base):
    """Price AND liquidity at one instant, from one source.

    Merged rather than split across two tables: DexScreener returns price,
    liquidity, market cap, volume and transaction counts in a single response
    at a single moment. Splitting them would duplicate every timestamp, double
    the row count, and turn "what was liquidity when price was X" into a join
    for no gain.

    Nullable almost everywhere on purpose. A source that omits a field must
    record NULL, never zero -- "no data" and "zero liquidity" mean opposite
    things, and conflating them would quietly turn missing data into evidence
    that a token was dead.
    """

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("token_id", "observed_ts", "source", name="uq_obs_token_ts_source"),
        Index("ix_obs_token_ts", "token_id", "observed_ts"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    pool_id: Mapped[int | None] = mapped_column(FK_TYPE, ForeignKey("pools.id"))
    observed_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64))

    price_usd: Mapped[float | None] = mapped_column(Price)
    price_native: Mapped[float | None] = mapped_column(Price)
    liquidity_usd: Mapped[float | None] = mapped_column(Money)
    market_cap_usd: Mapped[float | None] = mapped_column(Money)
    fdv_usd: Mapped[float | None] = mapped_column(Money)

    volume_5m: Mapped[float | None] = mapped_column(Money)
    volume_1h: Mapped[float | None] = mapped_column(Money)
    volume_24h: Mapped[float | None] = mapped_column(Money)
    buys_5m: Mapped[int | None] = mapped_column(Integer)
    sells_5m: Mapped[int | None] = mapped_column(Integer)
    buys_1h: Mapped[int | None] = mapped_column(Integer)
    sells_1h: Mapped[int | None] = mapped_column(Integer)

    raw_payload_id: Mapped[int | None] = mapped_column(FK_TYPE, ForeignKey("raw_payloads.id"))
    token: Mapped[Token] = relationship(back_populates="observations")


class SimulatedExit(Base):
    """A read-only answer to: could this position have been sold, right now?

    `succeeded=False` is the most valuable row in the table. It is the moment a
    paper gain stopped being realisable, and it is invisible in any price chart.

    A success here is evidence an exit was possible at that instant, for that
    size, with no competition. It is NOT proof the token is safe, and it does
    not model MEV, the priority-fee auction, or everyone selling in the same
    block. Real exits are strictly worse than this record.
    """

    __tablename__ = "simulated_exits"
    __table_args__ = (
        UniqueConstraint("token_id", "simulated_ts", "method", "notional_usd",
                         name="uq_exit_token_ts_method_size"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    simulated_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    method: Mapped[str] = mapped_column(String(32))  # 'quote' | 'rpc_sim'

    notional_usd: Mapped[float] = mapped_column(Money)
    input_amount_raw: Mapped[str | None] = mapped_column(String(64))
    expected_output_raw: Mapped[str | None] = mapped_column(String(64))
    expected_output_usd: Mapped[float | None] = mapped_column(Money)
    price_impact_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    slippage_bps: Mapped[int | None] = mapped_column(Integer)
    route_dex: Mapped[str | None] = mapped_column(String(128))
    route_hops: Mapped[int | None] = mapped_column(Integer)
    liquidity_at_sim_usd: Mapped[float | None] = mapped_column(Money)

    # NULLABLE ON PURPOSE. Three states, not two:
    #   True  -- an exit was available at this instant, for this size
    #   False -- we asked and there was genuinely no way out (the finding)
    #   None  -- we could not ask (timeout, 403, DNS). NOT evidence of anything.
    # Collapsing None into False would turn every network outage into a wave of
    # fake "unsellable" tokens and quietly corrupt the base rate Phase 2 exists
    # to measure. Phase 2 must count only rows where succeeded IS NOT NULL.
    succeeded: Mapped[bool | None] = mapped_column(Boolean, index=True)
    failure_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    raw_payload_id: Mapped[int | None] = mapped_column(FK_TYPE, ForeignKey("raw_payloads.id"))
    token: Mapped[Token] = relationship(back_populates="exits")


class HolderSnapshot(Base):
    """Holder distribution at a moment. Expensive, so sampled sparsely.

    Snapshotted rather than re-derived later: data providers drop dead tokens,
    and a dead token's final distribution is exactly the row we most need.
    """

    __tablename__ = "holder_snapshots"
    __table_args__ = (
        UniqueConstraint("token_id", "observed_ts", name="uq_holders_token_ts"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    observed_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    top1_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    top10_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    top50_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    creator_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    raw_payload_id: Mapped[int | None] = mapped_column(FK_TYPE,
                                                       ForeignKey("raw_payloads.id"))


class LiquidityEvent(Base):
    """Adds and removals. The rug, when it happens, is a row in this table."""

    __tablename__ = "liquidity_events"
    __table_args__ = (
        UniqueConstraint("tx_signature", "kind", name="uq_liqevent_tx_kind"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    pool_id: Mapped[int | None] = mapped_column(FK_TYPE, ForeignKey("pools.id"),
                                                index=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # add | remove
    amount_usd: Mapped[float | None] = mapped_column(Money)
    actor_address: Mapped[str | None] = mapped_column(String(128))
    tx_signature: Mapped[str] = mapped_column(String(128), index=True)


class Event(Base):
    """Append-only state transitions. Never updated, never deleted."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("token_id", "event_ts", "kind", name="uq_event_token_ts_kind"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    # JSONB on Postgres; plain JSON elsewhere so the schema can be exercised
    # against SQLite in tests without a live database.
    detail: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )


class RawPayload(Base):
    """Every external response, gzipped.

    When a parsing bug is found later, we reprocess these instead of admitting
    the data is gone. Re-collecting is impossible: the moment has passed.
    """

    __tablename__ = "raw_payloads"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    fetched_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    body_gz: Mapped[bytes | None] = mapped_column(LargeBinary)


class Creator(Base):
    """Deployer history. Refreshed rarely -- expensive, and slow-moving."""

    __tablename__ = "creators"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_creator_addr"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32), default="solana")
    address: Mapped[str] = mapped_column(String(128), index=True)
    first_seen_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_launched: Mapped[int] = mapped_column(Integer, default=0)
    last_refreshed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TokenStatus(Base):
    """Derived current state. Rebuildable from observations and events.

    Cached because Phase 2 and the scheduler both need "how is this token doing
    right now" constantly, and recomputing it from the full series every time
    would cost more than the collection itself.
    """

    __tablename__ = "token_status"

    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"),
                                          primary_key=True)
    is_tradable: Mapped[bool | None] = mapped_column(Boolean, index=True)
    last_tradable_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_price_usd: Mapped[float | None] = mapped_column(Price)
    peak_price_usd: Mapped[float | None] = mapped_column(Price)
    peak_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    peak_multiple: Mapped[float | None] = mapped_column(Numeric(20, 6))
    max_drawdown_from_peak_pct: Mapped[float | None] = mapped_column(Numeric(20, 6))
    observations_count: Mapped[int] = mapped_column(Integer, default=0)
    retired_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_reason: Mapped[str | None] = mapped_column(String(64))
    consecutive_dead_checks: Mapped[int] = mapped_column(Integer, default=0)


class WorkItem(Base):
    """The scheduler's queue, in Postgres rather than memory.

    A crash mid-observation must not lose the task. Rows carry attempts and the
    last error, so work that keeps failing is visible instead of vanishing --
    silently dropping a task and silently dropping data are the same bug.
    """

    __tablename__ = "work_queue"
    __table_args__ = (
        UniqueConstraint("token_id", "kind", name="uq_work_token_kind"),
        Index("ix_work_due", "due_at", "kind"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # observe|exit|holders
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_run_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingDetection(Base):
    """A detected creation whose mint is not resolved yet.

    Exists because resolution CANNOT happen on the websocket event loop. Doing
    the getTransaction call there blocked the loop, starved the keepalive, and
    cost a reconnect every ~90 seconds -- each one a recorded blind window.

    It is also retryable, which matters more: a transaction seen at `processed`
    commitment is frequently not yet queryable, so a single attempt lost 42% of
    all detections. That loss is not random -- it favours whatever confirms
    fastest -- so it was bias, not noise.
    """

    __tablename__ = "pending_detections"
    __table_args__ = (
        UniqueConstraint("signature", name="uq_pending_signature"),
        Index("ix_pending_unresolved", "resolved", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(String(128), index=True)
    detected_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    slot: Mapped[int | None] = mapped_column(BigInteger)
    program_label: Mapped[str | None] = mapped_column(String(64))
    instructions: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_mint: Mapped[str | None] = mapped_column(String(128))
    # Why we gave up, kept rather than deleted: a detection we could never
    # resolve is a hole in coverage, and holes have to be countable.
    give_up_reason: Mapped[str | None] = mapped_column(Text)


class PaperPosition(Base):
    """A hypothetical position. No money, no wallet, no order ever placed.

    The realism that matters is in the exit. A position can only be closed at a
    moment when the exit simulation actually found a route -- so if a token
    becomes unsellable while the paper position is open, the position STAYS
    OPEN, exactly as real money would. Closing it anyway at the last quoted
    price is the single most common way a memecoin backtest lies.
    """

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("token_id", "strategy", name="uq_paper_token_strategy"),
        Index("ix_paper_open", "is_open", "strategy"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(FK_TYPE, ForeignKey("tokens.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)

    opened_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_price_usd: Mapped[float] = mapped_column(Price)
    notional_usd: Mapped[float] = mapped_column(Money)
    entry_liquidity_usd: Mapped[float | None] = mapped_column(Money)
    # Age at entry, because "we could not have been this early" is the easiest
    # way for a replay to invent an edge.
    token_age_at_entry_s: Mapped[float | None] = mapped_column(Numeric(20, 3))

    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    closed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price_usd: Mapped[float | None] = mapped_column(Price)
    exit_reason: Mapped[str | None] = mapped_column(String(64))

    peak_price_usd: Mapped[float | None] = mapped_column(Price)
    peak_multiple: Mapped[float | None] = mapped_column(Numeric(20, 6))
    # What the position WOULD have made if an exit had always been available.
    # Recorded next to the real figure so the cost of unsellability is visible
    # rather than assumed away.
    unrealisable_peak_multiple: Mapped[float | None] = mapped_column(Numeric(20, 6))

    gross_pnl_usd: Mapped[float | None] = mapped_column(Money)
    costs_usd: Mapped[float | None] = mapped_column(Money)
    net_pnl_usd: Mapped[float | None] = mapped_column(Money)
    price_impact_at_exit_pct: Mapped[float | None] = mapped_column(Numeric(20, 10))
    blocked_exits: Mapped[int] = mapped_column(Integer, default=0)


class CollectorRun(Base):
    """Uptime record. Pairs with collection_gaps to bound what we can claim."""

    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    started_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shutdown_reason: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[str | None] = mapped_column(String(64))
    sample_rate: Mapped[float | None] = mapped_column(Numeric(10, 6))
    config_note: Mapped[str | None] = mapped_column(Text)


class CollectionGap(Base):
    """Windows where we were NOT collecting.

    Without this, downtime silently becomes "no launches happened", and Phase 2
    reads missing data as absence of opportunity. This table is what lets a
    query be restricted to windows we actually covered -- the difference
    between an honest base rate and a fabricated one.
    """

    __tablename__ = "collection_gaps"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    gap_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cause: Mapped[str] = mapped_column(String(128))
    affected_sources: Mapped[str | None] = mapped_column(Text)
