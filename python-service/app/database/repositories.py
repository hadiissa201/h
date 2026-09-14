"""Repositories — all SQL lives here.

Engines (risk, execution, portfolio) receive a repository, never a raw session,
so the business logic stays testable and the query patterns stay in one place.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models import (
    AIDecisionRecord,
    BacktestRunRecord,
    BalanceRecord,
    BotStateRecord,
    Candle,
    DailyStatsRecord,
    EmergencyEventRecord,
    EquitySnapshotRecord,
    FeatureSnapshotRecord,
    FillRecord,
    MarketSnapshotRecord,
    OrderRecord,
    PositionRecord,
    RegimeSnapshotRecord,
    ReportRecord,
    RiskDecisionRecord,
    SignalRecord,
    StrategyPerformanceRecord,
    SystemEventRecord,
    TradeCandidateRecord,
    TradeRecord,
)
from app.models.enums import BotStatus, OrderStatus, PositionStatus
from app.utils.time import start_of_utc_day, utcnow

logger = get_logger(__name__)


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:24]
    return f"{prefix}{raw}" if prefix else raw


def _supports_for_update(session: Session) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


# --------------------------------------------------------------------- state
class BotStateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, for_update: bool = False, mode: str = "paper") -> BotStateRecord:
        statement = select(BotStateRecord).where(BotStateRecord.id == 1)
        if for_update and _supports_for_update(self.session):
            statement = statement.with_for_update()
        record = self.session.scalars(statement).first()
        if record is None:
            record = BotStateRecord(
                id=1,
                status=BotStatus.RUNNING.value,
                mode=mode,
                updated_at=utcnow(),
                notes={},
            )
            self.session.add(record)
            self.session.flush()
        return record

    def halt(
        self,
        reason: str,
        detail: str = "",
        source: str = "api",
        requires_manual_reset: bool = True,
    ) -> BotStateRecord:
        record = self.get(for_update=True)
        now = utcnow()
        # First halt wins: a later trigger must not overwrite the original cause.
        if record.status != BotStatus.HALTED.value:
            record.status = BotStatus.HALTED.value
            record.halted_at = now
            record.halt_reason = reason
            record.halt_detail = detail
            record.halted_by = source
            record.requires_manual_reset = requires_manual_reset
        else:
            notes = dict(record.notes or {})
            additional = list(notes.get("additional_halt_triggers", []))
            additional.append(
                {"reason": reason, "detail": detail, "at": now.isoformat()}
            )
            notes["additional_halt_triggers"] = additional[-20:]
            record.notes = notes
        record.updated_at = now
        self.session.flush()
        return record

    def reset(self, operator: str, note: str = "", clear_losses: bool = True) -> BotStateRecord:
        record = self.get(for_update=True)
        now = utcnow()
        record.status = BotStatus.RUNNING.value
        record.halted_at = None
        record.halt_reason = None
        record.halt_detail = None
        record.halted_by = None
        record.requires_manual_reset = False
        if clear_losses:
            record.consecutive_losses = 0
            record.cooldown_until = None
        record.last_reset_at = now
        record.last_reset_by = operator
        notes = dict(record.notes or {})
        history = list(notes.get("reset_history", []))
        history.append({"at": now.isoformat(), "operator": operator, "note": note})
        notes["reset_history"] = history[-20:]
        record.notes = notes
        record.updated_at = now
        self.session.flush()
        return record

    def register_trade_outcome(
        self, pnl: Decimal, cooldown_minutes: int, loss_limit: int
    ) -> BotStateRecord:
        """Update the loss streak and arm the cooldown when the limit is hit."""
        record = self.get(for_update=True)
        if pnl < 0:
            record.consecutive_losses += 1
            if loss_limit > 0 and record.consecutive_losses >= loss_limit:
                record.cooldown_until = utcnow() + timedelta(minutes=cooldown_minutes)
        else:
            record.consecutive_losses = 0
            record.cooldown_until = None
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def set_mode(self, mode: str) -> BotStateRecord:
        record = self.get(for_update=True)
        record.mode = mode
        record.updated_at = utcnow()
        self.session.flush()
        return record


# -------------------------------------------------------------------- events
class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        event_type: str,
        *,
        message: str = "",
        level: str = "INFO",
        symbol: str | None = None,
        strategy: str | None = None,
        workflow: str | None = None,
        trade_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SystemEventRecord:
        record = SystemEventRecord(
            created_at=utcnow(),
            event_type=str(event_type),
            level=level,
            symbol=symbol,
            strategy=strategy,
            workflow=workflow,
            trade_id=trade_id,
            message=message,
            context=context or {},
        )
        self.session.add(record)
        return record

    def recent(
        self, limit: int = 100, event_types: list[str] | None = None
    ) -> list[SystemEventRecord]:
        statement = select(SystemEventRecord).order_by(
            SystemEventRecord.created_at.desc()
        )
        if event_types:
            statement = statement.where(SystemEventRecord.event_type.in_(event_types))
        return list(self.session.scalars(statement.limit(limit)))

    def record_emergency(
        self,
        reason: str,
        detail: str,
        source: str,
        *,
        equity: Decimal | None = None,
        drawdown_pct: Decimal | None = None,
        daily_pnl_pct: Decimal | None = None,
        positions_closed: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> EmergencyEventRecord:
        record = EmergencyEventRecord(
            id=new_id("emg_"),
            triggered_at=utcnow(),
            reason=reason,
            detail=detail,
            source=source,
            equity=equity,
            drawdown_pct=drawdown_pct,
            daily_pnl_pct=daily_pnl_pct,
            positions_closed=positions_closed,
            meta=metadata or {},
        )
        self.session.add(record)
        self.session.flush()
        return record

    def recent_emergencies(self, limit: int = 20) -> list[EmergencyEventRecord]:
        return list(
            self.session.scalars(
                select(EmergencyEventRecord)
                .order_by(EmergencyEventRecord.triggered_at.desc())
                .limit(limit)
            )
        )


# -------------------------------------------------------------------- market
class MarketRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_snapshot(self, payload: dict[str, Any]) -> MarketSnapshotRecord:
        record = MarketSnapshotRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def upsert_candles(
        self, symbol: str, timeframe: str, rows: list[dict[str, Any]], provider: str
    ) -> int:
        """Insert candles that are not stored yet. Returns the number added."""
        if not rows:
            return 0
        times = [row["open_time"] for row in rows]
        existing = set(
            self.session.scalars(
                select(Candle.open_time).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.open_time.in_(times),
                )
            )
        )
        added = 0
        for row in rows:
            if row["open_time"] in existing:
                continue
            self.session.add(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    provider=provider,
                    ingested_at=utcnow(),
                    **row,
                )
            )
            added += 1
        return added

    def latest_snapshot(
        self, symbol: str, timeframe: str | None = None
    ) -> MarketSnapshotRecord | None:
        statement = (
            select(MarketSnapshotRecord)
            .where(MarketSnapshotRecord.symbol == symbol)
            .order_by(MarketSnapshotRecord.fetched_at.desc())
        )
        if timeframe:
            statement = statement.where(MarketSnapshotRecord.timeframe == timeframe)
        return self.session.scalars(statement.limit(1)).first()

    def save_features(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        features: dict[str, Any],
        feature_config: dict[str, Any],
    ) -> FeatureSnapshotRecord | None:
        existing = self.session.scalars(
            select(FeatureSnapshotRecord).where(
                FeatureSnapshotRecord.symbol == symbol,
                FeatureSnapshotRecord.timeframe == timeframe,
                FeatureSnapshotRecord.bar_time == bar_time,
            )
        ).first()
        if existing is not None:
            return existing
        record = FeatureSnapshotRecord(
            symbol=symbol,
            timeframe=timeframe,
            bar_time=bar_time,
            computed_at=utcnow(),
            features=features,
            feature_config=feature_config,
        )
        self.session.add(record)
        return record

    def save_regime(self, payload: dict[str, Any]) -> RegimeSnapshotRecord:
        record = RegimeSnapshotRecord(**payload)
        self.session.add(record)
        return record

    def load_candles(
        self, symbol: str, timeframe: str, limit: int = 1000
    ) -> list[Candle]:
        return list(
            self.session.scalars(
                select(Candle)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.desc())
                .limit(limit)
            )
        )[::-1]


# ------------------------------------------------------------------ signals
class SignalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_candidate(self, payload: dict[str, Any]) -> TradeCandidateRecord:
        record = TradeCandidateRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def save_signal(self, payload: dict[str, Any]) -> SignalRecord:
        record = SignalRecord(**payload)
        self.session.add(record)
        return record

    def get_candidate(self, candidate_id: str) -> TradeCandidateRecord | None:
        return self.session.get(TradeCandidateRecord, candidate_id)

    def recent_signals(self, limit: int = 50) -> list[SignalRecord]:
        return list(
            self.session.scalars(
                select(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(limit)
            )
        )

    def signal_counts_by_strategy(self, since: datetime) -> dict[str, int]:
        rows = self.session.execute(
            select(SignalRecord.strategy, func.count(SignalRecord.id))
            .where(SignalRecord.created_at >= since)
            .group_by(SignalRecord.strategy)
        ).all()
        return {row[0]: int(row[1]) for row in rows}


# ----------------------------------------------------------------------- AI
class AIRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_decision(self, payload: dict[str, Any]) -> AIDecisionRecord:
        record = AIDecisionRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, decision_id: str) -> AIDecisionRecord | None:
        return self.session.get(AIDecisionRecord, decision_id)

    def last_for_symbol(self, symbol: str) -> AIDecisionRecord | None:
        return self.session.scalars(
            select(AIDecisionRecord)
            .where(AIDecisionRecord.symbol == symbol)
            .order_by(AIDecisionRecord.created_at.desc())
            .limit(1)
        ).first()

    def count_since(self, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count(AIDecisionRecord.id)).where(
                    AIDecisionRecord.created_at >= since
                )
            )
            or 0
        )

    def recent(self, limit: int = 25) -> list[AIDecisionRecord]:
        return list(
            self.session.scalars(
                select(AIDecisionRecord)
                .order_by(AIDecisionRecord.created_at.desc())
                .limit(limit)
            )
        )

    def decisions_between(
        self, start: datetime, end: datetime
    ) -> list[AIDecisionRecord]:
        return list(
            self.session.scalars(
                select(AIDecisionRecord)
                .where(
                    AIDecisionRecord.created_at >= start,
                    AIDecisionRecord.created_at < end,
                )
                .order_by(AIDecisionRecord.created_at)
            )
        )


# --------------------------------------------------------------------- risk
class RiskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_decision(self, payload: dict[str, Any]) -> RiskDecisionRecord:
        record = RiskDecisionRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def get_by_approval(
        self, approval_id: str, *, for_update: bool = False
    ) -> RiskDecisionRecord | None:
        statement = select(RiskDecisionRecord).where(
            RiskDecisionRecord.approval_id == approval_id
        )
        if for_update and _supports_for_update(self.session):
            statement = statement.with_for_update()
        return self.session.scalars(statement).first()

    def consume_approval(self, approval_id: str, order_id: str) -> None:
        record = self.get_by_approval(approval_id, for_update=True)
        if record is None:
            return
        record.consumed_at = utcnow()
        record.consumed_by_order_id = order_id
        self.session.flush()

    def recent(self, limit: int = 50) -> list[RiskDecisionRecord]:
        return list(
            self.session.scalars(
                select(RiskDecisionRecord)
                .order_by(RiskDecisionRecord.created_at.desc())
                .limit(limit)
            )
        )

    def rejection_counts(self, since: datetime) -> dict[str, int]:
        records = self.session.scalars(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.created_at >= since,
                RiskDecisionRecord.decision == "REJECTED",
            )
        )
        counts: dict[str, int] = {}
        for record in records:
            for code in record.rejection_codes or []:
                counts[code] = counts.get(code, 0) + 1
        return counts


# ---------------------------------------------------------------- execution
class ExecutionRepository:
    """Orders, fills, positions, trades and balances."""

    def __init__(self, session: Session, mode: str = "paper") -> None:
        self.session = session
        self.mode = mode

    # --- balances
    def get_balance(
        self, currency: str, *, for_update: bool = False
    ) -> BalanceRecord | None:
        statement = select(BalanceRecord).where(
            BalanceRecord.mode == self.mode, BalanceRecord.currency == currency
        )
        if for_update and _supports_for_update(self.session):
            statement = statement.with_for_update()
        return self.session.scalars(statement).first()

    def upsert_balance(
        self, currency: str, free: Decimal, locked: Decimal = Decimal("0")
    ) -> BalanceRecord:
        record = self.get_balance(currency, for_update=True)
        if record is None:
            record = BalanceRecord(
                mode=self.mode,
                currency=currency,
                free=free,
                locked=locked,
                updated_at=utcnow(),
            )
            self.session.add(record)
        else:
            record.free = free
            record.locked = locked
            record.updated_at = utcnow()
        self.session.flush()
        return record

    def list_balances(self) -> list[BalanceRecord]:
        return list(
            self.session.scalars(
                select(BalanceRecord).where(BalanceRecord.mode == self.mode)
            )
        )

    # --- orders
    def add_order(self, payload: dict[str, Any]) -> OrderRecord:
        record = OrderRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def get_order(self, order_id: str) -> OrderRecord | None:
        return self.session.get(OrderRecord, order_id)

    def get_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        return self.session.scalars(
            select(OrderRecord).where(
                OrderRecord.mode == self.mode,
                OrderRecord.client_order_id == client_order_id,
            )
        ).first()

    def open_orders(self, symbol: str | None = None) -> list[OrderRecord]:
        statement = select(OrderRecord).where(
            OrderRecord.mode == self.mode,
            OrderRecord.status.in_(
                [OrderStatus.NEW.value, OrderStatus.PARTIALLY_FILLED.value]
            ),
        )
        if symbol:
            statement = statement.where(OrderRecord.symbol == symbol)
        return list(self.session.scalars(statement.order_by(OrderRecord.created_at)))

    def recent_orders(self, limit: int = 50) -> list[OrderRecord]:
        return list(
            self.session.scalars(
                select(OrderRecord)
                .where(OrderRecord.mode == self.mode)
                .order_by(OrderRecord.created_at.desc())
                .limit(limit)
            )
        )

    def count_failed_orders_since(self, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count(OrderRecord.id)).where(
                    OrderRecord.mode == self.mode,
                    OrderRecord.status == OrderStatus.REJECTED.value,
                    OrderRecord.created_at >= since,
                )
            )
            or 0
        )

    # --- fills
    def add_fill(self, payload: dict[str, Any]) -> FillRecord:
        record = FillRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def fills_for_order(self, order_id: str) -> list[FillRecord]:
        return list(
            self.session.scalars(
                select(FillRecord)
                .where(FillRecord.order_id == order_id)
                .order_by(FillRecord.filled_at)
            )
        )

    # --- positions
    def add_position(self, payload: dict[str, Any]) -> PositionRecord:
        record = PositionRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def get_position(
        self, position_id: str, *, for_update: bool = False
    ) -> PositionRecord | None:
        if for_update and _supports_for_update(self.session):
            return self.session.scalars(
                select(PositionRecord)
                .where(PositionRecord.id == position_id)
                .with_for_update()
            ).first()
        return self.session.get(PositionRecord, position_id)

    def open_positions(self, symbol: str | None = None) -> list[PositionRecord]:
        statement = select(PositionRecord).where(
            PositionRecord.mode == self.mode,
            PositionRecord.status == PositionStatus.OPEN.value,
        )
        if symbol:
            statement = statement.where(PositionRecord.symbol == symbol)
        return list(self.session.scalars(statement.order_by(PositionRecord.opened_at)))

    def closed_positions(self, limit: int = 100) -> list[PositionRecord]:
        return list(
            self.session.scalars(
                select(PositionRecord)
                .where(
                    PositionRecord.mode == self.mode,
                    PositionRecord.status == PositionStatus.CLOSED.value,
                )
                .order_by(PositionRecord.closed_at.desc())
                .limit(limit)
            )
        )

    # --- trades
    def add_trade(self, payload: dict[str, Any]) -> TradeRecord:
        record = TradeRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def trades_between(self, start: datetime, end: datetime) -> list[TradeRecord]:
        return list(
            self.session.scalars(
                select(TradeRecord)
                .where(
                    TradeRecord.mode == self.mode,
                    TradeRecord.exit_time >= start,
                    TradeRecord.exit_time < end,
                )
                .order_by(TradeRecord.exit_time)
            )
        )

    def all_trades(self, limit: int | None = None) -> list[TradeRecord]:
        statement = (
            select(TradeRecord)
            .where(TradeRecord.mode == self.mode)
            .order_by(TradeRecord.exit_time)
        )
        if limit:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    def recent_trades(self, limit: int = 20) -> list[TradeRecord]:
        return list(
            self.session.scalars(
                select(TradeRecord)
                .where(TradeRecord.mode == self.mode)
                .order_by(TradeRecord.exit_time.desc())
                .limit(limit)
            )
        )

    def trade_for_position(self, position_id: str) -> TradeRecord | None:
        return self.session.scalars(
            select(TradeRecord).where(TradeRecord.position_id == position_id)
        ).first()


# --------------------------------------------------------------- performance
class PerformanceRepository:
    def __init__(self, session: Session, mode: str = "paper") -> None:
        self.session = session
        self.mode = mode

    def add_equity_snapshot(self, payload: dict[str, Any]) -> EquitySnapshotRecord:
        record = EquitySnapshotRecord(mode=self.mode, **payload)
        self.session.add(record)
        return record

    def latest_equity_snapshot(self) -> EquitySnapshotRecord | None:
        return self.session.scalars(
            select(EquitySnapshotRecord)
            .where(EquitySnapshotRecord.mode == self.mode)
            .order_by(EquitySnapshotRecord.taken_at.desc())
            .limit(1)
        ).first()

    def peak_equity(self) -> Decimal | None:
        return self.session.scalar(
            select(func.max(EquitySnapshotRecord.equity)).where(
                EquitySnapshotRecord.mode == self.mode
            )
        )

    def equity_curve(
        self, start: datetime | None = None, limit: int = 5000
    ) -> list[EquitySnapshotRecord]:
        statement = select(EquitySnapshotRecord).where(
            EquitySnapshotRecord.mode == self.mode
        )
        if start:
            statement = statement.where(EquitySnapshotRecord.taken_at >= start)
        return list(
            self.session.scalars(statement.order_by(EquitySnapshotRecord.taken_at).limit(limit))
        )

    # --- daily stats
    def get_or_create_day(
        self, day: date, starting_equity: Decimal
    ) -> DailyStatsRecord:
        record = self.session.scalars(
            select(DailyStatsRecord).where(
                DailyStatsRecord.mode == self.mode, DailyStatsRecord.day == day
            )
        ).first()
        if record is None:
            record = DailyStatsRecord(
                mode=self.mode,
                day=day,
                starting_equity=starting_equity,
                ending_equity=starting_equity,
                low_equity=starting_equity,
                high_equity=starting_equity,
                updated_at=utcnow(),
            )
            self.session.add(record)
            self.session.flush()
        return record

    def update_day_equity(self, day: date, equity: Decimal) -> DailyStatsRecord:
        record = self.get_or_create_day(day, equity)
        record.ending_equity = equity
        record.low_equity = (
            equity if record.low_equity is None else min(record.low_equity, equity)
        )
        record.high_equity = (
            equity if record.high_equity is None else max(record.high_equity, equity)
        )
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def register_trade(
        self, day: date, pnl: Decimal, fees: Decimal, equity: Decimal
    ) -> DailyStatsRecord:
        record = self.get_or_create_day(day, equity - pnl)
        record.realized_pnl = (record.realized_pnl or Decimal("0")) + pnl
        record.fees = (record.fees or Decimal("0")) + fees
        record.trades += 1
        if pnl > 0:
            record.wins += 1
        elif pnl < 0:
            record.losses += 1
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def today(self) -> DailyStatsRecord | None:
        return self.session.scalars(
            select(DailyStatsRecord).where(
                DailyStatsRecord.mode == self.mode,
                DailyStatsRecord.day == start_of_utc_day().date(),
            )
        ).first()

    def days(self, limit: int = 30) -> list[DailyStatsRecord]:
        return list(
            self.session.scalars(
                select(DailyStatsRecord)
                .where(DailyStatsRecord.mode == self.mode)
                .order_by(DailyStatsRecord.day.desc())
                .limit(limit)
            )
        )

    # --- strategy performance
    def upsert_strategy_performance(
        self, strategy: str, window: str, payload: dict[str, Any]
    ) -> StrategyPerformanceRecord:
        record = self.session.scalars(
            select(StrategyPerformanceRecord).where(
                StrategyPerformanceRecord.mode == self.mode,
                StrategyPerformanceRecord.strategy == strategy,
                StrategyPerformanceRecord.window == window,
            )
        ).first()
        if record is None:
            record = StrategyPerformanceRecord(
                mode=self.mode,
                strategy=strategy,
                window=window,
                updated_at=utcnow(),
                **payload,
            )
            self.session.add(record)
        else:
            for key, value in payload.items():
                setattr(record, key, value)
            record.updated_at = utcnow()
        self.session.flush()
        return record

    def strategy_performance(self, window: str = "all") -> list[StrategyPerformanceRecord]:
        return list(
            self.session.scalars(
                select(StrategyPerformanceRecord).where(
                    StrategyPerformanceRecord.mode == self.mode,
                    StrategyPerformanceRecord.window == window,
                )
            )
        )

    # --- reports and runs
    def save_report(self, payload: dict[str, Any]) -> ReportRecord:
        record = ReportRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def latest_report(self, kind: str = "daily") -> ReportRecord | None:
        return self.session.scalars(
            select(ReportRecord)
            .where(ReportRecord.kind == kind)
            .order_by(ReportRecord.created_at.desc())
            .limit(1)
        ).first()

    def save_run(self, payload: dict[str, Any]) -> BacktestRunRecord:
        record = BacktestRunRecord(**payload)
        self.session.add(record)
        self.session.flush()
        return record

    def recent_runs(self, kind: str | None = None, limit: int = 20) -> list[BacktestRunRecord]:
        statement = select(BacktestRunRecord).order_by(
            BacktestRunRecord.created_at.desc()
        )
        if kind:
            statement = statement.where(BacktestRunRecord.kind == kind)
        return list(self.session.scalars(statement.limit(limit)))
