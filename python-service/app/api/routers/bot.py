"""Bot state, kill switch and the live-trading readiness gate (Workflow 8)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import ServicesDep, SettingsDep
from app.core.config import LIVE_CONFIRMATION_PHRASE
from app.core.errors import ConflictError, ValidationError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.models.enums import BotStatus, ExitReason, TradingModeEnum
from app.models.system import (
    BotState,
    HaltRequest,
    LiveReadinessReport,
    ResetRequest,
    SafetyCheckItem,
)
from app.utils.time import utcnow

logger = get_logger(__name__)
router = APIRouter(prefix="/bot", tags=["bot"])


def _state_model(record, settings) -> BotState:  # noqa: ANN001
    return BotState(
        status=BotStatus(record.status),
        mode=TradingModeEnum(record.mode),
        live_trading_armed=settings.live_trading_armed,
        updated_at=record.updated_at,
        halted_at=record.halted_at,
        halt_reason=record.halt_reason,
        halt_detail=record.halt_detail,
        halted_by=record.halted_by,
        requires_manual_reset=record.requires_manual_reset,
        consecutive_losses=record.consecutive_losses,
        cooldown_until=record.cooldown_until,
        trading_allowed=record.status == BotStatus.RUNNING.value,
        notes=record.notes or {},
    )


@router.get("/state", response_model=BotState)
def state(services: ServicesDep, settings: SettingsDep) -> BotState:
    return _state_model(services.bot_repo.get(), settings)


@router.post("/halt")
def halt(
    payload: HaltRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Activate the kill switch.

    New entries stop immediately and stay stopped until an explicit reset.
    Existing positions are left alone unless ``close_positions`` is set: forced
    liquidation into a disorderly market can be worse than holding a position
    that still has a stop.
    """
    snapshot = services.portfolio.snapshot()
    record = services.bot_repo.halt(
        payload.reason, payload.detail, payload.source, requires_manual_reset=True
    )
    event = services.event_repo.record_emergency(
        reason=payload.reason,
        detail=payload.detail,
        source=payload.source,
        equity=snapshot.equity,
        drawdown_pct=snapshot.drawdown_pct,
        daily_pnl_pct=snapshot.daily_pnl_pct,
    )
    closed = []
    if payload.close_positions:
        closed = services.execution.flatten_all(
            reason=ExitReason.EMERGENCY, detail=f"halt: {payload.reason}"
        )
        event.positions_closed = len(closed)

    log_event(
        logger,
        EventType.BOT_HALTED,
        reason=payload.reason,
        detail=payload.detail,
        source=payload.source,
        equity=float(snapshot.equity),
        positions_closed=len(closed),
        level=40,
    )
    services.event_repo.record(
        EventType.BOT_HALTED,
        message=f"{payload.reason}: {payload.detail}",
        level="ERROR",
        context={"source": payload.source, "positions_closed": len(closed)},
    )
    return {
        "state": _state_model(record, settings).model_dump(mode="json"),
        "emergency_event_id": event.id,
        "positions_closed": len(closed),
        "requires_manual_reset": True,
    }


