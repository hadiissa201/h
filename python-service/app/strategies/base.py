"""Strategy framework.

Contract every strategy honours:

* it reads one feature row and returns one ``StrategySignal``;
* it never executes, sizes, or touches the portfolio — sizing belongs to the
  risk engine, which has final authority;
* it declares the regimes it is allowed to operate in, so a mean-reversion idea
  cannot fire inside a violent trend just because its own thresholds happened to
  line up;
* it is deterministic. Same row in, same signal out.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel

from app.core.numeric import to_decimal
from app.models.enums import MarketRegime, OrderType, SignalDirection
from app.models.signals import RegimeAssessment, StrategySignal


class StrategyParams(BaseModel):
    """Base for per-strategy parameters (frozen so a run cannot mutate them)."""

    model_config = {"frozen": True, "extra": "forbid"}


@dataclass
class StrategyContext:
    """Everything a strategy may look at for one decision.

    History is exposed only through ``history``/``feature_history``, which slice
    strictly up to the current bar. A strategy therefore cannot accidentally read
    the future even when it walks back over past values.
    """

    symbol: str
    timeframe: str
    timestamp: datetime
    row: dict[str, float | None]
    previous_row: dict[str, float | None]
    candles: pd.DataFrame
    features: pd.DataFrame
    index: int
    regime: RegimeAssessment
    allow_short: bool = False
    higher_timeframe_row: dict[str, float | None] | None = None

    @property
    def close(self) -> float:
        return float(self.row["close"])

    @property
    def position(self) -> int:
        """Absolute row offset of the current bar."""
        return self.index if self.index >= 0 else len(self.candles) + self.index

    def feature(self, name: str, default: float | None = None) -> float | None:
        value = self.row.get(name, default)
        return default if value is None else value

    def history(self, column: str, bars: int) -> pd.Series:
        """Last ``bars`` values of a *candle* column, ending at the current bar."""
        end = self.position + 1
        return self.candles[column].iloc[max(0, end - bars) : end]

    def feature_history(self, column: str, bars: int) -> pd.Series | None:
        """Last ``bars`` values of a *feature* column, ending at the current bar."""
        if column not in self.features.columns:
            return None
        end = self.position + 1
        return self.features[column].iloc[max(0, end - bars) : end]


class Strategy(abc.ABC):
    name: str = "base"
    description: str = ""
    allowed_regimes: frozenset[MarketRegime] = frozenset()
    # Features the strategy reads. Used by the engine to refuse to run a
    # strategy against a feature set that does not expose what it needs,
    # instead of silently treating missing values as neutral.
    required_features: tuple[str, ...] = ()

    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or self.default_params()

    @classmethod
    def default_params(cls) -> StrategyParams:
        return StrategyParams()

    def allows_regime(self, regime: MarketRegime) -> bool:
        return regime in self.allowed_regimes

    def missing_features(self, context: StrategyContext) -> list[str]:
        return [
            name
            for name in self.required_features
            if context.row.get(name) is None
        ]

    @abc.abstractmethod
    def evaluate(self, context: StrategyContext) -> StrategySignal:
        """Return an actionable signal or a HOLD."""

    # ------------------------------------------------------------- utilities
    def hold(
        self, context: StrategyContext, reason: str, confidence: float = 0.0
    ) -> StrategySignal:
        return StrategySignal(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            timestamp=context.timestamp,
            signal=SignalDirection.HOLD,
            confidence=confidence,
            reason=reason,
            regime=context.regime.regime,
        )

    def build_signal(
        self,
        context: StrategyContext,
        *,
        direction: SignalDirection,
        entry: float,
        stop_loss: float,
        take_profit: float | None,
        confidence: float,
        reason: str,
        invalidation_condition: str,
        trailing_stop_atr_multiple: float | None = None,
        breakeven_at_r: float | None = None,
        partial_exit_at_r: float | None = None,
        partial_exit_fraction: float | None = None,
        time_stop_bars: int | None = None,
        entry_order_type: OrderType = OrderType.MARKET,
        entry_valid_bars: int = 1,
        features_used: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> StrategySignal:
        return StrategySignal(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            timestamp=context.timestamp,
            signal=direction,
            confidence=round(min(0.95, max(0.0, confidence)), 4),
            entry=_price(entry),
            stop_loss=_price(stop_loss),
            take_profit=_price(take_profit) if take_profit is not None else None,
            reason=reason,
            regime=context.regime.regime,
            invalidation_condition=invalidation_condition,
            trailing_stop_atr_multiple=trailing_stop_atr_multiple,
            breakeven_at_r=breakeven_at_r,
            partial_exit_at_r=partial_exit_at_r,
            partial_exit_fraction=partial_exit_fraction,
            time_stop_bars=time_stop_bars,
            entry_order_type=entry_order_type,
            entry_valid_bars=entry_valid_bars,
            features_used={name: context.row.get(name) for name in features_used},
            metadata=metadata or {},
        )


def _price(value: float) -> Decimal:
    # 8 decimal places covers every crypto tick size we care about; the exchange
    # spec rounds to the real grid later.
    return to_decimal(round(float(value), 8))


def target_from_r(
    entry: float, stop: float, r_multiple: float, direction: SignalDirection
) -> float:
    """Take-profit at a fixed multiple of the risked distance."""
    risk = abs(entry - stop)
    if direction is SignalDirection.BUY:
        return entry + r_multiple * risk
    return entry - r_multiple * risk


def clamp_confidence(value: float, floor: float = 0.0, ceiling: float = 0.95) -> float:
    return max(floor, min(ceiling, value))
