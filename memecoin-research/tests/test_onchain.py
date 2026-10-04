"""Reading a pump.fun bonding curve from the chain.

The layout is an assumption until a real account confirms it, so these tests
pin the two things that would silently corrupt the data if I got them wrong:
refusing to parse an account that is not a curve, and never confusing the
reserves that set the PRICE with the reserves that can actually PAY.
"""

from __future__ import annotations

import base64
import struct

import pytest

from collector.onchain import (
    BONDING_CURVE_DISCRIMINATOR,
    MIN_ACCOUNT_BYTES,
    LayoutMismatch,
    parse_bonding_curve,
)


def account(virtual_tokens=1_073_000_191_000_000, virtual_sol=30_000_000_000,
            real_tokens=793_100_000_000_000, real_sol=5_000_000_000,
            supply=1_000_000_000_000_000, complete=0,
            discriminator=BONDING_CURVE_DISCRIMINATOR, tail=b"") -> str:
    raw = struct.pack("<8s5QB", discriminator, virtual_tokens, virtual_sol,
                      real_tokens, real_sol, supply, complete) + tail
    return base64.b64encode(raw).decode()


# ------------------------------------------------- refusing the wrong account
def test_a_foreign_account_is_refused_not_parsed():
    """Without this the parser reads arbitrary bytes as reserves and returns a
    confident price for something that is not a bonding curve."""
    with pytest.raises(LayoutMismatch):
        parse_bonding_curve(account(discriminator=b"\x00" * 8))


def test_a_truncated_account_is_refused():
    short = base64.b64encode(b"\x00" * (MIN_ACCOUNT_BYTES - 1)).decode()
    with pytest.raises(LayoutMismatch):
        parse_bonding_curve(short)


def test_trailing_bytes_do_not_prevent_parsing():
    """Anchor accounts are often padded; extra bytes are not an error."""
    assert parse_bonding_curve(account(tail=b"\x00" * 64)).virtual_sol_reserves


# ---------------------------------------------- price comes from VIRTUAL
def test_price_uses_the_virtual_reserves():
    curve = parse_bonding_curve(account(virtual_sol=30_000_000_000,
                                        virtual_tokens=1_000_000_000_000))
    # 30 SOL against 1,000,000 tokens (6 decimals) = 0.00003 SOL each.
    assert curve.price_sol(6) == pytest.approx(30.0 / 1_000_000)


def test_price_respects_token_decimals():
    """A 9-decimal token read as 6 would be priced 1000x wrong."""
    six = parse_bonding_curve(account()).price_sol(6)
    nine = parse_bonding_curve(account()).price_sol(9)
    assert six == pytest.approx(nine * 1000)


def test_an_empty_curve_has_no_price_rather_than_zero():
    assert parse_bonding_curve(account(virtual_tokens=0)).price_sol() is None
    assert parse_bonding_curve(account(virtual_sol=0)).price_sol() is None


# ------------------------------------------- what can actually be extracted
def test_extractable_sol_comes_from_the_REAL_reserves():
    """The distinction that matters.

    Virtual reserves shape the curve and are partly fiction. The SOL a seller
    can actually receive is capped by what is really in the account. Reporting
    virtual reserves as liquidity would overstate exit capacity on every single
    token -- the same error this project has already made in several forms.
    """
    curve = parse_bonding_curve(account(virtual_sol=30_000_000_000,
                                        real_sol=5_000_000_000))
    assert curve.extractable_sol() == pytest.approx(5.0)
    assert curve.virtual_sol_reserves / 10**9 == pytest.approx(30.0)


def test_extractable_sol_is_never_negative():
    assert parse_bonding_curve(account(real_sol=0)).extractable_sol() == 0.0


# ------------------------------------------------------------- graduation
def test_the_complete_flag_is_read():
    """A graduated token has left the curve, so the curve no longer holds its
    liquidity and a price from it would be stale."""
    assert parse_bonding_curve(account(complete=1)).complete is True
    assert parse_bonding_curve(account(complete=0)).complete is False


def test_the_discriminator_is_derived_not_pasted():
    import hashlib
    assert BONDING_CURVE_DISCRIMINATOR == hashlib.sha256(
        b"account:BondingCurve").digest()[:8]
