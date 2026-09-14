"""ORM record <-> pydantic model conversion for positions."""

from __future__ import annotations

from app.models.enums import PositionSide, PositionStatus, TradingModeEnum
from app.models.trading import Position, PositionExitPlan


def position_from_record(record) -> Position:
    return Position(
        position_id=record.id,
        symbol=record.symbol,
        side=PositionSide(record.side),
        status=PositionStatus(record.status),
        quantity=record.quantity,
        entry_price=record.entry_price,
        opened_at=record.opened_at,
        closed_at=record.closed_at,
        exit_price=record.exit_price,
        exit_reason=record.exit_reason,
        initial_quantity=record.initial_quantity,
        initial_risk_amount=record.initial_risk_amount,
        stop_loss=record.stop_loss,
        take_profit=record.take_profit,
        exit_plan=PositionExitPlan(**record.exit_plan) if record.exit_plan else None,
        realized_pnl=record.realized_pnl,
        fees_paid=record.fees_paid,
        strategy=record.strategy,
        regime=record.regime,
        ai_decision_id=record.ai_decision_id,
        risk_approval_id=record.risk_approval_id,
        mark_price=record.mark_price,
        max_favorable_price=record.max_favorable_price,
        max_adverse_price=record.max_adverse_price,
        bars_held=record.bars_held,
        mode=TradingModeEnum(record.mode),
        metadata=record.meta or {},
    )
