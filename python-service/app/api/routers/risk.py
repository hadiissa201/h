"""Risk endpoints (Workflow 4) — the gate everything else must pass."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import ServicesDep
from app.models.risk import RiskDecision, RiskLimitsView, RiskProposal

router = APIRouter(prefix="/risk", tags=["risk"])


@router.post("/check", response_model=RiskDecision)
def check(proposal: RiskProposal, services: ServicesDep) -> RiskDecision:
    """Validate a trade proposal and, if it passes, issue a single-use approval.

    Exposure, equity, drawdown and microstructure are resolved server-side: values
    supplied in the payload are only ever used as fallbacks and are still
    bounds-checked.
    """
    return services.risk.check(proposal)


@router.get("/limits", response_model=RiskLimitsView)
def limits(services: ServicesDep) -> RiskLimitsView:
    return services.risk.limits_view()


@router.get("/state")
def risk_state(services: ServicesDep) -> dict[str, Any]:
    account = services.portfolio.account_risk_state()
    return {
        "account": account.model_dump(mode="json"),
        "limits": services.risk.limits_view().model_dump(mode="json"),
        "exposure_headroom": float(services.risk.exposure_headroom()),
    }


@router.get("/decisions")
def decisions(services: ServicesDep, limit: int = 50) -> dict[str, Any]:
    return {
        "decisions": [
            {
                "id": record.id,
                "created_at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "direction": record.direction,
                "decision": record.decision,
                "strategy": record.strategy,
                "quantity": float(record.quantity) if record.quantity else None,
                "notional": float(record.notional) if record.notional else None,
                "risk_pct": float(record.risk_pct) if record.risk_pct else None,
                "rejection_codes": record.rejection_codes,
                "reasons": record.reasons,
                "approval_id": record.approval_id,
                "consumed_at": record.consumed_at.isoformat()
                if record.consumed_at
                else None,
                "ai_decision_id": record.ai_decision_id,
            }
            for record in services.risk_repo.recent(limit)
        ]
    }


@router.post("/safety-check")
def safety_check(
    services: ServicesDep,
    auto_halt: bool = True,
    stale_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate the automatic kill-switch triggers (Workflow 8 uses this)."""
    triggered = services.risk.run_safety_checks(
        stale_symbols=stale_symbols, auto_halt=auto_halt
    )
    state = services.bot_repo.get()
    return {
        "triggered": [event.model_dump(mode="json") for event in triggered],
        "halted": state.status != "RUNNING",
        "bot_status": state.status,
        "halt_reason": state.halt_reason,
    }
