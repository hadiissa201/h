"""Feature engine.

The engine computes features for *all* bars at once and hands strategies a
single row at a time. Live trading uses the last row; the backtester walks rows
0..n. Same code, same numbers — which is the only way a backtest result means
anything about live behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.core.errors import ValidationError
from app.features.blocks import DEFAULT_BLOCKS, REGISTRY
from app.features.config import FeatureConfig

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass
class FeatureSet:
    """Computed features for one symbol/timeframe."""

    symbol: str
    timeframe: str
    candles: pd.DataFrame
    frame: pd.DataFrame
    config: FeatureConfig
    blocks: tuple[str, ...]
    warmup_bars: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_warm(self) -> bool:
        """True when the most recent row has no missing features."""
        if self.frame.empty:
            return False
        return bool(self.frame.iloc[-1].notna().all())

    @property
    def last_timestamp(self) -> datetime:
        return self.candles.index[-1].to_pydatetime()

    @property
    def last_close(self) -> float:
        return float(self.candles["close"].iloc[-1])

    def row(self, index: int = -1) -> dict[str, float | None]:
        """One feature row as a JSON-safe dict (NaN/inf become ``None``)."""
        if self.frame.empty:
            raise ValidationError("feature frame is empty")
        series = self.frame.iloc[index]
        candle = self.candles.iloc[index]
        payload: dict[str, float | None] = {
            "open": _clean(candle["open"]),
            "high": _clean(candle["high"]),
            "low": _clean(candle["low"]),
            "close": _clean(candle["close"]),
            "volume": _clean(candle["volume"]),
        }
        for key, value in series.items():
            payload[str(key)] = _clean(value)
        return payload

    def missing_features(self, index: int = -1) -> list[str]:
        series = self.frame.iloc[index]
        return [str(k) for k, v in series.items() if _clean(v) is None]

    def to_summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.last_timestamp.isoformat(),
            "bars": int(len(self.candles)),
            "warmup_bars": self.warmup_bars,
            "is_warm": self.is_warm,
            "blocks": list(self.blocks),
            "features": self.row(-1),
        }


class FeatureEngine:
    def __init__(
        self,
        config: FeatureConfig | None = None,
        blocks: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.config = config or FeatureConfig()
        chosen = tuple(blocks) if blocks else DEFAULT_BLOCKS
        unknown = [name for name in chosen if name not in REGISTRY]
        if unknown:
            raise ValidationError(
                f"unknown feature blocks: {unknown}. available={sorted(REGISTRY)}"
            )
        self.blocks = chosen

    def compute(
        self, candles: pd.DataFrame, symbol: str, timeframe: str
    ) -> FeatureSet:
        frame = validate_ohlcv_frame(candles)
        computed = [
            REGISTRY[name].compute(frame, self.config, timeframe) for name in self.blocks
        ]
        features = (
            pd.concat(computed, axis=1)
            if computed
            else pd.DataFrame(index=frame.index)
        )
        features = features.replace([np.inf, -np.inf], np.nan)
        duplicated = features.columns[features.columns.duplicated()].tolist()
        if duplicated:
            raise ValidationError(f"duplicate feature columns produced: {duplicated}")
        return FeatureSet(
            symbol=symbol,
            timeframe=timeframe,
            candles=frame,
            frame=features,
            config=self.config,
            blocks=self.blocks,
            warmup_bars=self.config.warmup_bars,
            metadata={"feature_count": int(features.shape[1])},
        )


def validate_ohlcv_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Structural validation of an OHLCV frame.

    Only shape/typing concerns live here. Market-quality checks (staleness,
    gaps, duplicates) are the job of ``app.data.validation`` — they need to run
    before the data ever reaches the feature layer.
    """
    if candles is None or len(candles) == 0:
        raise ValidationError("no candles supplied")
    missing = [column for column in OHLCV_COLUMNS if column not in candles.columns]
    if missing:
        raise ValidationError(f"missing OHLCV columns: {missing}")

    frame = candles.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        if "timestamp" in frame.columns:
            frame = frame.set_index(pd.DatetimeIndex(frame["timestamp"]))
        else:
            raise ValidationError(
                "candles must be indexed by a DatetimeIndex or carry a 'timestamp' column"
            )
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")

    frame = frame[list(OHLCV_COLUMNS)].astype(float)
    if not frame.index.is_monotonic_increasing:
        raise ValidationError("candles must be sorted by ascending timestamp")
    if frame.index.has_duplicates:
        raise ValidationError("candles contain duplicate timestamps")
    return frame


def _clean(value: Any) -> float | None:
    """JSON-safe scalar: NaN/inf -> None, numpy -> python."""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number
