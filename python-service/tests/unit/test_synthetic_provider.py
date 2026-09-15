"""The synthetic feed must behave like a live feed that keeps running.

Two faults motivated this file, and they were two faces of one mistake. The path
used to be laid down as a fixed-length sequence ending at "now", so its values
were a function of *when you asked* rather than of the timestamps themselves:

1. Advancing the clock by a single 5-minute bar rewrote all of history -- BTC
   closes on the overlapping bars moved by more than $1,000.
2. Hiding (1) behind a per-UTC-day cache froze the newest candle at process
   start, so ``POST /market/collect`` in a long-running service reported
   ``STALE_DATA: last candle ... is >300s beyond one bar`` within minutes.

The fix indexes every bar on a fixed epoch grid. These tests pin both halves: the
feed advances with the clock, AND the past never changes when it does.

Nothing here relaxes staleness validation. The tests below run the real
``validate_candles`` with the production 300s limit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import app.data.providers.synthetic as synthetic_module
from app.data.providers.synthetic import SyntheticMarketDataProvider
from app.data.validation import validate_candles
from app.utils.time import staleness_seconds

TIMEFRAMES = ("5m", "15m", "1h", "4h")
MAX_STALENESS = 300  # production default; never raised in this file


@pytest.fixture
def clock(monkeypatch):
    """A controllable UTC clock for the provider module."""

    state = {"now": pd.Timestamp("2026-09-15 20:57:00", tz="UTC")}

    def _now():
        return state["now"].to_pydatetime()

    monkeypatch.setattr(synthetic_module, "utcnow", _now)

    class Clock:
        @property
        def now(self):
            return state["now"]

        def set(self, timestamp: str) -> None:
            state["now"] = pd.Timestamp(timestamp, tz="UTC")

        def advance(self, **kwargs) -> None:
            state["now"] = state["now"] + pd.Timedelta(**kwargs)

    return Clock()


# ------------------------------------------------------- 1. freshness on fetch
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_a_fresh_fetch_passes_real_staleness_validation(clock, timeframe):
    provider = SyntheticMarketDataProvider(seed=7)
    frame = provider.fetch_ohlcv("BTC/USDT", timeframe, limit=300)

    report = validate_candles(
        frame,
        symbol="BTC/USDT",
        timeframe=timeframe,
        max_staleness_seconds=MAX_STALENESS,
        now=clock.now.to_pydatetime(),
    )

    assert report.is_tradeable, report.summary()
    assert not [issue for issue in report.issues if issue.code == "STALE_DATA"]
    assert report.staleness_seconds == 0


@pytest.mark.parametrize(
    "moment",
    [
        "2026-09-15 00:00:00",  # bar boundary
        "2026-09-15 00:04:59",  # just inside the first 5m bar
        "2026-09-15 13:59:59",  # last second of an hour
        "2026-09-15 23:59:59",  # last second of a UTC day
        "2026-02-28 12:34:56",
        "2027-01-01 00:00:01",  # across a year boundary
    ],
)
def test_no_wall_clock_position_produces_a_stale_feed(clock, moment):
    """The old day-anchored cache made lateness depend on where in the day you
    started. Freshness must not depend on the wall clock at all."""
    clock.set(moment)
    provider = SyntheticMarketDataProvider(seed=7)

    for timeframe in TIMEFRAMES:
        frame = provider.fetch_ohlcv("BTC/USDT", timeframe, limit=200)
        stale_by = staleness_seconds(
            frame.index[-1].to_pydatetime(), timeframe, now=clock.now.to_pydatetime()
        )
        assert stale_by <= MAX_STALENESS, f"{timeframe} stale by {stale_by}s at {moment}"


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_the_forming_bar_is_never_published(clock, timeframe):
    """A bar is only published once the whole period is in the past."""
    provider = SyntheticMarketDataProvider(seed=7)
    frame = provider.fetch_ohlcv("BTC/USDT", timeframe, limit=50)

    last_open = frame.index[-1]
    period = pd.Timedelta(seconds=synthetic_module.timeframe_to_seconds(timeframe))
    assert last_open + period <= clock.now, "published a bar that is still forming"


# --------------------------------------------- 2. the feed advances with time
def test_a_long_running_provider_keeps_up_with_the_clock(clock):
    """The reported bug: one cached provider, hours of uptime, 1h candles.

    Before the fix the newest candle stayed pinned at process start and staleness
    grew without bound (420s after ten minutes, 10,620s after three hours).
    """
    provider = SyntheticMarketDataProvider(seed=7)
    seen: list[pd.Timestamp] = []

    for _ in range(24):  # twelve hours in half-hour steps
        frame = provider.fetch_ohlcv("BTC/USDT", "1h", limit=300)
        last = frame.index[-1]
        seen.append(last)

        stale_by = staleness_seconds(
            last.to_pydatetime(), "1h", now=clock.now.to_pydatetime()
        )
        assert stale_by <= MAX_STALENESS, (
            f"feed went stale by {stale_by}s at {clock.now}; the cache is not advancing"
        )
        clock.advance(minutes=30)

    assert seen == sorted(seen), "candle timestamps went backwards"
    assert seen[-1] > seen[0], "the feed never advanced over twelve hours"
    assert len(set(seen)) >= 11, f"expected ~12 distinct hourly bars, saw {len(set(seen))}"


def test_the_five_minute_feed_advances_bar_by_bar(clock):
    provider = SyntheticMarketDataProvider(seed=7)

    first = provider.fetch_ohlcv("BTC/USDT", "5m", limit=10).index[-1]
    clock.advance(minutes=5)
    second = provider.fetch_ohlcv("BTC/USDT", "5m", limit=10).index[-1]

    assert second - first == pd.Timedelta(minutes=5)


def test_advancing_across_utc_midnight_does_not_reset_the_series(clock):
    """The old cache was keyed on the UTC day, so midnight replaced the world."""
    clock.set("2026-09-15 23:40:00")
    provider = SyntheticMarketDataProvider(seed=7)
    before = provider.fetch_ohlcv("BTC/USDT", "1h", limit=300)

    clock.set("2026-09-16 00:20:00")
    after = provider.fetch_ohlcv("BTC/USDT", "1h", limit=300)

    overlap = before.index.intersection(after.index)
    assert len(overlap) > 250
    pd.testing.assert_series_equal(
        before.loc[overlap, "close"], after.loc[overlap, "close"]
    )


# ------------------------------------------ 3. determinism and backtest safety
def test_advancing_the_clock_never_rewrites_published_candles(clock):
    """The core guarantee. This is the assertion that fails on the old code."""
    provider = SyntheticMarketDataProvider(seed=7)
    baseline = provider.fetch_ohlcv("BTC/USDT", "1h", limit=300)

    for _ in range(6):
        clock.advance(minutes=5)
        # Check the long-lived provider AND a freshly built one. A frozen cache
        # would make the first comparison pass for the wrong reason -- which is
        # exactly how the history-rewrite bug stayed hidden behind the old
        # per-day cache.
        for current in (
            provider.fetch_ohlcv("BTC/USDT", "1h", limit=300),
            SyntheticMarketDataProvider(seed=7).fetch_ohlcv("BTC/USDT", "1h", limit=300),
        ):
            overlap = baseline.index.intersection(current.index)
            assert len(overlap) > 290
            for column in ("open", "high", "low", "close", "volume"):
                pd.testing.assert_series_equal(
                    baseline.loc[overlap, column],
                    current.loc[overlap, column],
                    check_names=False,
                )


def test_a_cached_provider_and_a_brand_new_one_agree(clock):
    """Caching must be a speed optimisation, never a source of truth."""
    warm = SyntheticMarketDataProvider(seed=7)
    warm.fetch_ohlcv("BTC/USDT", "1h", limit=300)
    clock.advance(minutes=35)

    from_cache_path = warm.fetch_ohlcv("BTC/USDT", "1h", limit=300)
    from_scratch = SyntheticMarketDataProvider(seed=7).fetch_ohlcv(
        "BTC/USDT", "1h", limit=300
    )

    pd.testing.assert_frame_equal(from_cache_path, from_scratch)


def test_the_same_seed_reproduces_the_same_series(clock):
    a = SyntheticMarketDataProvider(seed=7).generate("BTC/USDT", "1h", 500)
    b = SyntheticMarketDataProvider(seed=7).generate("BTC/USDT", "1h", 500)
    pd.testing.assert_frame_equal(a, b)


def test_different_seeds_and_symbols_produce_different_series(clock):
    a = SyntheticMarketDataProvider(seed=7).generate("BTC/USDT", "1h", 500)
    b = SyntheticMarketDataProvider(seed=99).generate("BTC/USDT", "1h", 500)
    c = SyntheticMarketDataProvider(seed=7).generate("ETH/USDT", "1h", 500)

    assert not np.allclose(a["close"].to_numpy(), b["close"].to_numpy())
    assert not np.allclose(
        a["close"].to_numpy() / a["close"].iloc[0],
        c["close"].to_numpy() / c["close"].iloc[0],
    )


def test_a_short_request_is_a_suffix_of_a_long_one(clock):
    """What the backtester's prefix/suffix reasoning depends on."""
    provider = SyntheticMarketDataProvider(seed=7)
    long_run = provider.generate("BTC/USDT", "1h", 2000)
    short_run = provider.generate("BTC/USDT", "1h", 400)

    pd.testing.assert_frame_equal(long_run.tail(400), short_run)


