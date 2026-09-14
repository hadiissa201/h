"""Exchange adapter interface.

One interface, two implementations: a paper engine that simulates fills against
real prices, and a live adapter that talks to an exchange. The rest of the system
only ever holds an ``ExchangeAdapter``, so adding a venue means adding a class —
not touching the risk engine or the workflows.

Two invariants the interface enforces on every implementation:

* ``mode`` states plainly whether real money can move;
* ``supports_short`` is False for spot adapters, and the risk engine refuses
  short proposals rather than discovering the problem at the exchange.
"""

from __future__ import annotations

import abc

import pandas as pd

from app.models.enums import TradingModeEnum
from app.models.market import OrderBook, Ticker
from app.models.trading import Balance, Order, OrderRequest, Position, SymbolSpec


class ExchangeAdapter(abc.ABC):
    name: str = "base"
    mode: TradingModeEnum = TradingModeEnum.PAPER
    supports_short: bool = False

    # ----------------------------------------------------------- market data
    @abc.abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
        ...

    @abc.abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        ...

    @abc.abstractmethod
    def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        ...

    @abc.abstractmethod
    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        ...

    # --------------------------------------------------------------- account
    @abc.abstractmethod
    def get_balance(self) -> list[Balance]:
        ...

    @abc.abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[Position]:
        ...

    # ---------------------------------------------------------------- orders
    @abc.abstractmethod
    def create_order(self, request: OrderRequest) -> Order:
        """Place an order.

        Implementations must be idempotent on ``client_order_id``: re-sending the
        same id returns the existing order instead of creating a second one, so a
        retried n8n HTTP node cannot double a position.
        """

    @abc.abstractmethod
    def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        ...

    @abc.abstractmethod
    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        ...

    @abc.abstractmethod
    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        ...

    # --------------------------------------------------------------- health
    def health_check(self) -> tuple[bool, str]:
        return True, "ok"

    def close(self) -> None:  # pragma: no cover - trivial
        return None
