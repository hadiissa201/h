"""Order, position and monitoring endpoints (Workflows 5 and 6).

``POST /paper/order`` is the only way to open a position, and it requires a valid
single-use risk approval. The same handler serves ``POST /orders`` so the route
name does not have to change when (if) live mode is ever armed — the adapter
behind it is chosen by configuration, not by the URL.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep
from app.core.errors import NotFoundError
from app.models.enums import ExitReason
from app.models.trading import ClosePositionRequest, PortfolioSnapshot

router = APIRouter(tags=["execution"])


class EntryOrderRequest(BaseModel):
    """Execution request. Size and levels come from the approval, not from here."""

    risk_approval_id: str = Field(min_length=6)
    symbol: str | None = None
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        description="Optional and may only be SMALLER than the approved quantity.",
    )
    strategy: str | None = None
    regime: str | None = None
    ai_decision_id: str | None = None
    client_order_id: str | None = Field(
        default=None,
        description="Idempotency key; defaults to entry-<approval_id>. Retries are safe.",
    )
    exit_plan: dict[str, Any] | None = None


def _place(payload: EntryOrderRequest, services: ServicesDep) -> dict[str, Any]:
    order, position = services.execution.place_entry(
        approval_id=payload.risk_approval_id,
        symbol=payload.symbol,
        quantity=payload.quantity,
        strategy=payload.strategy,
        regime=payload.regime,
        ai_decision_id=payload.ai_decision_id,
        exit_plan_overrides=payload.exit_plan,
        client_order_id=payload.client_order_id,
    )
    return {
        "mode": services.mode,
        "order": order.model_dump(mode="json"),
        "position": position.model_dump(mode="json"),
    }


@router.post("/paper/order")
def place_paper_order(payload: EntryOrderRequest, services: ServicesDep) -> dict[str, Any]:
    return _place(payload, services)


@router.post("/orders")
def place_order(payload: EntryOrderRequest, services: ServicesDep) -> dict[str, Any]:
    return _place(payload, services)


@router.get("/orders")
def list_orders(services: ServicesDep, limit: int = Query(default=50, le=500)) -> dict[str, Any]:
    return {
        "orders": [
            {
                "order_id": record.id,
                "client_order_id": record.client_order_id,
                "symbol": record.symbol,
                "side": record.side,
                "type": record.type,
                "status": record.status,
                "quantity": float(record.quantity),
                "filled_quantity": float(record.filled_quantity),
                "average_fill_price": float(record.average_fill_price)
                if record.average_fill_price
                else None,
                "fee_paid": float(record.fee_paid),
                "created_at": record.created_at.isoformat(),
                "reject_reason": record.reject_reason,
                "position_id": record.position_id,
                "risk_approval_id": record.risk_approval_id,
            }
            for record in services.execution_repo.recent_orders(limit)
        ]
    }


@router.get("/orders/open")
def open_orders(services: ServicesDep, symbol: str | None = None) -> dict[str, Any]:
    return {
        "orders": [
            order.model_dump(mode="json")
            for order in services.exchange.get_open_orders(symbol)
        ]
    }


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, services: ServicesDep) -> dict[str, Any]:
    order = services.exchange.cancel_order(order_id)
    return {"order": order.model_dump(mode="json")}


@router.get("/orders/{order_id}")
def get_order(order_id: str, services: ServicesDep) -> dict[str, Any]:
    order = services.exchange.get_order(order_id)
    return {"order": order.model_dump(mode="json")}


@router.get("/positions")
def list_positions(services: ServicesDep, symbol: str | None = None) -> dict[str, Any]:
    positions = services.portfolio.open_positions(symbol)
    prices = {
        position.symbol: position.mark_price or position.entry_price
        for position in positions
    }
    return {
        "count": len(positions),
        "positions": [
            {
                **position.model_dump(mode="json"),
                "unrealized_pnl": float(position.unrealized_pnl(prices.get(position.symbol))),
                "r_multiple": float(position.r_multiple() or 0),
            }
            for position in positions
        ],
    }


@router.get("/positions/closed")
def closed_positions(services: ServicesDep, limit: int = 50) -> dict[str, Any]:
    return {
        "positions": [
            {
                "position_id": record.id,
                "symbol": record.symbol,
                "opened_at": record.opened_at.isoformat(),
                "closed_at": record.closed_at.isoformat() if record.closed_at else None,
                "entry_price": float(record.entry_price),
                "exit_price": float(record.exit_price) if record.exit_price else None,
                "realized_pnl": float(record.realized_pnl),
                "exit_reason": record.exit_reason,
                "strategy": record.strategy,
            }
            for record in services.execution_repo.closed_positions(limit)
        ]
    }


@router.get("/positions/{position_id}")
def get_position(position_id: str, services: ServicesDep) -> dict[str, Any]:
    position = services.portfolio.get_position(position_id)
    if position is None:
        raise NotFoundError(f"position {position_id} not found")
    return {"position": position.model_dump(mode="json")}


@router.post("/positions/{position_id}/close")
def close_position(
    position_id: str, payload: ClosePositionRequest, services: ServicesDep
) -> dict[str, Any]:
    order, position = services.execution.close_position(
        position_id,
        fraction=payload.fraction,
        reason=payload.reason,
        detail=payload.note or "",
    )
    trade = services.execution_repo.trade_for_position(position_id)
    return {
        "order": order.model_dump(mode="json"),
        "position": position.model_dump(mode="json"),
        "trade": (
            {
                "trade_id": trade.id,
                "pnl": float(trade.pnl),
                "fees": float(trade.fees),
                "r_multiple": trade.r_multiple,
                "exit_reason": trade.exit_reason,
            }
            if trade
            else None
        ),
    }


@router.post("/positions/monitor")
def monitor(services: ServicesDep, timeframe: str | None = None) -> dict[str, Any]:
    """Workflow 6: mark, trail, and exit where the rules say so."""
    outcome = services.monitor.run(timeframe=timeframe)
    snapshot = services.portfolio.snapshot()
    triggered = services.risk.run_safety_checks(stale_symbols=outcome.stale_symbols)
    return {
        **outcome.as_dict(),
        "equity": float(snapshot.equity),
        "drawdown_pct": float(snapshot.drawdown_pct),
        "daily_pnl_pct": float(snapshot.daily_pnl_pct),
        "emergency_triggers": [event.model_dump(mode="json") for event in triggered],
        "bot_status": services.bot_repo.get().status,
    }


@router.post("/positions/flatten")
def flatten(
    services: ServicesDep, reason: ExitReason = ExitReason.EMERGENCY, note: str = ""
) -> dict[str, Any]:
    """Close every open position. Used by the emergency workflow."""
    closed = services.execution.flatten_all(reason=reason, detail=note)
    return {
        "closed": len(closed),
        "positions": [position.model_dump(mode="json") for position in closed],
        "remaining_open": len(services.portfolio.open_positions()),
    }


portfolio_router = APIRouter(tags=["portfolio"])


@portfolio_router.get("/portfolio", response_model=PortfolioSnapshot)
def portfolio(services: ServicesDep, persist: bool = False) -> PortfolioSnapshot:
    return services.portfolio.snapshot(persist=persist)


@portfolio_router.get("/portfolio/balances")
def balances(services: ServicesDep) -> dict[str, Any]:
    return {
        "mode": services.mode,
        "balances": [
            balance.model_dump(mode="json") for balance in services.exchange.get_balance()
        ],
    }
