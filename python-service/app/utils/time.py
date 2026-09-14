"""Timeframe and timestamp helpers.

Everything in the system is timezone-aware UTC. Naive datetimes are treated as
UTC rather than rejected, because exchange SDKs and Postgres drivers are
inconsistent about tzinfo and a silent local-time conversion would corrupt
staleness checks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdw])$")

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def timeframe_to_seconds(timeframe: str) -> int:
    match = _TIMEFRAME_RE.match(timeframe.strip().lower())
    if not match:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    amount, unit = match.groups()
    return int(amount) * _UNIT_SECONDS[unit]


def timeframe_to_timedelta(timeframe: str) -> timedelta:
    return timedelta(seconds=timeframe_to_seconds(timeframe))


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_timeframe(moment: datetime, timeframe: str) -> datetime:
    """Start of the candle that ``moment`` falls into."""
    seconds = timeframe_to_seconds(timeframe)
    moment = ensure_utc(moment)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def staleness_seconds(
    last_candle_open: datetime, timeframe: str, now: datetime | None = None
) -> float:
    """Seconds by which the newest *closed* candle is overdue.

    Feeds are expected to expose closed candles only, so the newest one is
    naturally up to two bars old: it closed one bar ago at the earliest, and the
    bar now forming will not be published until it closes. Example on 1h data at
    10:55 — the newest closed candle opened at 09:00 (closed 10:00) and is
    perfectly fresh. Anything past two bars is real staleness.
    """
    now = ensure_utc(now or utcnow())
    bar = timeframe_to_seconds(timeframe)
    age = (now - ensure_utc(last_candle_open)).total_seconds()
    return max(0.0, age - 2 * bar)


def start_of_utc_day(moment: datetime | None = None) -> datetime:
    moment = ensure_utc(moment or utcnow())
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
