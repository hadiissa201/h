"""Indicator library — pure functions over pandas Series/DataFrames.

Deliberately dependency-light (numpy + pandas only): no TA-Lib native build,
no vendored C extensions, so the same code runs identically in Docker, CI and a
laptop venv.
"""

from app.indicators.momentum import momentum, roc, rsi, stoch_rsi
from app.indicators.structure import (
    breakouts,
    consecutive_closes,
    donchian,
    find_pivots,
    market_structure,
)
from app.indicators.trend import (
    adx,
    efficiency_ratio,
    ema,
    macd,
    sma,
    slope,
    true_range,
    wilder_ema,
)
from app.indicators.volatility import (
    atr,
    atr_pct,
    bollinger_bands,
    keltner_channels,
    realized_volatility,
    squeeze,
    volatility_ratio,
)
from app.indicators.volume import (
    obv,
    obv_slope,
    relative_volume,
    volume_change,
    volume_ma,
    volume_price_confirmation,
    vwap,
)

__all__ = [
    "adx",
    "atr",
    "atr_pct",
    "bollinger_bands",
    "breakouts",
    "consecutive_closes",
    "donchian",
    "efficiency_ratio",
    "ema",
    "find_pivots",
    "keltner_channels",
    "macd",
    "market_structure",
    "momentum",
    "obv",
    "obv_slope",
    "realized_volatility",
    "relative_volume",
    "roc",
    "rsi",
    "slope",
    "sma",
    "squeeze",
    "stoch_rsi",
    "true_range",
    "volatility_ratio",
    "volume_change",
    "volume_ma",
    "volume_price_confirmation",
    "vwap",
    "wilder_ema",
]
