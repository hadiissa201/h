"""Database schema.

Everything the system decides is persisted: not for nostalgia, but because you
cannot evaluate whether an AI (or a strategy) adds value unless the inputs, the
decision and the outcome are all recoverable afterwards.

Tables group into:
  data        — candles, market snapshots, feature/regime snapshots
  decisions   — signals, candidates, ai_decisions, risk_decisions
  execution   — orders, fills, positions, trades, balances, equity snapshots
  operations  — bot_state, system_events, emergency_events, reports, runs
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.database.types import JSONColumn, Money, UTCDateTime


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------- data
class Candle(Base):
    """Stored OHLCV. Lets backtests re-run on exactly the bars we traded on."""

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candles_symbol_tf_time", "symbol", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    open_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    open: Mapped[Decimal] = mapped_column(Money, nullable=False)
    high: Mapped[Decimal] = mapped_column(Money, nullable=False)
    low: Mapped[Decimal] = mapped_column(Money, nullable=False)
    close: Mapped[Decimal] = mapped_column(Money, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Money, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


class MarketSnapshotRecord(Base):
    """One market-data fetch, with its quality verdict. Workflow 1 writes these."""

    __tablename__ = "market_snapshots"
    __table_args__ = (
        Index("ix_market_snapshots_symbol_time", "symbol", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    last_price: Mapped[Decimal | None] = mapped_column(Money)
    bid: Mapped[Decimal | None] = mapped_column(Money)
    ask: Mapped[Decimal | None] = mapped_column(Money)
    spread_bps: Mapped[Decimal | None] = mapped_column(Money)
    quote_volume_24h: Mapped[Decimal | None] = mapped_column(Money)
    funding_rate: Mapped[Decimal | None] = mapped_column(Money)
    open_interest: Mapped[Decimal | None] = mapped_column(Money)
    bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_candle_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_candle_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    staleness_seconds: Mapped[float | None] = mapped_column(Float)
    data_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )


class FeatureSnapshotRecord(Base):
    __tablename__ = "feature_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "bar_time", name="uq_feature_snapshot"),
        Index("ix_feature_snapshots_symbol_time", "symbol", "bar_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    bar_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    features: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    feature_config: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class RegimeSnapshotRecord(Base):
    __tablename__ = "regime_snapshots"
    __table_args__ = (Index("ix_regime_snapshots_symbol_time", "symbol", "bar_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    bar_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    trend_state: Mapped[str] = mapped_column(String(16), nullable=False)
    volatility_state: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_abnormal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    abnormal_reasons: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )


# ---------------------------------------------------------------- decisions
class TradeCandidateRecord(Base):
    __tablename__ = "trade_candidates"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    bar_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[Decimal] = mapped_column(Money, nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Money, nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Money)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    aligned_strategies: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    conflicting_strategies: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    invalidation_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requires_ai_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    signals: Mapped[list[SignalRecord]] = relationship(back_populates="candidate")


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_symbol_time", "symbol", "bar_time"),
        Index("ix_signals_strategy", "strategy"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    bar_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    entry: Mapped[Decimal | None] = mapped_column(Money)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    take_profit: Mapped[Decimal | None] = mapped_column(Money)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    invalidation_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    features_used: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("trade_candidates.id", ondelete="SET NULL")
    )

    candidate: Mapped[TradeCandidateRecord | None] = relationship(
        back_populates="signals"
    )


class AIDecisionRecord(Base):
    """Every AI call: the context shown, the raw reply, and how it was handled."""

    __tablename__ = "ai_decisions"
    __table_args__ = (Index("ix_ai_decisions_symbol_time", "symbol", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    context_id: Mapped[str] = mapped_column(String(40), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    strategy_alignment: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    invalidation_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_assessment: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    key_risks: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    raw_response: Mapped[str | None] = mapped_column(Text)
    parse_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    parse_error: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    violations: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)


class RiskDecisionRecord(Base):
    """Risk verdicts. Approvals are single-use and tied to a proposal fingerprint."""

    __tablename__ = "risk_decisions"
    __table_args__ = (
        Index("ix_risk_decisions_symbol_time", "symbol", "created_at"),
        Index("ix_risk_decisions_approval", "approval_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    regime: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    entry: Mapped[Decimal] = mapped_column(Money, nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Money, nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Money)
    quantity: Mapped[Decimal | None] = mapped_column(Money)
    notional: Mapped[Decimal | None] = mapped_column(Money)
    risk_amount: Mapped[Decimal | None] = mapped_column(Money)
    risk_pct: Mapped[Decimal | None] = mapped_column(Money)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    equity: Mapped[Decimal | None] = mapped_column(Money)
    confidence: Mapped[float | None] = mapped_column(Float)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    rejection_codes: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    reasons: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    account_state: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    sizing: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    ai_decision_id: Mapped[str | None] = mapped_column(String(40))
    candidate_id: Mapped[str | None] = mapped_column(String(40))

    # Single-use approval bookkeeping
    approval_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    proposal_fingerprint: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consumed_by_order_id: Mapped[str | None] = mapped_column(String(48))


# ---------------------------------------------------------------- execution
class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("mode", "client_order_id", name="uq_order_client_id"),
        Index("ix_orders_symbol_time", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    price: Mapped[Decimal | None] = mapped_column(Money)
    stop_price: Mapped[Decimal | None] = mapped_column(Money)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Money)
    fee_paid: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    position_id: Mapped[str | None] = mapped_column(String(40))
    risk_approval_id: Mapped[str | None] = mapped_column(String(40))
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    fills: Mapped[list[FillRecord]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_symbol_time", "symbol", "filled_at"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(48), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fee: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    fee_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    is_maker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    slippage_bps: Mapped[Decimal | None] = mapped_column(Money)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")

    order: Mapped[OrderRecord] = relationship(back_populates="fills")


class PositionRecord(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_status", "status"),
        Index("ix_positions_symbol_status", "symbol", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    initial_quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    take_profit: Mapped[Decimal | None] = mapped_column(Money)
    exit_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    initial_risk_amount: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    fees_paid: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    mark_price: Mapped[Decimal | None] = mapped_column(Money)
    max_favorable_price: Mapped[Decimal | None] = mapped_column(Money)
    max_adverse_price: Mapped[Decimal | None] = mapped_column(Money)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strategy: Mapped[str | None] = mapped_column(String(48))
    regime: Mapped[str | None] = mapped_column(String(32))
    ai_decision_id: Mapped[str | None] = mapped_column(String(40))
    risk_approval_id: Mapped[str | None] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class TradeRecord(Base):
    """A completed round trip. The unit of performance analysis."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_closed_at", "exit_time"),
        Index("ix_trades_strategy", "strategy"),
        Index("ix_trades_symbol", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(40), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(48))
    regime: Mapped[str | None] = mapped_column(String(32))
    entry_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    exit_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    take_profit: Mapped[Decimal | None] = mapped_column(Money)
    gross_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    pnl_pct: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    r_multiple: Mapped[float | None] = mapped_column(Float)
    max_favorable_excursion_r: Mapped[float | None] = mapped_column(Float)
    max_adverse_excursion_r: Mapped[float | None] = mapped_column(Float)
    holding_minutes: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_decision_id: Mapped[str | None] = mapped_column(String(40))
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    risk_approval_id: Mapped[str | None] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class BalanceRecord(Base):
    __tablename__ = "balances"
    __table_args__ = (UniqueConstraint("mode", "currency", name="uq_balance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    free: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    locked: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class EquitySnapshotRecord(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_snapshots_time", "taken_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    positions_value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(Money, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exposure_pct: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )


class DailyStatsRecord(Base):
    """Per-UTC-day roll-up. Backs the daily-loss limit and the daily report."""

    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("mode", "day", name="uq_daily_stats"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    day: Mapped[datetime] = mapped_column(Date, nullable=False)
    starting_equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    ending_equity: Mapped[Decimal | None] = mapped_column(Money)
    low_equity: Mapped[Decimal | None] = mapped_column(Money)
    high_equity: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    fees: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class StrategyPerformanceRecord(Base):
    __tablename__ = "strategy_performance"
    __table_args__ = (
        UniqueConstraint("mode", "strategy", "window", name="uq_strategy_performance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    window: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    net_pnl: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    win_rate: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    expectancy_r: Mapped[float | None] = mapped_column(Float)
    average_r: Mapped[float | None] = mapped_column(Float)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Money)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


# --------------------------------------------------------------- operations
class BotStateRecord(Base):
    """Single-row table (id=1) holding the kill switch and trading permission."""

    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="RUNNING")
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    halted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    halt_reason: Mapped[str | None] = mapped_column(String(64))
    halt_detail: Mapped[str | None] = mapped_column(Text)
    halted_by: Mapped[str | None] = mapped_column(String(64))
    requires_manual_reset: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reset_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    notes: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class SystemEventRecord(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_events_time", "created_at"),
        Index("ix_system_events_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    level: Mapped[str] = mapped_column(String(12), nullable=False, default="INFO")
    symbol: Mapped[str | None] = mapped_column(String(32))
    strategy: Mapped[str | None] = mapped_column(String(48))
    workflow: Mapped[str | None] = mapped_column(String(64))
    trade_id: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class EmergencyEventRecord(Base):
    __tablename__ = "emergency_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    triggered_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(48), nullable=False, default="system")
    equity: Mapped[Decimal | None] = mapped_column(Money)
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Money)
    daily_pnl_pct: Mapped[Decimal | None] = mapped_column(Money)
    positions_closed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )


class ReportRecord(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_kind_time", "kind", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="daily")
    period_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(String(24))
    body_markdown: Mapped[str | None] = mapped_column(Text)


class BacktestRunRecord(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(96))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="backtest")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    data_source: Mapped[str] = mapped_column(String(24), nullable=False)
    bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    period_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    strategies: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
