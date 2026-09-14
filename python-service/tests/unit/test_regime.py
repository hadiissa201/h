"""Market-regime classification.

The regime decides which strategies may speak at all, so the important tests are
the refusals: abnormal markets, missing features, and readings that look strong
on one indicator but have no corroboration.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.features import FeatureEngine
from app.models.enums import MarketRegime, TrendState, VolatilityState
from app.regime import RegimeConfig, RegimeDetector
from tests.conftest import make_ohlcv


def build(closes: list[float], **kwargs):
    frame = make_ohlcv(closes, **kwargs)
    return FeatureEngine().compute(frame, "TEST/USDT", "1h")


@pytest.fixture(scope="module")
def trending_up():
    rng = np.random.default_rng(1)
    closes = list(100 + np.arange(400) * 0.5 + rng.normal(0, 0.4, 400))
    return build(closes)


@pytest.fixture(scope="module")
def trending_down():
    rng = np.random.default_rng(2)
    closes = list(300 - np.arange(400) * 0.5 + rng.normal(0, 0.4, 400))
    return build(closes)


@pytest.fixture(scope="module")
def ranging():
    rng = np.random.default_rng(3)
    closes = list(100 + np.sin(np.arange(400) / 12) * 3 + rng.normal(0, 0.3, 400))
    return build(closes)


def test_uptrend_is_classified_as_a_bull_trend(trending_up, regime_detector):
    assessment = regime_detector.classify(trending_up)
    assert assessment.regime in (
        MarketRegime.STRONG_BULL_TREND,
        MarketRegime.WEAK_BULL_TREND,
    )
    assert assessment.trend_state in (TrendState.STRONG_UP, TrendState.WEAK_UP)
    assert assessment.confidence > 0.4


def test_downtrend_is_classified_as_a_bear_trend(trending_down, regime_detector):
    assessment = regime_detector.classify(trending_down)
    assert assessment.regime in (
        MarketRegime.STRONG_BEAR_TREND,
        MarketRegime.WEAK_BEAR_TREND,
    )
    assert assessment.trend_state in (TrendState.STRONG_DOWN, TrendState.WEAK_DOWN)


TREND_REGIMES = {
    MarketRegime.STRONG_BULL_TREND,
    MarketRegime.WEAK_BULL_TREND,
    MarketRegime.STRONG_BEAR_TREND,
    MarketRegime.WEAK_BEAR_TREND,
}


def trend_share(features, detector) -> float:
    """Fraction of warmed-up bars classified as any kind of trend."""
    labels = [
        detector.classify(features, index=index).regime
        for index in range(features.warmup_bars, len(features.candles))
    ]
    return sum(label in TREND_REGIMES for label in labels) / len(labels)


def test_an_oscillating_market_is_mostly_classified_as_a_range(
    ranging, trending_up, regime_detector
):
    """Judged over the whole series, not one bar.

    Any single leg of an oscillation is locally directional — that is what makes
    ranges dangerous for trend-following — so the meaningful question is what the
    classifier says *most of the time*.
    """
    oscillating_share = trend_share(ranging, regime_detector)
    trending_share = trend_share(trending_up, regime_detector)
    assert oscillating_share < 0.5, (
        f"{oscillating_share:.0%} of an oscillating market was called a trend"
    )
    assert trending_share > 0.8, (
        f"only {trending_share:.0%} of a clean trend was called a trend"
    )
    assert trending_share > oscillating_share * 2


def test_frozen_market_is_not_a_trend(regime_detector):
    """ADX pins at 100 on a frozen market (see test_indicators). The regime layer
    must still refuse to call it a trend, because nothing corroborates it."""
    frozen = build([100.0 + (i % 2) for i in range(400)], spread=0.002)
    assessment = regime_detector.classify(frozen)
    assert assessment.metrics.adx is None or assessment.metrics.adx > 90
    assert assessment.trend_state is TrendState.FLAT
    assert assessment.regime not in (
        MarketRegime.STRONG_BULL_TREND,
        MarketRegime.WEAK_BULL_TREND,
        MarketRegime.STRONG_BEAR_TREND,
        MarketRegime.WEAK_BEAR_TREND,
    )


def test_a_violent_gap_is_abnormal_and_untradeable(regime_detector):
    rng = np.random.default_rng(4)
    closes = list(100 + rng.normal(0, 0.3, 399))
    closes.append(closes[-1] * 1.35)  # 35% candle
    assessment = regime_detector.classify(build(closes))
    assert assessment.is_abnormal
    assert assessment.regime is MarketRegime.ABNORMAL
    assert not assessment.tradeable
    assert any("moved" in reason for reason in assessment.abnormal_reasons)


def test_unwarmed_features_give_unknown_not_a_guess(regime_detector):
    short = build([100 + i * 0.1 for i in range(80)])
    assessment = regime_detector.classify(short)
    assert assessment.regime is MarketRegime.UNKNOWN
    assert assessment.is_abnormal  # unknown is treated as untradeable
    assert assessment.confidence == 0.0


def test_data_quality_warnings_make_the_regime_abnormal(trending_up, regime_detector):
    assessment = regime_detector.classify(
        trending_up, data_quality_warnings=["MISSING_CANDLES"]
    )
    assert assessment.is_abnormal
    assert any("MISSING_CANDLES" in reason for reason in assessment.abnormal_reasons)


def test_volatility_state_reflects_the_percentile_rank(regime_detector):
    """The rank is relative to recent history, so a shock reads HIGH when it
    arrives — and stops reading HIGH once it has been the norm for a while."""
    rng = np.random.default_rng(5)
    quiet = list(100 + rng.normal(0, 0.05, 300))
    loud = list(100 + rng.normal(0, 3.0, 100).cumsum())
    features = build(quiet + loud)

    at_shock = regime_detector.classify(features, index=305)
    assert at_shock.volatility_state in (VolatilityState.HIGH, VolatilityState.EXTREME)

    much_later = regime_detector.classify(features, index=399)
    assert much_later.metrics.atr_pct > at_shock.metrics.atr_pct * 0.2


def test_higher_timeframe_agreement_raises_confidence(trending_up, regime_detector):
    alone = regime_detector.classify(trending_up)
    with_htf = regime_detector.classify(trending_up, higher_timeframe=trending_up)
    assert with_htf.confidence > alone.confidence
    assert any("agrees" in note for note in with_htf.notes)


def test_metrics_are_recorded_for_audit(trending_up, regime_detector):
    metrics = regime_detector.classify(trending_up).metrics
    assert metrics.adx is not None
    assert metrics.atr_pct is not None
    assert metrics.trend_slope is not None
    assert metrics.last_bar_move_pct is not None


def test_thresholds_are_configurable(trending_up):
    strict = RegimeDetector(RegimeConfig(adx_trend_min=101.0, adx_strong_trend_min=101.0))
    assessment = strict.classify(trending_up)
    assert assessment.trend_state is TrendState.FLAT


def test_classification_is_deterministic(trending_up, regime_detector):
    first = regime_detector.classify(trending_up)
    second = regime_detector.classify(trending_up)
    assert first.regime == second.regime
    assert first.confidence == second.confidence


def test_historical_index_classification_matches_a_truncated_run(trending_up, regime_detector):
    """Classifying bar i must not depend on bars after i."""
    index = 300
    live = RegimeDetector().classify(
        FeatureEngine().compute(
            trending_up.candles.iloc[: index + 1], "TEST/USDT", "1h"
        )
    )
    historical = regime_detector.classify(trending_up, index=index)
    assert historical.regime == live.regime
    assert historical.confidence == pytest.approx(live.confidence, abs=1e-9)
