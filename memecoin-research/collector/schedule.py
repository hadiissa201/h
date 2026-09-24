"""The cadence ladder: how often to look at a token, by how old it is.

Dense early, sparse later, because the question this dataset exists to answer --
did it reach +200%, and could you actually have sold -- is decided in the first
minutes. A token four days old contributes one more point to a curve whose shape
was settled long before.

The intervals are not taste. At ~217 observations per token over 7 days and a
MEASURED DexScreener ceiling of ~300/min, the ladder is what makes ~1,500
tokens/day fit inside the budget. Tighten it and the sample rate has to fall to
compensate; there is no third option.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0


class WorkKind(str, Enum):
    OBSERVE = "observe"
    EXIT = "exit"
    HOLDERS = "holders"


# (upper age bound in seconds, settings attribute suffix)
_LADDER = (
    (5 * MINUTE, "0_5m"),
    (60 * MINUTE, "5_60m"),
    (6 * HOUR, "1_6h"),
    (24 * HOUR, "6_24h"),
    (7 * DAY, "1_7d"),
)


def age_seconds(detected_ts: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    if detected_ts.tzinfo is None:
        detected_ts = detected_ts.replace(tzinfo=UTC)
    return max(0.0, (now - detected_ts).total_seconds())


def tier_for(age_s: float) -> str:
    """Which rung of the ladder this age sits on. Past the top rung: 'retired'."""
    for bound, name in _LADDER:
        if age_s < bound:
            return name
    return "retired"


def interval_for(settings, kind: WorkKind, age_s: float) -> float | None:  # noqa: ANN001
    """Seconds until the next visit of this kind, or None once retired.

    None means stop scheduling -- it does NOT mean delete. A dormant token keeps
    every row it ever produced; those are the losers, and they are the entire
    reason this dataset is worth more than a screenshot.
    """
    tier = tier_for(age_s)
    if tier == "retired":
        return None
    if kind is WorkKind.HOLDERS:
        # Holder data is expensive and slow-moving; two rates are enough.
        return (settings.holder_interval_0_60m if age_s < 60 * MINUTE
                else settings.holder_interval_after)
    prefix = "obs_interval_" if kind is WorkKind.OBSERVE else "exit_interval_"
    return float(getattr(settings, f"{prefix}{tier}"))


def next_due(settings, kind: WorkKind, detected_ts: datetime,  # noqa: ANN001
             now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    interval = interval_for(settings, kind, age_seconds(detected_ts, now))
    if interval is None:
        return None
    return now + timedelta(seconds=interval)


def backoff_due(attempts: int, now: datetime | None = None,
                base_s: float = 5.0, cap_s: float = 900.0) -> datetime:
    """Exponential backoff for work that failed.

    Failed work is rescheduled, never dropped. A task that keeps failing stays
    in the queue with its attempt count visible, because a silently abandoned
    task and a silently missing observation are the same hole in the data.
    """
    now = now or datetime.now(UTC)
    delay = min(cap_s, base_s * (2 ** max(0, attempts - 1)))
    return now + timedelta(seconds=delay)


def estimated_calls_per_token(settings) -> dict[str, int]:  # noqa: ANN001
    """How many calls one token costs across its whole tracked life.

    This is the arithmetic that decides the sample rate, so it is computed from
    the live configuration rather than written in a comment that can rot.
    """
    counts = {"observe": 0, "exit": 0, "holders": 0}
    for kind, key in ((WorkKind.OBSERVE, "observe"), (WorkKind.EXIT, "exit")):
        previous = 0.0
        for bound, _ in _LADDER:
            interval = interval_for(settings, kind, previous)
            if interval:
                counts[key] += int((bound - previous) / interval)
            previous = bound
    counts["holders"] = int(60 * MINUTE / settings.holder_interval_0_60m) + int(
        (settings.retire_after_days * DAY - 60 * MINUTE) / settings.holder_interval_after)
    return counts


def capacity(settings) -> dict[str, float | str | int]:  # noqa: ANN001
    """What this configuration can actually sustain, and which service binds.

    Two DIFFERENT numbers, and confusing them is a 7x error:

      concurrent  -- how many tokens can be tracked AT ONCE. A token costs
                     calls every day it stays on the ladder, so the steady-state
                     population is what consumes the budget.
      intake/day  -- how many NEW tokens can be admitted per day. At steady
                     state, intake x tracked-lifetime = concurrent, so intake is
                     concurrent / retire_after_days.

    Budgets come from measured ceilings (DexScreener ~300/min, Jupiter ~120/min)
    via the configured rates, which sit deliberately below them.
    """
    per_token = estimated_calls_per_token(settings)
    lifetime_days = max(settings.retire_after_days, 1e-9)

    # Calls per tracked token PER DAY, averaged over its tracked life.
    dex_daily = per_token["observe"] / lifetime_days
    jup_daily = per_token["exit"] / lifetime_days

    dex_concurrent = (settings.dexscreener_rps * DAY / dex_daily
                      if dex_daily else float("inf"))
    jup_concurrent = (settings.jupiter_rps * DAY / jup_daily
                      if jup_daily else float("inf"))

    concurrent = min(dex_concurrent, jup_concurrent)
    return {
        "dexscreener_concurrent_tokens": round(dex_concurrent, 1),
        "jupiter_concurrent_tokens": round(jup_concurrent, 1),
        "binding_service": "dexscreener" if dex_concurrent <= jup_concurrent else "jupiter",
        "concurrent_tokens": round(concurrent, 1),
        "intake_tokens_per_day": round(concurrent / lifetime_days, 1),
        "observations_per_token": per_token["observe"],
        "exit_sims_per_token": per_token["exit"],
        "tracked_days": settings.retire_after_days,
    }
