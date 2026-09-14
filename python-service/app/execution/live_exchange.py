"""Live exchange adapter (ccxt) — structure in place, disarmed by default.

This class exists so that going live later is a configuration and review step
rather than a rewrite. It is **not** a finished production execution path, and
the docs say so: before it is used for real money it needs the items in
``docs/LIVE_TRADING_CHECKLIST.md`` completed, most importantly
order-reconciliation-after-restart, partial-fill handling against real fill
streams, and exchange-specific rate-limit and error handling.

Safety properties enforced here:

* every mutating call re-checks ``settings.live_trading_armed`` at call time, so
  flipping a single env var is not enough and a stale adapter cannot outlive a
  disarm;
* the ccxt client is only created with credentials when armed — otherwise it is
  built without keys and can physically only read public data;
* ``reduce_only``/short orders are refused: this is a spot, long-only system.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

from app.core.config import Settings, get_settings
from app.core.errors import (
    ExchangeError,
    LiveTradingBlockedError,
    OrderRejectedError,
    ValidationError,
)
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.data.providers.ccxt_provider import CcxtMarketDataProvider
from app.execution.base import ExchangeAdapter
from app.models.enums import (
    OrderStatus,
    OrderType,
    Side,
    TradingModeEnum,
)
from app.models.market import OrderBook, Ticker
from app.models.trading import Balance, Fill, Order, OrderRequest, Position, SymbolSpec
from app.utils.time import utcnow

logger = get_logger(__name__)

_STATUS_MAP = {
    "open": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


class LiveExchangeAdapter(ExchangeAdapter):
    name = "live"
    mode = TradingModeEnum.LIVE
    supports_short = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = f"live:{self.settings.exchange_id}"
        self._provider = CcxtMarketDataProvider(
            exchange_id=self.settings.exchange_id,
            timeout_seconds=self.settings.market_data_timeout_seconds,
        )
        self._private: Any | None = None

    # ------------------------------------------------------------- arming
    def _assert_armed(self, action: str) -> None:
        if not self.settings.live_trading_armed:
            blockers = self.settings.live_mode_blockers()
            log_event(
                logger,
                EventType.LIVE_MODE_BLOCKED,
                message=f"blocked live action: {action}",
                action=action,
                blockers=blockers,
                level=40,
            )
            raise LiveTradingBlockedError(
                f"live trading is not armed; refusing to {action}",
                blockers=blockers,
            )

    def _client(self) -> Any:
        """Credentialed ccxt client, created lazily and only when armed."""
        self._assert_armed("create a credentialed exchange client")
        if self._private is None:
            import ccxt

            exchange_cls = getattr(ccxt, self.settings.exchange_id, None)
            if exchange_cls is None:
                raise ExchangeError(
                    f"unknown exchange id {self.settings.exchange_id!r}"
                )
            self._private = exchange_cls(
                {
                    "apiKey": self.settings.exchange_api_key,
                    "secret": self.settings.exchange_api_secret,
                    "password": self.settings.exchange_password or None,
                    "enableRateLimit": True,
                    "timeout": int(self.settings.market_data_timeout_seconds * 1000),
                    "options": {"defaultType": "spot"},
                }
            )
        return self._private

    # --------------------------------------------------------- market data
    def get_ticker(self, symbol: str) -> Ticker:
        return self._provider.fetch_ticker(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        return self._provider.fetch_ohlcv(symbol, timeframe, limit=limit)

    def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        return self._provider.fetch_order_book(symbol, limit=limit)

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = self._provider.fetch_symbol_spec(symbol)
        if spec is None:
            raise ExchangeError(f"symbol {symbol} not listed on {self.settings.exchange_id}")
        return spec

    # -------------------------------------------------------------- account
    def get_balance(self) -> list[Balance]:
        client = self._client()
        try:
            raw = client.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"fetch_balance failed: {exc}") from exc
        balances: list[Balance] = []
        for currency, entry in (raw.get("total") or {}).items():
            total = Decimal(str(entry or 0))
            if total <= 0:
                continue
            free = Decimal(str((raw.get("free") or {}).get(currency, 0) or 0))
            balances.append(
                Balance(currency=currency, free=free, locked=max(Decimal("0"), total - free))
            )
        return balances

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Spot has no exchange-side positions.

        Our own ``positions`` table is the source of truth for stops and strategy
        attribution; reconciling it against exchange balances on startup is a
        prerequisite on the live checklist, not something to fake here.
        """
        raise NotImplementedError(
            "live position state must be reconciled from the positions table and "
            "exchange balances; see docs/LIVE_TRADING_CHECKLIST.md"
        )

    # --------------------------------------------------------------- orders
    def create_order(self, request: OrderRequest) -> Order:
        self._assert_armed("place an order")
        if request.side is Side.SELL and request.metadata.get("open_short"):
            raise ValidationError("short selling is not supported (spot, long only)")
        client = self._client()
        params: dict[str, Any] = {}
        if request.client_order_id:
            params["clientOrderId"] = request.client_order_id
        try:
            if request.type is OrderType.MARKET:
                raw = client.create_order(
                    request.symbol,
                    "market",
                    request.side.value.lower(),
                    float(request.quantity),
                    None,
                    params,
                )
            elif request.type is OrderType.LIMIT:
                if request.price is None:
                    raise ValidationError("limit order requires a price")
                raw = client.create_order(
                    request.symbol,
                    "limit",
                    request.side.value.lower(),
                    float(request.quantity),
                    float(request.price),
                    params,
                )
            else:
                raise ValidationError(
                    f"order type {request.type} is not wired for live trading yet"
                )
        except Exception as exc:
            log_event(
                logger,
                EventType.ORDER_REJECTED,
                symbol=request.symbol,
                reason=str(exc),
                mode=self.mode.value,
                level=40,
            )
            raise OrderRejectedError(f"exchange rejected order: {exc}") from exc
        return self._order_from_ccxt(raw, request)

    def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        self._assert_armed("cancel an order")
        client = self._client()
        try:
            raw = client.cancel_order(order_id, symbol)
        except Exception as exc:
            raise ExchangeError(f"cancel_order failed: {exc}") from exc
        return self._order_from_ccxt(raw)

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        client = self._client()
        try:
            raw = client.fetch_order(order_id, symbol)
        except Exception as exc:
            raise ExchangeError(f"fetch_order failed: {exc}") from exc
        return self._order_from_ccxt(raw)

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        client = self._client()
        try:
            raw_orders = client.fetch_open_orders(symbol)
        except Exception as exc:
            raise ExchangeError(f"fetch_open_orders failed: {exc}") from exc
        return [self._order_from_ccxt(raw) for raw in raw_orders]

    # ------------------------------------------------------------ internals
    def _order_from_ccxt(
        self, raw: dict[str, Any], request: OrderRequest | None = None
    ) -> Order:
        status = _STATUS_MAP.get(str(raw.get("status") or "").lower(), OrderStatus.NEW)
        filled = Decimal(str(raw.get("filled") or 0))
        quantity = Decimal(str(raw.get("amount") or (request.quantity if request else 0)))
        if status is OrderStatus.NEW and 0 < filled < quantity:
            status = OrderStatus.PARTIALLY_FILLED
        timestamp = raw.get("timestamp")
        created = (
            pd.to_datetime(timestamp, unit="ms", utc=True).to_pydatetime()
            if timestamp
            else utcnow()
        )
        fills = [
            Fill(
                fill_id=str(trade.get("id") or f"{raw.get('id')}-{index}"),
                order_id=str(raw.get("id")),
                symbol=str(raw.get("symbol")),
                side=Side(str(trade.get("side", raw.get("side", "buy"))).upper()),
                price=Decimal(str(trade.get("price") or 0)),
                quantity=Decimal(str(trade.get("amount") or 0)),
                fee=Decimal(str(((trade.get("fee") or {}).get("cost")) or 0)),
                fee_currency=str(((trade.get("fee") or {}).get("currency")) or ""),
                timestamp=created,
                is_maker=bool(trade.get("takerOrMaker") == "maker"),
            )
            for index, trade in enumerate(raw.get("trades") or [])
        ]
        return Order(
            order_id=str(raw.get("id")),
            client_order_id=raw.get("clientOrderId"),
            symbol=str(raw.get("symbol") or (request.symbol if request else "")),
            side=Side(str(raw.get("side", "buy")).upper()),
            type=OrderType(str(raw.get("type", "market")).upper()),
            status=status,
            quantity=quantity,
            filled_quantity=filled,
            price=Decimal(str(raw["price"])) if raw.get("price") else None,
            average_fill_price=Decimal(str(raw["average"])) if raw.get("average") else None,
            fee_paid=Decimal(str(((raw.get("fee") or {}).get("cost")) or 0)),
            created_at=created,
            updated_at=utcnow(),
            fills=fills,
            mode=TradingModeEnum.LIVE,
            metadata={"raw_status": raw.get("status")},
        )

    def health_check(self) -> tuple[bool, str]:
        if not self.settings.live_trading_armed:
            return False, "live trading not armed: " + "; ".join(
                self.settings.live_mode_blockers()
            )
        try:
            self._client().fetch_balance()
            return True, "live credentials valid"
        except Exception as exc:
            return False, str(exc)

    def close(self) -> None:
        self._provider.close()
        client = self._private
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass
