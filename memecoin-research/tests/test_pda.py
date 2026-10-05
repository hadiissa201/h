"""Deriving a program address without a network call or a dependency.

Replaced getTokenLargestAccounts, which is among the most expensive RPC methods
and exhausted the quota on forty tokens. These tests pin the properties that
make a derived address trustworthy, because a wrong one would simply read an
account that does not exist and the failure would look like a missing token.
"""

from __future__ import annotations

import pytest

from collector.pda import (
    b58decode,
    b58encode,
    bonding_curve_address,
    find_program_address,
    is_on_curve,
)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"


# --------------------------------------------------------------- base58
@pytest.mark.parametrize("key", [SYSTEM, TOKEN_PROGRAM, PUMP])
def test_base58_round_trips_a_public_key(key):
    assert b58encode(b58decode(key)) == key
    assert len(b58decode(key)) == 32


def test_leading_zero_bytes_survive():
    """The system program is 32 zero bytes; dropping them would shorten the key
    and silently derive a different address."""
    assert b58decode(SYSTEM) == b"\x00" * 32
    assert b58encode(b"\x00" * 32) == SYSTEM


def test_a_non_base58_character_is_refused():
    with pytest.raises(ValueError):
        b58decode("not-base58!")


# ------------------------------------------------------------ curve test
def test_a_real_program_id_is_on_the_curve():
    assert is_on_curve(b58decode(TOKEN_PROGRAM))


def test_a_derived_address_is_off_the_curve():
    """The whole mechanism: a PDA must be somewhere no private key can reach."""
    address, _ = find_program_address([b"bonding-curve", b"\x01" * 32], PUMP)
    assert not is_on_curve(b58decode(address))


def test_wrong_length_is_not_a_point():
    assert is_on_curve(b"\x00" * 31) is False


# ----------------------------------------------------------- derivation
def test_derivation_is_deterministic():
    first = bonding_curve_address(TOKEN_PROGRAM, PUMP)
    assert first == bonding_curve_address(TOKEN_PROGRAM, PUMP)


def test_different_mints_derive_different_curves():
    assert bonding_curve_address(TOKEN_PROGRAM, PUMP) != bonding_curve_address(SYSTEM, PUMP)


def test_the_seed_matters():
    """'bonding-curve' is the literal pump.fun uses; another seed must not
    collide with it, or we would read an unrelated account."""
    curve, _ = find_program_address([b"bonding-curve", b58decode(TOKEN_PROGRAM)], PUMP)
    other, _ = find_program_address([b"something-else", b58decode(TOKEN_PROGRAM)], PUMP)
    assert curve != other


def test_the_program_matters():
    a, _ = find_program_address([b"bonding-curve", b58decode(SYSTEM)], PUMP)
    b, _ = find_program_address([b"bonding-curve", b58decode(SYSTEM)], TOKEN_PROGRAM)
    assert a != b


def test_the_bump_is_a_byte():
    _, bump = find_program_address([b"bonding-curve", b58decode(SYSTEM)], PUMP)
    assert 0 <= bump <= 255


def test_the_canonical_bump_is_the_highest_that_works():
    """Solana clients take the FIRST off-curve candidate walking down from 255.
    Taking a different one derives a valid but wrong address."""
    address, bump = find_program_address([b"bonding-curve", b58decode(SYSTEM)], PUMP)
    import hashlib
    for higher in range(255, bump, -1):
        digest = hashlib.sha256()
        digest.update(b"bonding-curve")
        digest.update(b58decode(SYSTEM))
        digest.update(bytes([higher]))
        digest.update(b58decode(PUMP))
        digest.update(b"ProgramDerivedAddress")
        assert is_on_curve(digest.digest()), (
            f"bump {higher} was off-curve and should have been chosen first")
