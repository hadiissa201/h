"""Health and readiness."""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.api.deps import ServicesDep, SettingsDep
from app.database.session import check_database
from app.models.enums import BotStatus, TradingModeEnum
from app.models.system import ComponentHealth, HealthResponse
from app.utils.time import utcnow

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse)
def health(services: ServicesDep, settings: SettingsDep) -> HealthResponse:
    components: list[ComponentHealth] = []

    started = time.perf_counter()
    db_ok, db_detail = check_database(settings)
    components.append(
        ComponentHealth(
            name="database",
            healthy=db_ok,
            detail=db_detail,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    )

    started = time.perf_counter()
    try:
        ticker = services.market_data.get_ticker(settings.trading_symbols[0])
        components.append(
            ComponentHealth(
                name="market_data",
                healthy=True,
                detail=f"{services.market_data.provider.name}: {ticker.symbol} @ {ticker.last}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
    except Exception as exc:
        components.append(
            ComponentHealth(
                name="market_data",
                healthy=False,
                detail=f"{services.market_data.provider.name}: {exc}",
            )
        )

    exchange_ok, exchange_detail = services.exchange.health_check()
    components.append(
        ComponentHealth(
            name=f"exchange:{services.exchange.name}",
            healthy=exchange_ok,
            detail=exchange_detail,
        )
    )

    ai_ok, ai_detail = services.ai.health_check()
    components.append(
        ComponentHealth(
            name=f"llm:{settings.llm_provider if settings.ai_enabled else 'disabled'}",
            healthy=ai_ok,
            detail=ai_detail,
        )
    )

    state = services.bot_repo.get()
    # The AI provider being down is not a reason to report the service unhealthy:
    # it degrades to "no new entries", which is a safe state.
    critical_ok = all(
        component.healthy
        for component in components
        if component.name in {"database", "market_data"}
        or component.name.startswith("exchange:")
    )
    return HealthResponse(
        status="ok" if critical_ok else "degraded",
        version=VERSION,
        mode=TradingModeEnum(services.mode),
        live_trading_armed=settings.live_trading_armed,
        bot_status=BotStatus(state.status),
        timestamp=utcnow(),
        components=components,
        live_mode_blockers=settings.live_mode_blockers(),
    )


@router.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    """Process liveness only — no dependencies touched."""
    return {"status": "alive", "timestamp": utcnow().isoformat()}
