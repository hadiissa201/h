"""Shared test fixtures.

Every test gets its own SQLite database and its own service graph, so tests can
run in any order and a leaked position in one cannot affect another. Market data
comes from the deterministic synthetic provider — no network, reproducible runs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

# Environment must be set before anything imports Settings.
os.environ.update(
    ENVIRONMENT="test",
    LOG_LEVEL="CRITICAL",
    LOG_FORMAT="console",
    DATABASE_URL="sqlite+pysqlite:///:memory:",
    MARKET_DATA_PROVIDER="synthetic",
    AI_ENABLED="false",
    AI_REQUIRED_FOR_ENTRY="false",
    SERVICE_API_KEY="test-api-key",
    MIN_24H_QUOTE_VOLUME="1000",
    PAPER_STARTING_BALANCE="10000",
)

from app.container import build_services, reset_market_data_service  # noqa: E402
from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.data.providers.synthetic import SyntheticMarketDataProvider  # noqa: E402
from app.data.service import MarketDataService  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database.session import configure_engine, reset_engine  # noqa: E402
from app.features import FeatureEngine  # noqa: E402
from app.models.enums import SignalDirection  # noqa: E402
from app.models.risk import RiskProposal  # noqa: E402
from app.regime import RegimeDetector  # noqa: E402
from app.services.bootstrap import bootstrap_session  # noqa: E402

configure_logging("CRITICAL", "console")


@pytest.fixture(autouse=True)
def _clean_settings() -> Iterator[None]:
    reset_settings_cache()
    reset_market_data_service()
    yield
    reset_settings_cache()
    reset_market_data_service()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def db_engine() -> Iterator[object]:
    """A fresh in-memory database per test.

    StaticPool keeps one connection alive so every session in the test sees the
    same in-memory database.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    configure_engine(engine)
    yield engine
    reset_engine()


@pytest.fixture
def session(db_engine) -> Iterator[object]:
    from app.database.session import get_session_factory

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def market_data() -> MarketDataService:
    return MarketDataService(provider=SyntheticMarketDataProvider(seed=7))


@pytest.fixture
def services(session, market_data, settings):
    """Fully wired service graph on a bootstrapped (funded) paper account."""
    bootstrap_session(session, settings)
    session.commit()
    return build_services(session, settings, market_data=market_data)


@pytest.fixture
def client(db_engine, market_data, settings) -> Iterator[object]:
    """FastAPI TestClient wired to this test's database and synthetic data."""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.database.session import session_scope
    from app.main import create_app

    def _services_override():
        # One transaction per request, exactly as in production — but bound to
        # the test engine and the synthetic provider.
        with session_scope() as request_session:
            yield build_services(request_session, settings, market_data=market_data)

    app = create_app()
    app.dependency_overrides[deps.services_dependency] = _services_override

    with TestClient(app) as test_client:
        test_client.headers.update({"X-API-Key": "test-api-key"})
        yield test_client


@pytest.fixture
def candles(market_data) -> pd.DataFrame:
    """~900 bars of deterministic synthetic 1h data."""
    return market_data.provider.generate("BTC/USDT", "1h", 900)


@pytest.fixture
def feature_engine() -> FeatureEngine:
    return FeatureEngine()


@pytest.fixture
def features(candles, feature_engine):
    return feature_engine.compute(candles, "BTC/USDT", "1h")


@pytest.fixture
def regime_detector() -> RegimeDetector:
    return RegimeDetector()


@pytest.fixture
def proposal_factory():
    """Build a risk proposal that passes every check unless a test breaks one."""

    def _make(**overrides) -> RiskProposal:
        entry = Decimal(overrides.pop("entry", "100"))
        payload = {
            "symbol": "BTC/USDT",
            "direction": SignalDirection.BUY,
            "entry": entry,
            "stop_loss": entry * Decimal("0.97"),
            "take_profit": entry * Decimal("1.08"),
            "confidence": 0.75,
            "strategy": "trend_following",
            "timeframe": "1h",
            "regime": "range",
            "spread_bps": Decimal("5"),
            "quote_volume_24h": Decimal("50000000"),
        }
        payload.update(overrides)
        return RiskProposal(**payload)

    return _make


def make_ohlcv(
    closes: list[float],
    *,
    start: str = "2026-01-01T00:00:00Z",
    freq: str = "1h",
    volume: float = 1000.0,
    spread: float = 0.002,
) -> pd.DataFrame:
    """Build a hand-specified OHLCV frame for deterministic indicator tests."""
    index = pd.date_range(start=start, periods=len(closes), freq=freq, tz="UTC")
    opens, highs, lows = [], [], []
    for position, close in enumerate(closes):
        open_price = closes[position - 1] if position else close
        highs.append(max(open_price, close) * (1 + spread))
        lows.append(min(open_price, close) * (1 - spread))
        opens.append(open_price)
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [volume] * len(closes),
        },
        index=index,
    )
    frame.index.name = "timestamp"
    return frame
