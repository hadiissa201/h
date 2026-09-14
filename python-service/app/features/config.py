"""Feature parameters.

Every lookback lives in one place so backtests, walk-forward runs and the live
service provably use the same numbers — and so a walk-forward optimiser has a
single object to perturb.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FeatureConfig(BaseModel):
    model_config = {"frozen": True}

    # trend
    sma_fast: int = Field(default=20, ge=2)
    sma_slow: int = Field(default=50, ge=3)
    ema_fast: int = Field(default=21, ge=2)
    ema_slow: int = Field(default=55, ge=3)
    ema_trend: int = Field(default=200, ge=10)
    macd_fast: int = Field(default=12, ge=2)
    macd_slow: int = Field(default=26, ge=3)
    macd_signal: int = Field(default=9, ge=2)
    adx_period: int = Field(default=14, ge=2)
    slope_period: int = Field(default=20, ge=3)
    # Long enough to span a typical range cycle: a 20-bar window sees a slow
    # oscillation as a clean trend, which is exactly the error to avoid.
    efficiency_period: int = Field(default=60, ge=10)

    # momentum
    rsi_period: int = Field(default=14, ge=2)
    stoch_rsi_period: int = Field(default=14, ge=2)
    roc_period: int = Field(default=10, ge=1)

    # volatility
    atr_period: int = Field(default=14, ge=2)
    bb_period: int = Field(default=20, ge=3)
    bb_std: float = Field(default=2.0, gt=0)
    realized_vol_period: int = Field(default=20, ge=3)
    vol_ratio_short: int = Field(default=10, ge=3)
    vol_ratio_long: int = Field(default=50, ge=5)
    # Percentile window for "is this volatility unusual *for this market*".
    # Must be much longer than vol_ratio_long: over 50 bars the rank saturates
    # at 1.0 on any breakout bar, which silently blocks breakout strategies.
    atr_rank_period: int = Field(default=200, ge=50)

    # volume
    volume_ma_period: int = Field(default=20, ge=2)

    # structure
    pivot_left: int = Field(default=3, ge=1)
    pivot_right: int = Field(default=3, ge=1)
    donchian_period: int = Field(default=20, ge=3)

    @property
    def warmup_bars(self) -> int:
        """Bars needed before every feature is defined.

        Used as a hard gate: a strategy is never shown a partially warmed-up
        feature row, because "NaN" silently compares False and would turn into
        an accidental signal.
        """
        return (
            max(
                self.sma_slow,
                self.ema_trend,
                self.macd_slow + self.macd_signal,
                self.adx_period * 2 + 1,
                self.rsi_period + self.stoch_rsi_period + 6,
                self.bb_period,
                self.vol_ratio_long,
                self.atr_rank_period,
                self.volume_ma_period,
                self.donchian_period,
                self.slope_period,
                self.efficiency_period,
            )
            + self.pivot_left
            + self.pivot_right
            + 2
        )
