"""Exit rules — shared by live monitoring and the backtester.

The pessimistic choices are the ones that matter and are asserted explicitly:
stop before target when a bar covers both, gap-through fills at the open, and
stops that only ever move in the favourable direction.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.enums import ExitReason, PositionSide
from app.portfolio.exit_rules import (
    BarPrices,
    PositionState,
    check_invalidation,
    evaluate_exit,
    update_stops,
)


def long_position(**overrides) -> PositionState:
    payload = {
        "side": PositionSide.LONG,
        "entry_price": Decimal("100"),
        "quantity": Decimal("10"),
        "stop_loss": Decimal("96"),
        "take_profit": Decimal("112"),
        "initial_stop": Decimal("96"),
    }
    payload.update(overrides)
    return PositionState(**payload)


def bar(close, high=None, low=None, open_=None) -> BarPrices:
    return BarPrices(
        close=Decimal(str(close)),
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)),
        open=Decimal(str(open_ if open_ is not None else close)),
    )


# ------------------------------------------------------------------- no exit
def test_quiet_bar_produces_no_exit():
    decision = evaluate_exit(long_position(), bar(102, 103, 101))
    assert not decision.should_exit


# ---------------------------------------------------------------- stop first
def test_stop_is_taken_when_a_bar_covers_both_stop_and_target():
    """Without tick data the sequence is unknowable; assuming the good one
    inflates every backtest, so we always assume the stop."""
    decision = evaluate_exit(long_position(), bar(105, high=115, low=95))
    assert decision.should_exit
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.price == Decimal("96")


def test_stop_hit_intrabar_fills_at_the_stop():
    decision = evaluate_exit(long_position(), bar(98, high=101, low=95.5))
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.price == Decimal("96")


def test_gap_through_the_stop_fills_at_the_open_not_the_stop():
    decision = evaluate_exit(long_position(), bar(91, high=92, low=90, open_=91.5))
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.price == Decimal("91.5"), "a gap does not fill politely at the stop"


def test_short_position_stop_is_above_entry():
    state = PositionState(
        side=PositionSide.SHORT,
        entry_price=Decimal("100"),
        quantity=Decimal("5"),
        stop_loss=Decimal("104"),
        take_profit=Decimal("92"),
        initial_stop=Decimal("104"),
    )
    assert evaluate_exit(state, bar(103, high=105, low=102)).reason is ExitReason.STOP_LOSS
    assert evaluate_exit(state, bar(93, high=94, low=91)).reason is ExitReason.TAKE_PROFIT


# --------------------------------------------------------------- take profit
def test_target_is_taken_when_the_stop_was_not_touched():
    decision = evaluate_exit(long_position(), bar(111, high=113, low=108))
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.price == Decimal("112")


# --------------------------------------------------------------- invalidation
def test_invalidation_closes_at_the_close():
    decision = evaluate_exit(
        long_position(), bar(104, 105, 103), invalidated=True, invalidation_detail="regime flip"
    )
    assert decision.reason is ExitReason.INVALIDATION
    assert decision.price == Decimal("104")
    assert "regime flip" in decision.detail


def test_check_invalidation_flags_abnormal_conditions():
    flagged, detail = check_invalidation(long_position(), abnormal_market=True)
    assert flagged and "abnormal" in detail
    flagged, detail = check_invalidation(long_position(), regime_flipped=True)
    assert flagged and "regime" in detail
    assert check_invalidation(long_position())[0] is False


# ------------------------------------------------------------- partial exits
def test_partial_exit_triggers_at_the_configured_r_multiple():
    state = long_position(partial_exit_at_r=2.0, partial_exit_fraction=0.5)
    # Risk is 4 per unit, so 2R is 108.
    decision = evaluate_exit(state, bar(107, high=109, low=106))
    assert decision.reason is ExitReason.PARTIAL_TAKE_PROFIT
    assert decision.is_partial
    assert decision.fraction == Decimal("0.5")
    assert decision.price == Decimal("108")


def test_partial_exit_only_happens_once():
    state = long_position(
        partial_exit_at_r=2.0, partial_exit_fraction=0.5, partial_exit_done=True
    )
    assert not evaluate_exit(state, bar(107, high=109, low=106)).should_exit


# ----------------------------------------------------------------- time stop
def test_time_stop_closes_a_position_that_never_resolved():
    state = long_position(time_stop_bars=10, bars_held=10)
    decision = evaluate_exit(state, bar(101, 102, 100))
    assert decision.reason is ExitReason.TIME_STOP


def test_time_stop_does_not_fire_early():
    state = long_position(time_stop_bars=10, bars_held=9)
    assert not evaluate_exit(state, bar(101, 102, 100)).should_exit


# --------------------------------------------------------------- stop moves
def test_breakeven_moves_the_stop_to_entry_at_the_configured_r():
    state = long_position(breakeven_at_r=1.0)
    changes = update_stops(state, bar(104, high=104.5, low=103), atr=None)
    assert state.stop_loss == Decimal("100")
    assert state.breakeven_applied
    assert any("break-even" in change for change in changes)


def test_breakeven_does_not_fire_before_the_threshold():
    state = long_position(breakeven_at_r=1.0)
    update_stops(state, bar(102, high=103, low=101), atr=None)
    assert state.stop_loss == Decimal("96")
    assert not state.breakeven_applied


def test_trailing_stop_follows_price_up():
    state = long_position(trailing_stop_atr_multiple=2.0)
    update_stops(state, bar(110, high=112, low=108), atr=Decimal("2"))
    assert state.trailing_stop_price == Decimal("108")
    assert state.effective_stop() == Decimal("108")


def test_trailing_stop_never_moves_backwards():
    state = long_position(trailing_stop_atr_multiple=2.0)
    update_stops(state, bar(120, high=122, low=118), atr=Decimal("2"))
    high_water = state.trailing_stop_price
    update_stops(state, bar(105, high=106, low=104), atr=Decimal("2"))
    assert state.trailing_stop_price == high_water, "a trailing stop must never widen risk"


def test_effective_stop_is_the_tighter_of_hard_and_trailing():
    state = long_position(trailing_stop_price=Decimal("103"))
    assert state.effective_stop() == Decimal("103")
    state.trailing_stop_price = Decimal("90")
    assert state.effective_stop() == Decimal("96")


def test_trailing_stop_exit_is_labelled_as_such():
    state = long_position(trailing_stop_atr_multiple=2.0)
    update_stops(state, bar(112, high=114, low=110), atr=Decimal("2"))
    decision = evaluate_exit(state, bar(109, high=111, low=109))
    assert decision.reason is ExitReason.TRAILING_STOP


def test_no_trailing_without_atr():
    state = long_position(trailing_stop_atr_multiple=2.0)
    update_stops(state, bar(110, high=112, low=108), atr=None)
    assert state.trailing_stop_price is None


# ---------------------------------------------------------------- r multiple
def test_r_multiple_uses_the_initial_stop_not_the_trailed_one():
    """Otherwise trailing a stop would silently inflate reported R."""
    state = long_position(initial_stop=Decimal("96"))
    state.stop_loss = Decimal("104")  # trailed well past break-even
    assert state.risk_per_unit == Decimal("4")
    assert state.r_multiple_at(Decimal("108")) == Decimal("2")


def test_r_multiple_is_zero_when_risk_is_undefined():
    state = long_position(entry_price=Decimal("100"), stop_loss=Decimal("100"), initial_stop=Decimal("100"))
    assert state.r_multiple_at(Decimal("110")) == Decimal("0")


def test_bar_prices_default_missing_fields_to_close():
    prices = BarPrices(close=Decimal("100"))
    assert prices.bar_high == prices.bar_low == prices.bar_open == Decimal("100")
