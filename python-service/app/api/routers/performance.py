"""Performance, reports and the error-sink endpoint used by n8n error workflows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep, SettingsDep
from app.core.events import EventType
from app.database.repositories import new_id
from app.reports.daily import build_daily_report
from app.utils.time import start_of_utc_day, utcnow

router = APIRouter(tags=["performance"])


class DailyReportRequest(BaseModel):
    day_offset: int = Field(
        default=0, le=0, ge=-30, description="0 = today, -1 = yesterday"
    )
    include_ai_summary: bool = True
    persist: bool = True


class WorkflowErrorRequest(BaseModel):
    """Payload posted by the n8n error workflow when any workflow fails."""

    workflow: str
    message: str
    node: str | None = None
    execution_id: str | None = None
    symbol: str | None = None
    trade_id: str | None = None
    severity: str = Field(default="ERROR", pattern="^(WARNING|ERROR|CRITICAL)$")
    halt_bot: bool = Field(
        default=False,
        description="Set for failures that make continued trading unsafe.",
    )
    context: dict[str, Any] = Field(default_factory=dict)


@router.get("/performance")
def performance(services: ServicesDep, days: int | None = None) -> dict[str, Any]:
    start = utcnow() - timedelta(days=days) if days else None
    metrics = services.analytics.metrics(start=start)
    snapshot = services.portfolio.snapshot()
    return {
        "mode": services.mode,
        "window_days": days,
        "metrics": metrics.model_dump(mode="json"),
        "equity": float(snapshot.equity),
        "cash": float(snapshot.cash),
        "open_positions": snapshot.open_positions,
        "drawdown_pct": float(snapshot.drawdown_pct),
        "daily_pnl_pct": float(snapshot.daily_pnl_pct),
        "per_strategy": metrics.per_strategy,
        "per_regime": metrics.per_regime,
        "honesty_note": (
            "Metrics describe realised trades in this environment only. Paper "
            "results systematically overstate live results."
        ),
    }


@router.get("/performance/daily")
def daily(services: ServicesDep, day_offset: int = 0) -> dict[str, Any]:
    start = start_of_utc_day() + timedelta(days=day_offset)
    return services.analytics.daily_summary(start)


@router.get("/performance/equity-curve")
def equity_curve(services: ServicesDep, days: int = 30, limit: int = 2000) -> dict[str, Any]:
    start = utcnow() - timedelta(days=days)
    points = services.performance_repo.equity_curve(start=start, limit=limit)
    return {
        "points": [
            {
                "timestamp": point.taken_at.isoformat(),
                "equity": float(point.equity),
                "cash": float(point.cash),
                "drawdown_pct": float(point.drawdown_pct),
                "open_positions": point.open_positions,
            }
            for point in points
        ]
    }


@router.get("/performance/trades")
def trades(services: ServicesDep, limit: int = 50) -> dict[str, Any]:
    return {
        "trades": [
            {
                "trade_id": record.id,
                "symbol": record.symbol,
                "strategy": record.strategy,
                "regime": record.regime,
                "entry_time": record.entry_time.isoformat(),
                "exit_time": record.exit_time.isoformat(),
                "entry_price": float(record.entry_price),
                "exit_price": float(record.exit_price),
                "quantity": float(record.quantity),
                "pnl": float(record.pnl),
                "fees": float(record.fees),
                "r_multiple": record.r_multiple,
                "mfe_r": record.max_favorable_excursion_r,
                "mae_r": record.max_adverse_excursion_r,
                "exit_reason": record.exit_reason,
                "ai_decision_id": record.ai_decision_id,
            }
            for record in services.execution_repo.recent_trades(limit)
        ]
    }


@router.post("/reports/daily")
def generate_daily_report(
    payload: DailyReportRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Workflow 7: build the daily report from stored data, with an AI summary."""
    return build_daily_report(
        services,
        day_offset=payload.day_offset,
        include_ai_summary=payload.include_ai_summary,
        persist=payload.persist,
    )


@router.get("/reports/daily/latest")
def latest_daily_report(services: ServicesDep) -> dict[str, Any]:
    record = services.performance_repo.latest_report("daily")
    if record is None:
        return {"exists": False}
    return {
        "exists": True,
        "report_id": record.id,
        "created_at": record.created_at.isoformat(),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "metrics": record.metrics,
        "ai_summary": record.ai_summary,
        "ai_provider": record.ai_provider,
        "body_markdown": record.body_markdown,
    }


@router.post("/workflow/error")
def workflow_error(
    payload: WorkflowErrorRequest, services: ServicesDep
) -> dict[str, Any]:
    """Sink for n8n error workflows, so failures are never silently swallowed."""
    services.event_repo.record(
        EventType.SYSTEM_ERROR,
        message=f"[{payload.workflow}] {payload.message}",
        level=payload.severity,
        symbol=payload.symbol,
        workflow=payload.workflow,
        trade_id=payload.trade_id,
        context={
            "node": payload.node,
            "execution_id": payload.execution_id,
            **payload.context,
        },
    )
    halted = False
    if payload.halt_bot:
        services.bot_repo.halt(
            "WORKFLOW_FAILURE",
            f"[{payload.workflow}] {payload.message}",
            source=f"n8n:{payload.workflow}",
        )
        services.event_repo.record_emergency(
            reason="WORKFLOW_FAILURE",
            detail=f"[{payload.workflow}] {payload.message}",
            source=f"n8n:{payload.workflow}",
        )
        halted = True
    return {
        "recorded": True,
        "incident_id": new_id("inc_"),
        "halted": halted,
        "bot_status": services.bot_repo.get().status,
    }
