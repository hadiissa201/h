"""Single-use risk approvals.

The mechanism that makes "no order may bypass the risk engine" structural rather
than a convention:

* ``/risk/check`` issues an ``approval_id`` bound to a fingerprint of the exact
  trade (symbol, direction, entry, stop, approved quantity, mode);
* ``/paper/order`` refuses to place anything without a valid approval, and
  re-verifies the fingerprint, the expiry, and that it has not been used;
* the approval is marked consumed inside the same transaction as the order.

So a compromised or over-enthusiastic n8n workflow — or an LLM told to "just
execute" — cannot produce an order the risk engine did not size and bless.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.errors import RiskRejectedError
from app.core.numeric import round_money
from app.utils.time import utcnow


def proposal_fingerprint(
    *,
    symbol: str,
    direction: str,
    entry: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    mode: str,
) -> str:
    payload = "|".join(
        [
            symbol.upper(),
            direction.upper(),
            str(round_money(entry, 8)),
            str(round_money(stop_loss, 8)),
            str(round_money(quantity, 8)),
            mode,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ApprovalCheck:
    valid: bool
    reason: str = ""
    approved_quantity: Decimal | None = None


def verify_approval(
    record,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    mode: str,
    now: datetime | None = None,
    quantity_tolerance: Decimal = Decimal("0.000001"),
) -> ApprovalCheck:
    """Validate an approval against the order actually being placed."""
    now = now or utcnow()
    if record is None:
        return ApprovalCheck(False, "risk approval not found")
    if record.decision != "APPROVED":
        return ApprovalCheck(False, f"risk approval is {record.decision}")
    if record.consumed_at is not None:
        return ApprovalCheck(
            False,
            f"risk approval already used by order {record.consumed_by_order_id}",
        )
    if record.expires_at is not None and record.expires_at <= now:
        return ApprovalCheck(
            False, f"risk approval expired at {record.expires_at.isoformat()}"
        )
    if record.mode != mode:
        return ApprovalCheck(
            False, f"risk approval was issued for mode {record.mode}, not {mode}"
        )
    if record.symbol.upper() != symbol.upper():
        return ApprovalCheck(
            False, f"risk approval is for {record.symbol}, not {symbol}"
        )
    if record.direction.upper() != side.upper():
        return ApprovalCheck(
            False, f"risk approval is for {record.direction}, not {side}"
        )
    approved_quantity = record.quantity or Decimal("0")
    if quantity > approved_quantity + quantity_tolerance:
        return ApprovalCheck(
            False,
            f"order quantity {quantity} exceeds approved {approved_quantity}",
            approved_quantity,
        )
    expected = proposal_fingerprint(
        symbol=record.symbol,
        direction=record.direction,
        entry=record.entry,
        stop_loss=record.stop_loss,
        quantity=approved_quantity,
        mode=record.mode,
    )
    if record.proposal_fingerprint and record.proposal_fingerprint != expected:
        # The stored decision no longer hashes to its own fingerprint: the row was
        # modified after issuance. Refuse, loudly.
        return ApprovalCheck(False, "risk approval fingerprint mismatch — tampering")
    return ApprovalCheck(True, "ok", approved_quantity)


def require_approval(check: ApprovalCheck) -> None:
    if not check.valid:
        raise RiskRejectedError(check.reason, stage="risk_approval")
