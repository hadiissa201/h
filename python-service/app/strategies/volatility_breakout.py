"""Volatility-expansion breakout (squeeze release).

Waits for a volatility *contraction* (Bollinger bands inside Keltner channels,
low ATR percentile) and then trades the first decisive expansion. The edge being
tested is the transition, not the direction — so the direction is taken from the
expansion bar itself rather than from a separate trend opinion.
"""

from __future__ import annotations

from app.models.enums import MarketRegime, SignalDirection
from app.models.signals import StrategySignal
from app.strategies.base import (
    Strategy,
    StrategyContext,
    StrategyParams,
    clamp_confidence,
    target_from_r,
)


class VolatilityBreakoutParams(StrategyParams):
    squeeze_lookback: int = 10
    min_squeeze_bars: int = 3
    min_vol_ratio: float = 1.15
    min_bar_range_atr: float = 1.0
    max_atr_pct_rank: float = 0.9
    atr_stop_multiple: float = 1.2
    reward_multiple: float = 3.0
    trailing_atr_multiple: float = 2.0
    time_stop_bars: int = 20
    min_relative_volume: float = 1.2


class VolatilityBreakoutStrategy(Strategy):
    name = "volatility_breakout"
    description = (
        "Trade the first expansion bar after a volatility squeeze, in the "
        "direction of the expansion."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.LOW_VOLATILITY,
            MarketRegime.RANGE,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.WEAK_BEAR_TREND,
        }
    )
    required_features = (
        "squeeze",
        "vol_ratio",
        "atr",
        "atr_pct_rank",
        "relative_volume",
        "bb_upper",
        "bb_lower",
    )

    @classmethod
    def default_params(cls) -> VolatilityBreakoutParams:
        return VolatilityBreakoutParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: VolatilityBreakoutParams = self.params  # type: ignore[assignment]
        row = context.row
        close = context.close
        atr = row["atr"]
        if atr is None or atr <= 0:
            return self.hold(context, "ATR unavailable")

        # The squeeze must be in the recent past and must have *ended*: an active
        # squeeze means the market has not moved yet.
        squeeze_series = context.feature_history("squeeze", params.squeeze_lookback + 1)
        if squeeze_series is None or squeeze_series.empty:
            return self.hold(context, "squeeze feature unavailable")
        squeeze_values = squeeze_series.fillna(0.0).to_numpy()
        if squeeze_values[-1] > 0:
            return self.hold(context, "still inside the squeeze — waiting for expansion")
        prior_squeeze_bars = int((squeeze_values[:-1] > 0).sum())
        if prior_squeeze_bars < params.min_squeeze_bars:
            return self.hold(
                context,
                f"only {prior_squeeze_bars} squeeze bars in the last "
                f"{params.squeeze_lookback} (need {params.min_squeeze_bars})",
            )

        vol_ratio = row.get("vol_ratio")
        if vol_ratio is None or vol_ratio < params.min_vol_ratio:
            return self.hold(
                context, f"volatility ratio {vol_ratio} shows no expansion yet"
            )
        atr_rank = row.get("atr_pct_rank")
        if atr_rank is not None and atr_rank > params.max_atr_pct_rank:
            return self.hold(context, "volatility already extended")
        relative_volume = row.get("relative_volume") or 0.0
        if relative_volume < params.min_relative_volume:
            return self.hold(context, "expansion without volume")

        bar_range = float(row["high"]) - float(row["low"])
        if bar_range < params.min_bar_range_atr * atr:
            return self.hold(context, "expansion bar is not decisive")

        direction_up = float(row["close"]) > float(row["open"])
        if not direction_up and not context.allow_short:
            return self.hold(
                context, "downside expansion but shorting is disabled (spot mode)"
            )

        if direction_up:
            stop = min(close - params.atr_stop_multiple * atr, float(row["low"]) * 0.999)
            target = target_from_r(close, stop, params.reward_multiple, SignalDirection.BUY)
            direction = SignalDirection.BUY
        else:
            stop = max(close + params.atr_stop_multiple * atr, float(row["high"]) * 1.001)
            target = target_from_r(close, stop, params.reward_multiple, SignalDirection.SELL)
            direction = SignalDirection.SELL

        return self.build_signal(
            context,
            direction=direction,
            entry=close,
            stop_loss=stop,
            take_profit=target,
            confidence=self._confidence(context, prior_squeeze_bars, vol_ratio, relative_volume),
            reason=(
                f"Volatility expansion after {prior_squeeze_bars} squeeze bars: "
                f"vol ratio {vol_ratio:.2f}, bar range {bar_range / atr:.2f}x ATR, "
                f"volume {relative_volume:.2f}x average"
            ),
            invalidation_condition=(
                f"close back beyond {stop:.4f} (expansion failed) or volatility "
                "re-contracting into a squeeze"
            ),
            trailing_stop_atr_multiple=params.trailing_atr_multiple,
            time_stop_bars=params.time_stop_bars,
            features_used=("squeeze", "vol_ratio", "atr_pct_rank", "relative_volume"),
            metadata={
                "prior_squeeze_bars": prior_squeeze_bars,
                "bar_range_atr": round(bar_range / atr, 3),
            },
        )

    def _confidence(
        self,
        context: StrategyContext,
        squeeze_bars: int,
        vol_ratio: float,
        relative_volume: float,
    ) -> float:
        score = 0.38
        score += min(0.12, squeeze_bars * 0.02)
        score += min(0.15, (vol_ratio - 1.0) * 0.3)
        score += min(0.10, (relative_volume - 1.0) * 0.1)
        if context.regime.regime is MarketRegime.LOW_VOLATILITY:
            score += 0.05
        structure = context.row.get("structure") or 0.0
        direction_up = float(context.row["close"]) > float(context.row["open"])
        if (structure > 0) == direction_up and structure != 0:
            score += 0.06
        return clamp_confidence(score)
