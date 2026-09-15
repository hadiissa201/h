"""Market-data validation — the first gate in the pipeline.

If a check fails with ``severity="error"`` the report is not tradeable and the
analysis workflow stops. Bad data is the cheapest way to lose money, so the
default posture is: when in doubt, NO TRADE.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from app.models.market import DataQualityIssue, DataQualityReport
from app.utils.time import (
    ensure_utc,
    staleness_seconds,
    timeframe_to_seconds,
    utcnow,
)


def validate_candles(
    frame: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    min_bars: int = 120,
    max_staleness_seconds: int = 300,
    max_gap_ratio: float = 0.02,
    max_zero_volume_ratio: float = 0.20,
    abnormal_move_pct: float = 0.10,
    now: datetime | None = None,
) -> DataQualityReport:
    now = ensure_utc(now or utcnow())
    issues: list[DataQualityIssue] = []

    if frame is None or len(frame) == 0:
        return DataQualityReport(
            symbol=symbol,
            timeframe=timeframe,
            bars=0,
            issues=[
                DataQualityIssue(
                    code="NO_DATA", detail="no candles returned", severity="error"
                )
            ],
        )

    required = {"open", "high", "low", "close", "volume"}
    missing_columns = sorted(required - set(frame.columns))
    if missing_columns:
        return DataQualityReport(
            symbol=symbol,
            timeframe=timeframe,
            bars=len(frame),
            issues=[
                DataQualityIssue(
                    code="MISSING_COLUMNS",
                    detail=f"missing columns: {missing_columns}",
                    severity="error",
                )
            ],
        )

    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        return DataQualityReport(
            symbol=symbol,
            timeframe=timeframe,
            bars=len(frame),
            issues=[
                DataQualityIssue(
                    code="BAD_INDEX",
                    detail="candles must be indexed by timestamp",
                    severity="error",
                )
            ],
        )
    if index.tz is None:
        index = index.tz_localize("UTC")
        frame = frame.copy()
        frame.index = index

    first_ts = index[0].to_pydatetime()
    last_ts = index[-1].to_pydatetime()

    # --- 1. bar count
    if len(frame) < min_bars:
        issues.append(
            DataQualityIssue(
                code="INSUFFICIENT_BARS",
                detail=f"{len(frame)} bars < required {min_bars}",
                severity="error",
                context={"bars": len(frame), "required": min_bars},
            )
        )

    # --- 2. ordering and duplicates
    if not index.is_monotonic_increasing:
        issues.append(
            DataQualityIssue(
                code="UNSORTED",
                detail="timestamps are not ascending",
                severity="error",
            )
        )
    duplicate_count = int(index.duplicated().sum())
    if duplicate_count:
        issues.append(
            DataQualityIssue(
                code="DUPLICATE_TIMESTAMPS",
                detail=f"{duplicate_count} duplicate candle timestamps",
                severity="error",
                context={"duplicates": duplicate_count},
            )
        )

    # --- 3. gaps / missing candles
    step = timeframe_to_seconds(timeframe)
    if len(frame) > 1:
        deltas = np.diff(index.asi8 // 1_000_000_000)
        expected_span = (len(frame) - 1) * step
        actual_span = int(deltas.sum())
        missing_bars = max(0, (actual_span - expected_span) // step)
        irregular = int((deltas != step).sum())
        gap_ratio = missing_bars / max(1, len(frame))
        if irregular:
            severity = "error" if gap_ratio > max_gap_ratio else "warning"
            issues.append(
                DataQualityIssue(
                    code="MISSING_CANDLES",
                    detail=(
                        f"{missing_bars} missing bars across {irregular} irregular "
                        f"intervals (gap ratio {gap_ratio:.4f})"
                    ),
                    severity=severity,
                    context={
                        "missing_bars": int(missing_bars),
                        "irregular_intervals": irregular,
                        "gap_ratio": round(gap_ratio, 6),
                        "limit": max_gap_ratio,
                    },
                )
            )

    # --- 4. staleness
    stale_by = staleness_seconds(last_ts, timeframe, now=now)
    if stale_by > max_staleness_seconds:
        issues.append(
            DataQualityIssue(
                code="STALE_DATA",
                detail=(
                    f"last candle {last_ts.isoformat()} is {stale_by:.0f}s beyond "
                    f"one bar; limit {max_staleness_seconds}s"
                ),
                severity="error",
                context={
                    "staleness_seconds": round(stale_by, 2),
                    "limit": max_staleness_seconds,
                },
            )
        )
    if last_ts > now:
        issues.append(
            DataQualityIssue(
                code="FUTURE_TIMESTAMP",
                detail=f"last candle {last_ts.isoformat()} is in the future",
                severity="error",
            )
        )

    # --- 5. NaN / non-finite values
    numeric = frame[["open", "high", "low", "close", "volume"]]
    nan_count = int(numeric.isna().sum().sum())
    if nan_count:
        issues.append(
            DataQualityIssue(
                code="NAN_VALUES",
                detail=f"{nan_count} NaN values in OHLCV",
                severity="error",
                context={"nan_count": nan_count},
            )
        )
    if np.isinf(numeric.to_numpy(dtype=float, na_value=0.0)).any():
        issues.append(
            DataQualityIssue(
                code="INFINITE_VALUES",
                detail="non-finite values in OHLCV",
                severity="error",
            )
        )

    # --- 6. OHLC integrity
    non_positive = int((numeric[["open", "high", "low", "close"]] <= 0).sum().sum())
    if non_positive:
        issues.append(
            DataQualityIssue(
                code="NON_POSITIVE_PRICE",
                detail=f"{non_positive} non-positive price values",
                severity="error",
            )
        )
    bad_range = int((frame["high"] < frame["low"]).sum())
    bad_open = int(
        ((frame["open"] > frame["high"]) | (frame["open"] < frame["low"])).sum()
    )
    bad_close = int(
        ((frame["close"] > frame["high"]) | (frame["close"] < frame["low"])).sum()
    )
    if bad_range or bad_open or bad_close:
        issues.append(
            DataQualityIssue(
                code="INVALID_OHLC",
                detail=(
                    f"high<low: {bad_range}, open outside range: {bad_open}, "
                    f"close outside range: {bad_close}"
                ),
                severity="error",
                context={
                    "high_lt_low": bad_range,
                    "open_out_of_range": bad_open,
                    "close_out_of_range": bad_close,
                },
            )
        )

    # --- 7. volume
    negative_volume = int((frame["volume"] < 0).sum())
    if negative_volume:
        issues.append(
            DataQualityIssue(
                code="NEGATIVE_VOLUME",
                detail=f"{negative_volume} negative volume values",
                severity="error",
            )
        )
    zero_volume = int((frame["volume"] == 0).sum())
    zero_ratio = zero_volume / len(frame)
    if zero_volume:
        severity = "error" if zero_ratio > max_zero_volume_ratio else "warning"
        issues.append(
            DataQualityIssue(
                code="ZERO_VOLUME_BARS",
                detail=f"{zero_volume} zero-volume bars ({zero_ratio:.2%})",
                severity=severity,
                context={"zero_volume_bars": zero_volume, "ratio": round(zero_ratio, 4)},
            )
        )
    if float(frame["volume"].iloc[-1]) == 0.0:
        issues.append(
            DataQualityIssue(
                code="LAST_BAR_NO_VOLUME",
                detail="most recent candle has zero volume",
                severity="error",
            )
        )

    # --- 8. abnormal move on the last bar (informational; the regime layer
    #        decides whether to refuse to trade)
    if len(frame) > 1:
        previous_close = float(frame["close"].iloc[-2])
        last_close = float(frame["close"].iloc[-1])
        if previous_close > 0:
            move = abs(last_close / previous_close - 1.0)
            if move > abnormal_move_pct:
                issues.append(
                    DataQualityIssue(
                        code="ABNORMAL_PRICE_MOVE",
                        detail=f"last bar moved {move:.2%} (limit {abnormal_move_pct:.2%})",
                        severity="warning",
                        context={"move_pct": round(move, 6), "limit": abnormal_move_pct},
                    )
                )

    return DataQualityReport(
        symbol=symbol,
        timeframe=timeframe,
        bars=len(frame),
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        staleness_seconds=round(stale_by, 2),
        issues=issues,
    )
