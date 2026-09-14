"""Volume indicators: volume MA, relative volume, OBV, VWAP, money-flow proxy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.trend import sma


def volume_ma(volume: pd.Series, period: int = 20) -> pd.Series:
    return sma(volume, period)


def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Current volume / average volume. 2.0 means twice the usual activity."""
    average = volume_ma(volume, period)
    return volume / average.replace(0.0, np.nan)


def volume_change(volume: pd.Series, period: int = 1) -> pd.Series:
    previous = volume.shift(period)
    return (volume - previous) / previous.replace(0.0, np.nan)


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative signed volume."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def obv_slope(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    """Normalised OBV trend — rising OBV confirms a price advance."""
    series = obv(close, volume)
    change = series.diff(period)
    scale = volume.rolling(period, min_periods=period).mean() * period
    return change / scale.replace(0.0, np.nan)


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Rolling VWAP (not session-anchored — crypto has no daily session)."""
    typical = (high + low + close) / 3.0
    numerator = (typical * volume).rolling(period, min_periods=period).sum()
    denominator = volume.rolling(period, min_periods=period).sum()
    return numerator / denominator.replace(0.0, np.nan)


def volume_price_confirmation(
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """+1 when price and volume agree, -1 when volume contradicts the move.

    Up-move on expanding volume is constructive; up-move on contracting volume
    is the classic exhaustion tell.
    """
    price_up = close.diff() > 0
    vol_expanding = relative_volume(volume, period) > 1.0
    agree = (price_up & vol_expanding) | (~price_up & ~vol_expanding)
    out = pd.Series(np.where(agree, 1.0, -1.0), index=close.index)
    return out.where(relative_volume(volume, period).notna())
