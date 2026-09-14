"""Position monitoring.

Driven by the n8n position-monitoring workflow on a schedule. Each pass:

1. refresh prices and mark positions (updating MFE/MAE);
2. fill any resting limit/stop orders the market traded through;
3. move break-even and trailing stops;
4. evaluate exits with the shared rules and execute them;
5. flag abnormal conditions and stale data for the emergency path.

Known limitation, stated plainly: this polls. Between polls a wick can pass
through a stop and recover, and the position will be exited at the next observed
price rather than at the stop. Tighter behaviour needs exchange-side stop orders
(supported by ``create_order(type=STOP_MARKET)``) or a streaming connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.errors import MarketDataError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.core.numeric import to_decimal
from app.data.service import MarketDataService
from app.database.repositories import EventRepository, ExecutionRepository
from app.execution.service import ExecutionService
from app.models.enums import ExitReason
from app.portfolio.exit_rules import BarPrices, evaluate_exit, update_stops
from app.portfolio.service import PortfolioService

logger = get_logger(__name__)


@dataclass
class MonitorOutcome:
    checked: int = 0
    exits: list[dict] = field(default_factory=list)
    stop_updates: list[dict] = field(default_factory=list)
    resting_fills: list[str] = field(default_factory=list)
    stale_symbols: list[str] = field(default_factory=list)
    abnormal_symbols: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "exits": self.exits,
            "stop_updates": self.stop_updates,
            "resting_fills": self.resting_fills,
            "stale_symbols": self.stale_symbols,
            "abnormal_symbols": self.abnormal_symbols,
            "errors": self.errors,
        }


class PositionMonitor:
    def __init__(
        self,
        execution: ExecutionService,
        portfolio: PortfolioService,
        market_data: MarketDataService,
        execution_repo: ExecutionRepository,
        event_repo: EventRepository,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.execution = execution
        self.portfolio = portfolio
        self.market_data = market_data
        self.repo = execution_repo
        self.events = event_repo

    def run(self, *, timeframe: str | None = None) -> MonitorOutcome:
        timeframe = timeframe or self.settings.primary_timeframe
        outcome = MonitorOutcome()
        open_records = self.repo.open_positions()
        outcome.checked = len(open_records)
        if not open_records:
            self.portfolio.snapshot(persist=True)
            return outcome

        symbols = sorted({record.symbol for record in open_records})
        prices: dict[str, Decimal] = {}
        bars: dict[str, BarPrices] = {}
        atrs: dict[str, Decimal | None] = {}

        for symbol in symbols:
            try:
                frame, report = self.market_data.get_validated_candles(
                    symbol,
                    timeframe,
                    limit=max(60, self.settings.min_candles_for_analysis),
                    raise_on_error=False,
                )
                if not report.is_tradeable:
                    outcome.stale_symbols.append(symbol)
                    codes = [issue.code for issue in report.errors]
                    log_event(
                        logger,
                        EventType.MARKET_DATA_STALE,
                        symbol=symbol,
                        reason=report.summary(),
                        codes=codes,
                        level=30,
                    )
                ticker = self.market_data.get_ticker(symbol)
                last = ticker.last
                prices[symbol] = last
                last_bar = frame.iloc[-1]
                # Ticker price combined with the current bar's extremes: the wick
                # may already have gone through a stop even if the last trade did not.
                bars[symbol] = BarPrices(
                    close=last,
                    high=max(to_decimal(float(last_bar["high"])), last),
                    low=min(to_decimal(float(last_bar["low"])), last),
                    open=to_decimal(float(last_bar["open"])),
                )
                atrs[symbol] = _latest_atr(frame, self.settings)
                move = abs(
                    float(last_bar["close"]) / float(frame["close"].iloc[-2]) - 1.0
                ) if len(frame) > 1 else 0.0
                if move > float(self.settings.abnormal_price_move_pct):
                    outcome.abnormal_symbols.append(symbol)
            except MarketDataError as exc:
                outcome.stale_symbols.append(symbol)
                outcome.errors.append({"symbol": symbol, "error": str(exc.detail)})
                continue

        self.portfolio.mark_positions(prices)

        # Resting orders first: a stop order placed on the exchange may already
        # have filled, and we must not double-exit the same position.
        for symbol in symbols:
            bar = bars.get(symbol)
            if bar is None or not hasattr(self.execution.exchange, "process_resting_orders"):
                continue
            filled = self.execution.exchange.process_resting_orders(
                symbol, high=bar.bar_high, low=bar.bar_low, last=bar.close
            )
            outcome.resting_fills.extend(order.order_id for order in filled)

        for record in self.repo.open_positions():
            bar = bars.get(record.symbol)
            if bar is None:
                continue
            state = self.execution.position_state(record)
            state.bars_held = (record.bars_held or 0) + 1
            record.bars_held = state.bars_held

            changes = update_stops(state, bar, atrs.get(record.symbol))
            decision = evaluate_exit(
                state,
                bar,
                invalidated=record.symbol in outcome.abnormal_symbols,
                invalidation_detail="abnormal price movement",
            )
            self.execution.persist_position_state(record, state)

            for change in changes:
                outcome.stop_updates.append(
                    {"position_id": record.id, "symbol": record.symbol, "change": change}
                )
                log_event(
                    logger,
                    EventType.TRAILING_STOP_MOVED
                    if "trailing" in change
                    else EventType.BREAKEVEN_STOP_MOVED,
                    symbol=record.symbol,
                    position_id=record.id,
                    detail=change,
                    price=float(bar.close),
                )

            if not decision.should_exit:
                continue

            try:
                _, position = self.execution.close_position(
                    record.id,
                    fraction=decision.fraction,
                    reason=decision.reason or ExitReason.MANUAL,
                    detail=decision.detail,
                )
            except Exception as exc:
                outcome.errors.append(
                    {"position_id": record.id, "symbol": record.symbol, "error": str(exc)}
                )
                log_event(
                    logger,
                    EventType.SYSTEM_ERROR,
                    symbol=record.symbol,
                    position_id=record.id,
                    reason=f"exit failed: {exc}",
                    level=40,
                )
                continue

            if decision.is_partial:
                plan = dict(record.exit_plan or {})
                plan["partial_exit_done"] = True
                record.exit_plan = plan
            outcome.exits.append(
                {
                    "position_id": record.id,
                    "symbol": record.symbol,
                    "reason": str(decision.reason),
                    "detail": decision.detail,
                    "fraction": float(decision.fraction),
                    "realized_pnl": float(position.realized_pnl),
                }
            )

        self.portfolio.snapshot(persist=True)
        return outcome


def _latest_atr(frame, settings: Settings) -> Decimal | None:
    from app.indicators import atr as atr_indicator

    if len(frame) < settings.min_candles_for_analysis // 4:
        return None
    series = atr_indicator(frame["high"], frame["low"], frame["close"], 14)
    value = series.iloc[-1]
    if value is None or value != value or value <= 0:  # NaN check
        return None
    return to_decimal(float(value))
