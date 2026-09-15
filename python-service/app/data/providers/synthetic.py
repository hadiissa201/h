"""Deterministic synthetic market data.

**This is a development and test fixture, not a market.** It exists so the whole
pipeline can be exercised without network access and so tests are reproducible.
Any performance figure produced on synthetic candles is a statement about this
generator, never about a trading edge — results carry ``source="synthetic"`` and
the backtester attaches an explicit warning.

Design notes that matter:

* One 5-minute base path per symbol per day, resampled up to every requested
  timeframe. Multi-timeframe features therefore describe *the same* market, which
  a per-timeframe random walk would not.
* Each bar opens exactly where the previous bar closed (continuous 24/7 market),
  and OHLC ordering is guaranteed by construction.
* Regime blocks cycle through trend / range / high-volatility phases so
  strategies and the regime classifier meet more than one world.
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

# (name, per-bar drift, per-bar volatility) applied in blocks.
_REGIME_BLOCKS = (
    ("uptrend", 0.00012, 0.0022),
    ("range", 0.00000, 0.0014),
    ("downtrend", -0.00011, 0.0026),
    ("high_vol_range", 0.00002, 0.0050),
    ("slow_grind_up", 0.00005, 0.0010),
)
_BLOCK_BARS = 2_880  # 10 days of 5m bars per regime block

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
        frame = self._resampled(symbol, timeframe)
        if end is not None:
            end_ts = pd.Timestamp(end)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            frame = frame.loc[frame.index <= end_ts]
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
    def _resampled(self, symbol: str, timeframe: str) -> pd.DataFrame:
        seconds = timeframe_to_seconds(timeframe)
        if seconds % BASE_SECONDS != 0:
            raise ValueError(
                f"synthetic provider supports multiples of {BASE_TIMEFRAME}; "
                f"got {timeframe!r}"
            )
        base = self._base_frame(symbol)
        if seconds == BASE_SECONDS:
            return base
        resampled = (
            base.resample(f"{seconds}s", label="left", closed="left")
            .agg(_RESAMPLE_AGG)
            .dropna()
        )
        return resampled

    def _base_frame(self, symbol: str) -> pd.DataFrame:
        # Keyed by UTC day so a long-running process sees a stable path within a
        # day and a fresh one after midnight.
        anchor = int(floor_to_timeframe(utcnow(), "1d").timestamp())
        key = (symbol.upper(), anchor)
        cached = self._base_cache.get(key)
        if cached is None:
            cached = self._generate_base(symbol)
            self._base_cache = {key: cached}  # only ever keep the current day
        return cached

    def _generate_base(self, symbol: str) -> pd.DataFrame:
        spec = self._spec(symbol)
        count = self.base_bars
        sub_steps = 4
        rng = np.random.default_rng(spec.seed)

        block_index = (np.arange(count) // _BLOCK_BARS) % len(_REGIME_BLOCKS)
        drifts = np.array([block[1] for block in _REGIME_BLOCKS])[block_index]
        vols = np.array([block[2] for block in _REGIME_BLOCKS])[block_index]

        shocks = rng.normal(
            loc=(drifts / sub_steps)[:, None],
            scale=(vols / np.sqrt(sub_steps))[:, None],
            size=(count, sub_steps),
        )
        log_path = np.log(spec.base_price) + np.cumsum(shocks.reshape(-1))
        prices = np.exp(log_path).reshape(count, sub_steps)

        closes = prices[:, -1]
        opens = np.empty(count)
        opens[0] = spec.base_price
        opens[1:] = closes[:-1]  # continuous market: open == previous close
        highs = np.maximum(prices.max(axis=1), opens)
        lows = np.minimum(prices.min(axis=1), opens)

        # Volume: heavy-tailed baseline, coupled to the size of the move, plus
        # occasional participation spikes. Real crypto volume regularly prints
        # 2-5x its 20-bar average on breakouts; an over-smooth fixture silently
        # starves every volume-confirmed strategy of signals.
        moves = np.abs(closes / opens - 1.0)
        typical_volume = 5_000_000.0 / spec.base_price
        baseline = rng.lognormal(mean=np.log(typical_volume), sigma=0.55, size=count)
        move_coupling = 1.0 + 40.0 * moves
        spikes = np.where(
            rng.random(count) < 0.04, rng.uniform(2.0, 5.0, size=count), 1.0
        )
        volumes = np.maximum(1.0, baseline * move_coupling * spikes)

        end = floor_to_timeframe(utcnow(), BASE_TIMEFRAME)
        index = pd.date_range(end=end, periods=count, freq=f"{BASE_SECONDS}s", tz="UTC")
        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=index,
        )
        frame.index.name = "timestamp"
        return frame

    def _spec(self, symbol: str) -> _SeriesSpec:
        symbol = symbol.upper()
        # crc32, not hash(): str hashing is salted per process, which would make
        # a "deterministic" fixture differ between runs.
        digest = zlib.crc32(symbol.encode())
        base_price = self.base_prices.get(symbol, 50.0 + (digest % 5000) / 10.0)
        return _SeriesSpec(base_price=base_price, seed=self.seed + (digest % 10_000))
