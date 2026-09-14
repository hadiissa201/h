"""Market data collection and analysis orchestration.

Backs n8n Workflow 1 (collection) and Workflow 2 (analysis). The pipeline order
is fixed and every stage can stop it:

    data -> validation -> features -> regime -> strategies -> candidate -> AI gate

The AI gate is the expensive one, so it is only opened when a deterministic
setup exists, the data is clean, the bot is running, and the symbol is not
already held.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

from app.core.config import Settings, get_settings
from app.core.errors import MarketDataError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.data.service import MarketDataService
from app.database.repositories import (
    BotStateRepository,
    EventRepository,
    MarketRepository,
    SignalRepository,
    new_id,
)
from app.features import FeatureEngine, FeatureSet
from app.models.enums import BotStatus
from app.models.market import MarketSnapshot
from app.models.signals import AnalysisResult
from app.portfolio.service import PortfolioService
from app.regime import RegimeDetector
from app.strategies import StrategyEngine
from app.utils.time import utcnow

logger = get_logger(__name__)


class AnalysisService:
    def __init__(
        self,
        *,
        market_data: MarketDataService,
        features: FeatureEngine,
        regime: RegimeDetector,
        strategies: StrategyEngine,
        portfolio: PortfolioService,
        bot_repo: BotStateRepository,
        market_repo: MarketRepository,
        signal_repo: SignalRepository,
        event_repo: EventRepository,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.market_data = market_data
        self.features = features
        self.regime = regime
        self.strategies = strategies
        self.portfolio = portfolio
        self.bot = bot_repo
        self.market_repo = market_repo
        self.signal_repo = signal_repo
        self.events = event_repo

    # ------------------------------------------------------ workflow 1: data
    def collect(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
        *,
        limit: int | None = None,
        store_candles: bool = True,
    ) -> dict[str, Any]:
        timeframes = timeframes or list(self.settings.timeframes)
        limit = limit or max(400, self.settings.min_candles_for_analysis * 2)
        results: dict[str, Any] = {
            "symbol": symbol.upper(),
            "fetched_at": utcnow().isoformat(),
            "provider": self.market_data.provider.name,
            "timeframes": {},
            "data_ok": True,
            "errors": [],
        }

        for timeframe in timeframes:
            try:
                snapshot = self.market_data.get_snapshot(
                    symbol,
                    timeframe,
                    limit=limit,
                    include_order_book=False,
                    raise_on_error=False,
                )
            except MarketDataError as exc:
                results["data_ok"] = False
                results["errors"].append(
                    {"timeframe": timeframe, "error": str(exc.detail)}
                )
                self.events.record(
                    EventType.MARKET_DATA_REJECTED,
                    message=str(exc.detail),
                    symbol=symbol,
                    level="ERROR",
                    workflow="market_data_collection",
                    context={"timeframe": timeframe},
                )
                continue

            self._persist_snapshot(snapshot, store_candles=store_candles)
            quality = snapshot.quality
            if not quality.is_tradeable:
                results["data_ok"] = False
            results["timeframes"][timeframe] = {
                "bars": quality.bars,
                "last_candle": quality.last_timestamp.isoformat()
                if quality.last_timestamp
                else None,
                "staleness_seconds": quality.staleness_seconds,
                "data_ok": quality.is_tradeable,
                "issues": [issue.model_dump() for issue in quality.issues],
                "last_price": float(snapshot.last_close) if snapshot.candles else None,
            }

        return results

    def _persist_snapshot(self, snapshot: MarketSnapshot, *, store_candles: bool) -> None:
        ticker = snapshot.ticker
        self.market_repo.save_snapshot(
            {
                "symbol": snapshot.symbol,
                "timeframe": snapshot.timeframe,
                "fetched_at": snapshot.fetched_at,
                "provider": snapshot.provider,
                "last_price": snapshot.candles[-1].close if snapshot.candles else None,
                "bid": ticker.bid if ticker else None,
                "ask": ticker.ask if ticker else None,
                "spread_bps": ticker.spread_bps if ticker else None,
                "quote_volume_24h": ticker.quote_volume_24h if ticker else None,
                "funding_rate": snapshot.funding_rate,
                "open_interest": snapshot.open_interest,
                "bars": snapshot.quality.bars,
                "first_candle_time": snapshot.quality.first_timestamp,
                "last_candle_time": snapshot.quality.last_timestamp,
                "staleness_seconds": snapshot.quality.staleness_seconds,
                "data_ok": snapshot.quality.is_tradeable,
                "issues": [issue.model_dump() for issue in snapshot.quality.issues],
            }
        )
        if store_candles and snapshot.candles:
            # Only the most recent slice: this table exists to make a backtest
            # reproducible on the bars we actually traded, not to be a full archive.
            rows = [
                {
                    "open_time": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in snapshot.candles[-250:]
            ]
            self.market_repo.upsert_candles(
                snapshot.symbol, snapshot.timeframe, rows, snapshot.provider
            )

    # -------------------------------------------------- workflow 2: analysis
    def analyse(
        self,
        symbol: str,
        timeframe: str | None = None,
        *,
        higher_timeframe: str | None = None,
        include_timeframes: list[str] | None = None,
        persist: bool = True,
    ) -> AnalysisResult:
        symbol = symbol.upper()
        timeframe = timeframe or self.settings.primary_timeframe
        higher_timeframe = higher_timeframe or self._higher_timeframe(timeframe)
        state = self.bot.get()
        trading_allowed = state.status == BotStatus.RUNNING.value

        limit = max(
            self.settings.min_candles_for_analysis,
            self.features.config.warmup_bars + 60,
        )
        try:
            frame, quality = self.market_data.get_validated_candles(
                symbol, timeframe, limit=limit, raise_on_error=False
            )
        except MarketDataError as exc:
            return AnalysisResult(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=utcnow(),
                data_ok=False,
                skip_reason=f"market data unavailable: {exc.detail}",
                bot_status=state.status,
                trading_allowed=trading_allowed,
                data_quality={"error": str(exc.detail)},
            )

        data_quality = {
            "bars": quality.bars,
            "staleness_seconds": quality.staleness_seconds,
            "is_tradeable": quality.is_tradeable,
            "issues": [issue.model_dump() for issue in quality.issues],
        }
        if not quality.is_tradeable:
            return AnalysisResult(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=utcnow(),
                data_ok=False,
                skip_reason=f"market data rejected: {quality.summary()}",
                bot_status=state.status,
                trading_allowed=trading_allowed,
                data_quality=data_quality,
            )

        primary = self.features.compute(frame, symbol, timeframe)
        if not primary.is_warm:
            return AnalysisResult(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=primary.last_timestamp,
                data_ok=True,
                skip_reason=(
                    f"features not warmed up: {primary.missing_features()[:6]} "
                    f"(need ~{primary.warmup_bars} bars, have {len(frame)})"
                ),
                bot_status=state.status,
                trading_allowed=trading_allowed,
                data_quality=data_quality,
            )

        higher: FeatureSet | None = None
        features_by_timeframe: dict[str, dict[str, float | None]] = {
            timeframe: primary.row(-1)
        }
        for extra in self._context_timeframes(timeframe, higher_timeframe, include_timeframes):
            try:
                extra_frame, extra_quality = self.market_data.get_validated_candles(
                    symbol, extra, limit=limit, raise_on_error=False
                )
            except MarketDataError:
                continue
            if not extra_quality.is_tradeable:
                continue
            extra_features = self.features.compute(extra_frame, symbol, extra)
            if not extra_features.is_warm:
                continue
            features_by_timeframe[extra] = extra_features.row(-1)
            if extra == higher_timeframe:
                higher = extra_features

        warnings = [issue.code for issue in quality.warnings]
        assessment = self.regime.classify(
            primary, higher_timeframe=higher, data_quality_warnings=None
        )
        result = self.strategies.analyse(
            primary, assessment, higher_timeframe=higher, data_ok=quality.is_tradeable
        )
        result.features_by_timeframe = features_by_timeframe
        result.data_quality = data_quality
        result.bot_status = state.status
        result.trading_allowed = trading_allowed

        open_positions = self.portfolio.open_positions(symbol)
        result.has_open_position = bool(open_positions)

        # Gate the LLM call on live account state, not just signal quality.
        if result.should_call_ai:
            if not self.settings.ai_enabled:
                # AI off means deterministic-only. Whether that is allowed to
                # trade is AI_REQUIRED_FOR_ENTRY's decision, not this gate's —
                # otherwise disabling the AI would silently disable trading.
                result.should_call_ai = False
                result.skip_reason = "AI layer disabled by configuration"
            elif not trading_allowed:
                result.should_call_ai = False
                result.skip_reason = f"bot is {state.status}; no new entries"
            elif open_positions:
                result.should_call_ai = False
                result.skip_reason = f"position already open in {symbol}"
            elif warnings:
                result.skip_reason = f"data warnings present: {warnings}"

        if result.candidate is not None and result.candidate.candidate_id is None:
            result.candidate.candidate_id = new_id("cand_")

        if persist:
            self._persist_analysis(primary, assessment, result)

        if not result.has_setup:
            log_event(
                logger,
                EventType.NO_SETUP,
                symbol=symbol,
                timeframe=timeframe,
                regime=str(assessment.regime),
                reason=result.skip_reason,
            )
        return result

    def _persist_analysis(self, features: FeatureSet, assessment, result) -> None:
        self.market_repo.save_features(
            features.symbol,
            features.timeframe,
            features.last_timestamp,
            features.row(-1),
            features.config.model_dump(),
        )
        self.market_repo.save_regime(
            {
                "symbol": assessment.symbol,
                "timeframe": assessment.timeframe,
                "bar_time": assessment.timestamp,
                "regime": str(assessment.regime),
                "trend_state": str(assessment.trend_state),
                "volatility_state": str(assessment.volatility_state),
                "confidence": assessment.confidence,
                "is_abnormal": assessment.is_abnormal,
                "metrics": assessment.metrics.model_dump(),
                "abnormal_reasons": assessment.abnormal_reasons,
            }
        )
        candidate = result.candidate
        candidate_id = candidate.candidate_id if candidate else None
        if candidate is not None:
            self.signal_repo.save_candidate(
                {
                    "id": candidate_id,
                    "bar_time": candidate.timestamp,
                    "symbol": candidate.symbol,
                    "timeframe": candidate.timeframe,
                    "direction": str(candidate.direction),
                    "entry": candidate.entry,
                    "stop_loss": candidate.stop_loss,
                    "take_profit": candidate.take_profit,
                    "confidence": candidate.confidence,
                    "risk_reward": float(candidate.risk_reward)
                    if candidate.risk_reward
                    else None,
                    "regime": str(candidate.regime.regime),
                    "aligned_strategies": candidate.aligned_strategies,
                    "conflicting_strategies": candidate.conflicting_strategies,
                    "reason": candidate.reason,
                    "invalidation_condition": candidate.invalidation_condition,
                    "requires_ai_review": candidate.requires_ai_review,
                }
            )
        for signal in result.signals:
            self.signal_repo.save_signal(
                {
                    "bar_time": signal.timestamp,
                    "symbol": signal.symbol,
                    "timeframe": signal.timeframe,
                    "strategy": signal.strategy,
                    "direction": str(signal.signal),
                    "confidence": signal.confidence,
                    "entry": signal.entry,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "risk_reward": float(signal.risk_reward) if signal.risk_reward else None,
                    "regime": str(signal.regime),
                    "reason": signal.reason,
                    "invalidation_condition": signal.invalidation_condition,
                    "features_used": signal.features_used,
                    "meta": signal.metadata,
                    "candidate_id": candidate_id,
                }
            )

    # ------------------------------------------------------------- utilities
    def _higher_timeframe(self, timeframe: str) -> str | None:
        from app.utils.time import timeframe_to_seconds

        ordered = sorted(self.settings.timeframes, key=timeframe_to_seconds)
        try:
            index = ordered.index(timeframe)
        except ValueError:
            return None
        return ordered[index + 1] if index + 1 < len(ordered) else None

    def _context_timeframes(
        self,
        primary: str,
        higher: str | None,
        include: list[str] | None,
    ) -> list[str]:
        chosen = list(include) if include else [tf for tf in (higher,) if tf]
        return [tf for tf in dict.fromkeys(chosen) if tf and tf != primary]

    def mark_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            try:
                prices[symbol] = self.market_data.get_ticker(symbol).last
            except MarketDataError:
                continue
        return prices

    def candles_frame(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        frame, _ = self.market_data.get_validated_candles(
            symbol, timeframe, limit=limit, raise_on_error=False
        )
        return frame
