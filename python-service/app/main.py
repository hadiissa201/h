"""FastAPI application.

Routes are grouped by the workflow that drives them (see ``docs/API.md``). Two
startup behaviours worth knowing:

* the log line emitted on boot states the effective mode and every reason live
  trading is not armed — so "why is it paper?" is answered before anyone asks;
* bootstrap is best-effort: if the database is unreachable the service still
  starts and reports ``degraded`` on ``/health`` rather than crash-looping, which
  makes the failure visible to n8n instead of invisible.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.deps import require_api_key, require_dashboard_access
from app.api.errors import install_error_handlers
from app.api.routers import (
    ai,
    analysis,
    backtest,
    bot,
    dashboard,
    execution,
    health,
    market,
    performance,
    risk,
)
from app.core.config import get_settings
from app.core.events import EventType
from app.core.logging import configure_logging, get_logger, log_event
from app.database.session import session_scope
from app.services.bootstrap import bootstrap_session

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log_event(
        logger,
        EventType.BOT_STARTED,
        message="starting python trading service",
        environment=settings.environment,
        trading_mode=settings.trading_mode,
        effective_mode=settings.effective_mode,
        live_trading_armed=settings.live_trading_armed,
        live_mode_blockers=settings.live_mode_blockers(),
        symbols=settings.trading_symbols,
        timeframes=settings.timeframes,
        market_data_provider=settings.market_data_provider,
        ai_enabled=settings.ai_enabled,
        llm_provider=settings.llm_provider,
        auth_enabled=bool(settings.service_api_key),
    )
    try:
        with session_scope() as session:
            state = bootstrap_session(session, settings)
        log_event(logger, EventType.BOT_STARTED, message="bootstrap complete", **state)
    except Exception as exc:
        logger.error(
            "bootstrap failed; service will report degraded health",
            extra={"event": str(EventType.SYSTEM_ERROR), "error": str(exc)},
        )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="AI Crypto Trading Service",
        version=health.VERSION,
        description=(
            "Quantitative, risk-managed crypto trading service orchestrated by n8n. "
            "Runs in PAPER mode by default; live trading requires three independent "
            "switches plus credentials."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    install_error_handlers(app)

    # Health is unauthenticated so container orchestrators can probe it. The
    # dashboard needs a key (header or ?key=) because it shows account state.
    app.include_router(health.router)
    app.include_router(dashboard.router, dependencies=[Depends(require_dashboard_access)])

    protected = [Depends(require_api_key)]
    app.include_router(market.router, dependencies=protected)
    app.include_router(market.features_router, dependencies=protected)
    app.include_router(analysis.router, dependencies=protected)
    app.include_router(ai.router, dependencies=protected)
    app.include_router(risk.router, dependencies=protected)
    app.include_router(execution.router, dependencies=protected)
    app.include_router(execution.portfolio_router, dependencies=protected)
    app.include_router(performance.router, dependencies=protected)
    app.include_router(bot.router, dependencies=protected)
    app.include_router(backtest.router, dependencies=protected)
    return app


app = create_app()
