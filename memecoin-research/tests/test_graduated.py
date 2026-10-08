"""The graduated cohort must not be allowed to look like an opportunity.

Selecting tokens by what they went on to do is survivorship bias, and this
project has already had one result (+2,271% on asset-class trend following)
reverse completely once the basket was rebuilt point-in-time. These tests pin
the two guards that keep the same thing happening here: a feature is only
"visible at entry" if it was recorded inside the entry window, and a bucket
difference is only a signal if the intervals do not overlap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.models import Base, Observation, Pool, Token
from graduated import (
    early_features,
    graduation_markers,
    separates,
    tercile_rates,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def session_with(tokens):
    """tokens: list of (address, dex_or_None, [(offset_s, liquidity)])."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    for address, dex, rows in tokens:
        token = Token(address=address, chain="solana",
                      detection_source="test", detected_ts=NOW)
        s.add(token)
        s.flush()
        if dex is not None:
            s.add(Pool(token_id=token.id, pair_address=f"p-{address}", dex=dex))
        for offset, liquidity in rows:
            s.add(Observation(token_id=token.id,
                              observed_ts=NOW + timedelta(seconds=offset),
                              source="dexscreener", price_usd=1e-7,
                              liquidity_usd=liquidity))
    s.commit()
    return s


# ------------------------------------------------------------ the label
def test_a_raydium_pool_marks_graduation_and_a_pumpfun_pool_does_not():
    with session_with([("grad", "raydium", []),
                       ("still-on-curve", "pumpfun", []),
                       ("no-pool", None, [])]) as s:
        graduated, counts = graduation_markers(s)
        addresses = {t.address for t in s.query(Token).all()
                     if t.id in graduated}
    assert addresses == {"grad"}
    assert counts["raydium"] == 1
    assert counts["pumpfun"] == 1


def test_the_dex_values_are_returned_so_the_label_can_be_checked():
    """Every wrong conclusion here came from a number printed without its
    provenance. The caller must be able to see what it labelled and why."""
    with session_with([("a", "raydium clmm", []), ("b", "meteora", [])]) as s:
        _, counts = graduation_markers(s)
    assert set(counts) == {"raydium clmm", "meteora"}


# ------------------------------------------------- no hindsight in a feature
def test_a_feature_recorded_after_the_window_is_not_visible_at_entry():
    """Liquidity an hour in is partly a RESULT of surviving, so using it to
    predict survival measures nothing but itself."""
    with session_with([("late", None, [(3600, 50_000.0)])]) as s:
        token = s.query(Token).one()
        assert early_features(s, token, window_s=300) == {}


def test_a_feature_recorded_inside_the_window_is_visible():
    with session_with([("early", None, [(120, 8_000.0)])]) as s:
        token = s.query(Token).one()
        assert early_features(s, token, window_s=300)["liquidity_usd"] == 8_000.0


def test_the_first_value_wins_not_the_best_one():
    """Taking the maximum inside the window would quietly reintroduce the
    hindsight the window exists to remove."""
    with session_with([("rising", None, [(10, 100.0), (200, 90_000.0)])]) as s:
        token = s.query(Token).one()
        assert early_features(s, token, window_s=300)["liquidity_usd"] == 100.0


# ------------------------------------------------------ what counts as signal
def test_overlapping_intervals_are_not_a_signal():
    """The guard that matters. On a few hundred tokens a 2-point gap between
    buckets is well inside sampling noise."""
    rates = tercile_rates([(float(i), i % 10 == 0) for i in range(300)])
    assert not separates(rates)


def test_a_planted_strong_signal_is_detected():
    """The negative result has to be falsifiable: if a real separation exists,
    this must find it, or the test above proves nothing."""
    low = [(float(i), False) for i in range(100)]
    high = [(float(1000 + i), True) for i in range(100)]
    rates = tercile_rates(low + high)
    assert separates(rates)


def test_every_bucket_carries_its_own_count_and_interval():
    rates = tercile_rates([(float(i), i % 3 == 0) for i in range(90)])
    assert len(rates) == 3
    for row in rates:
        assert row["n"] == 30
        assert row["low"] <= row["rate"] <= row["high"]


def test_too_few_values_produce_no_buckets_rather_than_fake_ones():
    assert tercile_rates([(1.0, True), (2.0, False)]) == []
