"""The Phase 2 read interface. Query helpers, and the rules Phase 2 must obey.

Phase 1 does not implement a strategy. This module exists so that when Phase 2
does, it cannot accidentally cheat -- the three rules below are the difference
between a measurement and a flattering fiction, and each one is enforced here
rather than left to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import CollectionGap, Observation, SimulatedExit, Token


def _aware(moment: datetime | None) -> datetime | None:
    """Force UTC-awareness on a timestamp read back from the database.

    SQLite returns naive datetimes even for DateTime(timezone=True); Postgres
    returns aware ones. Comparing the two raises, and -- worse -- any code that
    silently coerced them would mis-place gap boundaries by the local offset,
    quietly marking covered time as blind or vice versa.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Coverage:
    """The windows we were actually collecting in.

    Phase 2 MUST restrict itself to these. Outside them, the absence of launches
    is the absence of a collector -- and reading our downtime as a quiet market
    makes every base rate too low.
    """
    start: datetime
    end: datetime
    gaps: tuple[tuple[datetime, datetime], ...]

    def covers(self, moment: datetime) -> bool:
        moment = _aware(moment)
        if not (self.start <= moment <= self.end):
            return False
        return not any(a <= moment <= b for a, b in self.gaps)

    @property
    def covered_seconds(self) -> float:
        total = (self.end - self.start).total_seconds()
        blind = sum((b - a).total_seconds() for a, b in self.gaps)
        return max(0.0, total - blind)


def coverage(session: Session, start: datetime | None = None,
             end: datetime | None = None) -> Coverage:
    end = end or datetime.now(UTC)
    start = start or (end - timedelta(days=365))
    rows = session.scalars(select(CollectionGap)).all()
    gaps = tuple(
        (_aware(g.gap_start), _aware(g.gap_end) or end)
        for g in rows
        if start <= _aware(g.gap_start) <= end
    )
    return Coverage(start=_aware(start), end=_aware(end), gaps=gaps)


def tokens_in_coverage(session: Session, cover: Coverage) -> list[Token]:
    """Tokens detected while we were demonstrably watching."""
    candidates = session.scalars(select(Token)).all()
    return [t for t in candidates
            if cover.start <= _aware(t.detected_ts) <= cover.end
            and cover.covers(t.detected_ts)]


def price_path(session: Session, token_id: int) -> list[Observation]:
    """Every observation, in order. Phase 2 fills ONLY at observed prices.

    Interpolating between points, or filling at a peak we inferred rather than
    saw, invents liquidity that was never demonstrated to exist.
    """
    return list(session.scalars(
        select(Observation)
        .where(Observation.token_id == token_id)
        .order_by(Observation.observed_ts)
    ).all())


def exit_evidence(session: Session, token_id: int) -> list[SimulatedExit]:
    """Exit simulations that actually produced a verdict.

    succeeded IS NULL means our request failed and we learned nothing about the
    token. Counting those as "could not sell" would turn our own outages into
    evidence of rugs.
    """
    return list(session.scalars(
        select(SimulatedExit)
        .where(SimulatedExit.token_id == token_id,
               SimulatedExit.succeeded.is_not(None))
        .order_by(SimulatedExit.simulated_ts)
    ).all())


def earliest_entry_ts(token: Token) -> datetime:
    """The first moment this opportunity existed FOR US.

    detected_ts, never launch_ts. Pricing an entry at on-chain creation time
    hands the backtest a head start it never had and manufactures an edge out
    of latency we do not possess.
    """
    return _aware(token.detected_ts)


def was_sellable_at(session: Session, token_id: int,
                    moment: datetime) -> bool | None:
    """Most recent verdict at or before `moment`. None when we never knew.

    None is a real answer and must not be coerced. A strategy replay that
    assumes "unknown means fine" is assuming away the exact risk being measured.
    """
    row = session.scalars(
        select(SimulatedExit)
        .where(SimulatedExit.token_id == token_id,
               SimulatedExit.simulated_ts <= moment,
               SimulatedExit.succeeded.is_not(None))
        .order_by(SimulatedExit.simulated_ts.desc())
        .limit(1)
    ).first()
    return None if row is None else bool(row.succeeded)


def population_weight(token: Token) -> float:
    """How many real launches this sampled token stands for.

    A 5% sample means each stored token represents ~20. Phase 2 must weight by
    this before quoting any population rate, or it will report the sample's
    absolute counts as if they were the market's.
    """
    rate = float(token.sample_rate_at_detection or 1.0)
    return 1.0 / rate if rate > 0 else 1.0
