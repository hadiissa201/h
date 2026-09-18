"""Short-term reversal: fade a sharp, unsupported move.

The one short-horizon effect with real evidence behind it. Sharp moves over a
few bars, unaccompanied by a change in trend, tend to partially retrace --
documented in equities since Jegadeesh (1990) and visible across asset classes.
It is not a secret and it is not exotic; it is simply the short-horizon effect
that has actually survived out-of-sample testing, unlike the indicator crossovers
that make up the rest of this package.

Two design choices carry most of the weight:

* **Limit entries, not market.** At a 3% stop, crossing the spread costs about
  0.11 R per round trip; resting a bid roughly halves it. On a mean-reversion
  idea this matters twice over, because you *want* to be paid to provide
  liquidity into a fall rather than pay to demand it. The cost is that price must
  come back to you, and the misses are counted (see ``entry_valid_bars``).
* **It refuses to fade a trend.** "Oversold" inside a real downtrend is the
  downtrend working, not an opportunity. Hence the regime restriction, the
  trend-filter check, and the requirement that the fall be *stretched* relative
  to its own recent volatility rather than merely red.

A time stop matters more here than anywhere else: the reversal thesis has a
shelf life. If price has not reverted within a few bars, the premise was wrong
and holding turns a small planned loss into an unplanned trend trade.
"""

from __future__ import annotations

from app.models.enums import MarketRegime, OrderType, SignalDirection
from app.models.signals import StrategySignal
from app.strategies.base import (
    Strategy,
    StrategyContext,
    StrategyParams,
    clamp_confidence,
)


class ShortTermReversalParams(StrategyParams):
    # How stretched the move must be, in ATRs, before it is worth fading. Below
    # roughly 1.5 you are fading ordinary noise and paying costs to do it.
    min_stretch_atr: float = 1.8
    # Bars over which the stretch is measured. Short on purpose: this is a
    # reversal of a recent push, not a bet against a multi-week move.
    lookback_bars: int = 3
    min_down_streak: int = 2
    rsi_max: float = 38.0
    # Rest the bid this far below the close. Deeper means a better price and
    # fewer fills; the misses are real and are counted.
    limit_offset_atr: float = 0.25
    entry_valid_bars: int = 2
    atr_stop_multiple: float = 1.6
    reward_multiple: float = 1.8
    # The thesis expires. Reverted or not, this is not a position to hold.
    time_stop_bars: int = 12
    breakeven_at_r: float = 1.0
    # Refuse to fade anything below this: a collapsing market keeps collapsing.
    max_price_below_trend_pct: float = 0.12
    max_adx: float = 32.0


class ShortTermReversalStrategy(Strategy):
    name = "short_term_reversal"
    description = (
        "Fade a sharp multi-bar fall that is stretched against its own ATR, using a "
        "resting limit bid; exit on reversion to the short mean or a time stop."
    )
    allowed_regimes = frozenset(
        {
            MarketRegime.RANGE,
            MarketRegime.LOW_VOLATILITY,
            MarketRegime.HIGH_VOLATILITY,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.STRONG_BULL_TREND,
        }
    )
    required_features = (
        "atr",
        "rsi",
        "adx",
        "down_streak",
        "ema_fast",
        "ema_trend",
        "price_vs_ema_trend",
    )

    @classmethod
    def default_params(cls) -> ShortTermReversalParams:
        return ShortTermReversalParams()

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        params: ShortTermReversalParams = self.params  # type: ignore[assignment]
        row = context.row
        close = context.close

        atr = row.get("atr")
        if atr is None or atr <= 0:
            return self.hold(context, "ATR unavailable")

        # --- 1. is this a real trend? then do not fade it.
        adx = row.get("adx")
        if adx is not None and adx > params.max_adx:
            return self.hold(
                context, f"ADX {adx:.1f}: a trend is running, not a move to fade"
            )

        below_trend = row.get("price_vs_ema_trend")
        if below_trend is not None and below_trend < -params.max_price_below_trend_pct:
            return self.hold(
                context,
                f"price {below_trend * 100:.1f}% under its trend EMA — this is a "
                "breakdown, not an overshoot",
            )

        # --- 2. has price actually been pushed down?
        streak = row.get("down_streak") or 0.0
        if streak < params.min_down_streak:
            return self.hold(
                context, f"only {streak:.0f} down bars; no push worth fading"
            )

        closes = context.history("close", params.lookback_bars + 1)
        if len(closes) < params.lookback_bars + 1:
            return self.hold(context, "not enough history to measure the move")
        move = float(closes.iloc[-1]) - float(closes.iloc[0])
        stretch = abs(move) / atr
        if move >= 0:
            return self.hold(context, "no net fall over the lookback")
        if stretch < params.min_stretch_atr:
            return self.hold(
                context,
                f"fall of {stretch:.2f} ATR is inside normal noise "
                f"(needs {params.min_stretch_atr})",
            )

        # --- 3. momentum confirmation
        rsi = row.get("rsi")
        if rsi is None or rsi > params.rsi_max:
            return self.hold(context, f"RSI {rsi} not stretched enough to fade")

        # --- 4. levels. The bid rests BELOW the close: we are paid to provide
        # liquidity into the fall rather than paying to chase it.
        limit_price = close - params.limit_offset_atr * atr
        stop = limit_price - params.atr_stop_multiple * atr
        risk = limit_price - stop
        if risk <= 0:
            return self.hold(context, "invalid stop distance")

        # Target the short-term mean, floored at the minimum reward multiple.
        ema_fast = row.get("ema_fast")
        target = limit_price + params.reward_multiple * risk
        if ema_fast is not None and ema_fast > limit_price:
            target = max(ema_fast, target)

        return self.build_signal(
            context,
            direction=SignalDirection.BUY,
            entry=limit_price,
            stop_loss=stop,
            take_profit=target,
            confidence=self._confidence(stretch, rsi, params),
            reason=(
                f"Short-term reversal: {streak:.0f} down bars, {stretch:.2f} ATR fall, "
                f"RSI {rsi:.1f}, ADX {adx if adx is None else round(adx, 1)}. "
                f"Resting a bid {params.limit_offset_atr} ATR below."
            ),
            invalidation_condition=(
                f"close below {stop:.4f}, or no reversion within "
                f"{params.time_stop_bars} bars — the premise expires"
            ),
            entry_order_type=OrderType.LIMIT,
            entry_valid_bars=params.entry_valid_bars,
            breakeven_at_r=params.breakeven_at_r,
            time_stop_bars=params.time_stop_bars,
            features_used=("rsi", "adx", "atr", "down_streak", "price_vs_ema_trend"),
            metadata={"stretch_atr": round(stretch, 3), "limit_offset_atr": params.limit_offset_atr},
        )

    def _confidence(
        self, stretch: float, rsi: float, params: ShortTermReversalParams
    ) -> float:
        """More stretched and more oversold means more conviction -- to a point.

        Capped well below 1.0: this is a probabilistic fade, not a certainty, and
        an overconfident number here would let it through risk filters that exist
        to be sceptical.
        """
        stretch_score = min(1.0, (stretch - params.min_stretch_atr) / 2.0)
        rsi_score = min(1.0, (params.rsi_max - rsi) / 20.0)
        return clamp_confidence(0.55 + 0.15 * stretch_score + 0.15 * rsi_score)
