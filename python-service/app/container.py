"""Service assembly.

One place that wires repositories, engines and services together, so the API
routes, the CLI scripts and the tests all construct an identical graph. The
market-data provider is process-wide (it caches candles and symbol specs);
everything else is per-request, bound to one database session and therefore one
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.orm import Session

from app.ai.service import AIService
from app.analytics.performance import PerformanceAnalytics
from app.core.config import Settings, get_settings
from app.data.service import MarketDataService
from app.database.repositories import (
    AIRepository,
    BotStateRepository,
    EventRepository,
    ExecutionRepository,
    MarketRepository,
    PerformanceRepository,
    RiskRepository,
    SignalRepository,
)
from app.execution.base import ExchangeAdapter
from app.execution.factory import build_exchange
from app.execution.monitor import PositionMonitor
from app.execution.service import ExecutionService
from app.features import FeatureEngine
from app.portfolio.service import PortfolioService
from app.regime import RegimeDetector
from app.risk.service import RiskService
from app.services.analysis import AnalysisService
from app.strategies import StrategyEngine, build_strategies
from app.strategies.engine import EngineConfig


@lru_cache(maxsize=1)
def get_market_data_service() -> MarketDataService:
    """Process-wide market data service (holds the provider and its caches)."""
    return MarketDataService()


def reset_market_data_service() -> None:
    get_market_data_service.cache_clear()


@dataclass
class Services:
    settings: Settings
    session: Session
    mode: str

    market_data: MarketDataService
    features: FeatureEngine
    regime: RegimeDetector
    strategies: StrategyEngine

    bot_repo: BotStateRepository
    event_repo: EventRepository
    market_repo: MarketRepository
    signal_repo: SignalRepository
    ai_repo: AIRepository
    risk_repo: RiskRepository
    execution_repo: ExecutionRepository
    performance_repo: PerformanceRepository

    exchange: ExchangeAdapter
    portfolio: PortfolioService
    risk: RiskService
    execution: ExecutionService
    monitor: PositionMonitor
    ai: AIService
    analysis: AnalysisService
    analytics: PerformanceAnalytics


def build_services(
    session: Session,
    settings: Settings | None = None,
    market_data: MarketDataService | None = None,
) -> Services:
    settings = settings or get_settings()
    mode = settings.effective_mode
    market_data = market_data or get_market_data_service()

    bot_repo = BotStateRepository(session)
    event_repo = EventRepository(session)
    market_repo = MarketRepository(session)
    signal_repo = SignalRepository(session)
    ai_repo = AIRepository(session)
    risk_repo = RiskRepository(session)
    execution_repo = ExecutionRepository(session, mode=mode)
    performance_repo = PerformanceRepository(session, mode=mode)

    exchange = build_exchange(execution_repo, market_data, settings)
    portfolio = PortfolioService(execution_repo, performance_repo, bot_repo, settings)
    risk = RiskService(
        risk_repo,
        portfolio,
        market_data,
        bot_repo,
        event_repo,
        execution_repo,
        settings,
    )
    execution = ExecutionService(
        exchange,
        execution_repo,
        performance_repo,
        bot_repo,
        event_repo,
        portfolio,
        risk,
        settings,
    )
    monitor = PositionMonitor(
        execution, portfolio, market_data, execution_repo, event_repo, settings
    )

    features = FeatureEngine()
    regime = RegimeDetector()
    strategies = StrategyEngine(
        build_strategies(settings.enabled_strategies),
        EngineConfig(allow_short=False),
    )
    analytics = PerformanceAnalytics(
        execution_repo, performance_repo, ai_repo, risk_repo, settings
    )
    ai = AIService(ai_repo, event_repo, portfolio, analytics, settings)
    analysis = AnalysisService(
        market_data=market_data,
        features=features,
        regime=regime,
        strategies=strategies,
        portfolio=portfolio,
        bot_repo=bot_repo,
        market_repo=market_repo,
        signal_repo=signal_repo,
        event_repo=event_repo,
        settings=settings,
    )

    return Services(
        settings=settings,
        session=session,
        mode=mode,
        market_data=market_data,
        features=features,
        regime=regime,
        strategies=strategies,
        bot_repo=bot_repo,
        event_repo=event_repo,
        market_repo=market_repo,
        signal_repo=signal_repo,
        ai_repo=ai_repo,
        risk_repo=risk_repo,
        execution_repo=execution_repo,
        performance_repo=performance_repo,
        exchange=exchange,
        portfolio=portfolio,
        risk=risk,
        execution=execution,
        monitor=monitor,
        ai=ai,
        analysis=analysis,
        analytics=analytics,
    )
