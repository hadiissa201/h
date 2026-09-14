"""Paper trading engine.

A spot exchange simulator that keeps real books: quote cash and base holdings
are tracked per currency, orders and fills are persisted, and every fill pays
spread, slippage and fees through the same ``fill_model`` the backtester uses.

What it deliberately simulates:
  * balances per currency, with insufficient-balance rejections
  * market orders (immediate), limit orders (rest until the market trades through)
  * spread, slippage with size impact, taker/maker fees
  * optional partial fills
  * exchange trading rules: lot step, min quantity, min notional
  * idempotency on ``client_order_id``

What it cannot simulate, and the docs say so plainly: queue position, real
order-book depth, exchange outages, rate limits, and the fact that your own order
moves the market.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pandas as pd

from app.core.config import Settings, get_settings
from app.core.errors import (
    InsufficientBalanceError,
    OrderRejectedError,
    ValidationError,
)
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.core.numeric import ZERO, quantize_price, quantize_quantity, round_money, to_decimal
from app.data.service import MarketDataService
from app.database.repositories import ExecutionRepository, new_id
from app.execution.base import ExchangeAdapter
from app.execution.fill_model import CostModel, simulate_limit_fill, simulate_market_fill
from app.models.enums import (
    OrderStatus,
    OrderType,
    Side,
    TradingModeEnum,
)
from app.portfolio.mapping import position_from_record
from app.models.market import OrderBook, Ticker
from app.models.trading import (
    Balance,
    Fill,
    Order,
    OrderRequest,
    Position,
    SymbolSpec,
)
from app.utils.time import utcnow

logger = get_logger(__name__)


class PaperExchangeAdapter(ExchangeAdapter):
    name = "paper"
    mode = TradingModeEnum.PAPER
    supports_short = False

    def __init__(
        self,
        repository: ExecutionRepository,
        market_data: MarketDataService,
        settings: Settings | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self.market_data = market_data
        self.cost_model = cost_model or CostModel.from_settings(self.settings)
        self.quote_currency = self.settings.quote_currency

    # ----------------------------------------------------------- market data
    def get_ticker(self, symbol: str) -> Ticker:
        return self.market_data.get_ticker(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        return self.market_data.get_candles(symbol, timeframe, limit=limit)

    def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook | None:
        return self.market_data.get_order_book(symbol, limit=limit)

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.market_data.get_symbol_spec(symbol)

    # --------------------------------------------------------------- account
    def ensure_funded(self) -> Balance:
        """Create the starting quote balance on first use."""
        record = self.repository.get_balance(self.quote_currency)
        if record is None:
            record = self.repository.upsert_balance(
                self.quote_currency,
                round_money(to_decimal(self.settings.paper_starting_balance), 8),
            )
            log_event(
                logger,
                EventType.BOT_STARTED,
                message="paper account funded",
                currency=self.quote_currency,
                amount=float(record.free),
            )
        return Balance(currency=record.currency, free=record.free, locked=record.locked)

    def get_balance(self) -> list[Balance]:
        self.ensure_funded()
        return [
            Balance(currency=record.currency, free=record.free, locked=record.locked)
            for record in self.repository.list_balances()
        ]

    def get_cash(self) -> Decimal:
        self.ensure_funded()
        record = self.repository.get_balance(self.quote_currency)
        return record.free if record else ZERO

    def get_base_balance(self, symbol: str) -> Decimal:
        base = _base_currency(symbol)
        record = self.repository.get_balance(base)
        return record.free if record else ZERO

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        return [
            position_from_record(record)
            for record in self.repository.open_positions(symbol)
        ]

    # ---------------------------------------------------------------- orders
    def create_order(self, request: OrderRequest) -> Order:
        self.ensure_funded()

        if request.client_order_id:
            existing = self.repository.get_order_by_client_id(request.client_order_id)
            if existing is not None:
                # Idempotent replay: an n8n retry must never double a position.
                log_event(
                    logger,
                    EventType.ORDER_DUPLICATE_IGNORED,
                    symbol=request.symbol,
                    order_id=existing.id,
                    client_order_id=request.client_order_id,
                    reason="client_order_id already used",
                )
                return self._order_from_record(existing)

        spec = self.get_symbol_spec(request.symbol)
        quantity = quantize_quantity(request.quantity, spec.quantity_step)
        if quantity <= ZERO:
            return self._reject(
                request,
                f"quantity {request.quantity} rounds to zero at step {spec.quantity_step}",
            )
        if spec.min_quantity and quantity < spec.min_quantity:
            return self._reject(
                request,
                f"quantity {quantity} below exchange minimum {spec.min_quantity}",
            )

        if request.type is OrderType.MARKET:
            return self._execute_market_order(request, quantity, spec)
        if request.type is OrderType.LIMIT:
            return self._place_limit_order(request, quantity, spec)
        if request.type is OrderType.STOP_MARKET:
            return self._place_stop_order(request, quantity, spec)
        return self._reject(request, f"unsupported order type {request.type}")

    def _execute_market_order(
        self, request: OrderRequest, quantity: Decimal, spec: SymbolSpec
    ) -> Order:
        ticker = self.get_ticker(request.symbol)
        reference = ticker.mid
        if reference <= ZERO:
            return self._reject(request, "no valid reference price")

        notional_estimate = reference * quantity
        if spec.min_notional and notional_estimate < spec.min_notional:
            return self._reject(
                request,
                f"notional {notional_estimate:.2f} below exchange minimum "
                f"{spec.min_notional}",
            )

        fill_quantity = self._maybe_partial(request, quantity)
        simulated = simulate_market_fill(
            reference, fill_quantity, request.side, self.cost_model
        )

        if request.side is Side.BUY:
            required = simulated.notional + simulated.fee
            cash = self.get_cash()
            if cash < required:
                return self._reject(
                    request,
                    f"insufficient {self.quote_currency}: need {required:.8f}, have {cash:.8f}",
                    error=InsufficientBalanceError,
                )
        else:
            available = self.get_base_balance(request.symbol)
            if available < fill_quantity:
                return self._reject(
                    request,
                    f"insufficient {_base_currency(request.symbol)}: need "
                    f"{fill_quantity:.8f}, have {available:.8f}",
                    error=InsufficientBalanceError,
                )

        now = utcnow()
        order_id = new_id("po_")
        record = self.repository.add_order(
            {
                "id": order_id,
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "type": request.type.value,
                "status": OrderStatus.NEW.value,
                "quantity": quantity,
                "filled_quantity": ZERO,
                "price": None,
                "stop_price": None,
                "created_at": now,
                "updated_at": now,
                "mode": self.mode.value,
                "position_id": request.metadata.get("position_id"),
                "risk_approval_id": request.metadata.get("risk_approval_id"),
                "meta": dict(request.metadata),
            }
        )
        log_event(
            logger,
            EventType.ORDER_CREATED,
            symbol=request.symbol,
            order_id=order_id,
            side=request.side.value,
            order_type=request.type.value,
            quantity=float(quantity),
            price=float(reference),
            mode=self.mode.value,
        )
        self._settle_fill(record, simulated, request.side)
        return self._order_from_record(record)

    def _place_limit_order(
        self, request: OrderRequest, quantity: Decimal, spec: SymbolSpec
    ) -> Order:
        if request.price is None or request.price <= ZERO:
            return self._reject(request, "limit order requires a positive price")
        price = quantize_price(request.price, spec.price_tick)
        if spec.min_notional and price * quantity < spec.min_notional:
            return self._reject(
                request,
                f"notional {price * quantity:.2f} below minimum {spec.min_notional}",
            )
        if request.side is Side.BUY:
            required = price * quantity * Decimal("1.001")
            cash = self.get_cash()
            if cash < required:
                return self._reject(
                    request,
                    f"insufficient {self.quote_currency} to reserve for limit order",
                    error=InsufficientBalanceError,
                )
        now = utcnow()
        order_id = new_id("po_")
        record = self.repository.add_order(
            {
                "id": order_id,
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "type": request.type.value,
                "status": OrderStatus.NEW.value,
                "quantity": quantity,
                "filled_quantity": ZERO,
                "price": price,
                "created_at": now,
                "updated_at": now,
                "mode": self.mode.value,
                "position_id": request.metadata.get("position_id"),
                "risk_approval_id": request.metadata.get("risk_approval_id"),
                "meta": dict(request.metadata),
            }
        )
        log_event(
            logger,
            EventType.ORDER_CREATED,
            symbol=request.symbol,
            order_id=order_id,
            side=request.side.value,
            order_type="LIMIT",
            quantity=float(quantity),
            price=float(price),
            mode=self.mode.value,
        )
        # Cross immediately if the market is already through the limit.
        self.process_resting_orders(request.symbol)
        return self._order_from_record(self.repository.get_order(order_id))

    def _place_stop_order(
        self, request: OrderRequest, quantity: Decimal, spec: SymbolSpec
    ) -> Order:
        if request.stop_price is None or request.stop_price <= ZERO:
            return self._reject(request, "stop order requires a positive stop_price")
        now = utcnow()
        order_id = new_id("po_")
        record = self.repository.add_order(
            {
                "id": order_id,
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "type": request.type.value,
                "status": OrderStatus.NEW.value,
                "quantity": quantity,
                "filled_quantity": ZERO,
                "stop_price": quantize_price(request.stop_price, spec.price_tick),
                "created_at": now,
                "updated_at": now,
                "mode": self.mode.value,
                "position_id": request.metadata.get("position_id"),
                "risk_approval_id": request.metadata.get("risk_approval_id"),
                "meta": dict(request.metadata),
            }
        )
        self.process_resting_orders(request.symbol)
        return self._order_from_record(self.repository.get_order(order_id))

    def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        record = self.repository.get_order(order_id)
        if record is None:
            raise ValidationError(f"unknown order {order_id}")
        if record.status not in (
            OrderStatus.NEW.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ):
            raise OrderRejectedError(
                f"order {order_id} is {record.status} and cannot be cancelled"
            )
        record.status = OrderStatus.CANCELLED.value
        record.updated_at = utcnow()
        log_event(
            logger,
            EventType.ORDER_CANCELLED,
            symbol=record.symbol,
            order_id=order_id,
            mode=self.mode.value,
        )
        return self._order_from_record(record)

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        record = self.repository.get_order(order_id)
        if record is None:
            raise ValidationError(f"unknown order {order_id}")
        return self._order_from_record(record)

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            self._order_from_record(record)
            for record in self.repository.open_orders(symbol)
        ]

    # ------------------------------------------------------- resting orders
    def process_resting_orders(
        self,
        symbol: str,
        high: Decimal | None = None,
        low: Decimal | None = None,
        last: Decimal | None = None,
    ) -> list[Order]:
        """Fill resting limit/stop orders that the market has traded through.

        Called by the position-monitoring workflow. When ``high``/``low`` are not
        supplied the current ticker is used, which means intrabar wicks between
        polls can be missed — an honest limitation of polling rather than
        streaming, and documented as such.
        """
        orders = self.repository.open_orders(symbol)
        if not orders:
            return []
        if last is None or high is None or low is None:
            ticker = self.get_ticker(symbol)
            last = last or ticker.last
            high = high or last
            low = low or last

        filled: list[Order] = []
        for record in orders:
            side = Side(record.side)
            remaining = record.quantity - record.filled_quantity
            if remaining <= ZERO:
                continue
            if record.type == OrderType.LIMIT.value and record.price is not None:
                crossed = (
                    low <= record.price if side is Side.BUY else high >= record.price
                )
                if not crossed:
                    continue
                simulated = simulate_limit_fill(
                    record.price, remaining, side, self.cost_model
                )
            elif record.type == OrderType.STOP_MARKET.value and record.stop_price is not None:
                triggered = (
                    low <= record.stop_price
                    if side is Side.SELL
                    else high >= record.stop_price
                )
                if not triggered:
                    continue
                # Stops pay the spread and can gap: reference the worse of the
                # stop level and where the market actually traded.
                reference = (
                    min(record.stop_price, last)
                    if side is Side.SELL
                    else max(record.stop_price, last)
                )
                simulated = simulate_market_fill(
                    reference, remaining, side, self.cost_model
                )
            else:
                continue

            if side is Side.BUY and self.get_cash() < simulated.notional + simulated.fee:
                record.status = OrderStatus.REJECTED.value
                record.reject_reason = "insufficient balance at fill time"
                record.updated_at = utcnow()
                continue
            if side is Side.SELL and self.get_base_balance(symbol) < simulated.quantity:
                record.status = OrderStatus.REJECTED.value
                record.reject_reason = "insufficient base balance at fill time"
                record.updated_at = utcnow()
                continue

            self._settle_fill(record, simulated, side)
            filled.append(self._order_from_record(record))
        return filled

    # ------------------------------------------------------------- internals
    def _settle_fill(self, record, simulated, side: Side) -> None:
        """Move balances, persist the fill, and update the order."""
        fill_id = new_id("fi_")
        now = utcnow()

        if side is Side.BUY:
            cash = self.get_cash()
            self.repository.upsert_balance(
                self.quote_currency,
                round_money(cash - simulated.notional - simulated.fee, 8),
            )
            base = _base_currency(record.symbol)
            base_balance = self.repository.get_balance(base)
            current = base_balance.free if base_balance else ZERO
            self.repository.upsert_balance(
                base, round_money(current + simulated.quantity, 8)
            )
        else:
            base = _base_currency(record.symbol)
            base_balance = self.repository.get_balance(base)
            current = base_balance.free if base_balance else ZERO
            self.repository.upsert_balance(
                base, round_money(current - simulated.quantity, 8)
            )
            cash = self.get_cash()
            self.repository.upsert_balance(
                self.quote_currency,
                round_money(cash + simulated.notional - simulated.fee, 8),
            )

        self.repository.add_fill(
            {
                "id": fill_id,
                "order_id": record.id,
                "symbol": record.symbol,
                "side": side.value,
                "price": simulated.price,
                "quantity": simulated.quantity,
                "fee": simulated.fee,
                "fee_currency": self.quote_currency,
                "filled_at": now,
                "is_maker": simulated.is_maker,
                "slippage_bps": simulated.slippage_bps,
                "mode": self.mode.value,
            }
        )

        previous_filled = record.filled_quantity or ZERO
        previous_notional = (record.average_fill_price or ZERO) * previous_filled
        total_filled = previous_filled + simulated.quantity
        record.filled_quantity = round_money(total_filled, 8)
        record.average_fill_price = round_money(
            (previous_notional + simulated.notional) / total_filled, 8
        )
        record.fee_paid = round_money((record.fee_paid or ZERO) + simulated.fee, 8)
        record.status = (
            OrderStatus.FILLED.value
            if record.filled_quantity >= record.quantity
            else OrderStatus.PARTIALLY_FILLED.value
        )
        record.updated_at = now

        log_event(
            logger,
            EventType.ORDER_FILLED
            if record.status == OrderStatus.FILLED.value
            else EventType.ORDER_PARTIALLY_FILLED,
            symbol=record.symbol,
            order_id=record.id,
            fill_id=fill_id,
            side=side.value,
            price=float(simulated.price),
            quantity=float(simulated.quantity),
            fee=float(simulated.fee),
            slippage_bps=float(simulated.slippage_bps),
            filled_quantity=float(record.filled_quantity),
            order_quantity=float(record.quantity),
            mode=self.mode.value,
        )

    def _maybe_partial(self, request: OrderRequest, quantity: Decimal) -> Decimal:
        probability = self.settings.paper_partial_fill_probability
        if probability <= 0:
            return quantity
        seed = request.client_order_id or f"{request.symbol}:{quantity}"
        rng = random.Random(seed)  # deterministic per order id, so tests repeat
        if rng.random() >= probability:
            return quantity
        spec = self.get_symbol_spec(request.symbol)
        fraction = Decimal(str(round(rng.uniform(0.3, 0.9), 4)))
        partial = quantize_quantity(quantity * fraction, spec.quantity_step)
        return partial if partial > ZERO else quantity

    def _reject(
        self,
        request: OrderRequest,
        reason: str,
        error: type[Exception] = OrderRejectedError,
    ) -> Order:
        """Persist the rejection (so repeated failures can trip the kill switch)
        and then raise."""
        now = utcnow()
        order_id = new_id("po_")
        self.repository.add_order(
            {
                "id": order_id,
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "type": request.type.value,
                "status": OrderStatus.REJECTED.value,
                "quantity": request.quantity,
                "filled_quantity": ZERO,
                "price": request.price,
                "stop_price": request.stop_price,
                "created_at": now,
                "updated_at": now,
                "reject_reason": reason,
                "mode": self.mode.value,
                "risk_approval_id": request.metadata.get("risk_approval_id"),
                "meta": dict(request.metadata),
            }
        )
        log_event(
            logger,
            EventType.ORDER_REJECTED,
            symbol=request.symbol,
            order_id=order_id,
            side=request.side.value,
            quantity=float(request.quantity),
            reason=reason,
            mode=self.mode.value,
        )
        raise error(reason, symbol=request.symbol, order_id=order_id)

    def _order_from_record(self, record) -> Order:
        fills = [
            Fill(
                fill_id=fill.id,
                order_id=fill.order_id,
                symbol=fill.symbol,
                side=Side(fill.side),
                price=fill.price,
                quantity=fill.quantity,
                fee=fill.fee,
                fee_currency=fill.fee_currency,
                timestamp=fill.filled_at,
                is_maker=fill.is_maker,
                slippage_bps=fill.slippage_bps,
            )
            for fill in self.repository.fills_for_order(record.id)
        ]
        return Order(
            order_id=record.id,
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            side=Side(record.side),
            type=OrderType(record.type),
            status=OrderStatus(record.status),
            quantity=record.quantity,
            filled_quantity=record.filled_quantity,
            price=record.price,
            stop_price=record.stop_price,
            average_fill_price=record.average_fill_price,
            fee_paid=record.fee_paid,
            created_at=record.created_at,
            updated_at=record.updated_at,
            reject_reason=record.reject_reason,
            fills=fills,
            mode=TradingModeEnum(record.mode),
            metadata=record.meta or {},
        )

    def health_check(self) -> tuple[bool, str]:
        try:
            self.ensure_funded()
            return True, "paper engine ready"
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)


def _base_currency(symbol: str) -> str:
    base, _, _ = symbol.upper().partition("/")
    return base
