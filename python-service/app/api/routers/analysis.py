"""Strategy evaluation, regime and the in-process pipeline (Workflow 2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep, SettingsDep
from app.models.signals import AnalysisResult
from app.services.pipeline import TradingPipeline
from app.strategies import strategy_catalogue

router = APIRouter(tags=["analysis"])


class EvaluateRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    higher_timeframe: str | None = None
    include_timeframes: list[str] | None = Field(
        default=None,
        description="Extra timeframes to include in the AI context (multi-timeframe view)",
    )
    persist: bool = True


class PipelineRequest(BaseModel):
    symbols: list[str] | None = None
    timeframe: str | None = None
    dry_run: bool = Field(
        default=False,
        description="Run every stage including risk, but do not place an order.",
    )


@router.post("/strategy/evaluate", response_model=AnalysisResult)
def evaluate(
    payload: EvaluateRequest, services: ServicesDep, settings: SettingsDep
) -> AnalysisResult:
    """Workflow 2: features -> regime -> strategies -> candidate -> AI gate."""
    return services.analysis.analyse(
        payload.symbol,
        payload.timeframe,
        higher_timeframe=payload.higher_timeframe,
        include_timeframes=payload.include_timeframes,
        persist=payload.persist,
    )


@router.get("/strategies")
def strategies() -> dict[str, Any]:
    return {"strategies": strategy_catalogue()}


@router.post("/regime")
def regime(
    payload: EvaluateRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Regime classification on its own, without running strategies."""
    timeframe = payload.timeframe or settings.primary_timeframe
    limit = max(
        settings.min_candles_for_analysis, services.features.config.warmup_bars + 60
    )
    frame, quality = services.market_data.get_validated_candles(
        payload.symbol, timeframe, limit=limit, raise_on_error=False
    )
    features = services.features.compute(frame, payload.symbol.upper(), timeframe)
    assessment = services.regime.classify(features)
    return {
        "data_ok": quality.is_tradeable,
        "regime": assessment.model_dump(mode="json"),
        "allowed_strategies": [
            entry["name"]
            for entry in strategy_catalogue()
            if str(assessment.regime) in entry["allowed_regimes"]
        ],
    }


@router.post("/pipeline/run")
def run_pipeline(
    payload: PipelineRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Run the full pipeline in-process for one or more symbols.

    n8n normally drives these stages as separate nodes; this endpoint exists for
    smoke tests and scripted paper runs. It takes exactly the same path,
    including the risk approval, so it cannot trade around any control.
    """
    pipeline = TradingPipeline(services)
    symbols = payload.symbols or list(settings.trading_symbols)
    outcomes = [
        pipeline.run_symbol(symbol, payload.timeframe, dry_run=payload.dry_run).as_dict()
        for symbol in symbols
    ]
    return {
        "mode": services.mode,
        "dry_run": payload.dry_run,
        "traded": sum(1 for outcome in outcomes if outcome["traded"]),
        "outcomes": outcomes,
    }
