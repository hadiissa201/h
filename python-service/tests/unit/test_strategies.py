"""Strategy framework and the five strategies.

Two things are tested for every strategy: that it *can* fire when its conditions
are met, and that it refuses in the situations it is supposed to refuse. A
strategy that never fires is as broken as one that always does — both are easy
to ship without noticing.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import numpy as np
import pytest

from app.features import FeatureEngine
from app.models.enums import MarketRegime, SignalDirection
from app.models.signals import StrategySignal
from app.regime import RegimeDetector
from app.strategies import (
    STRATEGY_REGISTRY,
    BreakoutStrategy,
    EngineConfig,
    MeanReversionStrategy,
    StrategyEngine,
    TrendFollowingStrategy,
    build_strategies,
    strategy_catalogue,
)
from app.strategies.base import StrategyContext, target_from_r


@pytest.fixture(scope="module")
def synthetic_run():
    """A long synthetic series covering trends, ranges and volatility shocks."""
    from app.data.providers.synthetic import SyntheticMarketDataProvider

    provider = SyntheticMarketDataProvider(seed=7)
    candles = provider.generate("BTC/USDT", "1h", 3000)
    features = FeatureEngine().compute(candles, "BTC/USDT", "1h")
    detector = RegimeDetector()
    engine = StrategyEngine(build_strategies(), EngineConfig(allow_short=False))

    fired: Counter[str] = Counter()
    regimes: Counter[str] = Counter()
    candidates = 0
    for index in range(features.warmup_bars, len(candles)):
        assessment = detector.classify(features, index=index)
        regimes[str(assessment.regime)] += 1
        output = engine.evaluate(features, assessment, index=index)
        for signal in output.signals:
            fired[signal.strategy] += 1
        if output.candidate is not None:
            candidates += 1
    return {
        "fired": fired,
        "regimes": regimes,
        "candidates": candidates,
        "bars": len(candles) - features.warmup_bars,
        "features": features,
        "engine": engine,
        "detector": detector,
    }


# ------------------------------------------------------------------ catalogue
def test_all_five_strategies_are_registered():
    assert set(STRATEGY_REGISTRY) == {
        "trend_following",
        "ema_momentum",
        "breakout",
        "mean_reversion",
        "volatility_breakout",
    }


def test_catalogue_exposes_regimes_and_parameters():
    catalogue = {entry["name"]: entry for entry in strategy_catalogue()}
    for name, entry in catalogue.items():
        assert entry["allowed_regimes"], f"{name} declares no regimes"
        assert entry["required_features"], f"{name} declares no required features"
        assert entry["default_parameters"], f"{name} has no parameters"
        assert entry["description"]


def test_unknown_strategy_name_is_rejected():
    with pytest.raises(ValueError, match="unknown strategies"):
        build_strategies(["does_not_exist"])


def test_parameters_can_be_overridden():
    strategies = build_strategies(["trend_following"], {"trend_following": {"adx_min": 40.0}})
    assert strategies[0].params.adx_min == 40.0


def test_parameters_are_immutable():
    strategy = TrendFollowingStrategy()
    with pytest.raises(Exception):
        strategy.params.adx_min = 5.0


# ------------------------------------------------------------------- firing
def test_every_strategy_fires_at_least_once_over_a_long_run(synthetic_run):
    """A strategy that can never fire is dead code pretending to be a system."""
    never_fired = set(STRATEGY_REGISTRY) - set(synthetic_run["fired"])
    assert not never_fired, (
        f"these strategies never produced a signal in "
        f"{synthetic_run['bars']} bars: {sorted(never_fired)}"
    )


def test_strategies_do_not_fire_on_every_bar(synthetic_run):
    """Signals on most bars would mean the filters are not filtering."""
    for name, count in synthetic_run["fired"].items():
        ratio = count / synthetic_run["bars"]
        assert ratio < 0.25, f"{name} fired on {ratio:.0%} of bars"


def test_candidates_are_a_small_fraction_of_bars(synthetic_run):
    ratio = synthetic_run["candidates"] / synthetic_run["bars"]
    assert 0 < ratio < 0.25


def test_the_run_covers_multiple_regimes(synthetic_run):
    assert len(synthetic_run["regimes"]) >= 4


# ------------------------------------------------------------ regime gating
def test_mean_reversion_is_barred_from_trending_regimes():
    strategy = MeanReversionStrategy()
    assert not strategy.allows_regime(MarketRegime.STRONG_BULL_TREND)
    assert not strategy.allows_regime(MarketRegime.STRONG_BEAR_TREND)
    assert strategy.allows_regime(MarketRegime.RANGE)


def test_trend_following_is_barred_from_ranges():
    strategy = TrendFollowingStrategy()
    assert not strategy.allows_regime(MarketRegime.RANGE)
    assert strategy.allows_regime(MarketRegime.STRONG_BULL_TREND)


def test_no_strategy_is_allowed_in_an_abnormal_market():
    for cls in STRATEGY_REGISTRY.values():
        assert not cls().allows_regime(MarketRegime.ABNORMAL)
        assert not cls().allows_regime(MarketRegime.UNKNOWN)


def test_engine_skips_every_strategy_in_an_abnormal_market(synthetic_run):
    features = synthetic_run["features"]
    assessment = synthetic_run["detector"].classify(features)
    abnormal = assessment.model_copy(
        update={"is_abnormal": True, "abnormal_reasons": ["test"]}
    )
    output = synthetic_run["engine"].evaluate(features, abnormal)
    assert output.signals == []
    assert output.candidate is None
    assert "abnormal" in output.skip_reason


# ------------------------------------------------------------- spot-only
def test_engine_suppresses_short_signals_in_spot_mode(synthetic_run):
    features = synthetic_run["features"]
    detector = synthetic_run["detector"]
    engine = StrategyEngine(build_strategies(), EngineConfig(allow_short=False))
    for index in range(features.warmup_bars, len(features.candles)):
        assessment = detector.classify(features, index=index)
        output = engine.evaluate(features, assessment, index=index)
        for signal in output.signals:
            assert signal.signal is not SignalDirection.SELL


def test_short_signals_appear_when_shorting_is_enabled(synthetic_run):
    """Proves the suppression above is a policy, not an absence of signals."""
    features = synthetic_run["features"]
    detector = synthetic_run["detector"]
    engine = StrategyEngine(build_strategies(), EngineConfig(allow_short=True))
    shorts = 0
    for index in range(features.warmup_bars, len(features.candles)):
        assessment = detector.classify(features, index=index)
        output = engine.evaluate(features, assessment, index=index)
        shorts += sum(
            1 for signal in output.signals if signal.signal is SignalDirection.SELL
        )
    assert shorts > 0


# ------------------------------------------------------------ signal shape
def test_actionable_signals_always_carry_a_stop(synthetic_run):
    features = synthetic_run["features"]
    detector = synthetic_run["detector"]
    engine = synthetic_run["engine"]
    checked = 0
    for index in range(features.warmup_bars, len(features.candles)):
        assessment = detector.classify(features, index=index)
        for signal in engine.evaluate(features, assessment, index=index).signals:
            checked += 1
            assert signal.entry and signal.entry > 0
            assert signal.stop_loss and signal.stop_loss > 0
            assert signal.stop_loss < signal.entry  # long only here
            assert signal.take_profit is None or signal.take_profit > signal.entry
            assert 0 <= signal.confidence <= 0.95
            assert signal.reason
            assert signal.invalidation_condition
    assert checked > 0


def test_a_long_signal_with_a_stop_above_entry_is_impossible():
    from datetime import UTC, datetime

    with pytest.raises(ValueError, match="stop_loss must be below entry"):
        StrategySignal(
            strategy="x",
            symbol="BTC/USDT",
            timeframe="1h",
            timestamp=datetime.now(UTC),
            signal=SignalDirection.BUY,
            confidence=0.8,
            entry=Decimal("100"),
            stop_loss=Decimal("105"),
        )


def test_hold_signals_need_no_levels():
    from datetime import UTC, datetime

    signal = StrategySignal(
        strategy="x",
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=datetime.now(UTC),
        signal=SignalDirection.HOLD,
        confidence=0.0,
    )
    assert not signal.is_actionable


def test_target_from_r_is_symmetric():
    assert target_from_r(100, 96, 2.0, SignalDirection.BUY) == 108
    assert target_from_r(100, 104, 2.0, SignalDirection.SELL) == 92


# ---------------------------------------------------------- missing features
def test_strategy_is_skipped_when_a_required_feature_is_missing(synthetic_run):
    features = synthetic_run["features"]
    detector = synthetic_run["detector"]
    assessment = detector.classify(features)

    row = features.row(-1)
    row["adx"] = None
    context = StrategyContext(
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=features.last_timestamp,
        row=row,
        previous_row=features.row(-2),
        candles=features.candles,
        features=features.frame,
        index=len(features.candles) - 1,
        regime=assessment,
    )
    assert "adx" in TrendFollowingStrategy().missing_features(context)


# ------------------------------------------------------------- aggregation
def test_conflicting_signals_produce_no_candidate(synthetic_run):
    """Opposed views on the same bar mean stand aside, not pick a winner."""
    from datetime import UTC, datetime

    features = synthetic_run["features"]
    assessment = synthetic_run["detector"].classify(features)
    engine = StrategyEngine(build_strategies(), EngineConfig(allow_short=True))

    now = datetime.now(UTC)
    buy = StrategySignal(
        strategy="a", symbol="BTC/USDT", timeframe="1h", timestamp=now,
        signal=SignalDirection.BUY, confidence=0.70,
        entry=Decimal("100"), stop_loss=Decimal("96"), take_profit=Decimal("110"),
    )
    sell = StrategySignal(
        strategy="b", symbol="BTC/USDT", timeframe="1h", timestamp=now,
        signal=SignalDirection.SELL, confidence=0.68,
        entry=Decimal("100"), stop_loss=Decimal("104"), take_profit=Decimal("90"),
    )
    candidate, reason = engine._aggregate(features, assessment, [buy, sell])
    assert candidate is None
    assert "conflicting" in reason


def test_alignment_raises_confidence_but_keeps_the_primary_plan(synthetic_run):
    from datetime import UTC, datetime

    features = synthetic_run["features"]
    assessment = synthetic_run["detector"].classify(features)
    engine = synthetic_run["engine"]
    now = datetime.now(UTC)

    primary = StrategySignal(
        strategy="a", symbol="BTC/USDT", timeframe="1h", timestamp=now,
        signal=SignalDirection.BUY, confidence=0.70,
        entry=Decimal("100"), stop_loss=Decimal("96"), take_profit=Decimal("112"),
    )
    secondary = StrategySignal(
        strategy="b", symbol="BTC/USDT", timeframe="1h", timestamp=now,
        signal=SignalDirection.BUY, confidence=0.60,
        entry=Decimal("100"), stop_loss=Decimal("90"), take_profit=Decimal("130"),
    )
    candidate, _ = engine._aggregate(features, assessment, [primary, secondary])
    assert candidate is not None
    assert candidate.confidence > primary.confidence
    # Levels come from the highest-confidence strategy, never a blend.
    assert candidate.stop_loss == primary.stop_loss
    assert candidate.take_profit == primary.take_profit
    assert candidate.aligned_strategies == ["a", "b"]


def test_low_confidence_candidates_are_dropped(synthetic_run):
    from datetime import UTC, datetime

    features = synthetic_run["features"]
    assessment = synthetic_run["detector"].classify(features)
    engine = synthetic_run["engine"]
    weak = StrategySignal(
        strategy="a", symbol="BTC/USDT", timeframe="1h", timestamp=datetime.now(UTC),
        signal=SignalDirection.BUY, confidence=0.30,
        entry=Decimal("100"), stop_loss=Decimal("96"), take_profit=Decimal("112"),
    )
    candidate, reason = engine._aggregate(features, assessment, [weak])
    assert candidate is None
    assert "confidence" in reason


def test_breakout_needs_volume_confirmation():
    params = BreakoutStrategy().params
    assert params.min_relative_volume > 1.0, (
        "a breakout without participation is the one that fails"
    )
