"""Market-data provider interface.

Providers are read-only: they never place orders. Trading lives behind
``app.execution.base.ExchangeAdapter``, which *uses* a provider for its
market-data methods. Splitting the two means a data outage can never be
confused with an execution outage, and the paper engine can run on real prices
while touching no credentialed endpoint.
"""

from __future__ import annotations

import abc
from decimal import Decimal

import pandas as pd

from app.models.market import OrderBook, Ticker
from app.models.trading import SymbolSpec


class MarketDataProvider(abc.ABC):
    """Read-only market data source."""

    name: str = "base"

    @abc.abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        since: int | None = None,
    ) -> pd.DataFrame:
        """Return a UTC-indexed OHLCV frame, oldest first, newest candle last.

        Implementations must drop the currently-forming candle: acting on an
        unfinished bar is a look-ahead bug in live trading exactly as it is in a
        backtest.
        """

    @abc.abstractmethod
    def fetch_ticker(self, symbol: str) -> Ticker:
        ...

    def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        """Optional: providers without depth data return ``None``."""
        return None

    def fetch_funding_rate(self, symbol: str) -> Decimal | None:
        """Only meaningful for perpetuals; spot providers return ``None``."""
        return None

    def fetch_open_interest(self, symbol: str) -> Decimal | None:
        return None

    def fetch_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        """Exchange trading rules (tick size, lot step, min notional)."""
        return None

    def close(self) -> None:  # pragma: no cover - trivial
        return None
