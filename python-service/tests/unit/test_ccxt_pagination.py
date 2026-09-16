"""OHLCV paging against an exchange that caps page size.

Exchanges silently return fewer candles than asked for rather than erroring, so
a request for 5000 came back with ~800 and the backtester reported it as if the
full window had been loaded. A month of data was being presented as seven, with
nothing in the output to say so -- the kind of fault that makes every downstream
number quietly worthless.

A fake exchange is used rather than a live one: these must run offline and must
be able to assert the exact page boundaries.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import MarketDataError
from app.data.providers import ccxt_provider
from app.data.providers.ccxt_provider import CcxtMarketDataProvider

HOUR_MS = 3_600_000


def _current_hour_ms() -> int:
    now = pd.Timestamp.now(tz="UTC").floor("h")
    return int(now.timestamp() * 1000)


class FakeExchange:
    """Mimics a real exchange: caps page size, serves a finite recent history.

    The history ends at the current hour, because the provider computes its start
    cursor backwards from now. A fixture anchored to some fixed date in the past
    would sit entirely behind that cursor and return nothing.
    """

    def __init__(self, available: int, page_cap: int = 1000) -> None:
        self.available = available
        self.page_cap = page_cap
        self.calls: list[tuple[int | None, int | None]] = []
        self.end_ms = _current_hour_ms()
        self.start_ms = self.end_ms - (max(available, 1) - 1) * HOUR_MS

    def load_markets(self):
        return {}

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
        self.calls.append((since, limit))
        if self.available <= 0:
            return []
        if since is None:
            start_index = max(0, self.available - min(limit or self.page_cap, self.page_cap))
        else:
            start_index = max(0, (since - self.start_ms) // HOUR_MS)
        if start_index >= self.available:
            return []
        # The cap is the exchange's, and applies no matter what we asked for.
        count = min(limit or self.page_cap, self.page_cap, self.available - start_index)
        return [
            [
                self.start_ms + (start_index + offset) * HOUR_MS,
                100.0 + start_index + offset,  # open
                101.0 + start_index + offset,  # high
                99.0 + start_index + offset,   # low
                100.5 + start_index + offset,  # close
                10.0,                          # volume
            ]
            for offset in range(count)
        ]


@pytest.fixture
def provider(monkeypatch):
    def build(available: int, page_cap: int = 1000) -> tuple[CcxtMarketDataProvider, FakeExchange]:
        fake = FakeExchange(available=available, page_cap=page_cap)
        instance = CcxtMarketDataProvider(exchange_id="binance")
        monkeypatch.setattr(instance, "_exchange", fake, raising=False)
        monkeypatch.setattr(instance, "_load_markets", lambda: None)
        return instance, fake

    return build


def test_a_request_within_one_page_makes_a_single_call(provider):
    instance, fake = provider(available=5_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=300)

    assert len(fake.calls) == 1
    assert len(frame) == 300


def test_a_large_request_pages_until_satisfied(provider):
    """The regression: 5000 asked for, ~800 delivered, nobody told."""
    instance, fake = provider(available=20_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=5_000)

    assert len(frame) == 5_000, f"asked for 5000 bars, got {len(frame)}"
    assert len(fake.calls) > 1, "should have paged"
    assert all(call[1] <= 1000 for call in fake.calls), "page size must respect the cap"


def test_paged_candles_are_continuous_and_ordered(provider):
    instance, _ = provider(available=20_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=3_000)

    assert frame.index.is_monotonic_increasing
    assert not frame.index.has_duplicates
    gaps = frame.index.to_series().diff().dropna().unique()
    assert list(gaps) == [pd.Timedelta(hours=1)], f"paging introduced gaps: {gaps}"


def test_overlapping_pages_do_not_duplicate_candles(provider):
    """Some exchanges re-send the boundary candle; dedupe must absorb it."""

    class OverlappingExchange(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
            page = super().fetch_ohlcv(symbol, timeframe, since, limit)
            return [page[0], *page] if page else page  # repeat the first row

    instance, _ = provider(available=20_000)
    instance._exchange = OverlappingExchange(available=20_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=2_500)

    assert not frame.index.has_duplicates
    assert frame.index.is_monotonic_increasing


def test_a_short_history_returns_what_exists_without_hanging(provider):
    """A recently listed symbol has less history than requested."""
    instance, fake = provider(available=1_500)

    frame = instance.fetch_ohlcv("NEW/USDT", "1h", limit=5_000)

    assert 0 < len(frame) <= 1_500
    assert len(fake.calls) < 50, "must stop once the exchange stops advancing"


def test_the_forming_candle_is_dropped(provider):
    instance, fake = provider(available=2_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=1_500)
    newest_available = pd.to_datetime(fake.end_ms, unit="ms", utc=True)

    assert frame.index[-1] < newest_available, "published the still-forming candle"


def test_an_empty_response_is_an_error_not_an_empty_frame(provider):
    instance, _ = provider(available=0)

    with pytest.raises(MarketDataError):
        instance.fetch_ohlcv("GHOST/USDT", "1h", limit=100)


def test_paging_cannot_loop_forever(provider, monkeypatch):
    """A stuck exchange must hit the page cap, not hang the service."""

    class StuckExchange(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
            self.calls.append((since, limit))
            # Always returns the same single candle: never advances.
            return [[self.start_ms, 100.0, 101.0, 99.0, 100.5, 10.0]]

    instance, _ = provider(available=10_000)
    stuck = StuckExchange(available=10_000)
    instance._exchange = stuck
    monkeypatch.setattr(ccxt_provider, "_MAX_PAGES", 5)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=5_000)

    assert len(stuck.calls) <= 6
    assert len(frame) >= 1


# ------------------------------------------------------- request-count budget
# Paging is only worth having if it cannot cost more requests than it needs. The
# first version issued speculative follow-ups whenever a page came back short,
# which is the normal case: a live tick asking for 268 bars turned into a stream
# of tiny calls, each paying the rate limiter, until Binance began refusing them.
# One tick took 70 minutes and two symbols failed outright.


def test_a_live_sized_request_costs_exactly_one_call(provider):
    """The loop's own request, every few minutes, on every symbol."""
    instance, fake = provider(available=20_000)

    instance.fetch_ohlcv("BTC/USDT", "1h", limit=268)

    assert len(fake.calls) == 1, (
        f"a single-page request made {len(fake.calls)} calls; "
        "this is what got the IP rate-limited"
    )


def test_a_short_page_ends_paging_immediately(provider):
    """Fewer rows than asked for means the exchange has no more. Stop."""

    class ShortPageExchange(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
            page = super().fetch_ohlcv(symbol, timeframe, since, limit)
            return page[:-1] if len(page) > 1 else page  # always one short

    instance, _ = provider(available=20_000)
    short = ShortPageExchange(available=20_000)
    instance._exchange = short

    instance.fetch_ohlcv("BTC/USDT", "1h", limit=5_000)

    assert len(short.calls) <= 6, (
        f"short pages triggered {len(short.calls)} calls; must stop, not chase"
    )


def test_a_large_request_costs_about_what_it_must(provider):
    instance, fake = provider(available=40_000)

    frame = instance.fetch_ohlcv("BTC/USDT", "1h", limit=15_000)

    assert len(frame) == 15_000
    # 15001 bars at 1000 per page is 16 calls; allow a little slack, not 200.
    assert len(fake.calls) <= 20, f"{len(fake.calls)} calls for 15000 bars"
