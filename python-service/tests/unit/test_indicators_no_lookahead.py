"""Look-ahead protection.

The property under test: **a value computed at bar i must not change when later
bars are added.** If it does, the indicator is reading the future, every backtest
built on it is fiction, and the live system will behave differently from the
simulation that justified it.

This is checked by computing each indicator on a truncated series and on the full
series, then comparing the overlapping region exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.features import FeatureEngine
from app.indicators import (
    adx,
    atr,
    bollinger_bands,
    breakouts,
    donchian,
    ema,
    find_pivots,
    macd,
    market_structure,
    obv,
    obv_slope,
    realized_volatility,
    relative_volume,
    roc,
    rsi,
    slope,
    sma,
    squeeze,
    stoch_rsi,
    volatility_ratio,
)
from tests.conftest import make_ohlcv

CUT = 200
TOTAL = 320


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = list(100 + rng.normal(0, 1, TOTAL).cumsum())
    built = make_ohlcv(closes)
    built["volume"] = rng.lognormal(mean=6.0, sigma=0.4, size=TOTAL)
    return built


def assert_prefix_stable(full: pd.Series | pd.DataFrame, truncated: pd.Series | pd.DataFrame, name: str) -> None:
    """The truncated result must equal the head of the full result, exactly."""
    overlap = truncated.index
    if isinstance(full, pd.DataFrame):
        for column in full.columns:
            left = full.loc[overlap, column]
            right = truncated[column]
            _compare(left, right, f"{name}.{column}")
    else:
        _compare(full.loc[overlap], truncated, name)


def _compare(left: pd.Series, right: pd.Series, name: str) -> None:
    both_nan = left.isna() & right.isna()
    mismatch = ~both_nan & ~np.isclose(
        left.astype(float), right.astype(float), rtol=1e-12, atol=1e-12, equal_nan=True
    )
    if mismatch.any():
        index = mismatch[mismatch].index[0]
        raise AssertionError(
            f"{name} LOOKS AHEAD: value at {index} changed from "
            f"{right.loc[index]} to {left.loc[index]} once later bars arrived"
        )


INDICATORS = {
    "sma": lambda f: sma(f["close"], 20),
    "ema": lambda f: ema(f["close"], 21),
    "rsi": lambda f: rsi(f["close"], 14),
    "roc": lambda f: roc(f["close"], 10),
    "macd": lambda f: macd(f["close"]),
    "adx": lambda f: adx(f["high"], f["low"], f["close"], 14),
    "atr": lambda f: atr(f["high"], f["low"], f["close"], 14),
    "bollinger": lambda f: bollinger_bands(f["close"], 20),
    "stoch_rsi": lambda f: stoch_rsi(f["close"], 14, 14),
    "realized_vol": lambda f: realized_volatility(f["close"], 20),
    "vol_ratio": lambda f: volatility_ratio(f["close"], 10, 50),
    "squeeze": lambda f: squeeze(f["high"], f["low"], f["close"], 20, 20),
    "slope": lambda f: slope(f["close"], 20),
    "obv": lambda f: obv(f["close"], f["volume"]),
    "obv_slope": lambda f: obv_slope(f["close"], f["volume"], 20),
    "relative_volume": lambda f: relative_volume(f["volume"], 20),
    "donchian": lambda f: donchian(f["high"], f["low"], 20),
    "breakouts": lambda f: breakouts(f["high"], f["low"], f["close"], 20),
}


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_indicator_does_not_look_ahead(frame: pd.DataFrame, name: str) -> None:
    compute = INDICATORS[name]
    full = compute(frame)
    truncated = compute(frame.iloc[:CUT])
    assert_prefix_stable(full, truncated, name)


def test_confirmed_swing_features_do_not_look_ahead(frame: pd.DataFrame) -> None:
    """Only the *confirmed* pivot columns are safe; the raw ones are not."""
    full = find_pivots(frame["high"], frame["low"], 3, 3)
    truncated = find_pivots(frame["high"].iloc[:CUT], frame["low"].iloc[:CUT], 3, 3)
    for column in (
        "swing_high_confirmed",
        "swing_low_confirmed",
        "last_swing_high",
        "last_swing_low",
    ):
        left = full.loc[truncated.index, column].astype(float)
        right = truncated[column].astype(float)
        _compare(left, right, f"find_pivots.{column}")


def test_raw_pivot_columns_are_retrospective_by_design() -> None:
    """``pivot_high`` is for charting, and its last bars genuinely get revised.

    This test exists so nobody 'fixes' a strategy by reaching for the raw column:
    it is documented as unusable in real time, and here is the proof. A peak at
    the edge of the window is invisible until the bars to its right exist.
    """
    peak_index = 40
    closes = [100.0 + i for i in range(peak_index + 1)]  # rises to the peak
    closes += [closes[-1] - (i + 1) for i in range(6)]  # then falls away
    built = make_ohlcv(closes, spread=0.0)

    visible_now = find_pivots(
        built["high"].iloc[: peak_index + 1], built["low"].iloc[: peak_index + 1], 3, 3
    )
    visible_later = find_pivots(built["high"], built["low"], 3, 3)

    at_peak = built.index[peak_index]
    assert pd.isna(visible_now["pivot_high"].loc[at_peak]), (
        "a pivot cannot be visible on the bar it forms"
    )
    assert not pd.isna(visible_later["pivot_high"].loc[at_peak]), (
        "the pivot should appear once the bars to its right exist"
    )
    # The confirmed column — the one strategies use — is False at the peak and
    # only becomes True `right` bars later.
    assert not bool(visible_later["swing_high_confirmed"].loc[at_peak])
    assert bool(visible_later["swing_high_confirmed"].iloc[peak_index + 3])


def test_market_structure_does_not_look_ahead(frame: pd.DataFrame) -> None:
    full = market_structure(frame["high"], frame["low"], 3, 3)
    truncated = market_structure(frame["high"].iloc[:CUT], frame["low"].iloc[:CUT], 3, 3)
    for column in ("structure", "higher_high", "higher_low", "lower_high", "lower_low"):
        _compare(
            full.loc[truncated.index, column].astype(float),
            truncated[column].astype(float),
            f"market_structure.{column}",
        )


def test_full_feature_frame_does_not_look_ahead(frame: pd.DataFrame) -> None:
    """The end-to-end guarantee: every feature the strategies consume is causal."""
    engine = FeatureEngine()
    full = engine.compute(frame, "TEST/USDT", "1h")
    truncated = engine.compute(frame.iloc[:CUT], "TEST/USDT", "1h")

    offenders: list[str] = []
    for column in truncated.frame.columns:
        left = full.frame.loc[truncated.frame.index, column].astype(float)
        right = truncated.frame[column].astype(float)
        both_nan = left.isna() & right.isna()
        mismatch = ~both_nan & ~np.isclose(
            left, right, rtol=1e-10, atol=1e-10, equal_nan=True
        )
        if mismatch.any():
            offenders.append(column)
    assert not offenders, f"features that look ahead: {offenders}"


def test_feature_row_reflects_only_bars_up_to_index(frame: pd.DataFrame) -> None:
    """``row(i)`` must match what would have been known at bar i."""
    engine = FeatureEngine()
    full = engine.compute(frame, "TEST/USDT", "1h")
    index = 250
    as_of = engine.compute(frame.iloc[: index + 1], "TEST/USDT", "1h")

    historical = full.row(index)
    live = as_of.row(-1)
    for key, value in live.items():
        other = historical.get(key)
        if value is None or other is None:
            assert value == other, f"{key}: {other} vs {value}"
        else:
            assert value == pytest.approx(other, rel=1e-10, abs=1e-10), key
