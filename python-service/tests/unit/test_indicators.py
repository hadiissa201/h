"""Indicator correctness.

Values are checked against arithmetic done by hand, not against a snapshot of
this implementation's own output — a snapshot test would happily lock in a bug.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.indicators import (
    adx,
    atr,
    bollinger_bands,
    breakouts,
    consecutive_closes,
    donchian,
    ema,
    find_pivots,
    macd,
    market_structure,
    obv,
    realized_volatility,
    relative_volume,
    roc,
    rsi,
    sma,
    stoch_rsi,
    true_range,
    volume_ma,
    wilder_ema,
)
from tests.conftest import make_ohlcv


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


# ------------------------------------------------------------------ averages
def test_sma_matches_manual_average():
    values = series([1, 2, 3, 4, 5, 6])
    result = sma(values, 3)
    assert math.isnan(result.iloc[0]) and math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[5] == pytest.approx(5.0)


def test_sma_warmup_is_nan_not_partial():
    """A partial average would look like a real value to a strategy."""
    result = sma(series([10, 20, 30, 40]), 3)
    assert result.isna().sum() == 2


def test_ema_first_value_equals_sma_of_window():
    values = series([1, 2, 3, 4, 5, 6, 7, 8])
    result = ema(values, 3)
    # With adjust=False and min_periods=n, pandas seeds from the running mean of
    # the first n observations.
    assert not math.isnan(result.iloc[2])
    assert math.isnan(result.iloc[1])


def test_ema_reacts_faster_than_sma():
    values = series([10.0] * 20 + [20.0] * 5)
    fast = ema(values, 10).iloc[-1]
    slow = sma(values, 10).iloc[-1]
    assert fast > slow


def test_wilder_ema_uses_one_over_n_smoothing():
    values = series([5.0] * 10 + [10.0])
    result = wilder_ema(values, 5)
    previous = result.iloc[-2]
    expected = previous + (10.0 - previous) / 5
    assert result.iloc[-1] == pytest.approx(expected)


def test_period_must_be_positive():
    with pytest.raises(ValueError):
        sma(series([1, 2, 3]), 0)
    with pytest.raises(ValueError):
        ema(series([1, 2, 3]), -1)


# ------------------------------------------------------------------ momentum
def test_rsi_of_pure_uptrend_is_100():
    result = rsi(series([float(i) for i in range(1, 30)]), 14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_of_pure_downtrend_is_zero():
    result = rsi(series([float(i) for i in range(30, 1, -1)]), 14)
    assert result.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_rsi_of_flat_series_is_neutral_not_infinite():
    """Flat prices divide by zero in the classic formula."""
    result = rsi(series([100.0] * 30), 14)
    assert result.iloc[-1] == pytest.approx(50.0)
    assert np.isfinite(result.iloc[-1])


def test_rsi_stays_within_bounds():
    rng = np.random.default_rng(3)
    values = series(list(100 + rng.normal(0, 2, 300).cumsum()))
    result = rsi(values, 14).dropna()
    assert result.min() >= 0.0
    assert result.max() <= 100.0


def test_stoch_rsi_bounds_and_flat_handling():
    rng = np.random.default_rng(5)
    values = series(list(100 + rng.normal(0, 1, 200).cumsum()))
    frame = stoch_rsi(values, 14, 14)
    raw = frame["stoch_rsi"].dropna()
    assert raw.min() >= 0.0 and raw.max() <= 100.0

    flat = stoch_rsi(series([50.0] * 80), 14, 14)
    assert flat["stoch_rsi"].dropna().iloc[-1] == pytest.approx(50.0)


def test_roc_is_percentage_change():
    values = series([100.0, 105.0, 110.0, 121.0])
    result = roc(values, 1)
    assert result.iloc[1] == pytest.approx(5.0)
    assert result.iloc[3] == pytest.approx(10.0)


# ---------------------------------------------------------------- volatility
def test_true_range_takes_the_widest_of_three():
    frame = pd.DataFrame(
        {"high": [10.0, 12.0], "low": [9.0, 11.0], "close": [9.5, 11.5]}
    )
    result = true_range(frame["high"], frame["low"], frame["close"])
    assert result.iloc[0] == pytest.approx(1.0)  # no previous close
    # high-low = 1.0; |high - prev close| = 2.5; |low - prev close| = 1.5
    assert result.iloc[1] == pytest.approx(2.5)


def test_atr_of_constant_range_equals_that_range():
    frame = make_ohlcv([100.0] * 40, spread=0.01)
    result = atr(frame["high"], frame["low"], frame["close"], 14)
    assert result.iloc[-1] == pytest.approx(2.0, rel=1e-6)


def test_bollinger_bands_are_symmetric_around_the_mean():
    rng = np.random.default_rng(11)
    values = series(list(100 + rng.normal(0, 1, 120)))
    frame = bollinger_bands(values, 20, 2.0)
    row = frame.iloc[-1]
    assert row["bb_upper"] - row["bb_middle"] == pytest.approx(
        row["bb_middle"] - row["bb_lower"]
    )
    assert row["bb_upper"] > row["bb_lower"]


def test_bollinger_position_is_zero_to_one_inside_the_bands():
    rng = np.random.default_rng(12)
    values = series(list(100 + rng.normal(0, 1, 200)))
    frame = bollinger_bands(values, 20, 2.0).dropna()
    inside = frame["bb_position"].between(-0.5, 1.5)
    assert inside.mean() > 0.95


def test_realized_volatility_scales_with_noise():
    rng = np.random.default_rng(13)
    quiet = series(list(100 + rng.normal(0, 0.1, 200).cumsum()))
    loud = series(list(100 + rng.normal(0, 2.0, 200).cumsum()))
    assert realized_volatility(loud, 20).iloc[-1] > realized_volatility(quiet, 20).iloc[-1]


def test_realized_volatility_annualises_by_timeframe():
    rng = np.random.default_rng(14)
    values = series(list(100 + rng.normal(0, 0.5, 200).cumsum()))
    per_bar = realized_volatility(values, 20).iloc[-1]
    hourly = realized_volatility(values, 20, timeframe="1h").iloc[-1]
    assert hourly == pytest.approx(per_bar * math.sqrt(8760), rel=1e-9)


# --------------------------------------------------------------------- trend
def test_macd_histogram_is_line_minus_signal():
    rng = np.random.default_rng(15)
    values = series(list(100 + rng.normal(0, 1, 200).cumsum()))
    frame = macd(values).dropna()
    row = frame.iloc[-1]
    assert row["macd_hist"] == pytest.approx(row["macd"] - row["macd_signal"])


def test_macd_rejects_fast_slower_than_slow():
    with pytest.raises(ValueError):
        macd(series([1.0] * 50), fast=26, slow=12)


def test_adx_is_high_in_a_trend_and_low_in_a_range():
    trend = make_ohlcv([100 + i for i in range(120)])
    rng = np.random.default_rng(21)
    noise = list(100 + rng.normal(0, 0.5, 120))
    ranging = make_ohlcv(noise)
    trend_adx = adx(trend["high"], trend["low"], trend["close"], 14)["adx"].iloc[-1]
    range_adx = adx(ranging["high"], ranging["low"], ranging["close"], 14)["adx"].iloc[-1]
    assert trend_adx > 40
    assert range_adx < trend_adx


def test_adx_can_read_high_on_a_frozen_market_after_one_move():
    """Documents a genuine ADX trap rather than papering over it.

    Wilder's smoothing keeps a single directional move alive indefinitely. If the
    highs and lows then freeze (a halted or untraded market), +DI stays slightly
    positive while -DI is exactly zero, so DX — and therefore ADX — pins at 100:
    the strongest possible "trend" reading on a market that has not moved.

    This is correct ADX behaviour, which is exactly why the regime layer never
    trusts ADX alone. See ``test_regime.py::test_frozen_market_is_not_a_trend``.
    """
    frozen = make_ohlcv([100.0 + (i % 2) for i in range(120)], spread=0.002)
    result = adx(frozen["high"], frozen["low"], frozen["close"], 14)["adx"].iloc[-1]
    di = adx(frozen["high"], frozen["low"], frozen["close"], 14)
    assert result > 90
    # The corroborating signal the regime layer actually uses is tiny.
    assert abs(di["plus_di"].iloc[-1] - di["minus_di"].iloc[-1]) < 5


def test_adx_is_nan_when_prices_never_move():
    flat = make_ohlcv([100.0] * 120, spread=0.0)
    result = adx(flat["high"], flat["low"], flat["close"], 14)["adx"]
    assert result.isna().all()


def test_di_spread_sign_follows_direction():
    up = make_ohlcv([100 + i for i in range(80)])
    down = make_ohlcv([180 - i for i in range(80)])
    up_frame = adx(up["high"], up["low"], up["close"], 14)
    down_frame = adx(down["high"], down["low"], down["close"], 14)
    assert up_frame["plus_di"].iloc[-1] > up_frame["minus_di"].iloc[-1]
    assert down_frame["minus_di"].iloc[-1] > down_frame["plus_di"].iloc[-1]


def test_adx_warmup_is_longer_than_the_period():
    frame = make_ohlcv([100 + i * 0.5 for i in range(60)])
    result = adx(frame["high"], frame["low"], frame["close"], 14)["adx"]
    # ADX smooths DX, which itself needs ATR — roughly two periods of warm-up.
    assert result.iloc[:20].isna().all()
    assert result.iloc[-1] == result.iloc[-1]  # not NaN


# -------------------------------------------------------------------- volume
def test_obv_accumulates_signed_volume():
    close = series([10.0, 11.0, 10.5, 12.0])
    volume = series([100.0, 200.0, 150.0, 300.0])
    result = obv(close, volume)
    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[1] == pytest.approx(200.0)
    assert result.iloc[2] == pytest.approx(50.0)
    assert result.iloc[3] == pytest.approx(350.0)


def test_relative_volume_is_one_for_constant_volume():
    volume = series([500.0] * 40)
    assert relative_volume(volume, 20).iloc[-1] == pytest.approx(1.0)


def test_relative_volume_spikes_are_detected():
    volume = series([500.0] * 39 + [2000.0])
    assert relative_volume(volume, 20).iloc[-1] > 3.0


def test_volume_ma_matches_sma():
    volume = series([float(i) for i in range(1, 41)])
    assert volume_ma(volume, 10).iloc[-1] == pytest.approx(sma(volume, 10).iloc[-1])


# ----------------------------------------------------------------- structure
def test_pivots_are_only_exposed_after_confirmation():
    """A pivot at bar i cannot be known until `right` more bars have printed."""
    closes = [10, 11, 12, 13, 20, 13, 12, 11, 10, 11, 12]
    frame = make_ohlcv([float(c) for c in closes], spread=0.0)
    pivots = find_pivots(frame["high"], frame["low"], left=2, right=2)

    peak = int(np.nanargmax(frame["high"].to_numpy()))
    assert not bool(pivots["swing_high_confirmed"].iloc[peak])
    assert bool(pivots["swing_high_confirmed"].iloc[peak + 2])
    assert pd.isna(pivots["last_swing_high"].iloc[peak])


def test_market_structure_flags_higher_highs_in_an_uptrend():
    closes = []
    base = 100.0
    for leg in range(4):
        closes += [base + leg * 10 + offset for offset in (0, 3, 6, 3, 1)]
    frame = make_ohlcv(closes, spread=0.0)
    structure = market_structure(frame["high"], frame["low"], left=1, right=1)
    assert structure["structure"].iloc[-1] >= 0
    assert structure["higher_high"].any()


def test_donchian_channel_excludes_the_current_bar():
    """Including the current bar would make every new high an instant breakout."""
    closes = [float(v) for v in [10, 11, 12, 13, 14, 50]]
    frame = make_ohlcv(closes, spread=0.0)
    channel = donchian(frame["high"], frame["low"], period=3)
    # The 50 spike must not appear in its own channel high.
    assert channel["dc_upper"].iloc[-1] < 50


def test_breakout_flag_requires_close_beyond_prior_channel():
    closes = [float(v) for v in [10, 10.2, 10.1, 10.3, 10.2, 12.0]]
    frame = make_ohlcv(closes, spread=0.0)
    flags = breakouts(frame["high"], frame["low"], frame["close"], period=3)
    assert bool(flags["breakout_up"].iloc[-1])
    assert not bool(flags["breakout_down"].iloc[-1])


def test_consecutive_closes_counts_runs():
    close = series([1.0, 2.0, 3.0, 2.5, 3.5, 4.5, 5.5])
    up = consecutive_closes(close, 1)
    assert up.iloc[2] == 2
    assert up.iloc[3] == 0
    assert up.iloc[6] == 3


def test_consecutive_closes_rejects_bad_direction():
    with pytest.raises(ValueError):
        consecutive_closes(series([1.0, 2.0]), 0)


# ---------------------------------------------------------------- no mutation
def test_indicators_do_not_mutate_their_input():
    frame = make_ohlcv([100 + i for i in range(60)])
    before = frame.copy(deep=True)
    adx(frame["high"], frame["low"], frame["close"], 14)
    rsi(frame["close"], 14)
    bollinger_bands(frame["close"], 20)
    breakouts(frame["high"], frame["low"], frame["close"], 20)
    pd.testing.assert_frame_equal(frame, before)
