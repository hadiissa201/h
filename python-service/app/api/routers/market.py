"""Market data and feature endpoints (n8n Workflows 1 and 2, first half)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep, SettingsDep
from app.features import FeatureConfig, FeatureEngine

router = APIRouter(prefix="/market", tags=["market"])


class CollectRequest(BaseModel):
    symbols: list[str] | None = Field(
        default=None, description="Defaults to TRADING_SYMBOLS"
    )
    timeframes: list[str] | None = Field(default=None, description="Defaults to TIMEFRAMES")
    limit: int | None = Field(default=None, ge=50, le=1500)
    store_candles: bool = True


class FeatureRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    limit: int | None = Field(default=None, ge=50, le=2000)
    blocks: list[str] | None = Field(
        default=None, description="Feature blocks to compute; defaults to all"
    )
    config_overrides: dict[str, Any] | None = None
    include_history: int = Field(
        default=0, ge=0, le=200, description="Also return the last N feature rows"
    )


@router.post("/collect")
def collect(
    payload: CollectRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Workflow 1: fetch, validate and store market data for each symbol."""
    symbols = payload.symbols or list(settings.trading_symbols)
    results = [
        services.analysis.collect(
            symbol,
            payload.timeframes,
            limit=payload.limit,
            store_candles=payload.store_candles,
        )
        for symbol in symbols
    ]
    unhealthy = [item["symbol"] for item in results if not item["data_ok"]]
    return {
        "symbols": results,
        "all_data_ok": not unhealthy,
        "symbols_with_bad_data": unhealthy,
        "provider": services.market_data.provider.name,
    }


@router.get("/{symbol:path}/candles")
def candles(
    symbol: str,
    services: ServicesDep,
    settings: SettingsDep,
    timeframe: str | None = None,
    limit: int = Query(default=200, ge=2, le=1500),
) -> dict[str, Any]:
    timeframe = timeframe or settings.primary_timeframe
    frame, quality = services.market_data.get_validated_candles(
        symbol, timeframe, limit=limit, raise_on_error=False
    )
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "provider": services.market_data.provider.name,
        "data_ok": quality.is_tradeable,
        "quality": quality.model_dump(mode="json"),
        "candles": [
            {
                "timestamp": index.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for index, row in frame.iterrows()
        ],
    }


@router.get("/{symbol:path}")
def market_snapshot(
    symbol: str,
    services: ServicesDep,
    settings: SettingsDep,
    timeframe: str | None = None,
    limit: int = Query(default=300, ge=50, le=1500),
    include_order_book: bool = False,
) -> dict[str, Any]:
    """Current market state for one symbol, with its data-quality verdict."""
    timeframe = timeframe or settings.primary_timeframe
    snapshot = services.market_data.get_snapshot(
        symbol,
        timeframe,
        limit=limit,
        include_order_book=include_order_book,
        raise_on_error=False,
    )
    ticker = snapshot.ticker
    return {
        "symbol": snapshot.symbol,
        "timeframe": snapshot.timeframe,
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "data_ok": snapshot.quality.is_tradeable,
        "quality": snapshot.quality.model_dump(mode="json"),
        "last_price": float(snapshot.last_close) if snapshot.candles else None,
        "ticker": ticker.model_dump(mode="json") if ticker else None,
        "spread_bps": float(ticker.spread_bps) if ticker and ticker.spread_bps else None,
        "order_book": snapshot.order_book.model_dump(mode="json")
        if snapshot.order_book
        else None,
        "funding_rate": float(snapshot.funding_rate) if snapshot.funding_rate else None,
        "open_interest": float(snapshot.open_interest)
        if snapshot.open_interest
        else None,
        "last_candles": [
            candle.model_dump(mode="json") for candle in snapshot.candles[-5:]
        ],
    }


features_router = APIRouter(tags=["features"])


@features_router.post("/features")
def compute_features(
    payload: FeatureRequest, services: ServicesDep, settings: SettingsDep
) -> dict[str, Any]:
    """Feature vector for one symbol/timeframe.

    Blocks and lookbacks can be overridden so a feature family can be evaluated
    on its own rather than assumed useful.
    """
    timeframe = payload.timeframe or settings.primary_timeframe
    config = (
        FeatureConfig(**payload.config_overrides)
        if payload.config_overrides
        else services.features.config
    )
    engine = (
        FeatureEngine(config=config, blocks=payload.blocks)
        if payload.blocks or payload.config_overrides
        else services.features
    )
    limit = payload.limit or max(
        settings.min_candles_for_analysis, config.warmup_bars + 60
    )
    frame, quality = services.market_data.get_validated_candles(
        payload.symbol, timeframe, limit=limit, raise_on_error=False
    )
    feature_set = engine.compute(frame, payload.symbol.upper(), timeframe)

    response: dict[str, Any] = {
        **feature_set.to_summary(),
        "data_ok": quality.is_tradeable,
        "quality": quality.model_dump(mode="json"),
        "missing_features": feature_set.missing_features(),
    }
    if payload.include_history:
        history = min(payload.include_history, len(feature_set.frame))
        response["history"] = [
            {
                "timestamp": feature_set.candles.index[-history + offset].isoformat(),
                **feature_set.row(len(feature_set.frame) - history + offset),
            }
            for offset in range(history)
        ]
    return response