def test_generate_with_an_end_only_trims_and_does_not_reshape(clock):
    provider = SyntheticMarketDataProvider(seed=7)
    full = provider.generate("BTC/USDT", "1h", 800)
    cut_at = full.index[500]
    head = full.loc[:cut_at]

    # `bars` counts back from `end`, so asking for exactly the head length must
    # return that head unchanged -- trimming the window must not reshape the path.
    trimmed = provider.generate("BTC/USDT", "1h", len(head), end=cut_at)
    pd.testing.assert_frame_equal(head, trimmed)

    # And a shorter request ending at the same point is a suffix of it.
    shorter = provider.generate("BTC/USDT", "1h", 100, end=cut_at)
    pd.testing.assert_frame_equal(head.tail(100), shorter)


@pytest.mark.parametrize("timeframe", ("15m", "1h", "4h"))
def test_higher_timeframes_describe_the_same_market(clock, timeframe):
    provider = SyntheticMarketDataProvider(seed=7)
    base = provider.fetch_ohlcv("BTC/USDT", "5m", limit=20_000)
    higher = provider.fetch_ohlcv("BTC/USDT", timeframe, limit=40)

    period = pd.Timedelta(seconds=synthetic_module.timeframe_to_seconds(timeframe))
    checked = 0
    for opened_at, row in higher.iterrows():
        chunk = base.loc[(base.index >= opened_at) & (base.index < opened_at + period)]
        if len(chunk) != period // pd.Timedelta(minutes=5):
            continue
        checked += 1
        assert chunk["open"].iloc[0] == pytest.approx(row["open"])
        assert chunk["close"].iloc[-1] == pytest.approx(row["close"])
        assert chunk["high"].max() == pytest.approx(row["high"])
        assert chunk["low"].min() == pytest.approx(row["low"])
    assert checked >= 20


