"""AI layer contracts.

The LLM sits *between* deterministic analysis and the risk engine. It can
confirm or veto a candidate the quant layer already produced; it cannot invent
a trade, move a stop wider, or ask for more size. Everything it returns is
validated against ``AIDecision`` and anything unparseable becomes HOLD.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import MarketRegime, SignalDirection
from app.models.risk import RiskLimitsView
from app.models.signals import RegimeAssessment, StrategySignal


class AccountContext(BaseModel):
    equity: Decimal
    cash: Decimal
    open_positions: int
    exposure_pct: Decimal
    daily_pnl_pct: Decimal
    drawdown_pct: Decimal
    consecutive_losses: int
    mode: str
    bot_status: str


class PerformanceContext(BaseModel):
    trades_total: int = 0
    trades_last_7d: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    expectancy_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    best_strategy: str | None = None
    worst_strategy: str | None = None


class OpenPositionContext(BaseModel):
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal
    r_multiple: float | None = None
    strategy: str | None = None


class AIContext(BaseModel):
    """Exactly what the model is shown. Persisted verbatim for every decision so
    a later post-mortem can reconstruct the inputs."""

    context_id: str
    symbol: str
    timeframe: str
    timestamp: datetime
    price: Decimal
    proposed_direction: SignalDirection
    proposed_entry: Decimal
    proposed_stop_loss: Decimal
    proposed_take_profit: Decimal | None = None
    proposed_risk_reward: Decimal | None = None
    regime: RegimeAssessment
    features_by_timeframe: dict[str, dict[str, float | None]] = Field(
        default_factory=dict
    )
    signals: list[StrategySignal] = Field(default_factory=list)
    account: AccountContext
    open_positions: list[OpenPositionContext] = Field(default_factory=list)
    recent_performance: PerformanceContext = Field(default_factory=PerformanceContext)
    risk_limits: RiskLimitsView
    data_quality: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class AIDecision(BaseModel):
    """Strict schema for the model's reply.

    ``model_config`` forbids extra keys: a model that hallucinates a
    ``"position_size"`` field fails validation instead of having it silently
    ignored — and a failed validation means HOLD.
    """

    model_config = {"extra": "forbid"}

    decision: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)
    market_regime: str = ""
    strategy_alignment: list[str] = Field(default_factory=list)
    invalidation_condition: str = ""
    risk_assessment: Literal["low", "medium", "high", "extreme"] = "medium"
    key_risks: list[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise_decision(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("risk_assessment", mode="before")
    @classmethod
    def _normalise_risk(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> Any:
        """Accept ``0.81``, ``"0.81"`` and ``81`` (percent), reject the rest."""
        if isinstance(value, str):
            value = value.strip().rstrip("%")
            value = float(value)
        if isinstance(value, (int, float)) and value > 1.0:
            if value <= 100.0:
                return float(value) / 100.0
        return value

    @field_validator("strategy_alignment", "key_risks", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def direction(self) -> SignalDirection:
        return SignalDirection(self.decision)


class AIEvaluation(BaseModel):
    """Decision plus provenance and the outcome of validation."""

    decision_id: str
    context_id: str
    symbol: str
    timeframe: str
    created_at: datetime
    provider: str
    model: str
    ai_enabled: bool
    decision: AIDecision
    raw_response: str | None = None
    parse_ok: bool = True
    parse_error: str | None = None
    fallback_used: bool = False
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: Decimal | None = None
    # Set when the model asked for something the deterministic layer forbids.
    violations: list[str] = Field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.decision.decision != "HOLD" and not self.violations


class AIEvaluationRequest(BaseModel):
    """``POST /ai/evaluate`` body — n8n sends the candidate it got from analysis."""

    symbol: str
    timeframe: str = "1h"
    candidate: dict[str, Any] | None = None
    include_timeframes: list[str] | None = None
    force: bool = Field(
        default=False,
        description="Bypass the 'meaningful setup' gate. For manual review only; "
        "it never bypasses risk.",
    )


class AIDecisionOutcome(BaseModel):
    """Joined AI decision + realised trade result, for edge evaluation."""

    decision_id: str
    symbol: str
    created_at: datetime
    decision: str
    confidence: float
    regime: MarketRegime | str
    strategies: list[str] = Field(default_factory=list)
    traded: bool = False
    position_id: str | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    pnl: Decimal | None = None
    r_multiple: float | None = None
    max_favorable_excursion_r: float | None = None
    max_adverse_excursion_r: float | None = None
    holding_minutes: float | None = None
    outcome: str | None = None
