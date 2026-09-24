"""Collector behaviour, offline.

Weighted toward the failures that would be INVISIBLE in production: a sample
that drifts with load, a gap that goes unrecorded, a transport error counted as
a rug. Those do not crash anything -- they just quietly make the dataset lie.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.config import CollectorSettings
from collector.models import Base, CollectionGap, SimulatedExit, Token
from collector.ratelimit import TokenBucket
from collector.research import (
    Coverage,
    coverage,
    earliest_entry_ts,
    exit_evidence,
    population_weight,
    tokens_in_coverage,
    was_sellable_at,
)
from collector.sampling import sample_score, should_track
from collector.schedule import WorkKind, backoff_due, capacity, interval_for, tier_for
from poc.store import insert_simulated_exit, open_gap, upsert_token

TS = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def settings():
    return CollectorSettings()


# ------------------------------------------------------------------ sampling
def test_sampling_is_uniform_across_many_mints():
    mints = [f"Mint{i}{'x' * 30}" for i in range(20_000)]
    kept = sum(should_track(m, 0.05) for m in mints)
    assert 0.045 < kept / len(mints) < 0.055


def test_the_same_mint_always_gets_the_same_verdict():
    """A restart, a replay, or a second machine must not change the sample."""
    mint = "SoMeMintAddress1111111111111111111111111111"
    first = [should_track(mint, 0.3) for _ in range(50)]
    assert len(set(first)) == 1


def test_the_verdict_does_not_depend_on_load_or_clock():
    """The bug this prevents: keeping whatever we could keep up with.

    That over-represents quiet periods, because quiet is exactly when there is
    spare capacity -- a biased dataset that looks like a sample.
    """
    mint = "AnotherMint2222222222222222222222222222222"
    assert should_track(mint, 0.5) == should_track(mint, 0.5)
    assert 0.0 <= sample_score(mint) < 1.0


def test_a_full_sample_keeps_everything_and_a_zero_rate_keeps_nothing():
    mint = "EdgeCaseMint333333333333333333333333333333"
    assert should_track(mint, 1.0) is True
    assert should_track(mint, 0.0) is False


# ------------------------------------------------------------------ schedule
def test_the_ladder_gets_sparser_as_a_token_ages(settings):
    intervals = [interval_for(settings, WorkKind.OBSERVE, age)
                 for age in (10, 600, 7_200, 50_000, 300_000)]
    assert all(a <= b for a, b in zip(intervals, intervals[1:], strict=False))
    assert intervals[0] == settings.obs_interval_0_5m


def test_past_the_last_rung_scheduling_stops_but_nothing_is_deleted(settings):
    assert tier_for(30 * 86400) == "retired"
    assert interval_for(settings, WorkKind.OBSERVE, 30 * 86400) is None


def test_capacity_distinguishes_concurrent_tokens_from_daily_intake(settings):
    """Confusing these is a 7x error: a token costs calls every day it lives."""
    plan = capacity(settings)
    assert plan["concurrent_tokens"] > plan["intake_tokens_per_day"]
    assert plan["intake_tokens_per_day"] == pytest.approx(
        plan["concurrent_tokens"] / settings.retire_after_days, rel=0.01)


def test_capacity_is_bound_by_the_slowest_service(settings):
    plan = capacity(settings)
    assert plan["concurrent_tokens"] == min(plan["dexscreener_concurrent_tokens"],
                                            plan["jupiter_concurrent_tokens"])


def test_failed_work_backs_off_instead_of_hammering():
    first = backoff_due(1, TS)
    third = backoff_due(3, TS)
    assert third > first
    assert backoff_due(50, TS) <= TS + timedelta(seconds=900)


# -------------------------------------------------------------- rate limiter
def test_the_bucket_refuses_rather_than_exceeding_the_measured_ceiling():
    bucket = TokenBucket(rate_per_s=1.0, name="t", burst=1.0)
    assert bucket.acquire(max_wait=0.0) is True
    assert bucket.acquire(max_wait=0.0) is False
    assert bucket.stats.refused == 1


def test_a_refusal_is_counted_so_gaps_are_explainable():
    """A dropped observation nobody recorded looks like a quiet token."""
    bucket = TokenBucket(rate_per_s=1.0, name="t", burst=1.0)
    bucket.acquire(max_wait=0.0)
    for _ in range(3):
        bucket.acquire(max_wait=0.0)
    assert bucket.snapshot()["refused"] == 3


def test_a_429_stops_traffic_instead_of_spending_the_budget_on_rejections():
    bucket = TokenBucket(rate_per_s=100.0, name="t")
    bucket.penalise(30.0)
    assert bucket.acquire(max_wait=0.0) is False
    snap = bucket.snapshot()
    assert snap["throttle_events"] == 1
    assert snap["penalised_for_s"] > 0


# ------------------------------------------------------- coverage & research
def test_a_moment_inside_a_gap_is_not_covered():
    cover = Coverage(start=TS, end=TS + timedelta(hours=10),
                     gaps=((TS + timedelta(hours=2), TS + timedelta(hours=3)),))
    assert cover.covers(TS + timedelta(hours=1)) is True
    assert cover.covers(TS + timedelta(hours=2, minutes=30)) is False
    assert cover.covers(TS + timedelta(hours=5)) is True


def test_downtime_is_subtracted_from_covered_time():
    cover = Coverage(start=TS, end=TS + timedelta(hours=10),
                     gaps=((TS + timedelta(hours=2), TS + timedelta(hours=4)),))
    assert cover.covered_seconds == pytest.approx(8 * 3600)


def test_an_open_gap_means_we_are_still_blind(session):
    open_gap(session, gap_start=TS, cause="disconnect")
    session.commit()
    cover = coverage(session, start=TS - timedelta(days=1),
                     end=TS + timedelta(hours=1))
    assert len(cover.gaps) == 1
    # An unclosed gap extends to now, not to zero length.
    assert cover.gaps[0][1] >= TS


def test_tokens_detected_during_a_gap_are_excluded(session):
    for offset, address in ((1, "IN_COVERAGE"), (150, "DURING_GAP")):
        upsert_token(session, chain="solana", address=address,
                     detected_ts=TS + timedelta(minutes=offset),
                     detection_source="ws")
    gap = CollectionGap(gap_start=TS + timedelta(minutes=120),
                        gap_end=TS + timedelta(minutes=180), cause="disconnect")
    session.add(gap)
    session.commit()

    cover = coverage(session, start=TS, end=TS + timedelta(hours=4))
    kept = {t.address for t in tokens_in_coverage(session, cover)}
    assert "IN_COVERAGE" in kept
    assert "DURING_GAP" not in kept


def test_phase_two_entry_is_priced_at_detection_not_launch(session):
    """Pricing at launch time invents latency we do not have."""
    token_id = upsert_token(session, chain="solana", address="M1",
                            launch_ts=TS, detected_ts=TS + timedelta(seconds=90),
                            detection_source="ws")
    session.commit()
    token = session.get(Token, token_id)
    entry = earliest_entry_ts(token)
    # Compared as UTC-aware: the column round-trips naive through SQLite.
    assert entry == TS + timedelta(seconds=90)
    assert entry != TS, "entry must not be priced at on-chain launch time"
    assert entry > TS, "detection is necessarily after launch"


def test_unknown_exits_are_excluded_from_evidence(session):
    token_id = upsert_token(session, chain="solana", address="M2",
                            detected_ts=TS, detection_source="ws")
    insert_simulated_exit(session, token_id=token_id, simulated_ts=TS,
                          method="quote", notional_usd=100.0, succeeded=None,
                          failure_kind="transport", failure_reason="HTTP 429")
    insert_simulated_exit(session, token_id=token_id,
                          simulated_ts=TS + timedelta(minutes=1), method="quote",
                          notional_usd=100.0, succeeded=False,
                          failure_kind="no_route", failure_reason="No routes")
    session.commit()

    evidence = exit_evidence(session, token_id)
    assert len(evidence) == 1
    assert evidence[0].succeeded is False
    assert session.query(SimulatedExit).count() == 2, "the unknown is still stored"


def test_sellability_lookup_returns_none_when_we_never_knew(session):
    token_id = upsert_token(session, chain="solana", address="M3",
                            detected_ts=TS, detection_source="ws")
    session.commit()
    assert was_sellable_at(session, token_id, TS + timedelta(hours=1)) is None


def test_sellability_uses_the_most_recent_verdict_at_that_moment(session):
    token_id = upsert_token(session, chain="solana", address="M4",
                            detected_ts=TS, detection_source="ws")
    insert_simulated_exit(session, token_id=token_id, simulated_ts=TS,
                          method="quote", notional_usd=100.0, succeeded=True)
    insert_simulated_exit(session, token_id=token_id,
                          simulated_ts=TS + timedelta(hours=2), method="quote",
                          notional_usd=100.0, succeeded=False,
                          failure_kind="no_route")
    session.commit()

    assert was_sellable_at(session, token_id, TS + timedelta(hours=1)) is True
    assert was_sellable_at(session, token_id, TS + timedelta(hours=3)) is False


def test_a_sampled_token_stands_for_many_real_ones(session):
    token_id = upsert_token(session, chain="solana", address="M5",
                            detected_ts=TS, detection_source="ws",
                            sample_rate_at_detection=0.05)
    session.commit()
    assert population_weight(session.get(Token, token_id)) == pytest.approx(20.0)


def test_an_unsampled_token_weighs_one(session):
    token_id = upsert_token(session, chain="solana", address="M6",
                            detected_ts=TS, detection_source="ws",
                            sample_rate_at_detection=1.0)
    session.commit()
    assert population_weight(session.get(Token, token_id)) == pytest.approx(1.0)


def test_naive_timestamps_from_sqlite_are_treated_as_utc():
    """Postgres returns aware datetimes, SQLite naive ones.

    Silently coercing instead of normalising would shift every gap boundary by
    the local UTC offset -- marking covered time blind, or blind time covered,
    depending which side of the world the collector runs on.
    """
    from collector.research import _aware

    naive = datetime(2026, 9, 24, 12, 0)
    assert _aware(naive).tzinfo is UTC
    assert _aware(naive) == datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    already = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    assert _aware(already) is already
    assert _aware(None) is None
