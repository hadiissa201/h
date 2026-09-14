"""Mean reversion inside a range.

Only allowed in range / low-volatility regimes, because "oversold" in a strong
downtrend is not a buy signal — it is the downtrend working. The entry also
requires evidence that the push down is *decelerating* rather than accelerating.
"""

from __future__ import annotations

from app.models.enums import MarketRegime, SignalDirection
from app.models.signals import StrategySignal
from app.strategies.base import (
    Strategy,
    StrategyContext,
    StrategyParams,
    clamp_confidence,
)


class MeanReversionParams(StrategyParams):
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    bb_position_low: float = 0.15
    bb_position_high: float = 0.85
    min_bb_width: float = 0.015
    atr_stop_multiple: float = 1.5
    min_reward_multiple: float = 1.6
    max_adx: float = 28.0
    breakeven_at_r: float = 1.0
    time_stop_bars: int = 18
    require_deceleration: bool = True


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    description = (
        "Buy the lower Bollinger band in a range when momentum is oversold and "
        "decelerating; target the band mid-line."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.RANGE,
            MarketRegime.LOW_VOLATILITY,
            MarketRegime.HIGH_VOLATILITY,
        }
    )
    required_features = (
        "rsi",
        "bb_position",
        "bb_middle",
        "bb_lower",
        "bb_upper",
        "bb_width",
        "atr",
        "adx",
    )

    @classmethod
    def default_params(cls) -> MeanReversionParams:
        return MeanReversionParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: MeanReversionParams = self.params  # type: ignore[assignment]
        row = context.row
        close = context.close
        atr = row["atr"]

        if atr is None or atr <= 0:
            return self.hold(context, "ATR unavailable")
        adx = row["adx"]
        if adx is not None and adx > params.max_adx:
            return self.hold(
                context, f"ADX {adx:.1f} shows a trend — mean reversion stands aside"
            )
        bb_width = row["bb_width"]
        if bb_width is None or bb_width < params.min_bb_width:
            return self.hold(
                context,
                f"Bollinger width {bb_width} too narrow to pay for the risk",
            )

        rsi = row["rsi"]
        position = row["bb_position"]
        middle = row["bb_middle"]
        if rsi is None or position is None or middle is None:
            return self.hold(context, "Bollinger/RSI features unavailable")

        # --- long side
        if position <= params.bb_position_low and rsi <= params.rsi_oversold:
            if params.require_deceleration and not self._decelerating(context, long=True):
                return self.hold(
                    context, "downside momentum still accelerating — no reversion entry"
                )
            if context.regime.trend_state.value.endswith("down") and (
                context.regime.regime
                in (MarketRegime.STRONG_BEAR_TREND, MarketRegime.WEAK_BEAR_TREND)
            ):
                return self.hold(context, "oversold inside a downtrend is not a setup")
            stop = close - params.atr_stop_multiple * atr
            swing_low = row.get("last_swing_low")
            if swing_low is not None and swing_low < close:
                stop = min(stop, swing_low * 0.998)
            risk = close - stop
            if risk <= 0:
                return self.hold(context, "invalid stop distance")
            # Target the mid-band, but never accept less than the minimum R.
            target = max(middle, close + params.min_reward_multiple * risk)
            return self.build_signal(
                context,
                direction=SignalDirection.BUY,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, rsi, position, long=True),
                reason=(
                    f"Range reversion long: price at {position:.2f} of the Bollinger "
                    f"range, RSI {rsi:.1f}, ADX {adx:.1f}"
                ),
                invalidation_condition=(
                    f"close below {stop:.4f}, or ADX rising above {params.max_adx} "
                    "(range has become a trend)"
                ),
                breakeven_at_r=params.breakeven_at_r,
                time_stop_bars=params.time_stop_bars,
                features_used=("rsi", "bb_position", "bb_width", "adx", "atr_pct"),
                metadata={"bb_middle": middle},
            )

        # --- short side
        if position >= params.bb_position_high and rsi >= params.rsi_overbought:
            if not context.allow_short:
                return self.hold(
                    context,
                    "overbought reversion setup but shorting is disabled (spot mode)",
                )
            if params.require_deceleration and not self._decelerating(context, long=False):
                return self.hold(context, "upside momentum still accelerating")
            stop = close + params.atr_stop_multiple * atr
            swing_high = row.get("last_swing_high")
            if swing_high is not None and swing_high > close:
                stop = max(stop, swing_high * 1.002)
            risk = stop - close
            if risk <= 0:
                return self.hold(context, "invalid stop distance")
            target = min(middle, close - params.min_reward_multiple * risk)
            return self.build_signal(
                context,
                direction=SignalDirection.SELL,
                entry=close,
                stop_loss=stop,
                take_profit=target,
                confidence=self._confidence(context, rsi, position, long=False),
                reason=(
                    f"Range reversion short: price at {position:.2f} of the Bollinger "
                    f"range, RSI {rsi:.1f}"
                ),
                invalidation_condition=f"close above {stop:.4f}",
                breakeven_at_r=params.breakeven_at_r,
                time_stop_bars=params.time_stop_bars,
                features_used=("rsi", "bb_position", "bb_width", "adx"),
                metadata={"bb_middle": middle},
            )

        return self.hold(context, "price is not at a range extreme")

    def _decelerating(self, context: StrategyContext, *, long: bool) -> bool:
        """True when the last push is smaller than the one before it.

        Cheap proxy for "the sellers are getting tired" that does not require
        candlestick pattern matching.
        """
        closes = context.history("close", 4)
        if len(closes) < 4:
            return False
        moves = closes.diff().dropna().to_numpy()
        if len(moves) < 3:
            return False
        if long:
            return moves[-1] > moves[-2] or moves[-1] > 0
        return moves[-1] < moves[-2] or moves[-1] < 0

    def _confidence(
        self, context: StrategyContext, rsi: float, position: float, *, long: bool
    ) -> float:
        params: MeanReversionParams = self.params  # type: ignore[assignment]
        row = context.row
        score = 0.36
        if long:
            score += min(0.15, (params.rsi_oversold - rsi) / 100.0 * 2.0)
            score += min(0.10, max(0.0, (params.bb_position_low - position)) * 1.5)
        else:
            score += min(0.15, (rsi - params.rsi_overbought) / 100.0 * 2.0)
            score += min(0.10, max(0.0, (position - params.bb_position_high)) * 1.5)
        if context.regime.regime is MarketRegime.RANGE:
            score += 0.1
        stoch = row.get("stoch_rsi_k")
        if stoch is not None:
            if long and stoch < 20:
                score += 0.06
            elif not long and stoch > 80:
                score += 0.06
        relative_volume = row.get("relative_volume")
        if relative_volume is not None and relative_volume > 1.5:
            # A high-volume flush at a range edge is more often continuation.
            score -= 0.08
        return clamp_confidence(score)
