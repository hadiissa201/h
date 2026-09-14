"""Donchian breakout with participation and follow-through filters.

Breakouts fail constantly, so the filters here are the substance of the
strategy: the channel must be cleared by more than a tick, volume must actually
show up, and volatility must not already be at a blow-off extreme (which is
where breakout entries become someone else's exit liquidity).
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


class BreakoutParams(StrategyParams):
    min_clearance_atr: float = 0.1
    min_relative_volume: float = 1.3
    max_atr_pct_rank: float = 0.95
    atr_stop_multiple: float = 1.5
    reward_multiple: float = 2.0
    trailing_atr_multiple: float = 2.5
    breakeven_at_r: float = 1.0
    time_stop_bars: int = 24
    max_bars_in_channel_streak: int = 3


class BreakoutStrategy(Strategy):
    name = "breakout"
    description = (
        "Close beyond the prior N-bar Donchian channel with volume expansion and "
        "a stop back inside the channel."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.RANGE,
            MarketRegime.LOW_VOLATILITY,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.STRONG_BULL_TREND,
            MarketRegime.WEAK_BEAR_TREND,
            MarketRegime.STRONG_BEAR_TREND,
        }
    )
    required_features = (
        "breakout_up",
        "breakout_down",
        "dc_upper",
        "dc_lower",
        "atr",
        "atr_pct_rank",
        "relative_volume",
    )

    @classmethod
    def default_params(cls) -> BreakoutParams:
        return BreakoutParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: BreakoutParams = self.params  # type: ignore[assignment]
        row = context.row
        close = context.close
        atr = row["atr"]

        if atr is None or atr <= 0:
            return self.hold(context, "ATR unavailable")

        atr_rank = row.get("atr_pct_rank")
        if atr_rank is not None and atr_rank > params.max_atr_pct_rank:
            return self.hold(
                context,
                f"volatility percentile {atr_rank:.2f} too extreme for a breakout entry",
            )

        relative_volume = row.get("relative_volume") or 0.0
        if relative_volume < params.min_relative_volume:
            return self.hold(
                context,
                f"relative volume {relative_volume:.2f} below "
                f"{params.min_relative_volume} — breakout lacks participation",
            )

        upper = row["dc_upper"]
        lower = row["dc_lower"]
        min_clearance = params.min_clearance_atr * atr

        if bool(row["breakout_up"]) and upper is not None:
            if close - upper < min_clearance:
                return self.hold(
                    context,
                    f"close only {close - upper:.6f} above channel "
                    f"(need {min_clearance:.6f})",
                )
            # A breakout that has already been "true" for several bars is not a
            # breakout any more; that is a trend the trend strategies own.
            streak = self._breakout_streak(context, "breakout_up")
            if streak > params.max_bars_in_channel_streak:
                return self.hold(
                    context, f"breakout is {streak} bars old — no longer fresh"
                )
            # Stop just back inside the channel, or an ATR stop if that is
            # tighter — a breakout that re-enters the range has failed.
            stop = max(close - params.atr_stop_multiple * atr, upper - 0.25 * atr)
            target = target_from_r(close, stop, params.reward_multiple, SignalDirection.BUY)
            return self.build_signal(
                context,
                direction=SignalDirection.BUY,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, relative_volume, streak),
                reason=(
                    f"Upside breakout: close {close:.4f} above channel high "
                    f"{upper:.4f} on {relative_volume:.2f}x average volume"
                ),
                invalidation_condition=(
                    f"close back inside the channel below {stop:.4f}"
                ),
                trailing_stop_atr_multiple=params.trailing_atr_multiple,
                breakeven_at_r=params.breakeven_at_r,
                time_stop_bars=params.time_stop_bars,
                features_used=("breakout_up", "dc_upper", "relative_volume", "atr_pct_rank"),
                metadata={"channel_high": upper, "breakout_streak": streak},
            )

        if bool(row["breakout_down"]) and lower is not None:
            if not context.allow_short:
                return self.hold(
                    context, "downside breakout but shorting is disabled (spot mode)"
                )
            if lower - close < min_clearance:
                return self.hold(context, "close too close to the channel low")
            streak = self._breakout_streak(context, "breakout_down")
            if streak > params.max_bars_in_channel_streak:
                return self.hold(context, f"breakout is {streak} bars old")
            stop = min(close + params.atr_stop_multiple * atr, lower + 0.25 * atr)
            target = target_from_r(
                close, stop, params.reward_multiple, SignalDirection.SELL
            )
            return self.build_signal(
                context,
                direction=SignalDirection.SELL,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, relative_volume, streak),
                reason=(
                    f"Downside breakout: close {close:.4f} below channel low "
                    f"{lower:.4f} on {relative_volume:.2f}x average volume"
                ),
                invalidation_condition=f"close back above {stop:.4f}",
                trailing_stop_atr_multiple=params.trailing_atr_multiple,
                breakeven_at_r=params.breakeven_at_r,
                time_stop_bars=params.time_stop_bars,
                features_used=("breakout_down", "dc_lower", "relative_volume"),
                metadata={"channel_low": lower, "breakout_streak": streak},
            )

        return self.hold(context, "price inside the Donchian channel")

    def _breakout_streak(self, context: StrategyContext, column: str) -> int:
        series = context.feature_history(column, 12)
        if series is None or series.empty:
            return 0
        values = series.fillna(0.0).to_numpy()
        streak = 0
        for value in reversed(values):
            if value > 0:
                streak += 1
            else:
                break
        return streak

    def _confidence(
        self, context: StrategyContext, relative_volume: float, streak: int
    ) -> float:
        row = context.row
        score = 0.4
        score += min(0.18, (relative_volume - 1.0) * 0.15)
        score += max(0.0, 0.08 - 0.03 * (streak - 1))
        squeeze_before = context.feature_history("squeeze", 6)
        if squeeze_before is not None and (squeeze_before.fillna(0.0) > 0).any():
            # Breaking out of a volatility squeeze is the higher-quality version.
            score += 0.1
        if (row.get("adx") or 0) > 20:
            score += 0.06
        if context.regime.regime in (MarketRegime.RANGE, MarketRegime.LOW_VOLATILITY):
            score += 0.05
        return clamp_confidence(score)
