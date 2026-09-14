"""Trend indicators: SMA, EMA, MACD, ADX.

All functions are pure: ``Series -> Series`` / ``DataFrame``, no mutation of the
input, and never use information from bar ``i+1`` when producing value ``i``
(see ``tests/unit/test_indicators_no_lookahead.py``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    _check_period(period)
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average with the conventional ``alpha = 2/(n+1)``.

    ``min_periods=period`` keeps the warm-up region as NaN instead of emitting
    an under-smoothed value that a strategy might act on.
    """
    _check_period(period)
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_ema(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (``alpha = 1/n``) — used by ATR, ADX and RSI."""
    _check_period(period)
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": macd_line - signal_line,
        }
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI, Wilder's original method."""
    _check_period(period)
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    atr_ = wilder_ema(true_range(high, low, close), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * wilder_ema(plus_dm, period) / atr_
        minus_di = 100.0 * wilder_ema(minus_dm, period) / atr_
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    dx = dx.replace([np.inf, -np.inf], np.nan)
    # Wilder smoothing of DX; pandas' ewm skips the warm-up NaNs so ADX only
    # becomes valid once `period` DX observations exist (~2*period bars in).
    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": wilder_ema(dx, period)}
    )


def slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Least-squares slope of the last ``period`` points, normalised by price.

    Normalising makes the value comparable across symbols with very different
    nominal prices (BTC at 60k vs SOL at 150).
    """
    _check_period(period)
    x = np.arange(period, dtype=float)
    x_mean = x.mean()
    denominator = ((x - x_mean) ** 2).sum()

    def _fit(window: np.ndarray) -> float:
        y_mean = window.mean()
        if y_mean == 0 or denominator == 0:
            return float("nan")
        beta = ((x - x_mean) * (window - y_mean)).sum() / denominator
        return float(beta / y_mean)

    return series.rolling(window=period, min_periods=period).apply(_fit, raw=True)


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
