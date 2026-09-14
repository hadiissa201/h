"""Momentum indicators: RSI, Stochastic RSI, ROC."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.trend import wilder_ema


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI in 0..100.

    A flat series has no losses, which makes the classic formula divide by
    zero; we clamp that case to 100 (or 0 for a pure downtrend) rather than
    emitting ``inf``, which would poison every downstream comparison.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    avg_gain = wilder_ema(gains, period)
    avg_loss = wilder_ema(losses, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, other=np.where(avg_gain > 0.0, 100.0, 50.0))
    return out.where(avg_gain.notna() & avg_loss.notna())


def stoch_rsi(
    series: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> pd.DataFrame:
    """Stochastic RSI in 0..100 with %K / %D smoothing."""
    base = rsi(series, rsi_period)
    lowest = base.rolling(stoch_period, min_periods=stoch_period).min()
    highest = base.rolling(stoch_period, min_periods=stoch_period).max()
    span = highest - lowest
    raw = ((base - lowest) / span.replace(0.0, np.nan)) * 100.0
    # A completely flat RSI window is "mid range", not undefined.
    raw = raw.where(span != 0.0, 50.0).where(base.notna() & lowest.notna())
    k = raw.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_smooth, min_periods=d_smooth).mean()
    return pd.DataFrame({"stoch_rsi": raw, "stoch_rsi_k": k, "stoch_rsi_d": d})


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    """Rate of change in percent over ``period`` bars."""
    if period < 1:
        raise ValueError("period must be >= 1")
    previous = series.shift(period)
    return ((series - previous) / previous.replace(0.0, np.nan)) * 100.0


def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    """Absolute price change over ``period`` bars."""
    return series.diff(period)
