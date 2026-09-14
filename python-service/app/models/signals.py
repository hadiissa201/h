"""Strategy signals, regime assessments and trade candidates.

A signal is a *proposal*, never an instruction: strategies cannot execute, and
nothing downstream treats a signal as permission to trade.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    MarketRegime,
    SignalDirection,
    TrendState,
    VolatilityState,
)


class RegimeMetrics(BaseModel):
    adx: float | None = None
    di_spread: float | None = None
    ema_alignment: float | None = None
    trend_slope: float | None = None
    efficiency_ratio: float | None = None
    atr_pct: float | None = None
    atr_pct_rank: float | None = None
    vol_ratio: float | None = None
    bb_width: float | None = None
    structure: float | None = None
    last_bar_move_pct: float | None = None


class RegimeAssessment(BaseModel):
    symbol: str
    timeframe: str
    timestamp: datetime
    regime: MarketRegime
    trend_state: TrendState
    volatility_state: VolatilityState
    confidence: float = Field(ge=0.0, le=1.0)
    is_abnormal: bool = False
    abnormal_reasons: list[str] = Field(default_factory=list)
    metrics: RegimeMetrics = Field(default_factory=RegimeMetrics)
    notes: list[str] = Field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        """Abnormal markets are never traded, whatever a strategy thinks."""
        return not self.is_abnormal


class StrategySignal(BaseModel):
    """Structured output of one strategy on one symbol/timeframe."""

    strategy: str
    symbol: str
    timeframe: str
    timestamp: datetime
    signal: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    entry: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reason: str = ""
    regime: MarketRegime = MarketRegime.UNKNOWN
    invalidation_condition: str = ""
    # Strategy-specific exit plan, consumed by position management.
    trailing_stop_atr_multiple: float | None = None
    breakeven_at_r: float | None = None
    partial_exit_at_r: float | None = None
    partial_exit_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    time_stop_bars: int | None = None
    features_used: dict[str, float | None] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_levels(self) -> StrategySignal:
        if self.signal is SignalDirection.HOLD:
            return self
        if self.entry is None or self.entry <= 0:
            raise ValueError("actionable signal requires a positive entry price")
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError("actionable signal requires a positive stop_loss")
        if self.signal is SignalDirection.BUY and self.stop_loss >= self.entry:
            raise ValueError("BUY stop_loss must be below entry")
        if self.signal is SignalDirection.SELL and self.stop_loss <= self.entry:
            raise ValueError("SELL stop_loss must be above entry")
        if self.take_profit is not None:
            if self.signal is SignalDirection.BUY and self.take_profit <= self.entry:
                raise ValueError("BUY take_profit must be above entry")
            if self.signal is SignalDirection.SELL and self.take_profit >= self.entry:
                raise ValueError("SELL take_profit must be below entry")
        return self

    @property
    def is_actionable(self) -> bool:
        return self.signal is not SignalDirection.HOLD

    @property
    def stop_distance(self) -> Decimal | None:
        if self.entry is None or self.stop_loss is None:
            return None
        return abs(self.entry - self.stop_loss)

    @property
    def risk_reward(self) -> Decimal | None:
        if self.take_profit is None or self.entry is None:
            return None
        distance = self.stop_distance
        if not distance or distance == 0:
            return None
        return abs(self.take_profit - self.entry) / distance


class TradeCandidate(BaseModel):
    """Aggregated, deterministic view of one symbol — the input to the AI layer."""

    candidate_id: str | None = None
    symbol: str
    timeframe: str
    timestamp: datetime
    direction: SignalDirection
    price: Decimal
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    regime: RegimeAssessment
    signals: list[StrategySignal] = Field(default_factory=list)
    aligned_strategies: list[str] = Field(default_factory=list)
    conflicting_strategies: list[str] = Field(default_factory=list)
    reason: str = ""
    invalidation_condition: str = ""
    requires_ai_review: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def risk_reward(self) -> Decimal | None:
        if self.take_profit is None:
            return None
        distance = abs(self.entry - self.stop_loss)
        if distance == 0:
            return None
        return abs(self.take_profit - self.entry) / distance

    @property
    def primary_signal(self) -> StrategySignal | None:
        return self.signals[0] if self.signals else None


class AnalysisResult(BaseModel):
    """What the market-analysis workflow gets back from ``/strategy/evaluate``.

    ``should_call_ai`` is the gate that keeps LLM spend bounded: it is only true
    when the deterministic layer found a real setup, the data is clean, the bot is
    running and there is no open position in the symbol already.
    """

    symbol: str
    timeframe: str
    timestamp: datetime
    data_ok: bool
    regime: RegimeAssessment | None = None
    signals: list[StrategySignal] = Field(default_factory=list)
    candidate: TradeCandidate | None = None
    has_setup: bool = False
    should_call_ai: bool = False
    skip_reason: str | None = None
    features: dict[str, float | None] = Field(default_factory=dict)
    features_by_timeframe: dict[str, dict[str, float | None]] = Field(
        default_factory=dict
    )
    data_quality: dict[str, Any] = Field(default_factory=dict)
    suppressed_shorts: list[str] = Field(default_factory=list)
    skipped_strategies: dict[str, str] = Field(default_factory=dict)
    bot_status: str = "RUNNING"
    trading_allowed: bool = True
    has_open_position: bool = False
