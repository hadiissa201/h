"""One-time startup work.

Explicit rather than lazy: the paper account is funded and the bot-state row is
created here, so a missing balance row anywhere else is a real error rather than
something quietly papered over with a default. In live mode nothing is funded —
the exchange holds the money.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.container import build_services
from app.database.models import Base
from app.database.session import get_engine
from app.execution.paper_exchange import PaperExchangeAdapter

logger = get_logger(__name__)


def create_schema() -> None:
    """Create tables directly from the models.

    Used by tests and local scripts. Deployments run Alembic migrations instead
    (``alembic upgrade head``) so schema changes are versioned.
    """
    Base.metadata.create_all(get_engine())


def bootstrap_session(session: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    services = build_services(session, settings)

    state = services.bot_repo.get(mode=services.mode)
    if state.mode != services.mode:
        services.bot_repo.set_mode(services.mode)

    funded = None
    if isinstance(services.exchange, PaperExchangeAdapter):
        funded = services.exchange.ensure_funded()

    snapshot = services.portfolio.snapshot(persist=True)
    log_event(
        logger,
        EventType.BOT_STARTED,
        message="service bootstrapped",
        mode=services.mode,
        bot_status=state.status,
        equity=float(snapshot.equity),
        live_trading_armed=settings.live_trading_armed,
        symbols=settings.trading_symbols,
        provider=services.market_data.provider.name,
        ai_provider=settings.llm_provider if settings.ai_enabled else "disabled",
    )
    services.event_repo.record(
        EventType.BOT_STARTED,
        message=f"service started in {services.mode} mode",
        context={
            "equity": float(snapshot.equity),
            "bot_status": state.status,
            "live_trading_armed": settings.live_trading_armed,
            "market_data_provider": services.market_data.provider.name,
        },
    )
    return {
        "mode": services.mode,
        "bot_status": state.status,
        "equity": float(snapshot.equity),
        "funded_currency": funded.currency if funded else None,
        "live_trading_armed": settings.live_trading_armed,
    }