def test_candles_are_continuous_and_well_formed(clock):
    frame = SyntheticMarketDataProvider(seed=7).fetch_ohlcv(
        "BTC/USDT", "5m", limit=5_000
    )

    assert (frame["high"] >= frame["low"]).all()
    assert (frame["high"] >= frame[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (frame["low"] <= frame[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (frame[["open", "high", "low", "close"]] > 0).all().all()
    assert (frame["volume"] > 0).all()
    # Continuous 24/7 market: no gaps, and each bar opens where the last closed.
    assert (frame.index.to_series().diff().dropna() == pd.Timedelta("5min")).all()
    assert np.allclose(frame["open"].to_numpy()[1:], frame["close"].to_numpy()[:-1])


@pytest.mark.parametrize(
    "moment", ["2020-06-01", "2024-06-01", "2026-06-01", "2032-06-01", "2040-06-01"]
)
def test_the_price_level_stays_realistic_for_decades(clock, moment):
    """Guards the drift neutralisation.

    On an absolute grid a residual per-bar drift compounds forever. With the
    original +0.00008/bar the 62,000 base would reach the billions within a few
    years, which quietly breaks notional and precision handling everywhere.
    """
    clock.set(f"{moment} 12:00:00")
    frame = SyntheticMarketDataProvider(seed=7).fetch_ohlcv(
        "BTC/USDT", "1h", limit=500
    )
    base_price = 62_000.0

    assert (frame["close"] > base_price / 8).all()
    assert (frame["close"] < base_price * 8).all()
