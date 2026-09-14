"""Domain enums shared by the API contract, the database and the engines."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    INVALIDATION = "INVALIDATION"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    REGIME_CHANGE = "REGIME_CHANGE"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"
    TIME_STOP = "TIME_STOP"
    END_OF_BACKTEST = "END_OF_BACKTEST"


class MarketRegime(StrEnum):
    STRONG_BULL_TREND = "strong_bull_trend"
    WEAK_BULL_TREND = "weak_bull_trend"
    RANGE = "range"
    WEAK_BEAR_TREND = "weak_bear_trend"
    STRONG_BEAR_TREND = "strong_bear_trend"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    ABNORMAL = "abnormal"
    UNKNOWN = "unknown"


class VolatilityState(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


class TrendState(StrEnum):
    STRONG_UP = "strong_up"
    WEAK_UP = "weak_up"
    FLAT = "flat"
    WEAK_DOWN = "weak_down"
    STRONG_DOWN = "strong_down"


class RiskDecisionType(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BotStatus(StrEnum):
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    PAUSED = "PAUSED"


class TradingModeEnum(StrEnum):
    PAPER = "paper"
    LIVE = "live"
