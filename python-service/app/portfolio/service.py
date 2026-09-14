"""Portfolio valuation and account state.

Single source of truth for equity, exposure, drawdown and daily P&L — the numbers
the risk engine gates on. Nothing here accepts a caller-supplied equity figure:
everything is derived from persisted balances, positions and trades.

The books must always close::

    equity == starting_equity + realised P&L + unrealised P&L - unamortised entry fees

``reconciliation_error()`` returns that residual and is asserted to be zero in the
lifecycle tests. The last term exists because an entry fee is paid up front but
only enters realised P&L as exits amortise it.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.numeric import ZERO, round_money, safe_div
from app.database.repositories import (
    BotStateRepository,
    ExecutionRepository,
    PerformanceRepository,
)
from app.models.enums import BotStatus, PositionSide, TradingModeEnum
from app.models.risk import AccountRiskState
from app.models.trading import Balance, PortfolioSnapshot, Position
from app.portfolio.mapping import position_from_record
from app.utils.time import start_of_utc_day, utcnow


class PortfolioService:
    def __init__(
        self,
        execution_repo: ExecutionRepository,
        performance_repo: PerformanceRepository,
        bot_repo: BotStateRepository,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.execution = execution_repo
        self.performance = performance_repo
        self.bot = bot_repo
        self.mode = execution_repo.mode

    # ------------------------------------------------------------- positions
    def open_positions(self, symbol: str | None = None) -> list[Position]:
        return [
            position_from_record(record)
            for record in self.execution.open_positions(symbol)
        ]

    def get_position(self, position_id: str) -> Position | None:
        record = self.execution.get_position(position_id)
        return position_from_record(record) if record else None

    def mark_positions(self, prices: dict[str, Decimal]) -> list[Position]:
        """Update mark prices and the excursion extremes used for MFE/MAE.

        Excursions are tracked continuously because after the fact you cannot
        tell whether a winner was ever deeply underwater — which is exactly what
        you need to know to judge whether stops are sensibly placed.
        """
        updated: list[Position] = []
        for record in self.execution.open_positions():
            price = prices.get(record.symbol)
            if price is None:
                continue
            record.mark_price = round_money(price, 8)
            if record.max_favorable_price is None:
                record.max_favorable_price = price
                record.max_adverse_price = price
            elif record.side == PositionSide.LONG.value:
                record.max_favorable_price = max(record.max_favorable_price, price)
                record.max_adverse_price = min(record.max_adverse_price or price, price)
            else:
                record.max_favorable_price = min(record.max_favorable_price, price)
                record.max_adverse_price = max(record.max_adverse_price or price, price)
            updated.append(position_from_record(record))
        return updated

    # ------------------------------------------------------------- valuation
    def positions_value(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        total = ZERO
        for record in self.execution.open_positions():
            price = (prices or {}).get(record.symbol) or record.mark_price or record.entry_price
            total += price * record.quantity
        return round_money(total, 8)

    def unrealized_pnl(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        total = ZERO
        for record in self.execution.open_positions():
            price = (prices or {}).get(record.symbol) or record.mark_price or record.entry_price
            if record.side == PositionSide.LONG.value:
                total += (price - record.entry_price) * record.quantity
            else:
                total += (record.entry_price - price) * record.quantity
        return round_money(total, 8)

    def unamortized_entry_fees(self) -> Decimal:
        """Entry fees already paid that no P&L figure has absorbed yet.

        An entry fee is charged in full when the position opens, but it is only
        booked into realised P&L as each exit amortises its share (see
        ``ExecutionService._close``). While a position is open the residual share
        sits outside both realised and unrealised P&L, which is exactly the gap in
        the reconciliation below. Exposing it makes that gap auditable instead of
        looking like drift.
        """
        total = ZERO
        for record in self.execution.open_positions():
            meta = record.meta or {}
            remaining = meta.get("entry_fee_unamortized", meta.get("entry_fee"))
            if remaining is None:
                continue
            total += Decimal(str(remaining))
        return round_money(total, 8)

    #: Largest reconciliation residual attributable to rounding alone.
    #:
    #: Equity rounds ``price * quantity`` per leg, while P&L rounds
    #: ``(mark - entry) * quantity``; the two are identical before rounding but
    #: each 8dp quantisation can land one unit apart. The bound is therefore a
    #: few units of the last decimal place — one hundredth of a millionth of a
    #: quote unit. Anything larger is a real accounting error, not noise.
    RECONCILIATION_TOLERANCE = Decimal("0.000001")

    def reconciliation_error(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        """How far the books are from closing.

        ``equity == starting + realised + unrealised - unamortised entry fees``.
        A residual above :attr:`RECONCILIATION_TOLERANCE` means cash, positions and
        P&L genuinely disagree, and every performance number downstream is suspect.
        """
        expected = (
            self.starting_equity()
            + self.realized_pnl()
            + self.unrealized_pnl(prices)
            - self.unamortized_entry_fees()
        )
        return round_money(self.equity(prices) - expected, 8)

    def books_balance(self, prices: dict[str, Decimal] | None = None) -> bool:
        return abs(self.reconciliation_error(prices)) <= self.RECONCILIATION_TOLERANCE

    def cash(self) -> Decimal:
        record = self.execution.get_balance(self.settings.quote_currency)
        return record.free if record else ZERO

    def realized_pnl(self) -> Decimal:
        """P&L that has actually hit cash, including partial exits still open.

        A trade row is written only when a position closes completely, so summing
        trades alone hides the cash already banked by a partial exit: equity would
        rise while ``realized_pnl`` stayed flat and ``unrealized_pnl`` only covered
        the residual quantity, and the three would stop reconciling.
        """
        closed = sum((trade.pnl for trade in self.execution.all_trades()), start=ZERO)
        banked = sum(
            (record.realized_pnl or ZERO for record in self.execution.open_positions()),
            start=ZERO,
        )
        return round_money(closed + banked, 8)

    def fees_paid(self) -> Decimal:
        """Every fee already paid, not just the fees on closed trades.

        An open position has paid its entry fee; reporting 0 while that money has
        left the account is misleading and does not reconcile against equity.
        """
        closed = sum((trade.fees for trade in self.execution.all_trades()), start=ZERO)
        open_fees = sum(
            (record.fees_paid or ZERO for record in self.execution.open_positions()),
            start=ZERO,
        )
        return round_money(closed + open_fees, 8)

    def equity(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        return round_money(self.cash() + self.positions_value(prices), 8)

    def starting_equity(self) -> Decimal:
        return round_money(Decimal(str(self.settings.paper_starting_balance)), 8)

    def peak_equity(self, current: Decimal | None = None) -> Decimal:
        recorded = self.performance.peak_equity() or ZERO
        candidates = [recorded, self.starting_equity()]
        if current is not None:
            candidates.append(current)
        return max(candidates)

    def snapshot(
        self, prices: dict[str, Decimal] | None = None, persist: bool = False
    ) -> PortfolioSnapshot:
        cash = self.cash()
        positions_value = self.positions_value(prices)
        equity = round_money(cash + positions_value, 8)
        unrealized = self.unrealized_pnl(prices)
        realized = self.realized_pnl()
        starting = self.starting_equity()
        peak = self.peak_equity(equity)
        drawdown = safe_div(peak - equity, peak) if peak > ZERO else ZERO
        exposure = safe_div(positions_value, equity) if equity > ZERO else ZERO

        day = start_of_utc_day().date()
        daily = self.performance.get_or_create_day(day, equity)
        daily_start = daily.starting_equity or equity
        daily_pnl = round_money(equity - daily_start, 8)
        daily_pnl_pct = safe_div(daily_pnl, daily_start) if daily_start > ZERO else ZERO

        open_records = self.execution.open_positions()
        snapshot = PortfolioSnapshot(
            timestamp=utcnow(),
            mode=TradingModeEnum(self.mode),
            quote_currency=self.settings.quote_currency,
            cash=cash,
            positions_value=positions_value,
            equity=equity,
            starting_equity=starting,
            peak_equity=peak,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=round_money(equity - starting, 8),
            total_pnl_pct=safe_div(equity - starting, starting),
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            drawdown_pct=max(ZERO, drawdown),
            exposure_pct=exposure,
            open_positions=len(open_records),
            fees_paid=self.fees_paid(),
            balances=[
                Balance(currency=record.currency, free=record.free, locked=record.locked)
                for record in self.execution.list_balances()
            ],
            positions=[position_from_record(record) for record in open_records],
        )

        if persist:
            self.performance.add_equity_snapshot(
                {
                    "taken_at": snapshot.timestamp,
                    "cash": cash,
                    "positions_value": positions_value,
                    "equity": equity,
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "peak_equity": peak,
                    "drawdown_pct": snapshot.drawdown_pct,
                    "open_positions": snapshot.open_positions,
                    "exposure_pct": exposure,
                }
            )
            self.performance.update_day_equity(day, equity)

        return snapshot

    # ---------------------------------------------------------- risk context
    def account_risk_state(
        self, prices: dict[str, Decimal] | None = None
    ) -> AccountRiskState:
        snapshot = self.snapshot(prices)
        state = self.bot.get()
        return AccountRiskState(
            timestamp=snapshot.timestamp,
            mode=TradingModeEnum(self.mode),
            bot_status=BotStatus(state.status),
            equity=snapshot.equity,
            cash=snapshot.cash,
            starting_equity=snapshot.starting_equity,
            peak_equity=snapshot.peak_equity,
            open_positions=snapshot.open_positions,
            open_symbols=[position.symbol for position in snapshot.positions],
            exposure_pct=snapshot.exposure_pct,
            daily_pnl_pct=snapshot.daily_pnl_pct,
            drawdown_pct=snapshot.drawdown_pct,
            consecutive_losses=state.consecutive_losses,
            cooldown_until=state.cooldown_until,
            halt_reason=state.halt_reason,
        )
