"""Attempts to route around the risk engine.

This is the security model of the whole system: **no order exists without a
valid, single-use risk approval that matches it.** Each test here is an attack on
that property. If any of them starts passing, the claim in the README is false.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import BotHaltedError, RiskRejectedError
from app.models.enums import SignalDirection
from app.risk.approval import proposal_fingerprint, verify_approval
from app.utils.time import utcnow


@pytest.fixture
def approved(services, proposal_factory):
    """A genuine approval for a modest BTC long."""
    price = services.market_data.get_ticker("BTC/USDT").last
    proposal = proposal_factory(
        entry=price,
        stop_loss=price * Decimal("0.96"),
        take_profit=price * Decimal("1.09"),
    )
    decision = services.risk.check(proposal)
    assert decision.approved, decision.reasons
    services.session.commit()
    return decision


# --------------------------------------------------------- no approval at all
def test_execution_without_an_approval_is_refused(services):
    with pytest.raises(RiskRejectedError, match="not found"):
        services.execution.place_entry(approval_id="ra_this_does_not_exist")


def test_a_made_up_approval_id_is_refused(services):
    with pytest.raises(RiskRejectedError):
        services.execution.place_entry(approval_id="")


def test_a_rejected_decision_yields_no_approval_id(services, proposal_factory):
    decision = services.risk.check(proposal_factory(confidence=0.1))
    assert not decision.approved
    assert decision.approval_id is None
    with pytest.raises(RiskRejectedError):
        services.execution.place_entry(approval_id="ra_rejected")


# ------------------------------------------------------------------- replay
def test_an_approval_cannot_be_used_twice(services, approved):
    services.execution.place_entry(approval_id=approved.approval_id)
    with pytest.raises(RiskRejectedError, match="already used"):
        services.execution.place_entry(
            approval_id=approved.approval_id, client_order_id="a-different-order"
        )


def test_a_retry_with_the_same_client_id_returns_the_original_position(services, approved):
    """Idempotent replay is allowed; a *second* position is not."""
    first_order, first_position = services.execution.place_entry(
        approval_id=approved.approval_id
    )
    second_order, second_position = services.execution.place_entry(
        approval_id=approved.approval_id
    )
    assert second_order.order_id == first_order.order_id
    assert second_position.position_id == first_position.position_id
    assert len(services.execution_repo.open_positions()) == 1


# -------------------------------------------------------------------- size
def test_an_order_larger_than_approved_is_refused(services, approved):
    with pytest.raises(RiskRejectedError, match="exceeds approved"):
        services.execution.place_entry(
            approval_id=approved.approval_id,
            quantity=approved.quantity * Decimal("2"),
        )


def test_a_smaller_order_than_approved_is_allowed(services, approved):
    """Reducing risk never needs new permission."""
    _, position = services.execution.place_entry(
        approval_id=approved.approval_id,
        quantity=approved.quantity / Decimal("2"),
    )
    assert position.quantity <= approved.quantity


# ------------------------------------------------------------------ symbol
def test_an_approval_for_one_symbol_cannot_trade_another(services, approved):
    with pytest.raises(RiskRejectedError, match="is for"):
        services.execution.place_entry(
            approval_id=approved.approval_id, symbol="ETH/USDT"
        )


# ------------------------------------------------------------------ expiry
def test_an_expired_approval_is_refused(services, approved):
    record = services.risk_repo.get_by_approval(approved.approval_id)
    record.expires_at = utcnow() - timedelta(seconds=1)
    services.session.flush()
    with pytest.raises(RiskRejectedError, match="expired"):
        services.execution.place_entry(approval_id=approved.approval_id)


def test_approvals_carry_a_short_ttl(services, approved, settings):
    record = services.risk_repo.get_by_approval(approved.approval_id)
    lifetime = (record.expires_at - record.created_at).total_seconds()
    assert 0 < lifetime <= settings.risk_approval_ttl_seconds + 5


# -------------------------------------------------------------- tampering
def test_editing_the_stored_quantity_invalidates_the_fingerprint(services, approved):
    """Someone with database access raising the approved size must not succeed."""
    record = services.risk_repo.get_by_approval(approved.approval_id)
    record.quantity = record.quantity * Decimal("10")
    services.session.flush()

    check = verify_approval(
        record,
        symbol=record.symbol,
        side="BUY",
        quantity=record.quantity,
        mode="paper",
    )
    assert not check.valid
    assert "fingerprint" in check.reason

    with pytest.raises(RiskRejectedError):
        services.execution.place_entry(approval_id=approved.approval_id)


def test_editing_the_stored_stop_invalidates_the_fingerprint(services, approved):
    record = services.risk_repo.get_by_approval(approved.approval_id)
    record.stop_loss = record.entry * Decimal("0.5")  # far wider risk
    services.session.flush()
    with pytest.raises(RiskRejectedError, match="fingerprint"):
        services.execution.place_entry(approval_id=approved.approval_id)


def test_fingerprint_covers_every_field_that_matters():
    base = {
        "symbol": "BTC/USDT",
        "direction": "BUY",
        "entry": Decimal("100"),
        "stop_loss": Decimal("96"),
        "quantity": Decimal("1"),
        "mode": "paper",
    }
    reference = proposal_fingerprint(**base)
    for field, value in (
        ("symbol", "ETH/USDT"),
        ("direction", "SELL"),
        ("entry", Decimal("101")),
        ("stop_loss", Decimal("95")),
        ("quantity", Decimal("1.1")),
        ("mode", "live"),
    ):
        assert proposal_fingerprint(**{**base, field: value}) != reference, field


def test_a_paper_approval_cannot_be_used_in_live_mode(services, approved):
    record = services.risk_repo.get_by_approval(approved.approval_id)
    check = verify_approval(
        record,
        symbol=record.symbol,
        side="BUY",
        quantity=record.quantity,
        mode="live",
    )
    assert not check.valid
    assert "mode" in check.reason


# ---------------------------------------------------------------- bot state
def test_a_halted_bot_refuses_entries_even_with_a_valid_approval(services, approved):
    services.bot_repo.halt("MANUAL", "test halt", "test")
    services.session.flush()
    with pytest.raises(BotHaltedError):
        services.execution.place_entry(approval_id=approved.approval_id)


def test_a_halted_bot_still_allows_exits(services, approved):
    """Reducing risk must never be blocked by the kill switch."""
    _, position = services.execution.place_entry(approval_id=approved.approval_id)
    services.bot_repo.halt("MAX_DRAWDOWN", "test", "test")
    services.session.flush()

    _, closed = services.execution.close_position(position.position_id)
    assert str(closed.status) == "CLOSED"


# --------------------------------------------------------------- direction
def test_a_short_approval_cannot_be_issued_in_spot_mode(services, proposal_factory):
    price = services.market_data.get_ticker("BTC/USDT").last
    decision = services.risk.check(
        proposal_factory(
            direction=SignalDirection.SELL,
            entry=price,
            stop_loss=price * Decimal("1.04"),
            take_profit=price * Decimal("0.92"),
        )
    )
    assert not decision.approved
    assert "SHORT_NOT_ALLOWED" in decision.rejection_codes


# ----------------------------------------------------- account state is ours
def test_a_caller_cannot_inflate_equity_through_the_payload(services, proposal_factory):
    """Account state is resolved server-side; the payload has no equity field.

    The proposal schema simply has nowhere to put a fake balance, and the sizing
    that comes back reflects the real account.
    """
    proposal = proposal_factory()
    assert not hasattr(proposal, "equity")
    decision = services.risk.check(proposal)
    real_equity = services.portfolio.equity()
    assert decision.account.equity == real_equity
    if decision.sizing:
        assert decision.sizing.equity == real_equity


def test_risk_rejections_are_recorded_for_audit(services, proposal_factory):
    services.risk.check(proposal_factory(confidence=0.05))
    services.session.flush()
    recent = services.risk_repo.recent(10)
    assert recent
    assert recent[0].decision == "REJECTED"
    assert recent[0].rejection_codes
    assert recent[0].checks
