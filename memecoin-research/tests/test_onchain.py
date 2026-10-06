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


def account(virtual_tokens=1_073_000_000_000_000, virtual_sol=30_000_000_000,
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


def test_the_documented_launch_state_confirms_the_layout():
    """What the live run actually produced: untraded curves holding pump.fun's
    four documented launch values. Four simultaneous exact matches cannot come
    from a misaligned read, so this is the strongest check available -- and it
    needs no traded curve, which a stopped collector cannot supply."""
    got = checks([curve(real_sol=0), curve(real_sol=0), curve(real_sol=0)])
    check = got["fields reproduce the documented launch state"]
    assert check.passed
    assert "four documented" in check.detail


def test_two_matching_curves_are_not_enough():
    got = checks([curve(real_sol=0), curve(real_sol=0)])
    assert not got["fields reproduce the documented launch state"].passed


def test_a_misaligned_read_fails_because_it_cannot_hit_four_constants():
    """The whole argument for this check: shift the fields and every one of
    the four documented values is wrong at once."""
    got = checks([curve(real_sol=0, virtual_sol=1_073_000_191_000_000,
                        virtual_tokens=4_242_424_242),
                  curve(real_sol=0, virtual_sol=1_073_000_191_000_000,
                        virtual_tokens=4_242_424_242),
                  curve(real_sol=0, virtual_sol=1_073_000_191_000_000,
                        virtual_tokens=4_242_424_242)])
    assert not got["fields reproduce the documented launch state"].passed


def test_one_odd_curve_does_not_fail_a_layout_four_others_confirmed():
    """The defect this replaced. The live run read four curves at exactly the
    documented launch state and one with 0.43 SOL virtual, and the old check
    reported the LAYOUT as wrong -- when four exact matches had just proved it
    right. An outlier is a question about that token, not about the struct."""
    odd = curve(real_sol=0, virtual_sol=426_629_411)
    got = checks([curve(real_sol=0), curve(real_sol=0), curve(real_sol=0), odd])
    assert got["fields reproduce the documented launch state"].passed


def test_the_odd_curve_is_still_reported_as_an_anomaly():
    """Confirming the parse must not bury the unexplained curve."""
    from collector.onchain import anomalies
    odd = curve(real_sol=0, virtual_sol=426_629_411)
    found = anomalies([curve(real_sol=0), odd])
    assert len(found) == 1
    assert "virtual_sol_reserves=426,629,411" in found[0]


def test_a_traded_curve_is_not_an_anomaly():
    """Real buys raise virtual SOL above the seed. That is the curve working."""
    from collector.onchain import anomalies
    assert anomalies([curve(virtual_sol=34_000_000_000,
                            real_sol=4_000_000_000)]) == []


def test_no_traded_curve_is_reported_as_untested_not_as_a_pass_of_substance():
    """Every curve read so far has held zero real SOL, so the field that caps
    a real exit has never been seen non-zero. That must be said out loud."""
    got = checks([curve(real_sol=0), curve(real_sol=0), curve(real_sol=0)])
    check = got["traded curves keep the same virtual seed"]
    assert check.passed
    assert "UNTESTED" in check.detail


def test_a_traded_curve_with_a_shifted_seed_does_fail():
    got = checks([curve(virtual_sol=99_000_000_000, real_sol=4_000_000_000)])
    assert not got["traded curves keep the same virtual seed"].passed


def test_transposed_reserve_pairs_are_caught():
    got = checks([curve(virtual_sol=1_000_000_000, real_sol=9_000_000_000)])
    assert not got["virtual reserves exceed real"].passed


def test_the_account_length_is_recorded_so_a_version_change_is_visible():
    """The one anomalous curve may simply be a different curve version. Length
    is where that shows up, and it was being thrown away."""
    assert curve(real_sol=0).raw_len == 49
