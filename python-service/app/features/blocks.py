"""Feature blocks.

A block turns an OHLCV frame into a frame of named feature columns. Blocks are
registered by name so they can be enabled, disabled and unit-tested
independently — the point being that you can measure whether a feature family
actually earns its place rather than assuming it does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.features.config import FeatureConfig
from app.indicators import (
    adx,
    efficiency_ratio,
    atr,
    atr_pct,
    bollinger_bands,
    breakouts,
    consecutive_closes,
    ema,
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
    volume_change,
    volume_ma,
    volume_price_confirmation,
)

BlockFn = Callable[[pd.DataFrame, FeatureConfig, str], pd.DataFrame]


@dataclass(frozen=True)
class FeatureBlock:
    name: str
    compute: BlockFn
    description: str


def _trend_block(
    candles: pd.DataFrame, cfg: FeatureConfig, timeframe: str
) -> pd.DataFrame:
    close, high, low = candles["close"], candles["high"], candles["low"]
    out = pd.DataFrame(index=candles.index)

    out["sma_fast"] = sma(close, cfg.sma_fast)
    out["sma_slow"] = sma(close, cfg.sma_slow)
    out["ema_fast"] = ema(close, cfg.ema_fast)
    out["ema_slow"] = ema(close, cfg.ema_slow)
    out["ema_trend"] = ema(close, cfg.ema_trend)

    out["price_vs_ema_fast"] = close / out["ema_fast"] - 1.0
    out["price_vs_ema_trend"] = close / out["ema_trend"] - 1.0
    out["ema_fast_vs_slow"] = out["ema_fast"] / out["ema_slow"] - 1.0
    out["ema_stack_bull"] = (
        (out["ema_fast"] > out["ema_slow"]) & (out["ema_slow"] > out["ema_trend"])
    ).astype(float)
    out["ema_stack_bear"] = (
        (out["ema_fast"] < out["ema_slow"]) & (out["ema_slow"] < out["ema_trend"])
    ).astype(float)

    macd_frame = macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    out = out.join(macd_frame)
    out["macd_hist_norm"] = macd_frame["macd_hist"] / close.replace(0.0, np.nan)

    adx_frame = adx(high, low, close, cfg.adx_period)
    out = out.join(adx_frame)
    out["di_spread"] = adx_frame["plus_di"] - adx_frame["minus_di"]
    out["trend_slope"] = slope(close, cfg.slope_period)
    out["efficiency_ratio"] = efficiency_ratio(close, cfg.efficiency_period)
    return out


def _momentum_block(
    candles: pd.DataFrame, cfg: FeatureConfig, timeframe: str
) -> pd.DataFrame:
    close = candles["close"]
    out = pd.DataFrame(index=candles.index)
    out["rsi"] = rsi(close, cfg.rsi_period)
    out = out.join(stoch_rsi(close, cfg.rsi_period, cfg.stoch_rsi_period))
    out["roc"] = roc(close, cfg.roc_period)
    out["rsi_slope"] = out["rsi"].diff(3)
    return out


def _volatility_block(
    candles: pd.DataFrame, cfg: FeatureConfig, timeframe: str
) -> pd.DataFrame:
    close, high, low = candles["close"], candles["high"], candles["low"]
    out = pd.DataFrame(index=candles.index)
    out["atr"] = atr(high, low, close, cfg.atr_period)
    out["atr_pct"] = atr_pct(high, low, close, cfg.atr_period)
    out = out.join(bollinger_bands(close, cfg.bb_period, cfg.bb_std))
    out["realized_vol"] = realized_volatility(
        close, cfg.realized_vol_period, timeframe=timeframe
    )
    out["vol_ratio"] = volatility_ratio(close, cfg.vol_ratio_short, cfg.vol_ratio_long)
    out["squeeze"] = squeeze(high, low, close, cfg.bb_period, cfg.bb_period).astype(
        float
    )
    # Percentile rank of current ATR% within its own history: "is this loud or
    # quiet *for this market*", which a raw ATR cannot tell you.
    out["atr_pct_rank"] = (
        out["atr_pct"]
        .rolling(cfg.atr_rank_period, min_periods=cfg.atr_rank_period)
        .rank(pct=True)
    )
    return out


def _volume_block(
    candles: pd.DataFrame, cfg: FeatureConfig, timeframe: str
) -> pd.DataFrame:
    close, volume = candles["close"], candles["volume"]
    out = pd.DataFrame(index=candles.index)
    out["volume_ma"] = volume_ma(volume, cfg.volume_ma_period)
    out["relative_volume"] = relative_volume(volume, cfg.volume_ma_period)
    out["volume_change"] = volume_change(volume)
    out["obv"] = obv(close, volume)
    out["obv_slope"] = obv_slope(close, volume, cfg.volume_ma_period)
    out["volume_confirms"] = volume_price_confirmation(
        close, volume, cfg.volume_ma_period
    )
    return out


def _structure_block(
    candles: pd.DataFrame, cfg: FeatureConfig, timeframe: str
) -> pd.DataFrame:
    close, high, low = candles["close"], candles["high"], candles["low"]
    out = pd.DataFrame(index=candles.index)

    structure = market_structure(high, low, cfg.pivot_left, cfg.pivot_right)
    for column in (
        "higher_high",
        "higher_low",
        "lower_high",
        "lower_low",
    ):
        out[column] = structure[column].astype(float)
    out["structure"] = structure["structure"]
    out["last_swing_high"] = structure["last_swing_high"]
    out["last_swing_low"] = structure["last_swing_low"]
    out["dist_to_swing_high"] = (
        structure["last_swing_high"] - close
    ) / close.replace(0.0, np.nan)
    out["dist_to_swing_low"] = (close - structure["last_swing_low"]) / close.replace(
        0.0, np.nan
    )

    breakout_frame = breakouts(high, low, close, cfg.donchian_period)
    out["breakout_up"] = breakout_frame["breakout_up"].astype(float)
    out["breakout_down"] = breakout_frame["breakout_down"].astype(float)
    out["dist_to_dc_upper"] = breakout_frame["dist_to_upper"]
    out["dist_to_dc_lower"] = breakout_frame["dist_to_lower"]
    out["dc_upper"] = breakout_frame["dc_upper"]
    out["dc_lower"] = breakout_frame["dc_lower"]
    out["up_streak"] = consecutive_closes(close, 1)
    out["down_streak"] = consecutive_closes(close, -1)
    return out


REGISTRY: dict[str, FeatureBlock] = {
    "trend": FeatureBlock(
        "trend", _trend_block, "Moving averages, MACD, ADX/DI, normalised slope"
    ),
    "momentum": FeatureBlock(
        "momentum", _momentum_block, "RSI, Stochastic RSI, rate of change"
    ),
    "volatility": FeatureBlock(
        "volatility",
        _volatility_block,
        "ATR, Bollinger, realized vol, squeeze, vol percentile",
    ),
    "volume": FeatureBlock(
        "volume", _volume_block, "Volume MA, relative volume, OBV and confirmation"
    ),
    "structure": FeatureBlock(
        "structure",
        _structure_block,
        "Swings, HH/HL/LH/LL, Donchian breakouts, close streaks",
    ),
}

DEFAULT_BLOCKS: tuple[str, ...] = ("trend", "momentum", "volatility", "volume", "structure")
