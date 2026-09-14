"""The canonical trade pipeline, in one place.

n8n orchestrates these stages as separate workflow nodes (so each step is
visible, retryable and independently monitored). This class runs the exact same
sequence in-process, which makes it useful for smoke tests, for a scripted paper
run, and as the executable definition of the required order:

    MARKET DATA -> FEATURES -> REGIME -> STRATEGY -> CANDIDATE
                -> AI REVIEW -> RISK ENGINE -> EXECUTION

No stage is skippable: execution still requires a risk approval, and the risk
engine still resolves account state itself. Running the pipeline in-process is a
convenience, never a bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.errors import TradingError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.models.signals import AnalysisResult
from app.utils.time import utcnow

logger = get_logger(__name__)


@dataclass
class PipelineOutcome:
    symbol: str
    timeframe: str
    stage: str = "analysis"
    traded: bool = False
    analysis: AnalysisResult | None = None
    ai_decision: dict[str, Any] | None = None
    risk_decision: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "stage_reached": self.stage,
            "traded": self.traded,
            "reason": self.reason,
            "analysis": self.analysis.model_dump(mode="json") if self.analysis else None,
            "ai_decision": self.ai_decision,
            "risk_decision": self.risk_decision,
            "order": self.order,
            "position": self.position,
            "errors": self.errors,
            "evaluated_at": utcnow().isoformat(),
        }


class TradingPipeline:
    def __init__(self, services) -> None:
        self.services = services

    def run_symbol(
        self, symbol: str, timeframe: str | None = None, *, dry_run: bool = False
    ) -> PipelineOutcome:
        services = self.services
        timeframe = timeframe or services.settings.primary_timeframe
        outcome = PipelineOutcome(symbol=symbol.upper(), timeframe=timeframe)

        # --- 1..5: data, features, regime, strategies, candidate
        analysis = services.analysis.analyse(symbol, timeframe)
        outcome.analysis = analysis
        if not analysis.data_ok:
            outcome.stage = "market_data"
            outcome.reason = analysis.skip_reason
            return outcome
        if analysis.candidate is None:
            outcome.stage = "strategy"
            outcome.reason = analysis.skip_reason or "no trade candidate"
            return outcome
        candidate = analysis.candidate

        # Hard stops that no later stage should be asked to re-litigate.
        if not analysis.trading_allowed:
            outcome.stage = "bot_state"
            outcome.reason = f"bot status is {analysis.bot_status}; entries blocked"
            return outcome
        if analysis.has_open_position:
            outcome.stage = "portfolio"
            outcome.reason = f"position already open in {outcome.symbol}"
            return outcome

        # --- 6: AI review (only when the deterministic gate opened)
        if analysis.should_call_ai:
            context = services.ai.build_context(
                candidate,
                analysis.features_by_timeframe,
                services.risk.limits_view(),
                data_quality=analysis.data_quality,
            )
            evaluation = services.ai.evaluate(context)
            outcome.ai_decision = {
                "decision_id": evaluation.decision_id,
                "decision": evaluation.decision.decision,
                "confidence": evaluation.decision.confidence,
                "reason": evaluation.decision.reason,
                "risk_assessment": evaluation.decision.risk_assessment,
                "parse_ok": evaluation.parse_ok,
                "fallback_used": evaluation.fallback_used,
                "provider": evaluation.provider,
                "violations": evaluation.violations,
            }
            proposal, notes = services.ai.apply_to_candidate(candidate, evaluation)
            if proposal is None:
                outcome.stage = "ai_review"
                outcome.reason = "; ".join(notes) or "AI declined the candidate"
                return outcome
        else:
            proposal = services.ai.proposal_without_ai(
                candidate, analysis.skip_reason or "AI review not required"
            )
            if proposal is None:
                outcome.stage = "ai_review"
                outcome.reason = (
                    analysis.skip_reason
                    or "AI review unavailable and AI_REQUIRED_FOR_ENTRY is set"
                )
                return outcome

        # --- 7: risk engine (final authority)
        decision = services.risk.check(proposal)
        outcome.risk_decision = decision.model_dump(mode="json")
        if not decision.approved:
            outcome.stage = "risk"
            outcome.reason = "; ".join(decision.reasons) or "risk rejected"
            return outcome

        if dry_run:
            outcome.stage = "risk"
            outcome.reason = "dry run: approved but not executed"
            return outcome

        # --- 8: execution
        try:
            order, position = services.execution.place_entry(
                approval_id=decision.approval_id,
                strategy=",".join(candidate.aligned_strategies) or None,
                regime=str(candidate.regime.regime),
                ai_decision_id=proposal.ai_decision_id,
                exit_plan_overrides=_exit_plan(candidate),
            )
        except TradingError as exc:
            outcome.stage = "execution"
            outcome.reason = exc.detail
            outcome.errors.append(f"{exc.error_code}: {exc.detail}")
            log_event(
                logger,
                EventType.ORDER_REJECTED,
                symbol=symbol,
                reason=exc.detail,
                error_code=exc.error_code,
                level=40,
            )
            return outcome

        outcome.stage = "executed"
        outcome.traded = True
        outcome.order = order.model_dump(mode="json")
        outcome.position = position.model_dump(mode="json")
        return outcome

    def run_all(
        self, symbols: list[str] | None = None, timeframe: str | None = None
    ) -> list[PipelineOutcome]:
        symbols = symbols or list(self.services.settings.trading_symbols)
        outcomes: list[PipelineOutcome] = []
        for symbol in symbols:
            try:
                outcomes.append(self.run_symbol(symbol, timeframe))
            except TradingError as exc:
                outcome = PipelineOutcome(
                    symbol=symbol,
                    timeframe=timeframe or self.services.settings.primary_timeframe,
                    stage="error",
                    reason=exc.detail,
                )
                outcome.errors.append(f"{exc.error_code}: {exc.detail}")
                outcomes.append(outcome)
        return outcomes


def _exit_plan(candidate) -> dict[str, Any]:
    """Carry the winning strategy's exit plan onto the position."""
    primary = candidate.primary_signal
    if primary is None:
        return {}
    return {
        "initial_stop": str(candidate.stop_loss),
        "trailing_stop_atr_multiple": primary.trailing_stop_atr_multiple,
        "breakeven_at_r": primary.breakeven_at_r,
        "partial_exit_at_r": primary.partial_exit_at_r,
        "partial_exit_fraction": primary.partial_exit_fraction,
        "time_stop_bars": primary.time_stop_bars,
        "invalidation_condition": candidate.invalidation_condition,
    }
