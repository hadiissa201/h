"""Real market data via ccxt public endpoints.

Read-only by construction: this class never receives API credentials, so it
cannot place an order even if something tried to make it. Credentialed access
lives in ``app.execution.live_exchange`` behind the live-trading arming checks.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

import pandas as pd

from app.core.errors import MarketDataError
from app.core.logging import get_logger
from app.data.providers.base import MarketDataProvider
from app.models.market import OrderBook, OrderBookLevel, Ticker
from app.models.trading import SymbolSpec
from app.utils.time import ensure_utc, utcnow

logger = get_logger(__name__)


class CcxtMarketDataProvider(MarketDataProvider):
    def __init__(
        self,
        exchange_id: str = "binance",
        timeout_seconds: float = 15.0,
        enable_rate_limit: bool = True,
        options: dict[str, Any] | None = None,
    ) -> None:
        import ccxt  # imported lazily so tests never need the network

        if not hasattr(ccxt, exchange_id):
            raise MarketDataError(f"unknown ccxt exchange id: {exchange_id!r}")
        self.exchange_id = exchange_id
        self.name = f"ccxt:{exchange_id}"
        exchange_cls = getattr(ccxt, exchange_id)
        self._exchange = exchange_cls(
            {
                "enableRateLimit": enable_rate_limit,
                "timeout": int(timeout_seconds * 1000),
                # Spot only. Explicit, so a stray default cannot land us on a
                # futures endpoint where leverage exists.
                "options": {"defaultType": "spot", **(options or {})},
            }
        )
        self._markets_loaded = False

    # ------------------------------------------------------------- internals
    def _load_markets(self) -> None:
        if self._markets_loaded:
            return
        try:
            self._exchange.load_markets()
            self._markets_loaded = True
        except Exception as exc:  # ccxt raises a wide range of network errors
            raise MarketDataError(
                f"failed to load markets from {self.exchange_id}: {exc}",
                exchange=self.exchange_id,
            ) from exc

    # ------------------------------------------------------------------ data
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        since: int | None = None,
    ) -> pd.DataFrame:
        self._load_markets()
        try:
            raw = self._exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=limit
            )
        except Exception as exc:
            raise MarketDataError(
                f"fetch_ohlcv failed for {symbol} {timeframe}: {exc}",
                symbol=symbol,
                timeframe=timeframe,
                exchange=self.exchange_id,
            ) from exc
        if not raw:
            raise MarketDataError(
                f"empty OHLCV response for {symbol} {timeframe}",
                symbol=symbol,
                timeframe=timeframe,
            )
        frame = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        frame = frame.set_index("timestamp").astype(float)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        # Exchanges include the in-progress candle; drop it so no strategy ever
        # sees a partial bar.
        return frame.iloc[:-1] if len(frame) > 1 else frame

    def fetch_ticker(self, symbol: str) -> Ticker:
        self._load_markets()
        try:
            raw = self._exchange.fetch_ticker(symbol)
        except Exception as exc:
            raise MarketDataError(
                f"fetch_ticker failed for {symbol}: {exc}",
                symbol=symbol,
                exchange=self.exchange_id,
            ) from exc
        timestamp = raw.get("timestamp")
        return Ticker(
            symbol=symbol.upper(),
            timestamp=(
                ensure_utc(pd.to_datetime(timestamp, unit="ms", utc=True).to_pydatetime())
                if timestamp
                else utcnow()
            ),
            last=_dec(raw.get("last") or raw.get("close")),
            bid=_dec_or_none(raw.get("bid")),
            ask=_dec_or_none(raw.get("ask")),
            quote_volume_24h=_dec_or_none(raw.get("quoteVolume")),
            price_change_pct_24h=(
                _dec(raw["percentage"]) / Decimal("100")
                if raw.get("percentage") is not None
                else None
            ),
        )

    def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        self._load_markets()
        try:
            raw = self._exchange.fetch_order_book(symbol, limit=limit)
        except Exception as exc:
            # Depth is a nice-to-have; a failure here degrades the liquidity
            # check to the ticker-based one rather than blocking analysis.
            logger.warning(
                "order book fetch failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return None
        return OrderBook(
            symbol=symbol.upper(),
            timestamp=utcnow(),
            bids=[
                OrderBookLevel(price=_dec(price), amount=_dec(amount))
                for price, amount in raw.get("bids", [])[:limit]
            ],
            asks=[
                OrderBookLevel(price=_dec(price), amount=_dec(amount))
                for price, amount in raw.get("asks", [])[:limit]
            ],
        )

    def fetch_funding_rate(self, symbol: str) -> Decimal | None:
        if not self._exchange.has.get("fetchFundingRate"):
            return None
        try:
            raw = self._exchange.fetch_funding_rate(symbol)
        except Exception:
            return None
        rate = raw.get("fundingRate")
        return _dec_or_none(rate)

    def fetch_open_interest(self, symbol: str) -> Decimal | None:
        if not self._exchange.has.get("fetchOpenInterest"):
            return None
        try:
            raw = self._exchange.fetch_open_interest(symbol)
        except Exception:
            return None
        return _dec_or_none(
            raw.get("openInterestValue") or raw.get("openInterestAmount")
        )

    def fetch_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        self._load_markets()
        market = self._exchange.markets.get(symbol)
        if not market:
            return None
        limits = market.get("limits") or {}
        precision = market.get("precision") or {}
        return SymbolSpec(
            symbol=symbol.upper(),
            base=market.get("base", ""),
            quote=market.get("quote", ""),
            price_tick=_step_from_precision(precision.get("price"), Decimal("0.01")),
            quantity_step=_step_from_precision(
                precision.get("amount"), Decimal("0.00001")
            ),
            min_quantity=_dec_or_none((limits.get("amount") or {}).get("min"))
            or Decimal("0"),
            min_notional=_dec_or_none((limits.get("cost") or {}).get("min"))
            or Decimal("10"),
            maker_fee_bps=_fee_bps(market.get("maker"), Decimal("10")),
            taker_fee_bps=_fee_bps(market.get("taker"), Decimal("10")),
            active=bool(market.get("active", True)),
        )

    def close(self) -> None:
        close = getattr(self._exchange, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):  # best effort on shutdown
                close()


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensive
        return None


def _step_from_precision(precision: Any, default: Decimal) -> Decimal:
    """ccxt reports precision either as a step size or as decimal places."""
    if precision is None:
        return default
    value = Decimal(str(precision))
    if value <= 0:
        return default
    if value < 1:
        return value
    # Integer precision == number of decimal places.
    return Decimal(1).scaleb(-int(value))


def _fee_bps(fee: Any, default: Decimal) -> Decimal:
    if fee is None:
        return default
    return Decimal(str(fee)) * Decimal("10000")
