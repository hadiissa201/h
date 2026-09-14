"""Position exit rules — one implementation, used by live monitoring and backtests.

Sharing this module is deliberate. If the backtester had its own copy of "when do
we get stopped out", backtest results would stop describing live behaviour, which
is the quiet way a system ends up looking better on paper than it is.

Evaluation order within a bar matters and is pessimistic on purpose:

1. **Stop first.** When a bar's range covers both the stop and the target, we
   assume the stop was hit. Without tick data you cannot know the sequence, and
   assuming the favourable order inflates every result.
2. Take profit.
3. Trailing stop (already folded into the effective stop).
4. Partial exit at a profit multiple.
5. Time stop.

Break-even and trailing updates are applied *after* exit evaluation for the bar,
so a stop can never be moved out of the way of a loss that already happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.numeric import ZERO, round_money
from app.models.enums import ExitReason, PositionSide


@dataclass(frozen=True)
class BarPrices:
    """Prices for one evaluation step.

    In live monitoring these come from the ticker (``high``/``low``/``open``
    default to ``close``); in a backtest they are the bar's real values.
    """

    close: Decimal
    high: Decimal | None = None
    low: Decimal | None = None
    open: Decimal | None = None

    @property
    def bar_high(self) -> Decimal:
        return self.high if self.high is not None else self.close

    @property
    def bar_low(self) -> Decimal:
        return self.low if self.low is not None else self.close

    @property
    def bar_open(self) -> Decimal:
        return self.open if self.open is not None else self.close


@dataclass
class PositionState:
    """Mutable exit state of one position."""

    side: PositionSide
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None = None
    initial_stop: Decimal | None = None
    trailing_stop_atr_multiple: float | None = None
    trailing_stop_price: Decimal | None = None
    breakeven_at_r: float | None = None
    breakeven_applied: bool = False
    partial_exit_at_r: float | None = None
    partial_exit_fraction: float | None = None
    partial_exit_done: bool = False
    time_stop_bars: int | None = None
    bars_held: int = 0

    @property
    def risk_per_unit(self) -> Decimal:
        base_stop = self.initial_stop or self.stop_loss
        return abs(self.entry_price - base_stop)

    def effective_stop(self) -> Decimal:
        """The stop actually in force: the tighter of hard stop and trailing stop."""
        if self.trailing_stop_price is None:
            return self.stop_loss
        if self.side is PositionSide.LONG:
            return max(self.stop_loss, self.trailing_stop_price)
        return min(self.stop_loss, self.trailing_stop_price)

    def r_multiple_at(self, price: Decimal) -> Decimal:
        risk = self.risk_per_unit
        if risk <= ZERO:
            return ZERO
        if self.side is PositionSide.LONG:
            return (price - self.entry_price) / risk
        return (self.entry_price - price) / risk


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    price: Decimal | None = None
    fraction: Decimal = Decimal("1")
    detail: str = ""

    @property
    def is_partial(self) -> bool:
        return self.should_exit and self.fraction < Decimal("1")


def evaluate_exit(
    state: PositionState,
    prices: BarPrices,
    *,
    invalidated: bool = False,
    invalidation_detail: str = "",
) -> ExitDecision:
    """Decide whether (and how much) to exit on this bar."""
    stop = state.effective_stop()
    is_long = state.side is PositionSide.LONG

    # 1. stop loss — evaluated before the target, deliberately
    stop_hit = prices.bar_low <= stop if is_long else prices.bar_high >= stop
    if stop_hit:
        trailing_active = (
            state.trailing_stop_price is not None
            and (
                state.trailing_stop_price > state.stop_loss
                if is_long
                else state.trailing_stop_price < state.stop_loss
            )
        )
        # A bar that *opens* beyond the stop gapped through it: the fill is the
        # open, not the stop level. Intrabar crossings fill at the stop, and the
        # cost model then adds spread and slippage on top.
        fill = stop
        gapped = prices.bar_open < stop if is_long else prices.bar_open > stop
        if gapped:
            fill = prices.bar_open
        return ExitDecision(
            True,
            ExitReason.TRAILING_STOP if trailing_active else ExitReason.STOP_LOSS,
            fill,
            Decimal("1"),
            f"{'trailing ' if trailing_active else ''}stop {stop} hit",
        )

    # 2. take profit
    if state.take_profit is not None:
        target_hit = (
            prices.bar_high >= state.take_profit
            if is_long
            else prices.bar_low <= state.take_profit
        )
        if target_hit:
            return ExitDecision(
                True,
                ExitReason.TAKE_PROFIT,
                state.take_profit,
                Decimal("1"),
                f"target {state.take_profit} reached",
            )

    # 3. strategy invalidation
    if invalidated:
        return ExitDecision(
            True,
            ExitReason.INVALIDATION,
            prices.close,
            Decimal("1"),
            invalidation_detail or "setup invalidated",
        )

    # 4. partial profit taking
    if (
        state.partial_exit_at_r
        and state.partial_exit_fraction
        and not state.partial_exit_done
    ):
        favourable = prices.bar_high if is_long else prices.bar_low
        if state.r_multiple_at(favourable) >= Decimal(str(state.partial_exit_at_r)):
            trigger_price = _price_at_r(state, Decimal(str(state.partial_exit_at_r)))
            return ExitDecision(
                True,
                ExitReason.PARTIAL_TAKE_PROFIT,
                trigger_price,
                Decimal(str(state.partial_exit_fraction)),
                f"partial exit at {state.partial_exit_at_r}R",
            )

    # 5. time stop
    if state.time_stop_bars is not None and state.bars_held >= state.time_stop_bars:
        return ExitDecision(
            True,
            ExitReason.TIME_STOP,
            prices.close,
            Decimal("1"),
            f"held {state.bars_held} bars without resolving",
        )

    return ExitDecision(False)


def update_stops(
    state: PositionState, prices: BarPrices, atr: Decimal | None
) -> list[str]:
    """Apply break-even and trailing-stop moves. Returns a list of changes made.

    Stops only ever move in the favourable direction; a trailing stop can never
    widen risk.
    """
    changes: list[str] = []
    is_long = state.side is PositionSide.LONG

    # break-even
    if state.breakeven_at_r and not state.breakeven_applied:
        favourable = prices.bar_high if is_long else prices.bar_low
        if state.r_multiple_at(favourable) >= Decimal(str(state.breakeven_at_r)):
            new_stop = state.entry_price
            improved = new_stop > state.stop_loss if is_long else new_stop < state.stop_loss
            if improved:
                state.stop_loss = round_money(new_stop, 8)
                changes.append(f"stop moved to break-even {state.stop_loss}")
            state.breakeven_applied = True

    # trailing stop
    if state.trailing_stop_atr_multiple and atr and atr > ZERO:
        distance = atr * Decimal(str(state.trailing_stop_atr_multiple))
        candidate = (
            prices.bar_high - distance if is_long else prices.bar_low + distance
        )
        current = state.trailing_stop_price
        if current is None:
            better = True
        else:
            better = candidate > current if is_long else candidate < current
        if better:
            # Never trail past the current price, and never loosen.
            state.trailing_stop_price = round_money(candidate, 8)
            changes.append(f"trailing stop moved to {state.trailing_stop_price}")
    return changes


def _price_at_r(state: PositionState, r_multiple: Decimal) -> Decimal:
    risk = state.risk_per_unit
    if state.side is PositionSide.LONG:
        return round_money(state.entry_price + risk * r_multiple, 8)
    return round_money(state.entry_price - risk * r_multiple, 8)


def check_invalidation(
    state: PositionState,
    *,
    regime_flipped: bool = False,
    abnormal_market: bool = False,
) -> tuple[bool, str]:
    """Non-price invalidations that should close a position.

    Kept separate from ``evaluate_exit`` because these come from the analysis
    layer rather than from the bar itself.
    """
    if abnormal_market:
        return True, "abnormal market conditions"
    if regime_flipped:
        return True, "market regime flipped against the position"
    return False, ""
