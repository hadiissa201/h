"""Market-data schemas and the data-quality report that gates trading."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class Candle(BaseModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @model_validator(mode="after")
    def _check_ohlc(self) -> Candle:
        if self.high < self.low:
            raise ValueError("high < low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open outside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close outside [low, high]")
        if self.volume < 0:
            raise ValueError("negative volume")
        return self


class Ticker(BaseModel):
    symbol: str
    timestamp: datetime
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    price_change_pct_24h: Decimal | None = None

    @property
    def mid(self) -> Decimal:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_bps(self) -> Decimal | None:
        if self.bid and self.ask and self.bid > 0:
            mid = (self.bid + self.ask) / 2
            if mid > 0:
                return (self.ask - self.bid) / mid * Decimal("10000")
        return None


class OrderBookLevel(BaseModel):
    price: Decimal
    amount: Decimal


class OrderBook(BaseModel):
    symbol: str
    timestamp: datetime
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def depth_quote(self, side: str, levels: int = 10) -> Decimal:
        """Notional resting within the top ``levels`` — a crude liquidity proxy."""
        book = self.bids if side.lower() == "bid" else self.asks
        return sum(
            (level.price * level.amount for level in book[:levels]), start=Decimal("0")
        )


class DataQualityIssue(BaseModel):
    code: str
    detail: str
    severity: str = Field(default="error", pattern="^(warning|error)$")
    context: dict[str, Any] = Field(default_factory=dict)


class DataQualityReport(BaseModel):
    """Verdict on whether a candle set may be traded on.

    ``is_tradeable`` false means NO TRADE — the analysis pipeline stops here.
    """

    symbol: str
    timeframe: str
    bars: int
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    staleness_seconds: float | None = None
    issues: list[DataQualityIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[DataQualityIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[DataQualityIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def is_tradeable(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.is_tradeable:
            return "ok" if not self.warnings else "ok_with_warnings"
        return "; ".join(f"{issue.code}: {issue.detail}" for issue in self.errors)


class MarketSnapshot(BaseModel):
    """Everything the analysis pipeline needs for one symbol at one instant."""

    symbol: str
    timeframe: str
    fetched_at: datetime
    candles: list[Candle]
    ticker: Ticker | None = None
    order_book: OrderBook | None = None
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None
    quality: DataQualityReport
    provider: str = "unknown"

    @property
    def last_close(self) -> Decimal:
        return self.candles[-1].close


class CandleRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"
    limit: int = Field(default=300, ge=2, le=1500)

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, value: str) -> str:
        return value.strip().upper()
