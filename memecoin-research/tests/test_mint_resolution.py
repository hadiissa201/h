"""Resolving a creation transaction into the mint it created.

This is where a launchpad sampling bias came from. Reading only
postTokenBalances works for pump.fun, which mints the full supply to its
bonding curve in the same transaction, and fails for Raydium LaunchLab, which
does not. The audit measured the consequence: 51.8% of LaunchLab detections
discarded as unresolvable against 17.7% of pump.fun's. Any conclusion drawn
from a sample skewed 34 points by one launchpad is a conclusion about our
resolver.
"""

from __future__ import annotations

from probe.checks_http import _mints_in_transaction

WSOL = "So11111111111111111111111111111111111111112"
NEW_MINT = "7xKiNEwMiNtAddre55000000000000000000000000"


def test_pumpfun_shape_resolves_from_balances():
    """The supply lands in a token account, so postTokenBalances carries it."""
    result = {"meta": {"postTokenBalances": [
        {"mint": NEW_MINT, "uiTokenAmount": {"amount": "1000000000000000"}},
        {"mint": WSOL, "uiTokenAmount": {"amount": "30000000"}},
    ]}}
    assert _mints_in_transaction(result) == {NEW_MINT}


def test_launchlab_shape_resolves_from_the_initialize_instruction():
    """No post balances at all -- the old code returned nothing and gave up."""
    result = {
        "meta": {
            "postTokenBalances": [],
            "innerInstructions": [{"instructions": [
                {"parsed": {"type": "createAccount", "info": {}}},
                {"parsed": {"type": "initializeMint2",
                            "info": {"mint": NEW_MINT, "decimals": 6}}},
            ]}],
        },
        "transaction": {"message": {"instructions": [
            {"programId": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"},
        ]}},
    }
    assert _mints_in_transaction(result) == {NEW_MINT}


def test_top_level_initialize_mint_is_read_too():
    result = {"meta": {}, "transaction": {"message": {"instructions": [
        {"parsed": {"type": "initializeMint", "info": {"mint": NEW_MINT}}},
    ]}}}
    assert _mints_in_transaction(result) == {NEW_MINT}


def test_wrapped_sol_is_never_mistaken_for_a_launch():
    """Wrapping SOL initialises a token account, not a new coin."""
    result = {"meta": {"postTokenBalances": [{"mint": WSOL}],
                       "innerInstructions": [{"instructions": [
                           {"parsed": {"type": "initializeMint2",
                                       "info": {"mint": WSOL}}}]}]}}
    assert _mints_in_transaction(result) == set()


def test_a_transaction_that_created_nothing_yields_nothing():
    """A create-like log does not guarantee a mint was born."""
    assert _mints_in_transaction({"meta": {}, "transaction": {}}) == set()


def test_both_routes_agree_rather_than_duplicating():
    """pump.fun satisfies both reads; that is one mint, not two."""
    result = {"meta": {
        "postTokenBalances": [{"mint": NEW_MINT}],
        "innerInstructions": [{"instructions": [
            {"parsed": {"type": "initializeMint2", "info": {"mint": NEW_MINT}}}]}],
    }}
    assert _mints_in_transaction(result) == {NEW_MINT}
