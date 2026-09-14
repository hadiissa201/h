"""Trend following: trade with an established trend, not against it.

Entry wants an *ongoing* trend plus a bar that resumes it, deliberately avoiding
the most stretched conditions (RSI extremes, price far above the fast EMA) where
a continuation entry mostly buys someone else's exit.
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


class TrendFollowingParams(StrategyParams):
    adx_min: float = 22.0
    rsi_long_min: float = 48.0
    rsi_long_max: float = 78.0
    rsi_short_min: float = 22.0
    rsi_short_max: float = 52.0
    max_stretch_from_ema: float = 0.06
    atr_stop_multiple: float = 2.0
    reward_multiple: float = 3.0
    trailing_atr_multiple: float = 3.0
    breakeven_at_r: float = 1.0
    partial_exit_at_r: float = 2.0
    partial_exit_fraction: float = 0.5
    min_atr_pct: float = 0.001


class TrendFollowingStrategy(Strategy):
    name = "trend_following"
    description = (
        "Long (or short) continuation inside an established ADX trend with the "
        "EMA stack aligned and structure confirming."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.STRONG_BULL_TREND,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.STRONG_BEAR_TREND,
            MarketRegime.WEAK_BEAR_TREND,
        }
    )
    required_features = (
        "adx",
        "di_spread",
        "ema_fast",
        "ema_slow",
        "ema_trend",
        "ema_stack_bull",
        "ema_stack_bear",
        "rsi",
        "atr",
        "atr_pct",
        "last_swing_low",
        "last_swing_high",
        "macd_hist",
        "structure",
    )

    @classmethod
    def default_params(cls) -> TrendFollowingParams:
        return TrendFollowingParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: TrendFollowingParams = self.params  # type: ignore[assignment]
        row = context.row
        close = context.close
        adx = row["adx"]
        atr = row["atr"]
        atr_pct = row["atr_pct"]

        if atr is None or atr <= 0 or (atr_pct or 0) < params.min_atr_pct:
            return self.hold(context, "ATR too small to place a meaningful stop")
        if adx is None or adx < params.adx_min:
            return self.hold(context, f"ADX {adx:.1f} below {params.adx_min}")

        bullish = (
            bool(row["ema_stack_bull"])
            and row["di_spread"] > 0
            and close > row["ema_fast"]
            and (row["macd_hist"] or 0) > 0
        )
        bearish = (
            bool(row["ema_stack_bear"])
            and row["di_spread"] < 0
            and close < row["ema_fast"]
            and (row["macd_hist"] or 0) < 0
        )

        if bullish:
            rsi = row["rsi"]
            if not (params.rsi_long_min <= rsi <= params.rsi_long_max):
                return self.hold(context, f"RSI {rsi:.1f} outside continuation band")
            stretch = close / row["ema_fast"] - 1.0
            if stretch > params.max_stretch_from_ema:
                return self.hold(
                    context, f"price {stretch:.2%} above fast EMA — too extended"
                )
            structural_stop = row.get("last_swing_low")
            atr_stop = close - params.atr_stop_multiple * atr
            # Prefer the structural level when it is not absurdly far away: a
            # stop under the last swing low is invalidation, an ATR stop is noise.
            stop = atr_stop
            used_structure = False
            if structural_stop is not None and structural_stop < close:
                if structural_stop > close - 3.0 * params.atr_stop_multiple * atr:
                    stop = min(structural_stop * 0.999, atr_stop)
                    used_structure = structural_stop * 0.999 <= atr_stop
            target = target_from_r(close, stop, params.reward_multiple, SignalDirection.BUY)
            confidence = self._confidence(context, adx, bullish=True)
            return self.build_signal(
                context,
                direction=SignalDirection.BUY,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=confidence,
                reason=(
                    f"Uptrend continuation: ADX {adx:.1f}, EMA stack bullish, "
                    f"+DI>-DI, MACD histogram positive, RSI {rsi:.1f}"
                ),
                invalidation_condition=(
                    f"close below {stop:.4f} (structural stop)"
                    if used_structure
                    else f"close below {stop:.4f} ({params.atr_stop_multiple}x ATR)"
                ),
                trailing_stop_atr_multiple=params.trailing_atr_multiple,
                breakeven_at_r=params.breakeven_at_r,
                partial_exit_at_r=params.partial_exit_at_r,
                partial_exit_fraction=params.partial_exit_fraction,
                features_used=("adx", "di_spread", "rsi", "atr_pct", "macd_hist", "structure"),
                metadata={"stop_source": "structure" if used_structure else "atr"},
            )

        if bearish:
            if not context.allow_short:
                return self.hold(
                    context,
                    "bearish continuation found but shorting is disabled (spot mode)",
                )
            rsi = row["rsi"]
            if not (params.rsi_short_min <= rsi <= params.rsi_short_max):
                return self.hold(context, f"RSI {rsi:.1f} outside continuation band")
            structural_stop = row.get("last_swing_high")
            atr_stop = close + params.atr_stop_multiple * atr
            stop = atr_stop
            if structural_stop is not None and structural_stop > close:
                if structural_stop < close + 3.0 * params.atr_stop_multiple * atr:
                    stop = max(structural_stop * 1.001, atr_stop)
            target = target_from_r(
                close, stop, params.reward_multiple, SignalDirection.SELL
            )
            return self.build_signal(
                context,
                direction=SignalDirection.SELL,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, adx, bullish=False),
                reason=(
                    f"Downtrend continuation: ADX {adx:.1f}, EMA stack bearish, "
                    f"-DI>+DI, MACD histogram negative, RSI {rsi:.1f}"
                ),
                invalidation_condition=f"close above {stop:.4f}",
                trailing_stop_atr_multiple=params.trailing_atr_multiple,
                breakeven_at_r=params.breakeven_at_r,
                partial_exit_at_r=params.partial_exit_at_r,
                partial_exit_fraction=params.partial_exit_fraction,
                features_used=("adx", "di_spread", "rsi", "atr_pct", "macd_hist"),
            )

        return self.hold(context, "no aligned trend continuation setup")

    def _confidence(
        self, context: StrategyContext, adx: float, *, bullish: bool
    ) -> float:
        row = context.row
        score = 0.35
        score += min(0.25, (adx - 20.0) / 60.0)  # trend quality
        structure = row.get("structure") or 0.0
        if (structure > 0 and bullish) or (structure < 0 and not bullish):
            score += 0.12
        confirms = row.get("volume_confirms")
        if confirms is not None and confirms > 0:
            score += 0.08
        obv = row.get("obv_slope")
        if obv is not None and ((obv > 0) == bullish):
            score += 0.06
        if context.regime.regime in (
            MarketRegime.STRONG_BULL_TREND,
            MarketRegime.STRONG_BEAR_TREND,
        ):
            score += 0.08
        if context.higher_timeframe_row:
            htf_di = context.higher_timeframe_row.get("di_spread")
            if htf_di is not None and ((htf_di > 0) == bullish):
                score += 0.08
            elif htf_di is not None:
                score -= 0.10
        return clamp_confidence(score)
