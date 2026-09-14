"""Strategy engine: regime gating, signal collection, candidate aggregation.

This is also where the "do not call the LLM on every tick" rule lives. The
deterministic layer must find a real setup first; only then does a candidate get
promoted for AI review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.features.engine import FeatureSet
from app.models.enums import MarketRegime, SignalDirection
from app.models.signals import (
    AnalysisResult,
    RegimeAssessment,
    StrategySignal,
    TradeCandidate,
)
from app.strategies.base import Strategy, StrategyContext
from app.strategies.breakout import BreakoutStrategy
from app.strategies.ema_momentum import EmaMomentumStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.trend_following import TrendFollowingStrategy
from app.strategies.volatility_breakout import VolatilityBreakoutStrategy

logger = get_logger(__name__)

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    TrendFollowingStrategy.name: TrendFollowingStrategy,
    EmaMomentumStrategy.name: EmaMomentumStrategy,
    BreakoutStrategy.name: BreakoutStrategy,
    MeanReversionStrategy.name: MeanReversionStrategy,
    VolatilityBreakoutStrategy.name: VolatilityBreakoutStrategy,
}


class EngineConfig(BaseModel):
    model_config = {"frozen": True}

    min_signal_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    min_candidate_confidence: float = Field(default=0.50, ge=0.0, le=1.0)
    alignment_bonus: float = Field(default=0.05, ge=0.0, le=0.2)
    max_alignment_bonus: float = Field(default=0.15, ge=0.0, le=0.5)
    conflict_penalty: float = Field(default=0.15, ge=0.0, le=1.0)
    # Setups below this confidence are not worth an LLM call.
    ai_review_min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    allow_short: bool = False


@dataclass
class EvaluationOutput:
    signals: list[StrategySignal]
    candidate: TradeCandidate | None
    skipped: dict[str, str] = field(default_factory=dict)
    suppressed_shorts: list[str] = field(default_factory=list)
    skip_reason: str | None = None


def build_strategies(
    names: list[str] | tuple[str, ...] | None = None,
    parameter_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[Strategy]:
    chosen = list(names) if names else list(STRATEGY_REGISTRY)
    unknown = [name for name in chosen if name not in STRATEGY_REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown strategies: {unknown}; available={sorted(STRATEGY_REGISTRY)}"
        )
    strategies: list[Strategy] = []
    for name in chosen:
        cls = STRATEGY_REGISTRY[name]
        overrides = (parameter_overrides or {}).get(name)
        if overrides:
            params = cls.default_params().model_copy(update=overrides)
            strategies.append(cls(params))
        else:
            strategies.append(cls())
    return strategies


class StrategyEngine:
    def __init__(
        self,
        strategies: list[Strategy] | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.strategies = strategies if strategies is not None else build_strategies()

    # ------------------------------------------------------------------ core
    def evaluate(
        self,
        features: FeatureSet,
        regime: RegimeAssessment,
        *,
        index: int = -1,
        higher_timeframe: FeatureSet | None = None,
        timestamp: datetime | None = None,
    ) -> EvaluationOutput:
        skipped: dict[str, str] = {}

        if regime.is_abnormal:
            return EvaluationOutput(
                signals=[],
                candidate=None,
                skipped={s.name: "abnormal market" for s in self.strategies},
                skip_reason=(
                    "abnormal market conditions: "
                    + "; ".join(regime.abnormal_reasons[:3])
                ),
            )
        if regime.regime is MarketRegime.UNKNOWN:
            return EvaluationOutput(
                signals=[],
                candidate=None,
                skipped={s.name: "regime unknown" for s in self.strategies},
                skip_reason="regime could not be determined",
            )

        position = index if index >= 0 else len(features.candles) + index
        if position < 1:
            return EvaluationOutput(
                signals=[], candidate=None, skip_reason="not enough bars"
            )

        row = features.row(index)
        previous_row = features.row(position - 1)
        context_timestamp = timestamp or features.candles.index[position].to_pydatetime()
        higher_row = (
            higher_timeframe.row(-1)
            if higher_timeframe is not None and not higher_timeframe.frame.empty
            else None
        )

        context = StrategyContext(
            symbol=features.symbol,
            timeframe=features.timeframe,
            timestamp=context_timestamp,
            row=row,
            previous_row=previous_row,
            candles=features.candles,
            features=features.frame,
            index=position,
            regime=regime,
            allow_short=self.config.allow_short,
            higher_timeframe_row=higher_row,
        )

        signals: list[StrategySignal] = []
        suppressed_shorts: list[str] = []
        for strategy in self.strategies:
            if not strategy.allows_regime(regime.regime):
                skipped[strategy.name] = f"not allowed in regime {regime.regime}"
                continue
            missing = strategy.missing_features(context)
            if missing:
                skipped[strategy.name] = f"missing features: {missing[:5]}"
                continue
            signal = strategy.evaluate(context)
            if not signal.is_actionable:
                skipped[strategy.name] = signal.reason or "no setup"
                continue
            if signal.signal is SignalDirection.SELL and not self.config.allow_short:
                # Defensive: a strategy should already have refused, but the
                # engine is the layer that guarantees spot-only behaviour.
                suppressed_shorts.append(strategy.name)
                skipped[strategy.name] = "short suppressed (spot mode)"
                continue
            if signal.confidence < self.config.min_signal_confidence:
                skipped[strategy.name] = (
                    f"confidence {signal.confidence:.2f} below "
                    f"{self.config.min_signal_confidence}"
                )
                continue
            signals.append(signal)
            log_event(
                logger,
                EventType.SIGNAL_GENERATED,
                symbol=signal.symbol,
                timeframe=signal.timeframe,
                strategy=signal.strategy,
                decision=str(signal.signal),
                confidence=signal.confidence,
                price=float(signal.entry) if signal.entry else None,
                stop_loss=float(signal.stop_loss) if signal.stop_loss else None,
                take_profit=float(signal.take_profit) if signal.take_profit else None,
                risk_reward=float(signal.risk_reward) if signal.risk_reward else None,
                regime=str(regime.regime),
                reason=signal.reason,
            )

        candidate, skip_reason = self._aggregate(features, regime, signals)
        return EvaluationOutput(
            signals=signals,
            candidate=candidate,
            skipped=skipped,
            suppressed_shorts=suppressed_shorts,
            skip_reason=skip_reason,
        )

    def analyse(
        self,
        features: FeatureSet,
        regime: RegimeAssessment,
        *,
        index: int = -1,
        higher_timeframe: FeatureSet | None = None,
        data_ok: bool = True,
    ) -> AnalysisResult:
        """Convenience wrapper used by ``POST /strategy/evaluate``."""
        output = self.evaluate(
            features, regime, index=index, higher_timeframe=higher_timeframe
        )
        candidate = output.candidate
        should_call_ai = bool(
            data_ok
            and candidate is not None
            and candidate.confidence >= self.config.ai_review_min_confidence
        )
        if candidate is not None:
            candidate.requires_ai_review = should_call_ai
        if candidate is None and output.skip_reason is None:
            output.skip_reason = "no strategy produced an actionable signal"
        if not should_call_ai and candidate is not None:
            log_event(
                logger,
                EventType.AI_SKIPPED,
                symbol=features.symbol,
                timeframe=features.timeframe,
                confidence=candidate.confidence,
                reason=(
                    f"candidate confidence {candidate.confidence:.2f} below AI review "
                    f"threshold {self.config.ai_review_min_confidence}"
                ),
            )
        return AnalysisResult(
            symbol=features.symbol,
            timeframe=features.timeframe,
            timestamp=features.last_timestamp,
            data_ok=data_ok,
            regime=regime,
            signals=output.signals,
            candidate=candidate,
            has_setup=candidate is not None,
            should_call_ai=should_call_ai,
            skip_reason=output.skip_reason,
            features=features.row(index),
        )

    # ------------------------------------------------------------- internals
    def _aggregate(
        self,
        features: FeatureSet,
        regime: RegimeAssessment,
        signals: list[StrategySignal],
    ) -> tuple[TradeCandidate | None, str | None]:
        if not signals:
            return None, None

        buys = [s for s in signals if s.signal is SignalDirection.BUY]
        sells = [s for s in signals if s.signal is SignalDirection.SELL]

        if buys and sells:
            best_buy = max(s.confidence for s in buys)
            best_sell = max(s.confidence for s in sells)
            # Genuinely opposed views on the same bar: stand aside rather than
            # pick a winner by a hair.
            if abs(best_buy - best_sell) < self.config.conflict_penalty:
                return None, (
                    "conflicting signals: "
                    f"BUY({best_buy:.2f}) vs SELL({best_sell:.2f})"
                )
            chosen = buys if best_buy > best_sell else sells
            conflicting = [s.strategy for s in (sells if best_buy > best_sell else buys)]
        else:
            chosen = buys or sells
            conflicting = []

        chosen = sorted(chosen, key=lambda s: s.confidence, reverse=True)
        primary = chosen[0]
        # The highest-confidence strategy owns the trade plan; agreement from
        # others only raises confidence. Mixing levels from different strategies
        # would produce a plan none of them actually proposed.
        bonus = min(
            self.config.max_alignment_bonus,
            self.config.alignment_bonus * (len(chosen) - 1),
        )
        confidence = min(0.95, primary.confidence + bonus)
        if conflicting:
            confidence = max(0.0, confidence - self.config.conflict_penalty)

        if confidence < self.config.min_candidate_confidence:
            return None, (
                f"best candidate confidence {confidence:.2f} below "
                f"{self.config.min_candidate_confidence}"
            )
        if primary.entry is None or primary.stop_loss is None:
            return None, "primary signal lacks entry/stop"

        candidate = TradeCandidate(
            symbol=primary.symbol,
            timeframe=primary.timeframe,
            timestamp=primary.timestamp,
            direction=primary.signal,
            price=primary.entry,
            entry=primary.entry,
            stop_loss=primary.stop_loss,
            take_profit=primary.take_profit,
            confidence=round(confidence, 4),
            regime=regime,
            signals=chosen,
            aligned_strategies=[s.strategy for s in chosen],
            conflicting_strategies=conflicting,
            reason=primary.reason,
            invalidation_condition=primary.invalidation_condition,
        )
        return candidate, None


def strategy_catalogue() -> list[dict[str, Any]]:
    """Machine-readable description of every strategy (served by the API)."""
    catalogue: list[dict[str, Any]] = []
    for name, cls in sorted(STRATEGY_REGISTRY.items()):
        catalogue.append(
            {
                "name": name,
                "description": cls.description,
                "allowed_regimes": sorted(str(r) for r in cls.allowed_regimes),
                "required_features": list(cls.required_features),
                "default_parameters": cls.default_params().model_dump(),
            }
        )
    return catalogue
