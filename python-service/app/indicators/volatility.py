"""Volatility indicators: ATR, Bollinger Bands, realized volatility, Keltner width."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.trend import ema, sma, true_range, wilder_ema

# Bars per year per timeframe, used to annualise realized volatility.
# Crypto trades 24/7/365, so there is no session-hours discount.
BARS_PER_YEAR = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1h": 8_760,
    "2h": 4_380,
    "4h": 2_190,
    "6h": 1_460,
    "12h": 730,
    "1d": 365,
}


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    return wilder_ema(true_range(high, low, close), period)


def atr_pct(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """ATR as a fraction of price — comparable across symbols."""
    return atr(high, low, close, period) / close.replace(0.0, np.nan)


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    middle = sma(series, period)
    # ddof=0: population std, matching the standard Bollinger definition.
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = (upper - lower) / middle.replace(0.0, np.nan)
    position = (series - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
            "bb_position": position,
        }
    )


def realized_volatility(
    close: pd.Series,
    period: int = 20,
    timeframe: str | None = None,
) -> pd.Series:
    """Standard deviation of log returns.

    When ``timeframe`` is known the result is annualised so that a 5m and a 4h
    reading are on the same scale; otherwise it is returned per-bar.
    """
    log_returns = np.log(close / close.shift(1))
    vol = log_returns.rolling(period, min_periods=period).std(ddof=1)
    if timeframe:
        factor = BARS_PER_YEAR.get(timeframe)
        if factor:
            vol = vol * np.sqrt(factor)
    return vol


def volatility_ratio(
    close: pd.Series,
    short_period: int = 10,
    long_period: int = 50,
) -> pd.Series:
    """Short-horizon vol / long-horizon vol.

    > 1 means volatility is expanding (breakout conditions), < 1 means it is
    contracting (squeeze / range conditions).
    """
    short = realized_volatility(close, short_period)
    long = realized_volatility(close, long_period)
    return short / long.replace(0.0, np.nan)


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> pd.DataFrame:
    middle = ema(close, period)
    band = atr(high, low, close, atr_period) * multiplier
    return pd.DataFrame(
        {
            "kc_middle": middle,
            "kc_upper": middle + band,
            "kc_lower": middle - band,
        }
    )


def squeeze(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    bb_period: int = 20,
    kc_period: int = 20,
) -> pd.Series:
    """True when Bollinger Bands sit inside Keltner Channels (volatility squeeze)."""
    bands = bollinger_bands(close, bb_period)
    channels = keltner_channels(high, low, close, kc_period)
    inside = (bands["bb_upper"] < channels["kc_upper"]) & (
        bands["bb_lower"] > channels["kc_lower"]
    )
    return inside.where(bands["bb_upper"].notna() & channels["kc_upper"].notna())
