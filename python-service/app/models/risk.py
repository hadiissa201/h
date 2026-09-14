"""Risk-engine contracts.

The proposal carries only what the *caller* legitimately knows (a trade idea).
Account state, exposure and drawdown are always resolved server-side from the
portfolio — never accepted from the caller — so no amount of creative payload
construction by n8n or an LLM can inflate the account and unlock a bigger size.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import (
    BotStatus,
    MarketRegime,
    RiskDecisionType,
    SignalDirection,
    TradingModeEnum,
)


class RiskProposal(BaseModel):
    """A trade idea submitted for risk validation."""

    symbol: str
    direction: SignalDirection
    entry: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: str = "unknown"
    timeframe: str = "1h"
    regime: MarketRegime = MarketRegime.UNKNOWN
    regime_abnormal: bool = False
    data_quality_ok: bool = True
    ai_decision_id: str | None = None
    ai_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_id: str | None = None
    # Market microstructure. Omitted values are fetched by the service; an
    # explicitly supplied value is still bounds-checked.
    spread_bps: Decimal | None = Field(default=None, ge=0)
    quote_volume_24h: Decimal | None = Field(default=None, ge=0)
    last_bar_move_pct: Decimal | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _check_direction(self) -> RiskProposal:
        if self.direction is SignalDirection.HOLD:
            raise ValueError("cannot risk-check a HOLD proposal")
        if self.direction is SignalDirection.BUY and self.stop_loss >= self.entry:
            raise ValueError("BUY stop_loss must be below entry")
        if self.direction is SignalDirection.SELL and self.stop_loss <= self.entry:
            raise ValueError("SELL stop_loss must be above entry")
        return self

    @property
    def stop_distance(self) -> Decimal:
        return abs(self.entry - self.stop_loss)

    @property
    def risk_reward(self) -> Decimal | None:
        if self.take_profit is None:
            return None
        if self.stop_distance == 0:
            return None
        return abs(self.take_profit - self.entry) / self.stop_distance


class RiskCheck(BaseModel):
    """One risk rule's outcome.

    ``value``/``limit`` are intentionally loose: most checks compare numbers, but
    some compare states ("RUNNING" vs "HALTED"), and the audit trail is more
    useful when it records what was actually compared.
    """

    name: str
    passed: bool
    detail: str = ""
    value: Decimal | float | str | None = None
    limit: Decimal | float | str | None = None
    blocking: bool = True


class PositionSizing(BaseModel):
    """How the quantity was derived — every intermediate value is exposed so a
    human can re-do the arithmetic by hand."""

    equity: Decimal
    risk_pct: Decimal
    risk_amount: Decimal
    entry: Decimal
    stop_loss: Decimal
    stop_distance: Decimal
    stop_distance_pct: Decimal
    raw_quantity: Decimal
    quantity: Decimal
    notional: Decimal
    notional_pct_equity: Decimal
    effective_risk_amount: Decimal
    effective_risk_pct: Decimal
    capped_by: list[str] = Field(default_factory=list)
    rejected_reason: str | None = None

    @property
    def is_viable(self) -> bool:
        return self.quantity > 0 and self.rejected_reason is None


class AccountRiskState(BaseModel):
    """Server-resolved account facts used by the risk checks."""

    timestamp: datetime
    mode: TradingModeEnum
    bot_status: BotStatus
    equity: Decimal
    cash: Decimal
    starting_equity: Decimal
    peak_equity: Decimal
    open_positions: int
    open_symbols: list[str] = Field(default_factory=list)
    exposure_pct: Decimal = Decimal("0")
    daily_pnl_pct: Decimal = Decimal("0")
    drawdown_pct: Decimal = Decimal("0")
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    halt_reason: str | None = None


class RiskDecision(BaseModel):
    decision: RiskDecisionType
    approval_id: str | None = None
    expires_at: datetime | None = None
    evaluated_at: datetime
    symbol: str
    direction: SignalDirection
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None = None
    sizing: PositionSizing | None = None
    checks: list[RiskCheck] = Field(default_factory=list)
    rejection_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    account: AccountRiskState | None = None
    mode: TradingModeEnum = TradingModeEnum.PAPER
    proposal_fingerprint: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision is RiskDecisionType.APPROVED

    @property
    def quantity(self) -> Decimal:
        return self.sizing.quantity if self.sizing else Decimal("0")


class RiskLimitsView(BaseModel):
    """Read-only view of the active limits (surfaced to the AI and dashboard)."""

    risk_per_trade: Decimal
    max_position_pct_equity: Decimal
    max_portfolio_exposure_pct: Decimal
    max_open_positions: int
    max_positions_per_symbol: int
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    consecutive_loss_limit: int
    cooldown_minutes: int
    min_confidence: Decimal
    min_risk_reward: Decimal
    max_spread_bps: Decimal
    min_24h_quote_volume: Decimal
    require_stop_loss: bool
    min_stop_distance_pct: Decimal
    max_stop_distance_pct: Decimal
    abnormal_price_move_pct: Decimal = Decimal("0.10")
    allow_short: bool = False
