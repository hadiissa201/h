"""Market-data service: fetch, validate, cache.

Every consumer (analysis, risk, execution, backtests) goes through here, so the
validation gate cannot be skipped by calling a provider directly from a route.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

from app.core.config import Settings, get_settings
from app.core.errors import MarketDataError, StaleDataError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.data.providers.base import MarketDataProvider
from app.data.validation import validate_candles
from app.models.market import (
    Candle,
    DataQualityIssue,
    DataQualityReport,
    MarketSnapshot,
    OrderBook,
    Ticker,
)
from app.models.trading import SymbolSpec
from app.utils.time import utcnow

logger = get_logger(__name__)


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class _TTLCache:
    """Small in-process TTL cache.

    Exists to stop a burst of n8n nodes hammering the exchange for the same
    candles within one workflow run. Redis is used for cross-process state; this
    is deliberately local and tiny.
    """

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                self._store.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._store[key] = _CacheEntry(
                value=value, expires_at=time.monotonic() + ttl_seconds
            )

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def build_provider(settings: Settings | None = None) -> MarketDataProvider:
    settings = settings or get_settings()
    if settings.market_data_provider == "synthetic":
        from app.data.providers.synthetic import SyntheticMarketDataProvider

        return SyntheticMarketDataProvider()
    from app.data.providers.ccxt_provider import CcxtMarketDataProvider

    return CcxtMarketDataProvider(
        exchange_id=settings.exchange_id,
        timeout_seconds=settings.market_data_timeout_seconds,
    )


class MarketDataService:
    def __init__(
        self,
        provider: MarketDataProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider or build_provider(self.settings)
        self._cache = _TTLCache()

    # ------------------------------------------------------------------ reads
    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        use_cache: bool = True,
        cache_ttl: float = 20.0,
    ) -> pd.DataFrame:
        key = f"ohlcv:{self.provider.name}:{symbol}:{timeframe}:{limit}"
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return cached.copy()
        frame = self.provider.fetch_ohlcv(symbol, timeframe, limit=limit)
        if use_cache:
            self._cache.set(key, frame.copy(), cache_ttl)
        return frame

    def get_ticker(
        self, symbol: str, use_cache: bool = True, cache_ttl: float = 5.0
    ) -> Ticker:
        key = f"ticker:{self.provider.name}:{symbol}"
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        ticker = self.provider.fetch_ticker(symbol)
        if use_cache:
            self._cache.set(key, ticker, cache_ttl)
        return ticker

    def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        return self.provider.fetch_order_book(symbol, limit=limit)

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        key = f"spec:{self.provider.name}:{symbol}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        spec = self.provider.fetch_symbol_spec(symbol)
        if spec is None:
            base, _, quote = symbol.upper().partition("/")
            spec = SymbolSpec(
                symbol=symbol.upper(), base=base, quote=quote or self.settings.quote_currency
            )
        self._cache.set(key, spec, 3600.0)
        return spec

    # ------------------------------------------------------------- validation
    def validate(
        self, frame: pd.DataFrame, symbol: str, timeframe: str
    ) -> DataQualityReport:
        return validate_candles(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            min_bars=self.settings.min_candles_for_analysis,
            max_staleness_seconds=self.settings.max_data_staleness_seconds,
            max_gap_ratio=self.settings.max_candle_gap_ratio,
            abnormal_move_pct=float(self.settings.abnormal_price_move_pct),
        )

    def get_validated_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        raise_on_error: bool = True,
    ) -> tuple[pd.DataFrame, DataQualityReport]:
        frame = self.get_candles(symbol, timeframe, limit=limit)
        report = self.validate(frame, symbol, timeframe)
        if not report.is_tradeable:
            codes = [issue.code for issue in report.errors]
            log_event(
                logger,
                EventType.MARKET_DATA_REJECTED,
                symbol=symbol,
                timeframe=timeframe,
                reason=report.summary(),
                codes=codes,
                bars=report.bars,
            )
            if raise_on_error:
                if "STALE_DATA" in codes:
                    raise StaleDataError(report.summary(), symbol=symbol, codes=codes)
                raise MarketDataError(report.summary(), symbol=symbol, codes=codes)
        return frame, report

    # --------------------------------------------------------------- snapshot
    def get_snapshot(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 300,
        include_order_book: bool = True,
        include_derivatives: bool = False,
        raise_on_error: bool = False,
    ) -> MarketSnapshot:
        timeframe = timeframe or self.settings.primary_timeframe
        frame, report = self.get_validated_candles(
            symbol, timeframe, limit=limit, raise_on_error=raise_on_error
        )
        ticker: Ticker | None = None
        try:
            ticker = self.get_ticker(symbol)
        except MarketDataError as exc:
            # A missing ticker degrades the spread/liquidity checks (the risk
            # engine treats unknown spread as a rejection) but does not by
            # itself invalidate the candle history.
            report.issues.append(
                DataQualityIssue(
                    code="TICKER_UNAVAILABLE",
                    detail=str(exc.detail),
                    severity="warning",
                )
            )
        order_book = self.get_order_book(symbol) if include_order_book else None
        funding = open_interest = None
        if include_derivatives:
            funding = self.provider.fetch_funding_rate(symbol)
            open_interest = self.provider.fetch_open_interest(symbol)

        snapshot = MarketSnapshot(
            symbol=symbol.upper(),
            timeframe=timeframe,
            fetched_at=utcnow(),
            candles=_frame_to_candles(frame),
            ticker=ticker,
            order_book=order_book,
            funding_rate=funding,
            open_interest=open_interest,
            quality=report,
            provider=self.provider.name,
        )
        log_event(
            logger,
            EventType.MARKET_UPDATE,
            symbol=snapshot.symbol,
            timeframe=timeframe,
            price=float(snapshot.last_close) if snapshot.candles else None,
            bars=report.bars,
            provider=self.provider.name,
            data_ok=report.is_tradeable,
            staleness_seconds=report.staleness_seconds,
        )
        return snapshot

    def market_conditions(self, symbol: str) -> dict[str, Decimal | None]:
        """Spread / liquidity facts the risk engine resolves for itself."""
        try:
            ticker = self.get_ticker(symbol)
        except MarketDataError:
            return {"spread_bps": None, "quote_volume_24h": None}
        return {
            "spread_bps": ticker.spread_bps,
            "quote_volume_24h": ticker.quote_volume_24h,
        }

    def clear_cache(self) -> None:
        self._cache.clear()


def _frame_to_candles(frame: pd.DataFrame) -> list[Candle]:
    candles: list[Candle] = []
    for timestamp, row in frame.iterrows():
        candles.append(
            Candle(
                timestamp=timestamp.to_pydatetime(),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
            )
        )
    return candles
