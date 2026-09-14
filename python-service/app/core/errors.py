"""Domain errors.

They map onto HTTP status codes in ``app.api.errors`` so n8n receives a
machine-readable ``{"error_code": ..., "detail": ...}`` body it can branch on.
"""

from __future__ import annotations

from typing import Any


class TradingError(Exception):
    """Base class for every deliberate, expected failure in the service."""

    error_code = "TRADING_ERROR"
    http_status = 400

    def __init__(self, detail: str, **context: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = context


class ValidationError(TradingError):
    error_code = "VALIDATION_ERROR"
    http_status = 422


class MarketDataError(TradingError):
    error_code = "MARKET_DATA_ERROR"
    http_status = 424


class StaleDataError(MarketDataError):
    error_code = "STALE_MARKET_DATA"
    http_status = 424


class ExchangeError(TradingError):
    error_code = "EXCHANGE_ERROR"
    http_status = 502


class InsufficientBalanceError(ExchangeError):
    error_code = "INSUFFICIENT_BALANCE"
    http_status = 409


class OrderRejectedError(ExchangeError):
    error_code = "ORDER_REJECTED"
    http_status = 409


class RiskRejectedError(TradingError):
    """Raised when execution is attempted without valid risk approval."""

    error_code = "RISK_REJECTED"
    http_status = 403


class BotHaltedError(TradingError):
    error_code = "BOT_HALTED"
    http_status = 423


class LiveTradingBlockedError(TradingError):
    """Raised when something tries to reach a real exchange without the
    full arming sequence in place."""

    error_code = "LIVE_TRADING_BLOCKED"
    http_status = 403


class LLMError(TradingError):
    error_code = "LLM_ERROR"
    http_status = 502


class NotFoundError(TradingError):
    error_code = "NOT_FOUND"
    http_status = 404


class ConflictError(TradingError):
    error_code = "CONFLICT"
    http_status = 409