@router.post("/reset")
def reset(
    payload: ResetRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Clear a halt. Deliberately manual: automation must not un-halt itself."""
    if payload.confirmation != "RESET":
        raise ValidationError(
            "reset requires confirmation='RESET' — this step is intentionally manual"
        )
    current = services.bot_repo.get()
    if current.status == BotStatus.RUNNING.value:
        raise ConflictError("bot is already RUNNING; nothing to reset")

    record = services.bot_repo.reset(
        operator=payload.operator,
        note=payload.note,
        clear_losses=payload.reset_consecutive_losses,
    )
    log_event(
        logger,
        EventType.BOT_RESET,
        operator=payload.operator,
        note=payload.note,
        previous_halt_reason=current.halt_reason,
        level=30,
    )
    services.event_repo.record(
        EventType.BOT_RESET,
        message=f"reset by {payload.operator}: {payload.note}",
        context={"previous_halt_reason": current.halt_reason},
    )
    return {"state": _state_model(record, settings).model_dump(mode="json")}


@router.get("/live-readiness", response_model=LiveReadinessReport)
def live_readiness(services: ServicesDep, settings: SettingsDep) -> LiveReadinessReport:
    """Checklist that must be satisfied before live trading is even considered.

    Everything here is evidence the *system* can be checked automatically. The
    human items (a completed walk-forward review, an accepted paper-trading
    period) are listed in ``docs/LIVE_TRADING_CHECKLIST.md`` and are not
    something this endpoint can attest to.
    """
    metrics = services.analytics.metrics()
    runs = services.performance_repo.recent_runs(limit=50)
    backtests = [run for run in runs if run.kind == "backtest"]
    walkforwards = [run for run in runs if run.kind == "walkforward"]
    state = services.bot_repo.get()
    trades = services.execution_repo.all_trades()
    emergencies = services.event_repo.recent_emergencies(limit=5)

    items = [
        SafetyCheckItem(
            key="paper_mode_default",
            description="Service is running in paper mode",
            satisfied=services.mode == "paper",
            detail=f"effective mode: {services.mode}",
        ),
        SafetyCheckItem(
            key="backtest_completed",
            description="At least one backtest has been run and stored",
            satisfied=bool(backtests),
            detail=f"{len(backtests)} stored backtest run(s)",
        ),
        SafetyCheckItem(
            key="walkforward_completed",
            description="At least one walk-forward run has been stored",
            satisfied=bool(walkforwards),
            detail=f"{len(walkforwards)} stored walk-forward run(s)",
        ),
        SafetyCheckItem(
            key="paper_trades_recorded",
            description="At least 30 closed paper trades exist to evaluate",
            satisfied=len(trades) >= 30,
            detail=f"{len(trades)} closed trades",
        ),
        SafetyCheckItem(
            key="paper_expectancy_known",
            description="Paper expectancy has been measured (not necessarily positive)",
            satisfied=metrics.expectancy_r is not None,
            detail=f"expectancy_r={metrics.expectancy_r}",
        ),
        SafetyCheckItem(
            key="kill_switch_exercised",
            description="The kill switch has been triggered at least once (drill or real)",
            satisfied=bool(emergencies),
            detail=f"{len(emergencies)} recorded emergency event(s)",
        ),
        SafetyCheckItem(
            key="bot_not_halted",
            description="Bot is not currently halted",
            satisfied=state.status == BotStatus.RUNNING.value,
            detail=f"status={state.status} reason={state.halt_reason}",
        ),
        SafetyCheckItem(
            key="exchange_credentials",
            description="Exchange API credentials are configured",
            satisfied=bool(settings.exchange_api_key and settings.exchange_api_secret),
            detail="credentials present" if settings.exchange_api_key else "not configured",
        ),
        SafetyCheckItem(
            key="api_auth_enabled",
            description="SERVICE_API_KEY is set so the API is not open",
            satisfied=bool(settings.service_api_key),
            detail="set" if settings.service_api_key else "not set",
        ),
        SafetyCheckItem(
            key="live_switches",
            description=(
                "TRADING_MODE=live, ENABLE_LIVE_TRADING=true and "
                f"LIVE_TRADING_CONFIRMATION={LIVE_CONFIRMATION_PHRASE}"
            ),
            satisfied=settings.live_trading_armed,
            detail="; ".join(settings.live_mode_blockers()) or "armed",
        ),
    ]
    blockers = [item.description for item in items if not item.satisfied]
    return LiveReadinessReport(
        ready=not blockers, checked_at=utcnow(), items=items, blockers=blockers
    )


@router.get("/emergencies")
def emergencies(services: ServicesDep, limit: int = 20) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": record.id,
                "triggered_at": record.triggered_at.isoformat(),
                "reason": record.reason,
                "detail": record.detail,
                "source": record.source,
                "equity": float(record.equity) if record.equity else None,
                "drawdown_pct": float(record.drawdown_pct) if record.drawdown_pct else None,
                "positions_closed": record.positions_closed,
                "resolved_at": record.resolved_at.isoformat()
                if record.resolved_at
                else None,
            }
            for record in services.event_repo.recent_emergencies(limit)
        ]
    }


@router.get("/events")
def events(
    services: ServicesDep, limit: int = 100, event_types: str | None = None
) -> dict[str, Any]:
    types = [item.strip() for item in event_types.split(",")] if event_types else None
    return {
        "events": [
            {
                "created_at": record.created_at.isoformat(),
                "event_type": record.event_type,
                "level": record.level,
                "symbol": record.symbol,
                "strategy": record.strategy,
                "workflow": record.workflow,
                "trade_id": record.trade_id,
                "message": record.message,
                "context": record.context,
            }
            for record in services.event_repo.recent(limit, types)
        ]
    }
