"""Deterministic synthetic market data.

**This is a development and test fixture, not a market.** It exists so the whole
pipeline can be exercised without network access and so tests are reproducible.
Any performance figure produced on synthetic candles is a statement about this
generator, never about a trading edge — results carry ``source="synthetic"`` and
the backtester attaches an explicit warning.

Design notes that matter:

* One 5-minute base path per symbol, resampled up to every requested timeframe.
  Multi-timeframe features therefore describe *the same* market, which a
  per-timeframe random walk would not.
* Each bar opens exactly where the previous bar closed (continuous 24/7 market),
  and OHLC ordering is guaranteed by construction.
* Regime blocks cycle through trend / range / high-volatility phases so
  strategies and the regime classifier meet more than one world.

**The path is a pure function of absolute time.** The bar at a given UTC
timestamp always holds the same values, no matter when it is asked for or how
many bars are requested around it. That is what lets a long-running process keep
producing *fresh* candles as the clock advances without rewriting its own past.

It was not always so. The original version laid a fixed-length random sequence
down ending at "now", so advancing the clock by one 5-minute bar slid the whole
series and changed every historical price (closes moved by >$1,000 on BTC). To
hide that, the base frame was cached per UTC *day* -- which froze the newest
candle at process start and made the feed read as ``STALE_DATA`` within minutes.
Both faults have the same cause, and absolute indexing is the fix for both:

* ``_absolute_index`` maps a timestamp to a bar number on a fixed epoch grid;
* regime blocks are keyed on that absolute index, not on array position;
* each block draws from its own seeded generator, so any window can be produced
  without generating everything before it, and neighbouring windows agree exactly;
* the level is anchored by subtracting a *trailing* mean (see ``_trailing_mean``),
  which is causal, so anchoring cannot reach forward and destabilise the past.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from app.data.providers.base import MarketDataProvider
from app.models.market import OrderBook, OrderBookLevel, Ticker
from app.models.trading import SymbolSpec
from app.utils.time import floor_to_timeframe, timeframe_to_seconds, utcnow

BASE_TIMEFRAME = "5m"
BASE_SECONDS = 300
# 400 days of 5m bars — enough for 1500 4h bars, cheap to generate vectorised.
BASE_BARS = 115_200

# Fixed window end for reproducible backtests.
#
# ``generate()`` defaults to the most recent bars, which is right for the live
# path and wrong for a backtest: the same request returns a different window
# depending on what time you ran it, so two runs minutes apart can disagree and
# results cannot be compared across days at all. Backtests therefore anchor here.
# Any fixed point inside the generated range works; this one is arbitrary and
# must stay put, because moving it changes every synthetic backtest ever run.
BACKTEST_WINDOW_END = pd.Timestamp("2025-06-23", tz="UTC")

# Fixed grid origin. Every bar's identity is its offset from here, so the value
# at a timestamp never depends on when it was generated. Changing this constant
# regenerates every synthetic series, so don't.
EPOCH = pd.Timestamp("2020-01-01", tz="UTC")

# (name, per-bar drift, per-bar volatility) applied in blocks.
#
# The drifts sum to zero over one full cycle on purpose. They did not originally
# (+0.00008/bar), which was harmless while the series was always regenerated at a
# fixed length, but on an absolute grid a residual drift compounds without limit:
# by 2030 it would put BTC in the billions. A fixture that has to stay usable for
# years must be level-stationary; the per-block trends are unchanged in character.
_REGIME_BLOCKS = (
    ("uptrend", 0.000104, 0.0022),
    ("range", -0.000016, 0.0014),
    ("downtrend", -0.000126, 0.0026),
    ("high_vol_range", 0.000004, 0.0050),
    ("slow_grind_up", 0.000034, 0.0010),
)
_BLOCK_BARS = 2_880  # 10 days of 5m bars per regime block
_SUB_STEPS = 4  # intra-bar path points, used to build a true high/low

_BASE_PRICES = {
    "BTC/USDT": 62_000.0,
    "ETH/USDT": 3_100.0,
    "SOL/USDT": 145.0,
}

_RESAMPLE_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


@dataclass(frozen=True)
class _SeriesSpec:
    base_price: float
    seed: int


class SyntheticMarketDataProvider(MarketDataProvider):
    name = "synthetic"

    def __init__(
        self,
        seed: int = 7,
        base_prices: dict[str, float] | None = None,
        base_bars: int = BASE_BARS,
    ) -> None:
        self.seed = seed
        self.base_prices = {**_BASE_PRICES, **(base_prices or {})}
        self.base_bars = base_bars
        # Warm-up length for the level anchor. Equal to the visible history, so a
        # returned bar's anchor never depends on where generation happened to start.
        self.anchor_bars = base_bars
        self._base_cache: dict[tuple[str, int], pd.DataFrame] = {}

    # ------------------------------------------------------------------ public
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        since: int | None = None,
    ) -> pd.DataFrame:
        frame = self._resampled(symbol, timeframe)
        # Drop the bar still forming, mirroring a real feed.
        closed = frame.iloc[:-1]
        return closed.tail(limit).copy()

    def generate(
        self,
        symbol: str,
        timeframe: str,
        bars: int,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Cache-free generation for backtests/tests.

        ``end`` only trims the window; it does not reshape the path, so a shorter
        request is always a suffix of a longer one.
        """
        end_index: int | None = None
        if end is not None:
            end_ts = pd.Timestamp(end)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            # Generate the window that ENDS there rather than slicing the recent
            # frame. Slicing only worked while `end` happened to fall inside the
            # last `base_bars`; an older anchor silently produced an empty frame.
            # Absolute indexing means any window is directly reachable.
            #
            # Extend to the LAST base bar of that period, not its first. `end`
            # names a bar by its opening timestamp, so stopping there would leave
            # the final bucket holding a single 5m bar and report a partial
            # candle as a complete one -- an hour's high and low collapsed to its
            # first five minutes.
            seconds = timeframe_to_seconds(timeframe)
            last_base_bar = end_ts + pd.Timedelta(seconds=seconds - BASE_SECONDS)
            end_index = _absolute_index(last_base_bar)
        frame = self._resampled(symbol, timeframe, end_index=end_index)
        return frame.tail(bars).copy()

    def current_price(self, symbol: str) -> float:
        """Latest price including the forming bar."""
        return float(self._base_frame(symbol)["close"].iloc[-1])

    def fetch_ticker(self, symbol: str) -> Ticker:
        base = self._base_frame(symbol)
        last_price = float(base["close"].iloc[-1])
        last = Decimal(str(round(last_price, 6)))
        half_spread = (last * Decimal("0.0002")).quantize(Decimal("0.000001"))
        # 24h == 288 five-minute bars.
        day = base.tail(288)
        first_close = Decimal(str(round(float(day["close"].iloc[0]), 6)))
        quote_volume = Decimal(
            str(round(float((day["close"] * day["volume"]).sum()), 2))
        )
        return Ticker(
            symbol=symbol.upper(),
            timestamp=utcnow(),
            last=last,
            bid=last - half_spread,
            ask=last + half_spread,
            quote_volume_24h=quote_volume,
            price_change_pct_24h=(
                (last - first_close) / first_close if first_close else Decimal("0")
            ),
        )

    def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        ticker = self.fetch_ticker(symbol)
        mid = ticker.mid
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for level in range(limit):
            offset = mid * Decimal("0.0002") * Decimal(level + 1)
            size = (Decimal("50000") / mid) / Decimal(level + 1)
            bids.append(OrderBookLevel(price=mid - offset, amount=size))
            asks.append(OrderBookLevel(price=mid + offset, amount=size))
        return OrderBook(
            symbol=symbol.upper(), timestamp=utcnow(), bids=bids, asks=asks
        )

    def fetch_symbol_spec(self, symbol: str) -> SymbolSpec:
        symbol = symbol.upper()
        base, _, quote = symbol.partition("/")
        price = self._spec(symbol).base_price
        return SymbolSpec(
            symbol=symbol,
            base=base,
            quote=quote or "USDT",
            price_tick=Decimal("0.01") if price > 10 else Decimal("0.0001"),
            quantity_step=Decimal("0.00001"),
            min_quantity=Decimal("0.00001"),
            min_notional=Decimal("10"),
            maker_fee_bps=Decimal("10"),
            taker_fee_bps=Decimal("10"),
        )

    # --------------------------------------------------------------- internals
    def _resampled(
        self, symbol: str, timeframe: str, end_index: int | None = None
    ) -> pd.DataFrame:
        seconds = timeframe_to_seconds(timeframe)
        if seconds % BASE_SECONDS != 0:
            raise ValueError(
                f"synthetic provider supports multiples of {BASE_TIMEFRAME}; "
                f"got {timeframe!r}"
            )
        base = self._base_frame(symbol, end_index=end_index)
        if seconds == BASE_SECONDS:
            return base
        resampled = (
            base.resample(f"{seconds}s", label="left", closed="left")
            .agg(_RESAMPLE_AGG)
            .dropna()
        )
        return resampled

    def _base_frame(self, symbol: str, end_index: int | None = None) -> pd.DataFrame:
        """The 5m path ending at the newest *started* bar, or at ``end_index``.

        Keyed on that bar, so the feed advances with the clock. Regenerating is
        safe precisely because the path is absolute: the bars a previous call
        returned come back bit-identical, and only new ones are added.
        """
        if end_index is None:
            end_index = _absolute_index(floor_to_timeframe(utcnow(), BASE_TIMEFRAME))
        key = (symbol.upper(), end_index)
        cached = self._base_cache.get(key)
        if cached is None:
            cached = self._generate_base(symbol, end_index)
            self._base_cache = {key: cached}  # only ever keep the newest bar
        return cached

    def _generate_base(self, symbol: str, end_index: int) -> pd.DataFrame:
        """Build ``base_bars`` 5m candles ending at absolute bar ``end_index``."""
        spec = self._spec(symbol)
        count = self.base_bars

        # Warm-up ahead of the returned window so every returned bar sees a full
        # anchoring window (see _trailing_mean). Without it the oldest bars would
        # be anchored on a shorter window and would shift as the clock advanced.
        warmup = self.anchor_bars
        total = count + warmup
        start_index = end_index - total + 1

        shocks, vol_draws = _draw_range(spec, start_index, total)

        # Anchor the level. `walk` is a free random walk, so on an absolute grid
        # it wanders as sqrt(time) without bound; subtracting its own trailing
        # mean removes only that very-low-frequency wander. The subtracted term
        # moves by ~1e-5 per bar against a per-bar vol of ~2.5e-3, so local
        # dynamics -- returns, ATR, every indicator -- are untouched.
        walk = np.cumsum(shocks.reshape(-1)).reshape(total, _SUB_STEPS)
        anchor = _trailing_mean(walk[:, -1], warmup)
        log_path = np.log(spec.base_price) + walk - anchor[:, None]
        prices = np.exp(log_path)

        closes = prices[:, -1]
        opens = np.empty(total)
        opens[0] = float(np.exp(np.log(spec.base_price) + walk[0, 0] - anchor[0]))
        opens[1:] = closes[:-1]  # continuous market: open == previous close
        highs = np.maximum(prices.max(axis=1), opens)
        lows = np.minimum(prices.min(axis=1), opens)

        # Volume: heavy-tailed baseline, coupled to the size of the move, plus
        # occasional participation spikes. Real crypto volume regularly prints
        # 2-5x its 20-bar average on breakouts; an over-smooth fixture silently
        # starves every volume-confirmed strategy of signals.
        moves = np.abs(closes / opens - 1.0)
        typical_volume = 5_000_000.0 / spec.base_price
        baseline = np.exp(np.log(typical_volume) + 0.55 * vol_draws["baseline"])
        move_coupling = 1.0 + 40.0 * moves
        spikes = np.where(vol_draws["spike_u"] < 0.04, vol_draws["spike_size"], 1.0)
        volumes = np.maximum(1.0, baseline * move_coupling * spikes)

        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=_index_for_range(start_index, total),
        )
        frame.index.name = "timestamp"
        # Drop the warm-up: it exists only to give the anchor a full window.
        return frame.iloc[warmup:]

    def _spec(self, symbol: str) -> _SeriesSpec:
        symbol = symbol.upper()
        # crc32, not hash(): str hashing is salted per process, which would make
        # a "deterministic" fixture differ between runs.
        digest = zlib.crc32(symbol.encode())
        base_price = self.base_prices.get(symbol, 50.0 + (digest % 5000) / 10.0)
        return _SeriesSpec(base_price=base_price, seed=self.seed + (digest % 10_000))


