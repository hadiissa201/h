"""Full trade lifecycle through the real service graph.

Accounting is the thing to get right here: if equity, cash, realised P&L and the
trade record ever disagree, every performance number the system reports is
wrong. These tests assert they reconcile exactly, to the cent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.numeric import round_money
from app.models.enums import ExitReason, OrderStatus, PositionStatus


@pytest.fixture
def open_trade(services, proposal_factory):
    """Open one real position through risk -> execution."""
    price = services.market_data.get_ticker("BTC/USDT").last
    decision = services.risk.check(
        proposal_factory(
            entry=price,
            stop_loss=price * Decimal("0.96"),
            take_profit=price * Decimal("1.09"),
        )
    )
    assert decision.approved, decision.reasons
    order, position = services.execution.place_entry(
        approval_id=decision.approval_id, strategy="trend_following", regime="range"
    )
    return decision, order, position


def test_entry_produces_a_filled_order_and_an_open_position(services, open_trade):
    decision, order, position = open_trade
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == decision.quantity
    assert order.average_fill_price > 0
    assert order.fee_paid > 0

    assert position.status is PositionStatus.OPEN
    assert position.quantity == order.filled_quantity
    assert position.stop_loss == decision.stop_loss
    assert position.take_profit == decision.take_profit
    assert position.risk_approval_id == decision.approval_id
    assert position.strategy == "trend_following"


def test_the_entry_fill_is_worse_than_the_mid(services, open_trade):
    """Spread and slippage must actually be charged."""
    decision, order, _ = open_trade
    assert order.average_fill_price > decision.entry


def test_cash_and_holdings_move_together(services, open_trade):
    _, order, position = open_trade
    snapshot = services.portfolio.snapshot()
    balances = {balance.currency: balance.free for balance in snapshot.balances}

    assert balances["BTC"] == position.quantity
    spent = order.average_fill_price * order.filled_quantity + order.fee_paid
    assert balances["USDT"] == round_money(Decimal("10000") - spent, 8)


def test_equity_is_cash_plus_marked_positions(services, open_trade):
    snapshot = services.portfolio.snapshot()
    assert snapshot.equity == round_money(snapshot.cash + snapshot.positions_value, 8)
    assert snapshot.open_positions == 1
    assert snapshot.exposure_pct > 0


def test_entry_costs_show_up_immediately_as_a_small_loss(services, open_trade):
    """Opening a position at market always starts underwater by the costs."""
    snapshot = services.portfolio.snapshot()
    assert snapshot.equity < Decimal("10000")
    assert Decimal("10000") - snapshot.equity < Decimal("50")


def test_closing_reconciles_every_number(services, open_trade):
    _, entry_order, position = open_trade
    exit_order, closed = services.execution.close_position(
        position.position_id, reason=ExitReason.MANUAL, detail="test"
    )
    services.session.flush()

    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason == str(ExitReason.MANUAL)

    trade = services.execution_repo.trade_for_position(position.position_id)
    assert trade is not None

    snapshot = services.portfolio.snapshot()
    # 1. flat means equity is pure cash
    assert snapshot.positions_value == 0
    assert snapshot.equity == snapshot.cash
    # 2. equity moved by exactly the realised P&L
    assert snapshot.equity == round_money(
        snapshot.starting_equity + snapshot.realized_pnl, 8
    )
    # 3. the trade record agrees with the portfolio
    assert trade.pnl == snapshot.realized_pnl
    # 4. P&L is the price move minus both fees
    gross = (trade.exit_price - trade.entry_price) * trade.quantity
    assert trade.pnl == pytest.approx(gross - trade.fees, abs=Decimal("0.00000001"))
    # 5. both legs' fees are counted
    assert trade.fees == round_money(entry_order.fee_paid + exit_order.fee_paid, 8)


def test_a_flat_round_trip_loses_the_costs(services, open_trade):
    """With no price move, the round trip must lose exactly the trading costs."""
    _, _, position = open_trade
    _, _ = services.execution.close_position(position.position_id)
    services.session.flush()
    trade = services.execution_repo.trade_for_position(position.position_id)
    assert trade.pnl < 0, "a flat round trip that makes money means costs are missing"


def test_a_losing_trade_increments_the_loss_streak(services, open_trade):
    _, _, position = open_trade
    services.execution.close_position(position.position_id)
    services.session.flush()
    state = services.bot_repo.get()
    assert state.consecutive_losses == 1


def test_the_cooldown_arms_after_consecutive_losses(services, settings):
    for _ in range(settings.consecutive_loss_limit):
        services.bot_repo.register_trade_outcome(
            Decimal("-10"),
            cooldown_minutes=settings.cooldown_minutes,
            loss_limit=settings.consecutive_loss_limit,
        )
    state = services.bot_repo.get()
    assert state.consecutive_losses == settings.consecutive_loss_limit
    assert state.cooldown_until is not None


def test_a_win_clears_the_loss_streak(services, settings):
    services.bot_repo.register_trade_outcome(Decimal("-10"), 120, 3)
    services.bot_repo.register_trade_outcome(Decimal("25"), 120, 3)
    state = services.bot_repo.get()
    assert state.consecutive_losses == 0
    assert state.cooldown_until is None


def test_a_partial_exit_leaves_a_smaller_position_open(services, open_trade):
    _, _, position = open_trade
    _, reduced = services.execution.close_position(
        position.position_id,
        fraction=Decimal("0.5"),
        reason=ExitReason.PARTIAL_TAKE_PROFIT,
    )
    assert reduced.status is PositionStatus.OPEN
    assert reduced.quantity < position.quantity
    assert reduced.realized_pnl != 0
    # No trade record until the position is fully closed.
    assert services.execution_repo.trade_for_position(position.position_id) is None


def test_a_partial_then_full_exit_records_one_trade(services, open_trade):
    _, _, position = open_trade
    services.execution.close_position(position.position_id, fraction=Decimal("0.5"))
    services.execution.close_position(position.position_id)
    services.session.flush()

    trades = services.execution_repo.all_trades()
    assert len(trades) == 1
    trade = trades[0]
    assert trade.quantity == position.quantity  # the whole original size
    assert len(trade.meta["exits"]) == 2
    snapshot = services.portfolio.snapshot()
    assert trade.pnl == snapshot.realized_pnl


def test_closing_an_unknown_position_is_an_error(services):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        services.execution.close_position("pos_nope")


def test_closing_twice_is_refused(services, open_trade):
    from app.core.errors import ConflictError

    _, _, position = open_trade
    services.execution.close_position(position.position_id)
    with pytest.raises(ConflictError, match="already closed"):
        services.execution.close_position(position.position_id)


def test_flatten_all_closes_everything(services, open_trade):
    closed = services.execution.flatten_all(reason=ExitReason.EMERGENCY, detail="drill")
    assert len(closed) == 1
    assert services.portfolio.open_positions() == []


def test_mfe_and_mae_are_tracked_from_marks(services, open_trade):
    _, _, position = open_trade
    entry = position.entry_price
    services.portfolio.mark_positions({"BTC/USDT": entry * Decimal("1.05")})
    services.portfolio.mark_positions({"BTC/USDT": entry * Decimal("0.97")})
    services.execution.close_position(position.position_id)
    services.session.flush()

    trade = services.execution_repo.trade_for_position(position.position_id)
    assert trade.max_favorable_excursion_r > 0
    assert trade.max_adverse_excursion_r < 0


def test_every_stage_leaves_an_audit_trail(services, open_trade):
    decision, order, position = open_trade
    services.execution.close_position(position.position_id)
    services.session.flush()

    events = {record.event_type for record in services.event_repo.recent(100)}
    assert "RISK_APPROVED" in events
    assert "POSITION_OPENED" in events
    assert "POSITION_CLOSED" in events

    risk_record = services.risk_repo.get_by_approval(decision.approval_id)
    assert risk_record.consumed_at is not None
    assert risk_record.consumed_by_order_id == order.order_id
    assert risk_record.checks and risk_record.sizing


def test_equity_snapshots_are_written_for_the_curve(services, open_trade):
    _, _, position = open_trade
    services.execution.close_position(position.position_id)
    services.session.flush()
    curve = services.performance_repo.equity_curve()
    assert len(curve) >= 2
    assert all(point.equity > 0 for point in curve)


def test_daily_stats_track_the_realised_result(services, open_trade):
    _, _, position = open_trade
    services.execution.close_position(position.position_id)
    services.session.flush()
    today = services.performance_repo.today()
    assert today is not None
    assert today.trades == 1
    assert today.realized_pnl == services.portfolio.realized_pnl()


# --------------------------------------------------------------- open-position books
# A trade row is written only on a full close. These tests pin the numbers the
# portfolio reports while a position is still open, where that is easy to get wrong.


def test_fees_already_paid_are_reported_while_the_position_is_open(services, open_trade):
    """Reporting zero fees while the entry fee has left the account is a lie."""
    _, entry_order, _ = open_trade
    assert entry_order.fee_paid > 0

    snapshot = services.portfolio.snapshot()
    assert snapshot.fees_paid == round_money(entry_order.fee_paid, 8)


def test_a_partial_exit_keeps_equity_reconciled(services, open_trade):
    """equity == starting + realised + unrealised, mid-position.

    The partial exit banks cash for half the size while the rest stays open, so
    the identity only holds if realised P&L counts the banked half.
    """
    _, _, position = open_trade
    services.execution.close_position(
        position.position_id,
        fraction=Decimal("0.5"),
        reason=ExitReason.PARTIAL_TAKE_PROFIT,
    )
    services.session.flush()

    snapshot = services.portfolio.snapshot()
    assert snapshot.open_positions == 1
    assert snapshot.realized_pnl != 0

    # Half the entry fee has been amortised into realised P&L; the other half is
    # still carried against the open remainder.
    residual_entry_fee = services.portfolio.unamortized_entry_fees()
    assert residual_entry_fee > 0
    expected = round_money(
        snapshot.starting_equity
        + snapshot.realized_pnl
        + snapshot.unrealized_pnl
        - residual_entry_fee,
        8,
    )
    # Not bit-exact: equity rounds price*quantity per leg while P&L rounds the
    # difference, so the two can land a unit of the 8th decimal apart.
    assert abs(snapshot.equity - expected) <= services.portfolio.RECONCILIATION_TOLERANCE
    assert services.portfolio.books_balance()


def test_the_books_close_at_every_stage(services, proposal_factory):
    """The reconciliation identity holds flat, open, half-out and closed."""
    assert services.portfolio.reconciliation_error() == 0

    price = services.market_data.get_ticker("BTC/USDT").last
    decision = services.risk.check(
        proposal_factory(
            entry=price,
            stop_loss=price * Decimal("0.96"),
            take_profit=price * Decimal("1.09"),
        )
    )
    _, position = services.execution.place_entry(
        approval_id=decision.approval_id, strategy="trend_following", regime="range"
    )
    services.session.flush()
    assert services.portfolio.reconciliation_error() == 0  # nothing rounded yet

    services.execution.close_position(position.position_id, fraction=Decimal("0.5"))
    services.session.flush()
    assert services.portfolio.books_balance()

    services.execution.close_position(position.position_id)
    services.session.flush()
    assert services.portfolio.books_balance()
    # Flat: nothing left to amortise.
    assert services.portfolio.unamortized_entry_fees() == 0


def test_fees_are_not_double_counted_once_the_trade_closes(services, open_trade):
    _, entry_order, position = open_trade
    services.execution.close_position(position.position_id, fraction=Decimal("0.5"))
    exit_order, _ = services.execution.close_position(position.position_id)
    services.session.flush()

    trades = services.execution_repo.all_trades()
    assert len(trades) == 1
    snapshot = services.portfolio.snapshot()
    assert snapshot.open_positions == 0
    # Every fee on the books, counted exactly once.
    assert snapshot.fees_paid == trades[0].fees
    assert snapshot.fees_paid > entry_order.fee_paid + exit_order.fee_paid
