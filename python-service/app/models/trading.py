"""Order, fill, position, balance and portfolio schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    ExitReason,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    Side,
    TimeInForce,
    TradingModeEnum,
)


class SymbolSpec(BaseModel):
    """Exchange trading rules for one market.

    Sizing respects these: an order below ``min_notional`` or off the lot grid
    gets rejected by a real exchange, so the paper engine rejects it too.
    """

    symbol: str
    base: str
    quote: str
    price_tick: Decimal = Decimal("0.01")
    quantity_step: Decimal = Decimal("0.00001")
    min_quantity: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("10")
    maker_fee_bps: Decimal = Decimal("10")
    taker_fee_bps: Decimal = Decimal("10")
    active: bool = True


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    type: OrderType = OrderType.MARKET
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str | None = Field(
        default=None,
        description="Idempotency key. Re-sending the same id never creates a "
        "second order — n8n retries are therefore safe.",
    )
    reduce_only: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class Fill(BaseModel):
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_currency: str
    timestamp: datetime
    is_maker: bool = False
    slippage_bps: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


class Order(BaseModel):
    order_id: str
    client_order_id: str | None = None
    symbol: str
    side: Side
    type: OrderType
    status: OrderStatus
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    price: Decimal | None = None
    stop_price: Decimal | None = None
    average_fill_price: Decimal | None = None
    fee_paid: Decimal = Decimal("0")
    created_at: datetime
    updated_at: datetime
    reject_reason: str | None = None
    fills: list[Fill] = Field(default_factory=list)
    mode: TradingModeEnum = TradingModeEnum.PAPER
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class PositionExitPlan(BaseModel):
    stop_loss: Decimal
    take_profit: Decimal | None = None
    trailing_stop_atr_multiple: float | None = None
    trailing_stop_price: Decimal | None = None
    breakeven_at_r: float | None = None
    breakeven_applied: bool = False
    partial_exit_at_r: float | None = None
    partial_exit_fraction: float | None = None
    partial_exit_done: bool = False
    time_stop_bars: int | None = None
    invalidation_condition: str = ""


class Position(BaseModel):
    position_id: str
    symbol: str
    side: PositionSide
    status: PositionStatus
    quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: ExitReason | None = None
    initial_quantity: Decimal | None = None
    initial_risk_amount: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    exit_plan: PositionExitPlan | None = None
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    strategy: str | None = None
    regime: str | None = None
    ai_decision_id: str | None = None
    risk_approval_id: str | None = None
    mark_price: Decimal | None = None
    max_favorable_price: Decimal | None = None
    max_adverse_price: Decimal | None = None
    bars_held: int = 0
    mode: TradingModeEnum = TradingModeEnum.PAPER
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        price = self.mark_price or self.entry_price
        return price * self.quantity

    @property
    def cost_basis(self) -> Decimal:
        return self.entry_price * self.quantity

    def unrealized_pnl(self, mark: Decimal | None = None) -> Decimal:
        price = mark or self.mark_price
        if price is None or self.status is PositionStatus.CLOSED:
            return Decimal("0")
        if self.side is PositionSide.LONG:
            return (price - self.entry_price) * self.quantity
        return (self.entry_price - price) * self.quantity

    def r_multiple(self, mark: Decimal | None = None) -> Decimal | None:
        """Current profit expressed in units of the initially risked amount."""
        if not self.initial_risk_amount or self.initial_risk_amount == 0:
            return None
        pnl = (
            self.realized_pnl
            if self.status is PositionStatus.CLOSED
            else self.unrealized_pnl(mark)
        )
        return pnl / self.initial_risk_amount


class Balance(BaseModel):
    currency: str
    free: Decimal
    locked: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    mode: TradingModeEnum
    quote_currency: str
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    starting_equity: Decimal
    peak_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_pnl_pct: Decimal
    daily_pnl: Decimal
    daily_pnl_pct: Decimal
    drawdown_pct: Decimal
    exposure_pct: Decimal
    open_positions: int
    fees_paid: Decimal = Decimal("0")
    balances: list[Balance] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)


class ClosePositionRequest(BaseModel):
    reason: ExitReason = ExitReason.MANUAL
    fraction: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    price: Decimal | None = Field(
        default=None,
        description="Mark price to close at. Omitted means the engine fetches "
        "the current market price itself.",
    )
    note: str | None = None
