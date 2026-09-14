"""Backtest and walk-forward endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import ServicesDep
from app.backtesting.runner import run_backtest
from app.backtesting.walkforward import run_walkforward
from app.models.backtest import (
    BacktestRequest,
    BacktestResult,
    WalkForwardRequest,
    WalkForwardResult,
)

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.post("/run", response_model=BacktestResult)
def run(payload: BacktestRequest, services: ServicesDep) -> BacktestResult:
    """Run a backtest over historical (or synthetic) candles.

    Uses the same strategies, risk engine, sizing and cost model as live trading.
    The response carries ``warnings`` describing exactly what the result does and
    does not establish — read them.
    """
    return run_backtest(payload, services)


@router.post("/walkforward", response_model=WalkForwardResult)
def walkforward(payload: WalkForwardRequest, services: ServicesDep) -> WalkForwardResult:
    """Rolling train/validate/test evaluation.

    ``verdict`` is written to be able to say the edge did not survive.
    """
    return run_walkforward(payload, services)


@router.get("/runs")
def runs(
    services: ServicesDep,
    kind: str | None = Query(default=None, pattern="^(backtest|walkforward)$"),
    limit: int = 20,
) -> dict[str, Any]:
    return {
        "runs": [
            {
                "id": record.id,
                "kind": record.kind,
                "label": record.label,
                "created_at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "timeframe": record.timeframe,
                "data_source": record.data_source,
                "bars": record.bars,
                "strategies": record.strategies,
                "metrics": record.metrics,
                "warnings": record.warnings,
            }
            for record in services.performance_repo.recent_runs(kind, limit)
        ]
    }


@router.get("/runs/{run_id}")
def run_detail(run_id: str, services: ServicesDep) -> dict[str, Any]:
    for record in services.performance_repo.recent_runs(None, 200):
        if record.id == run_id:
            return {
                "id": record.id,
                "kind": record.kind,
                "label": record.label,
                "created_at": record.created_at.isoformat(),
                "symbol": record.symbol,
                "timeframe": record.timeframe,
                "data_source": record.data_source,
                "config": record.config,
                "metrics": record.metrics,
                "result": record.result,
                "warnings": record.warnings,
            }
    return {"found": False, "run_id": run_id}
