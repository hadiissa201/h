"""Market-data validation and time handling.

Bad data is the cheapest way to lose money, so every one of these checks must
produce an *error* (no trade), not a warning that something downstream ignores.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.data.validation import validate_candles
from app.utils.time import (
    ensure_utc,
    floor_to_timeframe,
    staleness_seconds,
    start_of_utc_day,
    timeframe_to_seconds,
    utcnow,
)
from tests.conftest import make_ohlcv


def fresh_frame(bars: int = 200, freq: str = "1h") -> pd.DataFrame:
    """A clean frame whose newest candle is current."""
    end = floor_to_timeframe(utcnow(), freq) - timedelta(seconds=timeframe_to_seconds(freq))
    index = pd.date_range(end=end, periods=bars, freq=freq, tz="UTC")
    frame = make_ohlcv([100 + i * 0.1 for i in range(bars)])
    frame.index = index
    return frame


def codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def error_codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


# ------------------------------------------------------------------ the good
def test_clean_current_data_is_tradeable():
    report = validate_candles(fresh_frame(), "BTC/USDT", "1h", min_bars=120)
    assert report.is_tradeable
    assert not report.errors
    assert report.bars == 200
    assert report.summary() in ("ok", "ok_with_warnings")


# ------------------------------------------------------------------- the bad
def test_empty_data_is_rejected():
    report = validate_candles(pd.DataFrame(), "BTC/USDT", "1h")
    assert not report.is_tradeable
    assert "NO_DATA" in error_codes(report)


def test_missing_columns_are_rejected():
    frame = fresh_frame().drop(columns=["volume"])
    report = validate_candles(frame, "BTC/USDT", "1h")
    assert "MISSING_COLUMNS" in error_codes(report)


def test_too_few_bars_is_rejected():
    report = validate_candles(fresh_frame(bars=50), "BTC/USDT", "1h", min_bars=120)
    assert "INSUFFICIENT_BARS" in error_codes(report)


def test_duplicate_timestamps_are_rejected():
    frame = fresh_frame()
    frame = pd.concat([frame, frame.iloc[[-1]]])
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "DUPLICATE_TIMESTAMPS" in error_codes(report)


def test_unsorted_data_is_rejected():
    frame = fresh_frame().iloc[::-1]
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "UNSORTED" in error_codes(report)


def test_a_large_gap_is_an_error():
    frame = fresh_frame(bars=200)
    frame = pd.concat([frame.iloc[:50], frame.iloc[120:]])  # 70 bars missing
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=100, max_gap_ratio=0.02)
    assert "MISSING_CANDLES" in error_codes(report)


def test_a_single_missing_bar_is_only_a_warning():
    frame = fresh_frame(bars=200).drop(index=fresh_frame(bars=200).index[100])
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=100, max_gap_ratio=0.05)
    assert "MISSING_CANDLES" in codes(report)
    assert report.is_tradeable


def test_stale_data_is_rejected():
    frame = fresh_frame()
    frame.index = frame.index - timedelta(days=2)
    report = validate_candles(
        frame, "BTC/USDT", "1h", min_bars=10, max_staleness_seconds=300
    )
    assert "STALE_DATA" in error_codes(report)
    assert report.staleness_seconds > 300


def test_future_timestamps_are_rejected():
    frame = fresh_frame()
    frame.index = frame.index + timedelta(days=1)
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "FUTURE_TIMESTAMP" in error_codes(report)


def test_nan_values_are_rejected():
    frame = fresh_frame()
    frame.iloc[10, frame.columns.get_loc("close")] = np.nan
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "NAN_VALUES" in error_codes(report)


def test_non_positive_prices_are_rejected():
    frame = fresh_frame()
    frame.iloc[10, frame.columns.get_loc("low")] = 0.0
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "NON_POSITIVE_PRICE" in error_codes(report)


def test_high_below_low_is_rejected():
    frame = fresh_frame()
    position = frame.columns.get_loc("high")
    frame.iloc[10, position] = float(frame["low"].iloc[10]) - 1
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "INVALID_OHLC" in error_codes(report)


def test_close_outside_the_bar_range_is_rejected():
    frame = fresh_frame()
    frame.iloc[10, frame.columns.get_loc("close")] = float(frame["high"].iloc[10]) + 5
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "INVALID_OHLC" in error_codes(report)


def test_negative_volume_is_rejected():
    frame = fresh_frame()
    frame.iloc[10, frame.columns.get_loc("volume")] = -5.0
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "NEGATIVE_VOLUME" in error_codes(report)


def test_a_zero_volume_latest_bar_is_rejected():
    """No trades in the newest bar means the price is not real."""
    frame = fresh_frame()
    frame.iloc[-1, frame.columns.get_loc("volume")] = 0.0
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "LAST_BAR_NO_VOLUME" in error_codes(report)


def test_mostly_zero_volume_is_rejected():
    frame = fresh_frame()
    frame.iloc[:150, frame.columns.get_loc("volume")] = 0.0
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "ZERO_VOLUME_BARS" in error_codes(report)


def test_a_few_zero_volume_bars_are_only_a_warning():
    frame = fresh_frame()
    frame.iloc[10:14, frame.columns.get_loc("volume")] = 0.0
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10)
    assert "ZERO_VOLUME_BARS" in codes(report)
    assert report.is_tradeable


def test_an_abnormal_move_is_flagged_as_a_warning():
    """The regime layer decides what to do; validation only reports it."""
    frame = fresh_frame()
    position = frame.columns.get_loc("close")
    frame.iloc[-1, position] = float(frame["close"].iloc[-2]) * 1.30
    frame.iloc[-1, frame.columns.get_loc("high")] = float(frame["close"].iloc[-1]) * 1.01
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=10, abnormal_move_pct=0.10)
    assert "ABNORMAL_PRICE_MOVE" in codes(report)


def test_multiple_problems_are_all_reported():
    frame = fresh_frame(bars=30)
    frame.iloc[5, frame.columns.get_loc("volume")] = -1.0
    frame.index = frame.index - timedelta(days=3)
    report = validate_candles(frame, "BTC/USDT", "1h", min_bars=120)
    assert {"INSUFFICIENT_BARS", "STALE_DATA", "NEGATIVE_VOLUME"} <= error_codes(report)
    assert not report.is_tradeable
    assert len(report.summary()) > 20


# ------------------------------------------------------------------ the time
def test_timeframe_parsing():
    assert timeframe_to_seconds("5m") == 300
    assert timeframe_to_seconds("1h") == 3600
    assert timeframe_to_seconds("4h") == 14400
    assert timeframe_to_seconds("1d") == 86400
    with pytest.raises(ValueError):
        timeframe_to_seconds("1 hour")
    with pytest.raises(ValueError):
        timeframe_to_seconds("banana")


def test_a_just_closed_candle_is_not_stale():
    """At 10:55 on 1h data, the newest closed candle opened at 09:00."""
    now = ensure_utc(pd.Timestamp("2026-01-01T10:55:00Z").to_pydatetime())
    last_open = ensure_utc(pd.Timestamp("2026-01-01T09:00:00Z").to_pydatetime())
    assert staleness_seconds(last_open, "1h", now=now) == 0.0


def test_a_missed_candle_registers_as_stale():
    now = ensure_utc(pd.Timestamp("2026-01-01T12:30:00Z").to_pydatetime())
    last_open = ensure_utc(pd.Timestamp("2026-01-01T09:00:00Z").to_pydatetime())
    assert staleness_seconds(last_open, "1h", now=now) == pytest.approx(5400.0)


def test_floor_to_timeframe_snaps_down():
    moment = ensure_utc(pd.Timestamp("2026-01-01T10:37:12Z").to_pydatetime())
    assert floor_to_timeframe(moment, "1h").hour == 10
    assert floor_to_timeframe(moment, "1h").minute == 0
    assert floor_to_timeframe(moment, "15m").minute == 30


def test_naive_datetimes_are_treated_as_utc():
    naive = pd.Timestamp("2026-01-01T10:00:00").to_pydatetime()
    assert ensure_utc(naive).tzinfo is not None


def test_start_of_day_is_midnight_utc():
    start = start_of_utc_day()
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert start.tzinfo is not None
