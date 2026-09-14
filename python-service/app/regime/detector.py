"""Market-regime classification.

Deterministic and threshold-based on purpose: the regime decides which
strategies are even allowed to speak, so it must be reproducible, explainable
and testable. No model, no LLM, no hidden state.

Order of evaluation matters:

1. **Abnormal** — gap, volatility explosion, broken feed. Nothing trades.
2. **Volatility state** — where current volatility sits in its own history.
3. **Trend state** — ADX/DI, EMA stack, normalised slope, market structure.
4. **Composite regime** — the label strategies gate on.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.features.engine import FeatureSet
from app.models.enums import MarketRegime, TrendState, VolatilityState
from app.models.signals import RegimeAssessment, RegimeMetrics

logger = get_logger(__name__)


class RegimeConfig(BaseModel):
    """Thresholds. Exposed so walk-forward runs can perturb them."""

    model_config = {"frozen": True}

    adx_trend_min: float = Field(default=20.0)
    adx_strong_trend_min: float = Field(default=28.0)
    di_spread_min: float = Field(default=5.0)
    slope_min: float = Field(default=0.0005)
    # Kaufman efficiency floors. Below `efficiency_min_trend` the market has not
    # gone anywhere, whatever ADX says, so no trend label is allowed.
    efficiency_min_trend: float = Field(default=0.12)
    efficiency_min_strong: float = Field(default=0.25)
    vol_rank_high: float = Field(default=0.85)
    vol_rank_extreme: float = Field(default=0.97)
    vol_rank_low: float = Field(default=0.15)
    vol_ratio_abnormal: float = Field(default=3.0)
    abnormal_bar_move_pct: float = Field(default=0.10)
    abnormal_atr_pct: float = Field(default=0.12)
    trend_alignment_bonus: float = Field(default=0.1)


class RegimeDetector:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def classify(
        self,
        features: FeatureSet,
        *,
        index: int = -1,
        higher_timeframe: FeatureSet | None = None,
        data_quality_warnings: list[str] | None = None,
        timestamp: datetime | None = None,
    ) -> RegimeAssessment:
        row = features.row(index)
        cfg = self.config
        notes: list[str] = []
        abnormal_reasons: list[str] = []

        adx = row.get("adx")
        di_spread = row.get("di_spread")
        slope = row.get("trend_slope")
        atr_pct = row.get("atr_pct")
        atr_rank = row.get("atr_pct_rank")
        vol_ratio = row.get("vol_ratio")
        bb_width = row.get("bb_width")
        structure = row.get("structure")
        efficiency = row.get("efficiency_ratio")
        stack_bull = row.get("ema_stack_bull")
        stack_bear = row.get("ema_stack_bear")
        price_vs_trend = row.get("price_vs_ema_trend")

        candles = features.candles
        last_move: float | None = None
        position = index if index >= 0 else len(candles) + index
        if position > 0:
            previous_close = float(candles["close"].iloc[position - 1])
            if previous_close > 0:
                last_move = float(candles["close"].iloc[position]) / previous_close - 1.0

        metrics = RegimeMetrics(
            adx=adx,
            di_spread=di_spread,
            ema_alignment=(stack_bull or 0.0) - (stack_bear or 0.0),
            trend_slope=slope,
            efficiency_ratio=efficiency,
            atr_pct=atr_pct,
            atr_pct_rank=atr_rank,
            vol_ratio=vol_ratio,
            bb_width=bb_width,
            structure=structure,
            last_bar_move_pct=last_move,
        )

        # --- missing features => UNKNOWN, never a tradeable regime.
        # Every input the classifier reads must be present: classifying on a
        # half-warm feature row silently treats NaN as "neutral".
        required = (
            adx,
            di_spread,
            atr_pct,
            slope,
            efficiency,
            stack_bull,
            stack_bear,
            price_vs_trend,
            atr_rank,
        )
        if any(value is None for value in required):
            missing = features.missing_features(index)
            return RegimeAssessment(
                symbol=features.symbol,
                timeframe=features.timeframe,
                timestamp=timestamp or features.last_timestamp,
                regime=MarketRegime.UNKNOWN,
                trend_state=TrendState.FLAT,
                volatility_state=VolatilityState.NORMAL,
                confidence=0.0,
                is_abnormal=True,
                abnormal_reasons=[f"features not warmed up: {missing[:8]}"],
                metrics=metrics,
                notes=["insufficient history for a regime call"],
            )

        # --- 1. abnormal conditions
        if last_move is not None and abs(last_move) > cfg.abnormal_bar_move_pct:
            abnormal_reasons.append(
                f"last bar moved {last_move:.2%} (limit {cfg.abnormal_bar_move_pct:.2%})"
            )
        if atr_pct is not None and atr_pct > cfg.abnormal_atr_pct:
            abnormal_reasons.append(
                f"ATR {atr_pct:.2%} of price exceeds {cfg.abnormal_atr_pct:.2%}"
            )
        if vol_ratio is not None and vol_ratio > cfg.vol_ratio_abnormal:
            abnormal_reasons.append(
                f"short/long volatility ratio {vol_ratio:.2f} > {cfg.vol_ratio_abnormal}"
            )
        for warning in data_quality_warnings or []:
            abnormal_reasons.append(f"data warning: {warning}")

        # --- 2. volatility state
        volatility_state = VolatilityState.NORMAL
        if atr_rank is not None:
            if atr_rank >= cfg.vol_rank_extreme:
                volatility_state = VolatilityState.EXTREME
            elif atr_rank >= cfg.vol_rank_high:
                volatility_state = VolatilityState.HIGH
            elif atr_rank <= cfg.vol_rank_low:
                volatility_state = VolatilityState.LOW

        # --- 3. trend state
        trend_state, trend_strength = self._trend_state(
            adx=adx,
            di_spread=di_spread,
            slope=slope,
            efficiency=efficiency,
            stack_bull=bool(stack_bull),
            stack_bear=bool(stack_bear),
            structure=structure,
            price_vs_trend=price_vs_trend,
        )

        # --- higher timeframe agreement (confirmation only, never a veto)
        htf_alignment: float | None = None
        if higher_timeframe is not None and not higher_timeframe.frame.empty:
            htf_row = higher_timeframe.row(-1)
            htf_adx = htf_row.get("adx")
            htf_di = htf_row.get("di_spread")
            if htf_adx is not None and htf_di is not None:
                htf_direction = 1.0 if htf_di > 0 else -1.0
                own_direction = (
                    1.0
                    if trend_state in (TrendState.STRONG_UP, TrendState.WEAK_UP)
                    else -1.0
                    if trend_state in (TrendState.STRONG_DOWN, TrendState.WEAK_DOWN)
                    else 0.0
                )
                htf_alignment = htf_direction * own_direction
                notes.append(
                    f"higher timeframe {higher_timeframe.timeframe} "
                    f"{'agrees' if htf_alignment > 0 else 'disagrees' if htf_alignment < 0 else 'neutral'}"
                )

        # --- 4. composite regime
        if abnormal_reasons:
            regime = MarketRegime.ABNORMAL
        elif trend_state is TrendState.STRONG_UP:
            regime = MarketRegime.STRONG_BULL_TREND
        elif trend_state is TrendState.WEAK_UP:
            regime = MarketRegime.WEAK_BULL_TREND
        elif trend_state is TrendState.STRONG_DOWN:
            regime = MarketRegime.STRONG_BEAR_TREND
        elif trend_state is TrendState.WEAK_DOWN:
            regime = MarketRegime.WEAK_BEAR_TREND
        elif volatility_state in (VolatilityState.HIGH, VolatilityState.EXTREME):
            regime = MarketRegime.HIGH_VOLATILITY
        elif volatility_state is VolatilityState.LOW:
            regime = MarketRegime.LOW_VOLATILITY
        else:
            regime = MarketRegime.RANGE

        confidence = self._confidence(
            regime=regime,
            trend_strength=trend_strength,
            atr_rank=atr_rank,
            htf_alignment=htf_alignment,
        )

        assessment = RegimeAssessment(
            symbol=features.symbol,
            timeframe=features.timeframe,
            timestamp=timestamp or features.last_timestamp,
            regime=regime,
            trend_state=trend_state,
            volatility_state=volatility_state,
            confidence=confidence,
            is_abnormal=bool(abnormal_reasons),
            abnormal_reasons=abnormal_reasons,
            metrics=metrics,
            notes=notes,
        )
        log_event(
            logger,
            EventType.REGIME_CLASSIFIED,
            symbol=features.symbol,
            timeframe=features.timeframe,
            regime=str(regime),
            trend_state=str(trend_state),
            volatility_state=str(volatility_state),
            confidence=confidence,
            abnormal=assessment.is_abnormal,
            reason="; ".join(abnormal_reasons) or None,
        )
        return assessment

    # ------------------------------------------------------------- internals
    def _trend_state(
        self,
        *,
        adx: float,
        di_spread: float,
        slope: float,
        efficiency: float,
        stack_bull: bool,
        stack_bear: bool,
        structure: float | None,
        price_vs_trend: float | None,
    ) -> tuple[TrendState, float]:
        cfg = self.config

        # A market that has travelled a long way and ended where it started is a
        # range, however strong ADX looks. This veto comes first because ADX can
        # read 100 on an oscillation, and on a frozen market.
        if efficiency < cfg.efficiency_min_trend:
            return TrendState.FLAT, 0.0

        bullish_votes = sum(
            (
                di_spread > cfg.di_spread_min,
                slope > cfg.slope_min,
                stack_bull,
                (structure or 0.0) > 0,
                (price_vs_trend or 0.0) > 0,
            )
        )
        bearish_votes = sum(
            (
                di_spread < -cfg.di_spread_min,
                slope < -cfg.slope_min,
                stack_bear,
                (structure or 0.0) < 0,
                (price_vs_trend or 0.0) < 0,
            )
        )

        if adx < cfg.adx_trend_min or bullish_votes == bearish_votes:
            return TrendState.FLAT, 0.0

        direction_up = bullish_votes > bearish_votes
        votes = bullish_votes if direction_up else bearish_votes
        strong = (
            adx >= cfg.adx_strong_trend_min
            and votes >= 4
            and efficiency >= cfg.efficiency_min_strong
        )
        # 0..1 strength blending trend quality (ADX), breadth of agreement and
        # how directly the market actually travelled.
        strength = min(
            1.0, (adx / 50.0) * 0.4 + (votes / 5.0) * 0.4 + min(efficiency, 1.0) * 0.2
        )

        if direction_up:
            return (TrendState.STRONG_UP if strong else TrendState.WEAK_UP), strength
        return (TrendState.STRONG_DOWN if strong else TrendState.WEAK_DOWN), strength

    def _confidence(
        self,
        *,
        regime: MarketRegime,
        trend_strength: float,
        atr_rank: float | None,
        htf_alignment: float | None,
    ) -> float:
        if regime is MarketRegime.ABNORMAL:
            # High confidence that conditions are abnormal — not confidence to trade.
            return 0.9
        if regime in (
            MarketRegime.STRONG_BULL_TREND,
            MarketRegime.STRONG_BEAR_TREND,
            MarketRegime.WEAK_BULL_TREND,
            MarketRegime.WEAK_BEAR_TREND,
        ):
            confidence = 0.4 + 0.5 * trend_strength
        elif regime is MarketRegime.RANGE:
            centrality = 1.0 - abs((atr_rank if atr_rank is not None else 0.5) - 0.5) * 2
            confidence = 0.4 + 0.3 * max(0.0, centrality)
        else:
            confidence = 0.55
        if htf_alignment is not None:
            confidence += self.config.trend_alignment_bonus * htf_alignment
        return round(min(0.99, max(0.05, confidence)), 4)
