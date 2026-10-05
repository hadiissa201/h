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
    """A 9-decimal token read as 6 would be priced 1000x wrong.

    More decimals means the same raw reserve is FEWER whole tokens, so each one
    is worth more. I asserted this backwards the first time, which is exactly
    the error the test exists to catch in the parser.
    """
    six = parse_bonding_curve(account()).price_sol(6)
    nine = parse_bonding_curve(account()).price_sol(9)
    assert nine == pytest.approx(six * 1000)


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


# ------------------------------------------------------ the bool is a bool
def test_a_non_boolean_complete_byte_is_refused():
    """A misaligned read lands on a legal bool only 2 times in 256, so this
    byte is the cheapest check that the five u64s above it are the right
    widths in the right order. Without it, struct.unpack accepts anything and
    bool(7) is silently True."""
    with pytest.raises(LayoutMismatch, match="not a bool"):
        parse_bonding_curve(account(complete=7))


def test_both_legal_bool_values_still_parse():
    assert parse_bonding_curve(account(complete=0)).complete is False
    assert parse_bonding_curve(account(complete=1)).complete is True


# ------------------------------------------- judging the layout without a second source
def curve(**kw):
    return parse_bonding_curve(account(**kw))


def checks(curves):
    from collector.onchain import layout_evidence
    return {c.name: c for c in layout_evidence(curves)}


def test_no_curves_is_not_silently_a_pass():
    """The first real run read three curves and zero DexScreener prices, and
    the old script printed a paragraph implying success. An empty or absent
    sample must never read as confirmation."""
    from collector.onchain import layout_evidence
    result = layout_evidence([])
    assert result and not any(c.passed for c in result)


def test_a_constant_sol_seed_across_differing_curves_passes():
    """The load-bearing check: real buys differ, the fictional seed does not."""
    got = checks([
        curve(virtual_sol=30_000_000_000, real_sol=0),
        curve(virtual_sol=34_000_000_000, real_sol=4_000_000_000),
        curve(virtual_sol=41_500_000_000, real_sol=11_500_000_000),
    ])
    assert got["virtual SOL seed is one constant"].passed


def test_a_scattered_sol_seed_fails_because_that_is_a_wrong_offset():
    got = checks([
        curve(virtual_sol=30_000_000_000, real_sol=0),
        curve(virtual_sol=34_000_000_000, real_sol=1_000_000_000),
    ])
    check = got["virtual SOL seed is one constant"]
    assert not check.passed
    assert "offset is wrong" in check.detail


def test_a_constant_that_contradicts_the_documented_seed_is_flagged_not_passed():
    """One constant means the offset is probably right, but if it is not the
    documented 30 SOL then something is unexplained, and an unexplained
    constant must not be reported as a confirmation."""
    got = checks([
        curve(virtual_sol=99_000_000_000, real_sol=0),
        curve(virtual_sol=99_500_000_000, real_sol=500_000_000),
    ])
    check = got["virtual SOL seed is one constant"]
    assert not check.passed
    assert "documents" in check.detail


def test_graduated_only_sample_cannot_test_the_seed_and_says_so():
    """A graduated curve has been drained, so the invariant does not hold and
    its absence is not evidence either way."""
    got = checks([curve(complete=1), curve(complete=1)])
    check = got["virtual SOL seed is one constant"]
    assert not check.passed
    assert "graduated" in check.detail


def test_transposed_reserve_pairs_are_caught():
    got = checks([curve(virtual_sol=1_000_000_000, real_sol=9_000_000_000)])
    assert not got["virtual reserves exceed real"].passed


def test_a_varying_total_supply_fails():
    got = checks([curve(supply=1_000_000_000_000_000),
                  curve(supply=7_777_000_000_000)])
    assert not got["total supply is one constant"].passed


def test_untouched_curves_cannot_prove_the_invariant():
    """The trap the first live run fell into: three curves, every one at
    real_sol == 0, so virtual - real is identical no matter where the fields
    are read from. Identical-by-construction is not evidence."""
    got = checks([curve(real_sol=0), curve(real_sol=0), curve(real_sol=0)])
    check = got["virtual SOL seed is one constant"]
    assert not check.passed
    assert "DIFFERENT real SOL" in check.detail


def test_one_curve_alone_cannot_prove_the_invariant():
    got = checks([curve(real_sol=5_000_000_000)])
    assert not got["virtual SOL seed is one constant"].passed
