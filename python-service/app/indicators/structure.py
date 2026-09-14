"""Price-structure features: swings, market structure, Donchian breakouts.

**Look-ahead discipline.** A fractal pivot at bar ``j`` can only be *confirmed*
once ``k`` bars have printed to its right. Every series returned here is
therefore shifted so that the value at index ``t`` uses nothing newer than
``t``. ``swing_high_confirmed[t]`` means "bar ``t-k`` turned out to be a pivot,
and we learned that now".
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def find_pivots(
    high: pd.Series,
    low: pd.Series,
    left: int = 3,
    right: int = 3,
) -> pd.DataFrame:
    """Detect fractal pivots and expose them only when confirmable.

    Returns columns:
      ``pivot_high`` / ``pivot_low``  - raw (retrospective) pivot prices, NaN
        elsewhere. Useful for charting/backtest reporting, **not** for signals.
      ``swing_high_confirmed`` / ``swing_low_confirmed`` - booleans available in
        real time.
      ``last_swing_high`` / ``last_swing_low`` - most recent confirmed pivot
        price as known at each bar.
    """
    if left < 1 or right < 1:
        raise ValueError("left/right must be >= 1")
    window = left + right + 1

    rolling_max = high.rolling(window, min_periods=window).max().shift(-right)
    rolling_min = low.rolling(window, min_periods=window).min().shift(-right)

    is_pivot_high = high.eq(rolling_max) & rolling_max.notna()
    is_pivot_low = low.eq(rolling_min) & rolling_min.notna()

    pivot_high = high.where(is_pivot_high)
    pivot_low = low.where(is_pivot_low)

    confirmed_high = is_pivot_high.shift(right, fill_value=False).astype(bool)
    confirmed_low = is_pivot_low.shift(right, fill_value=False).astype(bool)

    last_high = pivot_high.shift(right).ffill()
    last_low = pivot_low.shift(right).ffill()

    return pd.DataFrame(
        {
            "pivot_high": pivot_high,
            "pivot_low": pivot_low,
            "swing_high_confirmed": confirmed_high,
            "swing_low_confirmed": confirmed_low,
            "last_swing_high": last_high,
            "last_swing_low": last_low,
        }
    )


def market_structure(
    high: pd.Series,
    low: pd.Series,
    left: int = 3,
    right: int = 3,
) -> pd.DataFrame:
    """Classify structure from the last two confirmed swings on each side.

    ``structure`` is ``+1`` for higher-high & higher-low, ``-1`` for lower-high
    & lower-low, ``0`` otherwise (mixed / ranging).
    """
    pivots = find_pivots(high, low, left, right)

    highs = pivots["pivot_high"].shift(right).dropna()
    lows = pivots["pivot_low"].shift(right).dropna()

    prev_high = _previous_pivot_series(highs, high.index)
    prev_low = _previous_pivot_series(lows, low.index)

    last_high = pivots["last_swing_high"]
    last_low = pivots["last_swing_low"]

    higher_high = (last_high > prev_high) & prev_high.notna()
    lower_high = (last_high < prev_high) & prev_high.notna()
    higher_low = (last_low > prev_low) & prev_low.notna()
    lower_low = (last_low < prev_low) & prev_low.notna()

    structure = pd.Series(0.0, index=high.index)
    structure = structure.mask(higher_high & higher_low, 1.0)
    structure = structure.mask(lower_high & lower_low, -1.0)

    return pd.DataFrame(
        {
            "higher_high": higher_high,
            "higher_low": higher_low,
            "lower_high": lower_high,
            "lower_low": lower_low,
            "structure": structure,
            "last_swing_high": last_high,
            "last_swing_low": last_low,
            "prev_swing_high": prev_high,
            "prev_swing_low": prev_low,
        }
    )


def _previous_pivot_series(pivots: pd.Series, index: pd.Index) -> pd.Series:
    """For each bar, the pivot *before* the most recent one (as known then)."""
    if pivots.empty:
        return pd.Series(np.nan, index=index)
    shifted = pivots.shift(1)
    return shifted.reindex(index).ffill()


def donchian(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> pd.DataFrame:
    """Donchian channel built from *prior* bars only (excludes the current bar)."""
    if period < 2:
        raise ValueError("period must be >= 2")
    upper = high.rolling(period, min_periods=period).max().shift(1)
    lower = low.rolling(period, min_periods=period).min().shift(1)
    middle = (upper + lower) / 2.0
    return pd.DataFrame(
        {"dc_upper": upper, "dc_lower": lower, "dc_middle": middle}
    )


def breakouts(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.DataFrame:
    """Breakout flags plus distance to each channel edge (in fractions of price)."""
    channel = donchian(high, low, period)
    up = close > channel["dc_upper"]
    down = close < channel["dc_lower"]
    return pd.DataFrame(
        {
            "breakout_up": up.fillna(False),
            "breakout_down": down.fillna(False),
            "dist_to_upper": (channel["dc_upper"] - close) / close.replace(0.0, np.nan),
            "dist_to_lower": (close - channel["dc_lower"]) / close.replace(0.0, np.nan),
            "dc_upper": channel["dc_upper"],
            "dc_lower": channel["dc_lower"],
        }
    )


def consecutive_closes(close: pd.Series, direction: int = 1) -> pd.Series:
    """Length of the current run of up (``direction=1``) or down closes."""
    if direction not in (1, -1):
        raise ValueError("direction must be 1 or -1")
    change = close.diff()
    matches = change > 0 if direction == 1 else change < 0
    groups = (~matches).cumsum()
    return matches.groupby(groups).cumsum().astype(float)
