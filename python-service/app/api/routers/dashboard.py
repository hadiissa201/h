"""Monitoring dashboard: one JSON summary endpoint and a single HTML page.

Deliberately not a separate frontend project. The page is one self-contained file
that polls ``/dashboard/summary``, which keeps the monitoring surface honest (it
shows exactly what the API returns) and means there is nothing to build or deploy
alongside the service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.deps import ServicesDep, SettingsDep
from app.utils.time import utcnow

router = APIRouter(tags=["dashboard"])

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "web" / "dashboard.html"


@router.get("/dashboard/summary")
def summary(services: ServicesDep, settings: SettingsDep) -> dict[str, Any]:
    snapshot = services.portfolio.snapshot()
    state = services.bot_repo.get()
    metrics = services.analytics.metrics()
    account = services.portfolio.account_risk_state()
    limits = services.risk.limits_view()

    return {
        "generated_at": utcnow().isoformat(),
        "mode": services.mode,
        "live_trading_armed": settings.live_trading_armed,
        "bot": {
            "status": state.status,
            "halt_reason": state.halt_reason,
            "halt_detail": state.halt_detail,
            "halted_at": state.halted_at.isoformat() if state.halted_at else None,
            "requires_manual_reset": state.requires_manual_reset,
            "consecutive_losses": state.consecutive_losses,
            "cooldown_until": state.cooldown_until.isoformat()
            if state.cooldown_until
            else None,
        },
        "account": {
            "equity": float(snapshot.equity),
            "cash": float(snapshot.cash),
            "positions_value": float(snapshot.positions_value),
            "starting_equity": float(snapshot.starting_equity),
            "peak_equity": float(snapshot.peak_equity),
            "total_pnl": float(snapshot.total_pnl),
            "total_pnl_pct": float(snapshot.total_pnl_pct),
            "realized_pnl": float(snapshot.realized_pnl),
            "unrealized_pnl": float(snapshot.unrealized_pnl),
            "daily_pnl": float(snapshot.daily_pnl),
            "daily_pnl_pct": float(snapshot.daily_pnl_pct),
            "drawdown_pct": float(snapshot.drawdown_pct),
            "exposure_pct": float(snapshot.exposure_pct),
            "fees_paid": float(snapshot.fees_paid),
        },
        "risk": {
            "limits": limits.model_dump(mode="json"),
            "daily_loss_used_pct": float(
                abs(min(snapshot.daily_pnl_pct, 0)) / limits.max_daily_loss_pct * 100
            )
            if limits.max_daily_loss_pct
            else 0.0,
            "drawdown_used_pct": float(
                snapshot.drawdown_pct / limits.max_drawdown_pct * 100
            )
            if limits.max_drawdown_pct
            else 0.0,
            "positions_used": f"{snapshot.open_positions}/{limits.max_open_positions}",
            "exposure_headroom": float(services.risk.exposure_headroom()),
            "cooldown_active": bool(
                account.cooldown_until and account.cooldown_until > utcnow()
            ),
        },
        "positions": [
            {
                "position_id": position.position_id,
                "symbol": position.symbol,
                "quantity": float(position.quantity),
                "entry_price": float(position.entry_price),
                "mark_price": float(position.mark_price or position.entry_price),
                "stop_loss": float(position.stop_loss) if position.stop_loss else None,
                "take_profit": float(position.take_profit)
                if position.take_profit
                else None,
                "unrealized_pnl": float(position.unrealized_pnl()),
                "r_multiple": float(position.r_multiple() or 0),
                "strategy": position.strategy,
                "opened_at": position.opened_at.isoformat(),
                "bars_held": position.bars_held,
            }
            for position in snapshot.positions
        ],
        "performance": {
            "trades": metrics.trades,
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
            "expectancy_r": metrics.expectancy_r,
            "net_pnl": float(metrics.net_pnl),
            "max_drawdown_pct": float(metrics.max_drawdown_pct),
            "sharpe_ratio": metrics.sharpe_ratio,
            "sortino_ratio": metrics.sortino_ratio,
            "insufficient_data": metrics.insufficient_data,
            "notes": metrics.notes,
            "per_strategy": metrics.per_strategy,
            "per_regime": metrics.per_regime,
        },
        "recent_signals": [
            {
                "at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "timeframe": record.timeframe,
                "strategy": record.strategy,
                "direction": record.direction,
                "confidence": record.confidence,
                "regime": record.regime,
                "reason": record.reason[:160],
            }
            for record in services.signal_repo.recent_signals(12)
        ],
        "recent_ai_decisions": [
            {
                "at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "decision": record.decision,
                "confidence": record.confidence,
                "risk_assessment": record.risk_assessment,
                "parse_ok": record.parse_ok,
                "fallback_used": record.fallback_used,
                "provider": record.provider,
                "reason": (record.reason or "")[:200],
            }
            for record in services.ai_repo.recent(8)
        ],
        "recent_risk_decisions": [
            {
                "at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "decision": record.decision,
                "direction": record.direction,
                "quantity": float(record.quantity) if record.quantity else None,
                "rejection_codes": record.rejection_codes,
                "reasons": record.reasons[:2],
            }
            for record in services.risk_repo.recent(10)
        ],
        "recent_trades": [
            {
                "at": record.exit_time.isoformat(),
                "symbol": record.symbol,
                "strategy": record.strategy,
                "pnl": float(record.pnl),
                "r_multiple": record.r_multiple,
                "exit_reason": record.exit_reason,
            }
            for record in services.execution_repo.recent_trades(10)
        ],
        "equity_curve": [
            {"t": point.taken_at.isoformat(), "equity": float(point.equity)}
            for point in services.performance_repo.equity_curve(limit=500)
        ],
        "ai_evaluation": services.analytics.ai_evaluation(),
        "recent_events": [
            {
                "at": record.created_at.isoformat(),
                "type": record.event_type,
                "level": record.level,
                "symbol": record.symbol,
                "message": record.message[:200],
            }
            for record in services.event_repo.recent(15)
        ],
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    if not TEMPLATE_PATH.exists():  # pragma: no cover - packaging guard
        return HTMLResponse(
            "<h1>Dashboard template missing</h1>"
            f"<p>Expected at {TEMPLATE_PATH}</p>",
            status_code=500,
        )
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))
