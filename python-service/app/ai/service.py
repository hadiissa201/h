"""AI service: build context, call the model, validate, record — and constrain.

The constraint that matters is ``apply_to_candidate``: whatever the model returns
is folded into a risk proposal under veto-only rules.

* direction must match the deterministic candidate, or it is a HOLD;
* effective confidence is ``min(candidate, model)`` — the model can lower it,
  never raise it;
* entry, stop and target come from the candidate, never from the model;
* any attempt to do otherwise is recorded as a ``violation`` and the trade is
  dropped.

So the worst an unhinged (or prompt-injected) model can do is prevent trades.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.ai.parser import hold_decision, parse_ai_decision
from app.ai.prompt import SYSTEM_PROMPT, build_user_prompt, select_prompt_features
from app.ai.providers.base import LLMProvider
from app.ai.providers.implementations import build_provider
from app.analytics.performance import PerformanceAnalytics
from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.database.repositories import AIRepository, EventRepository, new_id
from app.models.ai import (
    AccountContext,
    AIContext,
    AIDecision,
    AIEvaluation,
    OpenPositionContext,
)
from app.models.risk import RiskLimitsView, RiskProposal
from app.models.signals import TradeCandidate
from app.portfolio.service import PortfolioService
from app.utils.time import utcnow

logger = get_logger(__name__)


class AIService:
    def __init__(
        self,
        ai_repo: AIRepository,
        event_repo: EventRepository,
        portfolio: PortfolioService,
        analytics: PerformanceAnalytics,
        settings: Settings | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = ai_repo
        self.events = event_repo
        self.portfolio = portfolio
        self.analytics = analytics
        self._provider = provider

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = build_provider(self.settings)
        return self._provider

    # ---------------------------------------------------------------- context
    def build_context(
        self,
        candidate: TradeCandidate,
        features_by_timeframe: dict[str, dict[str, Any]],
        risk_limits: RiskLimitsView,
        data_quality: dict[str, Any] | None = None,
        notes: list[str] | None = None,
    ) -> AIContext:
        snapshot = self.portfolio.snapshot()
        account = AccountContext(
            equity=snapshot.equity,
            cash=snapshot.cash,
            open_positions=snapshot.open_positions,
            exposure_pct=snapshot.exposure_pct,
            daily_pnl_pct=snapshot.daily_pnl_pct,
            drawdown_pct=snapshot.drawdown_pct,
            consecutive_losses=self.portfolio.bot.get().consecutive_losses,
            mode=str(snapshot.mode),
            bot_status=self.portfolio.bot.get().status,
        )
        positions = [
            OpenPositionContext(
                symbol=position.symbol,
                side=str(position.side),
                quantity=position.quantity,
                entry_price=position.entry_price,
                mark_price=position.mark_price,
                unrealized_pnl=position.unrealized_pnl(),
                r_multiple=float(position.r_multiple() or 0) or None,
                strategy=position.strategy,
            )
            for position in snapshot.positions
        ]
        context = AIContext(
            context_id=new_id("ctx_"),
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            timestamp=candidate.timestamp,
            price=candidate.price,
            proposed_direction=candidate.direction,
            proposed_entry=candidate.entry,
            proposed_stop_loss=candidate.stop_loss,
            proposed_take_profit=candidate.take_profit,
            proposed_risk_reward=candidate.risk_reward,
            regime=candidate.regime,
            features_by_timeframe={
                timeframe: select_prompt_features(features)
                for timeframe, features in features_by_timeframe.items()
            },
            signals=candidate.signals,
            account=account,
            open_positions=positions,
            recent_performance=self.analytics.performance_context(),
            risk_limits=risk_limits,
            data_quality=data_quality or {},
            notes=notes or [],
        )
        log_event(
            logger,
            EventType.AI_CONTEXT_BUILT,
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            context_id=context.context_id,
            timeframes=list(context.features_by_timeframe),
            signals=len(candidate.signals),
        )
        return context

    # --------------------------------------------------------------- decision
    def evaluate(self, context: AIContext, *, persist: bool = True) -> AIEvaluation:
        provider = self.provider
        decision_id = new_id("ai_")

        if not provider.enabled:
            return self._record(
                decision_id,
                context,
                hold_decision("AI layer disabled by configuration"),
                provider_name="disabled",
                model="none",
                ai_enabled=False,
                parse_ok=True,
                fallback_used=True,
                persist=persist,
            )

        blocked = self._rate_limit_reason(context.symbol)
        if blocked:
            log_event(
                logger,
                EventType.AI_SKIPPED,
                symbol=context.symbol,
                reason=blocked,
                level=30,
            )
            return self._record(
                decision_id,
                context,
                hold_decision(blocked),
                provider_name=provider.name,
                model=provider.model,
                ai_enabled=True,
                parse_ok=True,
                fallback_used=True,
                persist=persist,
            )

        user_prompt = build_user_prompt(context)
        try:
            response = provider.complete_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_output_tokens,
            )
        except LLMError as exc:
            log_event(
                logger,
                EventType.AI_PARSE_FAILED,
                symbol=context.symbol,
                reason=f"provider error: {exc.detail}",
                level=40,
            )
            return self._record(
                decision_id,
                context,
                hold_decision(f"LLM provider error: {exc.detail}"),
                provider_name=provider.name,
                model=provider.model,
                ai_enabled=True,
                parse_ok=False,
                parse_error=str(exc.detail),
                fallback_used=True,
                persist=persist,
            )

        parsed = parse_ai_decision(response.text)
        if not parsed.ok:
            log_event(
                logger,
                EventType.AI_PARSE_FAILED,
                symbol=context.symbol,
                reason=parsed.error,
                decision="HOLD",
                level=30,
            )
            self.events.record(
                EventType.AI_PARSE_FAILED,
                message=parsed.error or "unparseable AI response",
                symbol=context.symbol,
                level="WARNING",
                context={"raw_response_excerpt": (response.text or "")[:500]},
            )
        return self._record(
            decision_id,
            context,
            parsed.decision,
            provider_name=response.provider,
            model=response.model,
            ai_enabled=True,
            parse_ok=parsed.ok,
            parse_error=parsed.error,
            fallback_used=parsed.fallback_used,
            raw_response=response.text,
            latency_ms=response.latency_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost_usd=response.cost_usd,
            persist=persist,
        )

    # ------------------------------------------------------- veto-only gluing
    def apply_to_candidate(
        self, candidate: TradeCandidate, evaluation: AIEvaluation
    ) -> tuple[RiskProposal | None, list[str]]:
        """Fold an AI decision into a risk proposal under veto-only rules."""
        violations: list[str] = []
        decision = evaluation.decision

        if decision.decision == "HOLD":
            return None, ["AI returned HOLD"]

        if str(decision.direction) != str(candidate.direction):
            # The model wants to trade the other way. It is a reviewer, not a
            # trader: refuse rather than obey.
            violations.append(
                f"AI proposed {decision.decision} against the deterministic "
                f"candidate {candidate.direction}; trade dropped"
            )
            evaluation.violations.extend(violations)
            log_event(
                logger,
                EventType.AI_DECISION,
                symbol=candidate.symbol,
                decision=decision.decision,
                reason="direction conflict with deterministic candidate",
                violations=violations,
                level=30,
            )
            return None, violations

        effective_confidence = min(candidate.confidence, decision.confidence)
        if decision.confidence > candidate.confidence:
            # Not a violation, just not allowed to help: recorded for the audit.
            violations.append(
                f"AI confidence {decision.confidence:.2f} capped to deterministic "
                f"{candidate.confidence:.2f} (veto-only policy)"
            )

        proposal = RiskProposal(
            symbol=candidate.symbol,
            direction=candidate.direction,
            entry=candidate.entry,
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            confidence=effective_confidence,
            strategy=",".join(candidate.aligned_strategies) or "unknown",
            timeframe=candidate.timeframe,
            regime=candidate.regime.regime,
            regime_abnormal=candidate.regime.is_abnormal,
            data_quality_ok=True,
            ai_decision_id=evaluation.decision_id,
            ai_confidence=decision.confidence,
            candidate_id=candidate.candidate_id,
            metadata={
                "ai_risk_assessment": decision.risk_assessment,
                "ai_strategy_alignment": decision.strategy_alignment,
                "ai_invalidation": decision.invalidation_condition,
            },
        )
        evaluation.violations.extend(violations)
        return proposal, violations

    def proposal_without_ai(
        self, candidate: TradeCandidate, reason: str
    ) -> RiskProposal | None:
        """Path used when the AI layer is unavailable.

        Whether this is allowed at all is a configuration decision
        (``AI_REQUIRED_FOR_ENTRY``, default true => no trade).
        """
        if self.settings.ai_required_for_entry:
            log_event(
                logger,
                EventType.AI_SKIPPED,
                symbol=candidate.symbol,
                reason=f"{reason}; AI_REQUIRED_FOR_ENTRY is set, so no trade",
                level=30,
            )
            return None
        return RiskProposal(
            symbol=candidate.symbol,
            direction=candidate.direction,
            entry=candidate.entry,
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            confidence=candidate.confidence,
            strategy=",".join(candidate.aligned_strategies) or "unknown",
            timeframe=candidate.timeframe,
            regime=candidate.regime.regime,
            regime_abnormal=candidate.regime.is_abnormal,
            metadata={"ai_bypassed_reason": reason},
        )

    # ------------------------------------------------------------- internals
    def _rate_limit_reason(self, symbol: str) -> str | None:
        hourly = self.repo.count_since(utcnow() - timedelta(hours=1))
        if hourly >= self.settings.llm_max_calls_per_hour:
            return (
                f"LLM rate limit reached ({hourly} calls in the last hour, limit "
                f"{self.settings.llm_max_calls_per_hour})"
            )
        cooldown = self.settings.ai_min_minutes_between_calls_per_symbol
        if cooldown > 0:
            last = self.repo.last_for_symbol(symbol)
            if last is not None:
                elapsed = (utcnow() - last.created_at).total_seconds() / 60.0
                if elapsed < cooldown:
                    return (
                        f"AI was consulted for {symbol} {elapsed:.1f} minutes ago; "
                        f"per-symbol cooldown is {cooldown} minutes"
                    )
        return None

    def _record(
        self,
        decision_id: str,
        context: AIContext,
        decision: AIDecision,
        *,
        provider_name: str,
        model: str,
        ai_enabled: bool,
        parse_ok: bool,
        parse_error: str | None = None,
        fallback_used: bool = False,
        raw_response: str | None = None,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: Decimal | None = None,
        persist: bool = True,
    ) -> AIEvaluation:
        evaluation = AIEvaluation(
            decision_id=decision_id,
            context_id=context.context_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            created_at=utcnow(),
            provider=provider_name,
            model=model,
            ai_enabled=ai_enabled,
            decision=decision,
            raw_response=raw_response,
            parse_ok=parse_ok,
            parse_error=parse_error,
            fallback_used=fallback_used,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
        if persist:
            self.repo.save_decision(
                {
                    "id": decision_id,
                    "created_at": evaluation.created_at,
                    "context_id": context.context_id,
                    "candidate_id": context.data_quality.get("candidate_id"),
                    "symbol": context.symbol,
                    "timeframe": context.timeframe,
                    "provider": provider_name,
                    "model": model,
                    "decision": decision.decision,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "market_regime": decision.market_regime
                    or str(context.regime.regime),
                    "strategy_alignment": decision.strategy_alignment,
                    "invalidation_condition": decision.invalidation_condition,
                    "risk_assessment": decision.risk_assessment,
                    "key_risks": decision.key_risks,
                    "context": context.model_dump(mode="json"),
                    "raw_response": (raw_response or "")[:20000] or None,
                    "parse_ok": parse_ok,
                    "parse_error": parse_error,
                    "fallback_used": fallback_used,
                    "violations": evaluation.violations,
                    "latency_ms": latency_ms,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": cost_usd,
                }
            )
        log_event(
            logger,
            EventType.AI_DECISION,
            symbol=context.symbol,
            timeframe=context.timeframe,
            decision=decision.decision,
            confidence=decision.confidence,
            provider=provider_name,
            model=model,
            latency_ms=latency_ms,
            parse_ok=parse_ok,
            fallback_used=fallback_used,
            risk=decision.risk_assessment,
            reason=decision.reason[:280],
            decision_id=decision_id,
        )
        self.events.record(
            EventType.AI_DECISION,
            message=f"{decision.decision} ({decision.confidence:.2f}) {decision.reason[:200]}",
            symbol=context.symbol,
            level="INFO",
            context={
                "decision_id": decision_id,
                "provider": provider_name,
                "model": model,
                "parse_ok": parse_ok,
                "fallback_used": fallback_used,
                "risk_assessment": decision.risk_assessment,
            },
        )
        return evaluation

    def health_check(self) -> tuple[bool, str]:
        try:
            return self.provider.health_check()
        except LLMError as exc:
            return False, str(exc.detail)
