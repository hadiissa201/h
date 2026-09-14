"""EMA crossover with momentum confirmation.

A bare EMA cross is one of the most over-fitted signals in retail trading, so
this one only fires when the cross is *recent* (not a stale condition that has
been true for fifty bars) and momentum, MACD and volume all agree.
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


class EmaMomentumParams(StrategyParams):
    max_bars_since_cross: int = 4
    rsi_min: float = 52.0
    rsi_max: float = 80.0
    min_relative_volume: float = 1.0
    atr_stop_multiple: float = 1.8
    reward_multiple: float = 2.5
    trailing_atr_multiple: float = 2.5
    breakeven_at_r: float = 1.0
    time_stop_bars: int = 48
    min_adx: float = 15.0


class EmaMomentumStrategy(Strategy):
    name = "ema_momentum"
    description = (
        "Fast/slow EMA cross within the last few bars, confirmed by RSI, MACD "
        "histogram and participation."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.STRONG_BULL_TREND,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.RANGE,
            MarketRegime.LOW_VOLATILITY,
            MarketRegime.STRONG_BEAR_TREND,
            MarketRegime.WEAK_BEAR_TREND,
        }
    )
    required_features = (
        "ema_fast",
        "ema_slow",
        "ema_fast_vs_slow",
        "rsi",
        "macd_hist",
        "atr",
        "adx",
        "relative_volume",
    )

    @classmethod
    def default_params(cls) -> EmaMomentumParams:
        return EmaMomentumParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: EmaMomentumParams = self.params  # type: ignore[assignment]
        row = context.row
        previous = context.previous_row
        close = context.close
        atr = row["atr"]

        if atr is None or atr <= 0:
            return self.hold(context, "ATR unavailable")
        if (row["adx"] or 0) < params.min_adx:
            return self.hold(context, f"ADX {row['adx']:.1f} too low for a momentum entry")

        spread_now = row["ema_fast_vs_slow"]
        spread_before = previous.get("ema_fast_vs_slow")
        if spread_now is None or spread_before is None:
            return self.hold(context, "EMA spread unavailable")

        bars_since_cross = self._bars_since_cross(context)
        if bars_since_cross is None or bars_since_cross > params.max_bars_since_cross:
            return self.hold(
                context,
                "no EMA cross within "
                f"{params.max_bars_since_cross} bars (last: {bars_since_cross})",
            )

        bullish_cross = spread_now > 0
        relative_volume = row.get("relative_volume") or 0.0
        rsi = row["rsi"]
        macd_hist = row["macd_hist"] or 0.0

        if bullish_cross:
            if not (params.rsi_min <= rsi <= params.rsi_max):
                return self.hold(context, f"RSI {rsi:.1f} does not confirm the cross")
            if macd_hist <= 0:
                return self.hold(context, "MACD histogram does not confirm the cross")
            if relative_volume < params.min_relative_volume:
                return self.hold(
                    context,
                    f"relative volume {relative_volume:.2f} below "
                    f"{params.min_relative_volume}",
                )
            stop = close - params.atr_stop_multiple * atr
            swing_low = row.get("last_swing_low")
            if swing_low is not None and swing_low < close:
                stop = min(stop, swing_low * 0.999)
            target = target_from_r(close, stop, params.reward_multiple, SignalDirection.BUY)
            return self.build_signal(
                context,
                direction=SignalDirection.BUY,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, bars_since_cross, bullish=True),
                reason=(
                    f"EMA fast crossed above slow {bars_since_cross} bar(s) ago; "
                    f"RSI {rsi:.1f}, MACD histogram {macd_hist:.4f}, "
                    f"relative volume {relative_volume:.2f}"
                ),
                invalidation_condition=(
                    f"close below {stop:.4f} or fast EMA back under slow EMA"
                ),
                trailing_stop_atr_multiple=params.trailing_atr_multiple,
                breakeven_at_r=params.breakeven_at_r,
                time_stop_bars=params.time_stop_bars,
                features_used=("ema_fast_vs_slow", "rsi", "macd_hist", "relative_volume", "adx"),
                metadata={"bars_since_cross": bars_since_cross},
            )

        if not context.allow_short:
            return self.hold(
                context, "bearish EMA cross but shorting is disabled (spot mode)"
            )
        if not (100 - params.rsi_max <= rsi <= 100 - params.rsi_min):
            return self.hold(context, f"RSI {rsi:.1f} does not confirm the cross")
        if macd_hist >= 0:
            return self.hold(context, "MACD histogram does not confirm the cross")
        stop = close + params.atr_stop_multiple * atr
        swing_high = row.get("last_swing_high")
        if swing_high is not None and swing_high > close:
            stop = max(stop, swing_high * 1.001)
        target = target_from_r(close, stop, params.reward_multiple, SignalDirection.SELL)
        return self.build_signal(
            context,
            direction=SignalDirection.SELL,
            entry=close,
            stop_loss=stop,
            take_profit=target,
            confidence=self._confidence(context, bars_since_cross, bullish=False),
            reason=(
                f"EMA fast crossed below slow {bars_since_cross} bar(s) ago; "
                f"RSI {rsi:.1f}, MACD histogram {macd_hist:.4f}"
            ),
            invalidation_condition=f"close above {stop:.4f}",
            trailing_stop_atr_multiple=params.trailing_atr_multiple,
            breakeven_at_r=params.breakeven_at_r,
            time_stop_bars=params.time_stop_bars,
            features_used=("ema_fast_vs_slow", "rsi", "macd_hist", "relative_volume"),
            metadata={"bars_since_cross": bars_since_cross},
        )

    def _bars_since_cross(self, context: StrategyContext) -> int | None:
        """How many bars ago the EMA spread last changed sign (0 = this bar).

        Reads the feature frame's own history, so live and backtest agree exactly.
        Returns ``None`` when no sign change exists in the lookback window — an
        old, stale condition rather than a fresh cross.
        """
        params: EmaMomentumParams = self.params  # type: ignore[assignment]
        lookback = params.max_bars_since_cross + 3
        series = context.feature_history("ema_fast_vs_slow", lookback)
        if series is None or len(series) < 2:
            return None
        window = series.dropna().to_numpy()
        if len(window) < 2:
            return None
        current_sign = window[-1] > 0
        for offset in range(2, len(window) + 1):
            if (window[-offset] > 0) != current_sign:
                return offset - 2
        return None

    def _confidence(
        self, context: StrategyContext, bars_since_cross: int, *, bullish: bool
    ) -> float:
        row = context.row
        score = 0.38
        # Fresher cross, higher confidence.
        score += max(0.0, 0.12 - 0.03 * bars_since_cross)
        relative_volume = row.get("relative_volume") or 0.0
        score += min(0.12, max(0.0, (relative_volume - 1.0) * 0.12))
        adx = row.get("adx") or 0.0
        score += min(0.15, max(0.0, (adx - 15.0) / 100.0))
        favourable_regime = (
            MarketRegime.STRONG_BULL_TREND if bullish else MarketRegime.STRONG_BEAR_TREND
        )
        if context.regime.regime is favourable_regime:
            score += 0.08
        obv = row.get("obv_slope")
        if obv is not None and ((obv > 0) == bullish):
            score += 0.06
        return clamp_confidence(score)
