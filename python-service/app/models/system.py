"""Bot state, health and emergency-control schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import BotStatus, TradingModeEnum


class BotState(BaseModel):
    status: BotStatus
    mode: TradingModeEnum
    live_trading_armed: bool
    updated_at: datetime
    halted_at: datetime | None = None
    halt_reason: str | None = None
    halt_detail: str | None = None
    halted_by: str | None = None
    requires_manual_reset: bool = False
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    trading_allowed: bool = True
    notes: dict[str, Any] = Field(default_factory=dict)


class HaltRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=64)
    detail: str = ""
    source: str = "api"
    close_positions: bool = Field(
        default=False,
        description="Attempt to flatten open positions at market. Off by "
        "default: forced liquidation in a disorderly market can be worse than "
        "holding a stopped-out position.",
    )


class ResetRequest(BaseModel):
    confirmation: str = Field(
        description="Must equal 'RESET' — a manual, explicit acknowledgement."
    )
    note: str = ""
    operator: str = "unknown"
    reset_consecutive_losses: bool = True


class ComponentHealth(BaseModel):
    name: str
    healthy: bool
    detail: str = ""
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    mode: TradingModeEnum
    live_trading_armed: bool
    bot_status: BotStatus
    timestamp: datetime
    components: list[ComponentHealth] = Field(default_factory=list)
    live_mode_blockers: list[str] = Field(default_factory=list)


class EmergencyEvent(BaseModel):
    event_id: str
    triggered_at: datetime
    reason: str
    detail: str
    source: str
    equity: Decimal | None = None
    drawdown_pct: Decimal | None = None
    daily_pnl_pct: Decimal | None = None
    positions_closed: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SafetyCheckItem(BaseModel):
    key: str
    description: str
    satisfied: bool
    detail: str = ""


class LiveReadinessReport(BaseModel):
    """The live-trading gate. All items must be satisfied *and* the operator must
    still flip the switches by hand."""

    ready: bool
    checked_at: datetime
    items: list[SafetyCheckItem]
    blockers: list[str] = Field(default_factory=list)
    reminder: str = (
        "Paper results do not transfer to live markets. Real fills, latency and "
        "liquidity will be worse than simulated ones."
    )
