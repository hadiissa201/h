"""Execution service: the only path from a decision to a position.

Order of operations for every entry, with no way around it:

    risk approval -> validate & consume -> exchange order -> position record

``place_entry`` refuses to do anything without a valid, unconsumed, unexpired
approval whose fingerprint matches the order. That is what makes "the AI cannot
bypass risk management" a property of the code rather than a promise.

Exits are always allowed (reducing risk never needs permission) but are still
recorded, priced through the same cost model, and accounted into trades.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.errors import (
    BotHaltedError,
    ConflictError,
    NotFoundError,
    RiskRejectedError,
)
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.core.numeric import ZERO, quantize_quantity, round_money, safe_div
from app.database.repositories import (
    BotStateRepository,
    EventRepository,
    ExecutionRepository,
    PerformanceRepository,
    new_id,
)
from app.execution.base import ExchangeAdapter
from app.models.enums import (
    BotStatus,
    ExitReason,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    Side,
    SignalDirection,
)
from app.models.trading import Order, OrderRequest, Position
from app.portfolio.exit_rules import PositionState
from app.portfolio.mapping import position_from_record
from app.portfolio.service import PortfolioService
from app.risk.service import RiskService
from app.utils.time import start_of_utc_day, utcnow

logger = get_logger(__name__)


class ExecutionService:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        execution_repo: ExecutionRepository,
        performance_repo: PerformanceRepository,
        bot_repo: BotStateRepository,
        event_repo: EventRepository,
        portfolio: PortfolioService,
        risk: RiskService,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.exchange = exchange
        self.repo = execution_repo
        self.performance = performance_repo
        self.bot = bot_repo
        self.events = event_repo
        self.portfolio = portfolio
        self.risk = risk
        self.mode = execution_repo.mode

    # ------------------------------------------------------------------ entry
    def place_entry(
        self,
        *,
        approval_id: str,
        symbol: str | None = None,
        quantity: Decimal | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        ai_decision_id: str | None = None,
        exit_plan_overrides: dict | None = None,
        client_order_id: str | None = None,
    ) -> tuple[Order, Position]:
        state = self.bot.get()
        if state.status != BotStatus.RUNNING.value:
            raise BotHaltedError(
                f"bot is {state.status} ({state.halt_reason or 'no reason recorded'}); "
                "entries are blocked until a manual reset",
                halt_reason=state.halt_reason,
            )

        # Read the approval first so the order can be resolved from it; the
        # caller may omit symbol/quantity entirely and cannot widen either.
        record = self.risk.repo.get_by_approval(approval_id, for_update=True)
        if record is None:
            raise RiskRejectedError(f"risk approval {approval_id} not found")

        resolved_symbol = (symbol or record.symbol).upper()
        side = Side.BUY if record.direction == SignalDirection.BUY.value else Side.SELL
        approved_quantity = record.quantity or ZERO
        resolved_quantity = quantity if quantity is not None else approved_quantity

        spec = self.exchange.get_symbol_spec(resolved_symbol)
        resolved_quantity = quantize_quantity(resolved_quantity, spec.quantity_step)

        # Idempotent replay comes *before* approval validation. A retried n8n node
        # (first call succeeded, response lost) must get the original position
        # back rather than "approval already consumed" — the approval was consumed
        # by this very order.
        client_id = client_order_id or f"entry-{approval_id}"
        existing_order = self.repo.get_order_by_client_id(client_id)
        if (
            existing_order is not None
            and existing_order.position_id
            and existing_order.risk_approval_id == approval_id
        ):
            position_record = self.repo.get_position(existing_order.position_id)
            if position_record is not None:
                log_event(
                    logger,
                    EventType.ORDER_DUPLICATE_IGNORED,
                    symbol=resolved_symbol,
                    order_id=existing_order.id,
                    position_id=position_record.id,
                    approval_id=approval_id,
                    reason="entry already executed for this approval",
                )
                return (
                    self.exchange.get_order(existing_order.id),
                    position_from_record(position_record),
                )

        # The single gate: validated against the resolved order, every time.
        record, check = self.risk.validate_approval(
            approval_id,
            symbol=resolved_symbol,
            side=side.value,
            quantity=resolved_quantity,
        )
        if check is None or not check.valid:
            raise RiskRejectedError(
                check.reason if check else "risk approval could not be validated",
                approval_id=approval_id,
            )
        if side is Side.SELL:
            raise RiskRejectedError(
                "short entries are not supported in spot mode",
                approval_id=approval_id,
            )

        request = OrderRequest(
            symbol=resolved_symbol,
            side=side,
            type=OrderType.MARKET,
            quantity=resolved_quantity,
            client_order_id=client_id,
            metadata={
                "risk_approval_id": approval_id,
                "strategy": strategy or record.strategy,
                "ai_decision_id": ai_decision_id or record.ai_decision_id,
            },
        )
        order = self.exchange.create_order(request)
        if order.filled_quantity <= ZERO:
            raise ConflictError(
                f"entry order {order.order_id} did not fill", order_id=order.order_id
            )

        self.risk.consume_approval(approval_id, order.order_id)

        entry_price = order.average_fill_price or order.price
        stop_loss = record.stop_loss
        take_profit = record.take_profit
        risk_amount = round_money(
            abs(entry_price - stop_loss) * order.filled_quantity, 8
        )

        exit_plan = {
            "stop_loss": str(stop_loss),
            "take_profit": str(take_profit) if take_profit is not None else None,
            "invalidation_condition": (record.reasons or [""])[0]
            if isinstance(record.reasons, list)
            else "",
        }
        exit_plan.update(exit_plan_overrides or {})

        position_id = new_id("pos_")
        position_record = self.repo.add_position(
            {
                "id": position_id,
                "symbol": resolved_symbol,
                "side": PositionSide.LONG.value,
                "status": PositionStatus.OPEN.value,
                "quantity": order.filled_quantity,
                "initial_quantity": order.filled_quantity,
                "entry_price": entry_price,
                "opened_at": utcnow(),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "exit_plan": exit_plan,
                "initial_risk_amount": risk_amount,
                "realized_pnl": ZERO,
                "fees_paid": order.fee_paid,
                "mark_price": entry_price,
                "max_favorable_price": entry_price,
                "max_adverse_price": entry_price,
                "strategy": strategy or record.strategy,
                "regime": regime or record.regime,
                "ai_decision_id": ai_decision_id or record.ai_decision_id,
                "risk_approval_id": approval_id,
                "mode": self.mode,
                "meta": {
                    "entry_order_id": order.order_id,
                    # Stored separately from fees_paid, which accumulates exit
                    # fees too — the entry fee must be amortised across exits
                    # exactly once.
                    "entry_fee": str(order.fee_paid),
                    "exits": [],
                },
            }
        )
        order_record = self.repo.get_order(order.order_id)
        if order_record is not None:
            order_record.position_id = position_id

        log_event(
            logger,
            EventType.POSITION_OPENED,
            symbol=resolved_symbol,
            position_id=position_id,
            order_id=order.order_id,
            strategy=position_record.strategy,
            regime=position_record.regime,
            price=float(entry_price),
            quantity=float(order.filled_quantity),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit) if take_profit else None,
            risk=float(safe_div(risk_amount, self.portfolio.equity())),
            risk_amount=float(risk_amount),
            approval_id=approval_id,
            ai_decision_id=position_record.ai_decision_id,
            mode=self.mode,
        )
        self.events.record(
            EventType.POSITION_OPENED,
            message=f"opened {resolved_symbol} long {order.filled_quantity} @ {entry_price}",
            symbol=resolved_symbol,
            strategy=position_record.strategy,
            trade_id=position_id,
            context={
                "order_id": order.order_id,
                "stop_loss": float(stop_loss),
                "take_profit": float(take_profit) if take_profit else None,
                "risk_amount": float(risk_amount),
                "approval_id": approval_id,
            },
        )
        self.portfolio.snapshot(persist=True)
        return order, position_from_record(position_record)

    # ------------------------------------------------------------------- exit
    def close_position(
        self,
        position_id: str,
        *,
        fraction: Decimal = Decimal("1"),
        reason: ExitReason = ExitReason.MANUAL,
        detail: str = "",
        client_order_id: str | None = None,
    ) -> tuple[Order, Position]:
        record = self.repo.get_position(position_id, for_update=True)
        if record is None:
            raise NotFoundError(f"position {position_id} not found")
        if record.status != PositionStatus.OPEN.value:
            raise ConflictError(f"position {position_id} is already closed")

        fraction = max(Decimal("0"), min(Decimal("1"), fraction))
        spec = self.exchange.get_symbol_spec(record.symbol)
        quantity = quantize_quantity(record.quantity * fraction, spec.quantity_step)
        if quantity <= ZERO:
            raise ConflictError(
                f"exit quantity for {position_id} rounds to zero at fraction {fraction}"
            )
        # Dust guard: if what would remain cannot be traded, close it all.
        remainder = record.quantity - quantity
        if remainder > ZERO and remainder * (record.mark_price or record.entry_price) < spec.min_notional:
            quantity = record.quantity
            fraction = Decimal("1")

        exits = list((record.meta or {}).get("exits", []))
        client_id = client_order_id or f"exit-{position_id}-{len(exits)}"
        request = OrderRequest(
            symbol=record.symbol,
            side=Side.SELL,
            type=OrderType.MARKET,
            quantity=quantity,
            client_order_id=client_id,
            metadata={
                "position_id": position_id,
                "exit_reason": str(reason),
                "detail": detail,
            },
        )
        order = self.exchange.create_order(request)
        if order.filled_quantity <= ZERO:
            raise ConflictError(f"exit order {order.order_id} did not fill")

        exit_price = order.average_fill_price or order.price
        filled = order.filled_quantity
        gross = round_money((exit_price - record.entry_price) * filled, 8)
        entry_fee = _decimal_or_none((record.meta or {}).get("entry_fee")) or ZERO
        entry_fee_share = round_money(
            safe_div(entry_fee, record.initial_quantity) * filled, 8
        )
        # Net P&L is what actually hit cash: price move minus BOTH fees. Omitting
        # the entry fee here would make realized P&L disagree with the balance.
        net = round_money(gross - order.fee_paid - entry_fee_share, 8)

        record.quantity = round_money(record.quantity - filled, 8)
        record.realized_pnl = round_money((record.realized_pnl or ZERO) + net, 8)
        record.fees_paid = round_money((record.fees_paid or ZERO) + order.fee_paid, 8)
        record.mark_price = exit_price
        exits.append(
            {
                "order_id": order.order_id,
                "price": str(exit_price),
                "quantity": str(filled),
                "reason": str(reason),
                "detail": detail,
                "at": utcnow().isoformat(),
                "pnl": str(net),
            }
        )
        meta = dict(record.meta or {})
        meta["exits"] = exits
        record.meta = meta

        fully_closed = record.quantity <= ZERO
        if fully_closed:
            record.status = PositionStatus.CLOSED.value
            record.closed_at = utcnow()
            record.exit_price = _weighted_exit_price(exits)
            record.exit_reason = str(reason)

        order_record = self.repo.get_order(order.order_id)
        if order_record is not None:
            order_record.position_id = position_id

        log_event(
            logger,
            EventType.POSITION_CLOSED if fully_closed else EventType.PARTIAL_EXIT,
            symbol=record.symbol,
            position_id=position_id,
            order_id=order.order_id,
            strategy=record.strategy,
            price=float(exit_price),
            quantity=float(filled),
            pnl=float(net),
            reason=f"{reason}: {detail}" if detail else str(reason),
            r_multiple=float(safe_div(record.realized_pnl, record.initial_risk_amount or ZERO)),
            mode=self.mode,
        )

        trade = None
        if fully_closed:
            trade = self._record_trade(record, reason)
        else:
            self.events.record(
                EventType.PARTIAL_EXIT,
                message=f"partial exit {filled} of {record.symbol} at {exit_price}",
                symbol=record.symbol,
                strategy=record.strategy,
                trade_id=position_id,
                context={"pnl": float(net), "reason": str(reason)},
            )

        self.portfolio.snapshot(persist=True)
        if trade is not None:
            self.risk.run_safety_checks()
        return order, position_from_record(record)

    def _record_trade(self, record, reason: ExitReason):  # noqa: ANN001
        entry_price = record.entry_price
        exit_price = record.exit_price or record.mark_price or entry_price
        quantity = record.initial_quantity
        gross = round_money((exit_price - entry_price) * quantity, 8)
        pnl = record.realized_pnl
        risk_amount = record.initial_risk_amount or ZERO
        holding_minutes = (
            (record.closed_at - record.opened_at).total_seconds() / 60.0
            if record.closed_at
            else None
        )

        mfe_r = mae_r = None
        if risk_amount > ZERO and quantity > ZERO:
            risk_per_unit = safe_div(risk_amount, quantity)
            if record.max_favorable_price is not None and risk_per_unit > ZERO:
                mfe_r = float(
                    safe_div(record.max_favorable_price - entry_price, risk_per_unit)
                )
            if record.max_adverse_price is not None and risk_per_unit > ZERO:
                mae_r = float(
                    safe_div(record.max_adverse_price - entry_price, risk_per_unit)
                )

        trade = self.repo.add_trade(
            {
                "id": new_id("trd_"),
                "position_id": record.id,
                "symbol": record.symbol,
                "side": record.side,
                "strategy": record.strategy,
                "regime": record.regime,
                "entry_time": record.opened_at,
                "entry_price": entry_price,
                "exit_time": record.closed_at or utcnow(),
                "exit_price": exit_price,
                "quantity": quantity,
                "stop_loss": record.stop_loss,
                "take_profit": record.take_profit,
                "gross_pnl": gross,
                "fees": record.fees_paid or ZERO,
                "pnl": pnl,
                "pnl_pct": safe_div(pnl, entry_price * quantity),
                "r_multiple": float(safe_div(pnl, risk_amount)) if risk_amount > ZERO else None,
                "max_favorable_excursion_r": mfe_r,
                "max_adverse_excursion_r": mae_r,
                "holding_minutes": holding_minutes,
                "exit_reason": str(reason),
                "ai_decision_id": record.ai_decision_id,
                "risk_approval_id": record.risk_approval_id,
                "mode": self.mode,
                "meta": {"exits": (record.meta or {}).get("exits", [])},
            }
        )

        equity = self.portfolio.equity()
        self.performance.register_trade(
            start_of_utc_day().date(), pnl, record.fees_paid or ZERO, equity
        )
        self.bot.register_trade_outcome(
            pnl,
            cooldown_minutes=self.settings.cooldown_minutes,
            loss_limit=self.settings.consecutive_loss_limit,
        )
        self.events.record(
            EventType.POSITION_CLOSED,
            message=(
                f"closed {record.symbol} {quantity} @ {exit_price} "
                f"pnl={pnl} reason={reason}"
            ),
            symbol=record.symbol,
            strategy=record.strategy,
            trade_id=trade.id,
            context={
                "pnl": float(pnl),
                "r_multiple": trade.r_multiple,
                "exit_reason": str(reason),
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "holding_minutes": holding_minutes,
            },
        )
        return trade

    # ------------------------------------------------------------- bulk exits
    def flatten_all(
        self, reason: ExitReason = ExitReason.EMERGENCY, detail: str = ""
    ) -> list[Position]:
        """Close every open position. Used by the emergency workflow.

        Failures are recorded and the loop continues: a single symbol that cannot
        be sold must not leave the remaining positions untouched.
        """
        closed: list[Position] = []
        for record in self.repo.open_positions():
            try:
                _, position = self.close_position(
                    record.id, reason=reason, detail=detail or "emergency flatten"
                )
                closed.append(position)
            except Exception as exc:  # noqa: BLE001 - keep flattening
                log_event(
                    logger,
                    EventType.SYSTEM_ERROR,
                    symbol=record.symbol,
                    position_id=record.id,
                    reason=f"emergency close failed: {exc}",
                    level=40,
                )
                self.events.record(
                    EventType.SYSTEM_ERROR,
                    message=f"emergency close failed for {record.symbol}: {exc}",
                    symbol=record.symbol,
                    level="ERROR",
                    trade_id=record.id,
                )
        return closed

    # ------------------------------------------------------------- book-keeping
    def position_state(self, record) -> PositionState:  # noqa: ANN001
        """Build the shared exit-rule state from a stored position."""
        plan = record.exit_plan or {}
        return PositionState(
            side=PositionSide(record.side),
            entry_price=record.entry_price,
            quantity=record.quantity,
            stop_loss=record.stop_loss or ZERO,
            take_profit=record.take_profit,
            initial_stop=_decimal_or_none(plan.get("initial_stop")) or record.stop_loss,
            trailing_stop_atr_multiple=plan.get("trailing_stop_atr_multiple"),
            trailing_stop_price=_decimal_or_none(plan.get("trailing_stop_price")),
            breakeven_at_r=plan.get("breakeven_at_r"),
            breakeven_applied=bool(plan.get("breakeven_applied", False)),
            partial_exit_at_r=plan.get("partial_exit_at_r"),
            partial_exit_fraction=plan.get("partial_exit_fraction"),
            partial_exit_done=bool(plan.get("partial_exit_done", False)),
            time_stop_bars=plan.get("time_stop_bars"),
            bars_held=record.bars_held or 0,
        )

    def persist_position_state(self, record, state: PositionState) -> None:  # noqa: ANN001
        plan = dict(record.exit_plan or {})
        plan.update(
            {
                "initial_stop": str(state.initial_stop) if state.initial_stop else None,
                "trailing_stop_atr_multiple": state.trailing_stop_atr_multiple,
                "trailing_stop_price": str(state.trailing_stop_price)
                if state.trailing_stop_price
                else None,
                "breakeven_at_r": state.breakeven_at_r,
                "breakeven_applied": state.breakeven_applied,
                "partial_exit_at_r": state.partial_exit_at_r,
                "partial_exit_fraction": state.partial_exit_fraction,
                "partial_exit_done": state.partial_exit_done,
                "time_stop_bars": state.time_stop_bars,
            }
        )
        record.exit_plan = plan
        record.stop_loss = state.stop_loss
        record.take_profit = state.take_profit

    def cancel_all_open_orders(self, symbol: str | None = None) -> list[Order]:
        cancelled: list[Order] = []
        for record in self.repo.open_orders(symbol):
            if record.status in (OrderStatus.NEW.value, OrderStatus.PARTIALLY_FILLED.value):
                cancelled.append(self.exchange.cancel_order(record.id, record.symbol))
        return cancelled


def _weighted_exit_price(exits: list[dict]) -> Decimal:
    total_quantity = ZERO
    total_notional = ZERO
    for exit_event in exits:
        quantity = Decimal(str(exit_event["quantity"]))
        price = Decimal(str(exit_event["price"]))
        total_quantity += quantity
        total_notional += quantity * price
    if total_quantity <= ZERO:
        return ZERO
    return round_money(total_notional / total_quantity, 8)


def _decimal_or_none(value) -> Decimal | None:  # noqa: ANN001
    if value in (None, "", "None"):
        return None
    return Decimal(str(value))
