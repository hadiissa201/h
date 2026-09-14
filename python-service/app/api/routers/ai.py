"""AI evaluation endpoints (Workflow 3).

``/ai/evaluate`` returns the model's decision *and* the risk proposal it produced
under veto-only rules. n8n forwards that proposal to ``/risk/check``; it cannot
construct a proposal the AI layer did not sanction, and even if it did, the risk
engine resolves account state itself and the execution endpoint demands an
approval.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep, SettingsDep
from app.core.errors import ConflictError, ValidationError

router = APIRouter(prefix="/ai", tags=["ai"])


class AIEvaluateRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    include_timeframes: list[str] | None = None
    force: bool = Field(
        default=False,
        description="Consult the model even when the deterministic gate says the "
        "setup is not worth an LLM call. Never bypasses risk.",
    )


@router.post("/context")
def build_context(
    payload: AIEvaluateRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Build the structured AI context without calling the model."""
    analysis = services.analysis.analyse(
        payload.symbol,
        payload.timeframe,
        include_timeframes=payload.include_timeframes,
    )
    if analysis.candidate is None:
        return {
            "has_candidate": False,
            "skip_reason": analysis.skip_reason,
            "analysis": analysis.model_dump(mode="json"),
        }
    context = services.ai.build_context(
        analysis.candidate,
        analysis.features_by_timeframe,
        services.risk.limits_view(),
        data_quality=analysis.data_quality,
    )
    return {
        "has_candidate": True,
        "should_call_ai": analysis.should_call_ai,
        "context": context.model_dump(mode="json"),
    }


@router.post("/evaluate")
def evaluate(
    payload: AIEvaluateRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    analysis = services.analysis.analyse(
        payload.symbol,
        payload.timeframe,
        include_timeframes=payload.include_timeframes,
    )
    if not analysis.data_ok:
        raise ValidationError(
            f"market data is not tradeable: {analysis.skip_reason}",
            symbol=payload.symbol,
        )
    if analysis.candidate is None:
        return {
            "evaluated": False,
            "reason": analysis.skip_reason or "no trade candidate",
            "analysis": analysis.model_dump(mode="json"),
        }
    if not analysis.should_call_ai and not payload.force:
        return {
            "evaluated": False,
            "reason": analysis.skip_reason or "deterministic gate did not open",
            "candidate": analysis.candidate.model_dump(mode="json"),
        }
    if analysis.has_open_position and not payload.force:
        raise ConflictError(
            f"a position is already open in {payload.symbol}", symbol=payload.symbol
        )

    context = services.ai.build_context(
        analysis.candidate,
        analysis.features_by_timeframe,
        services.risk.limits_view(),
        data_quality=analysis.data_quality,
    )
    evaluation = services.ai.evaluate(context)
    proposal, notes = services.ai.apply_to_candidate(analysis.candidate, evaluation)

    return {
        "evaluated": True,
        "decision_id": evaluation.decision_id,
        "decision": evaluation.decision.model_dump(mode="json"),
        "provider": evaluation.provider,
        "model": evaluation.model,
        "parse_ok": evaluation.parse_ok,
        "parse_error": evaluation.parse_error,
        "fallback_used": evaluation.fallback_used,
        "latency_ms": evaluation.latency_ms,
        "violations": evaluation.violations,
        "notes": notes,
        "actionable": proposal is not None,
        "risk_proposal": proposal.model_dump(mode="json") if proposal else None,
        "candidate": analysis.candidate.model_dump(mode="json"),
    }


@router.get("/decisions")
def recent_decisions(services: ServicesDep, limit: int = 25) -> dict[str, Any]:
    return {
        "decisions": [
            {
                "decision_id": record.id,
                "created_at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "timeframe": record.timeframe,
                "decision": record.decision,
                "confidence": record.confidence,
                "reason": record.reason,
                "risk_assessment": record.risk_assessment,
                "provider": record.provider,
                "model": record.model,
                "parse_ok": record.parse_ok,
                "fallback_used": record.fallback_used,
                "violations": record.violations,
                "latency_ms": record.latency_ms,
            }
            for record in services.ai_repo.recent(limit)
        ]
    }


@router.get("/evaluation")
def ai_value(services: ServicesDep, lookback_days: int = 90) -> dict[str, Any]:
    """Does the AI layer measurably help? Reports 'no' when that is the answer."""
    return services.analytics.ai_evaluation(lookback_days=lookback_days)
