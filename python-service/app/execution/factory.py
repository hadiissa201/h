"""The one place that decides whether orders go to a simulator or an exchange.

Every caller obtains its adapter here. The decision is ``settings.effective_mode``,
which fails closed: unless all three live switches agree *and* credentials exist,
this returns the paper engine. Adding an exchange key by accident therefore
cannot move real money.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.data.service import MarketDataService
from app.database.repositories import ExecutionRepository
from app.execution.base import ExchangeAdapter
from app.execution.paper_exchange import PaperExchangeAdapter

logger = get_logger(__name__)


def build_exchange(
    repository: ExecutionRepository,
    market_data: MarketDataService,
    settings: Settings | None = None,
) -> ExchangeAdapter:
    settings = settings or get_settings()
    if settings.effective_mode == "live":
        from app.execution.live_exchange import LiveExchangeAdapter

        log_event(
            logger,
            EventType.BOT_STARTED,
            message="LIVE exchange adapter selected",
            exchange=settings.exchange_id,
            level=30,
        )
        return LiveExchangeAdapter(settings)

    if settings.trading_mode == "live" or settings.enable_live_trading:
        # Someone tried to go live but the arming sequence is incomplete. Say so
        # loudly instead of silently paper-trading while they believe otherwise.
        log_event(
            logger,
            EventType.LIVE_MODE_BLOCKED,
            message="live mode requested but not armed — using paper engine",
            blockers=settings.live_mode_blockers(),
            level=30,
        )

    return PaperExchangeAdapter(
        repository=repository, market_data=market_data, settings=settings
    )