# ------------------------------------------------------------------- absolute grid
def _absolute_index(moment) -> int:
    """Bar number of ``moment`` on the fixed 5m grid anchored at ``EPOCH``."""
    ts = pd.Timestamp(moment)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int((ts - EPOCH).total_seconds()) // BASE_SECONDS


def _index_for_range(start_index: int, count: int) -> pd.DatetimeIndex:
    start = EPOCH + pd.Timedelta(seconds=start_index * BASE_SECONDS)
    return pd.date_range(
        start=start, periods=count, freq=f"{BASE_SECONDS}s", tz="UTC"
    )


def _trailing_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Causal rolling mean; expanding until ``window`` samples exist.

    Causal matters here. A centred window would make a bar's value depend on bars
    after it, so the past would change every time the clock advanced -- exactly
    the fault this rewrite removes.
    """
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    positions = np.arange(len(values))
    lower = np.maximum(0, positions - window + 1)
    counts = positions - lower + 1
    return (cumulative[positions + 1] - cumulative[lower]) / counts


def _draw_range(
    spec: _SeriesSpec, start_index: int, count: int
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Draw shocks and volume components for an absolute index range.

    Each regime block owns independent generators seeded from ``(seed, block)``,
    so a window can be produced without generating everything before it and two
    overlapping windows agree exactly. Separate streams per component matter too:
    sharing one stream would make every draw depend on how many values earlier
    draws had consumed, which reintroduces length-dependence through the back door.
    """
    first_block = (start_index) // _BLOCK_BARS
    last_block = (start_index + count - 1) // _BLOCK_BARS

    shock_parts: list[np.ndarray] = []
    baseline_parts: list[np.ndarray] = []
    spike_u_parts: list[np.ndarray] = []
    spike_size_parts: list[np.ndarray] = []

    for block in range(first_block, last_block + 1):
        _, drift, vol = _REGIME_BLOCKS[block % len(_REGIME_BLOCKS)]
        # A window opening before EPOCH yields negative block numbers, and
        # SeedSequence rejects negative entropy. Wrapping keeps the seed distinct
        # per block (injective for any |block| < 2**31) and leaves every
        # non-negative block's value untouched.
        block_seed = block % (2**32)

        price_rng = np.random.default_rng([spec.seed, block_seed, 0])
        raw = price_rng.standard_normal((_BLOCK_BARS, _SUB_STEPS))
        shock_parts.append(drift / _SUB_STEPS + raw * (vol / np.sqrt(_SUB_STEPS)))

        volume_rng = np.random.default_rng([spec.seed, block_seed, 1])
        baseline_parts.append(volume_rng.standard_normal(_BLOCK_BARS))
        spike_u_parts.append(volume_rng.random(_BLOCK_BARS))
        spike_size_parts.append(volume_rng.uniform(2.0, 5.0, size=_BLOCK_BARS))

    offset = start_index - first_block * _BLOCK_BARS
    window = slice(offset, offset + count)
    return (
        np.concatenate(shock_parts)[window],
        {
            "baseline": np.concatenate(baseline_parts)[window],
            "spike_u": np.concatenate(spike_u_parts)[window],
            "spike_size": np.concatenate(spike_size_parts)[window],
        },
    )
