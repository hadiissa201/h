"""Parser behaviour, verified offline against fixtures.

The network is unreachable from the build environment, so these fixtures are
hand-built to the documented response shapes. They prove the PARSING logic is
correct; they do NOT prove the live API returns this shape. That second half is
what the probe is for, and until the probe runs on the target machine the
field names here remain unverified.
"""

from __future__ import annotations

import json

from poc.sources import (
    FAILURE_NO_ROUTE,
    FAILURE_UNPARSEABLE,
    parse_dexscreener,
    parse_jupiter_quote,
)


def pair(liquidity: float, price: str = "0.000123", **over) -> dict:
    base = {
        "pairAddress": f"pair{int(liquidity)}",
        "dexId": "raydium",
        "baseToken": {"address": "MINT", "name": "Test", "symbol": "TST"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
        "priceUsd": price,
        "priceNative": "0.0000004",
        "liquidity": {"usd": liquidity},
        "marketCap": 50000,
        "fdv": 60000,
        "volume": {"m5": 1200.5, "h1": 8000, "h24": 40000},
        "txns": {"m5": {"buys": 30, "sells": 12}, "h1": {"buys": 200, "sells": 150}},
        "pairCreatedAt": 1739000000000,
    }
    base.update(over)
    return base


def test_the_deepest_pool_is_chosen_not_the_first():
    """An exit routes through the deepest pool, so that is the one we record."""
    body = json.dumps([pair(500.0), pair(90000.0), pair(1200.0)])
    snap = parse_dexscreener(body)
    assert snap is not None
    assert snap.liquidity_usd == 90000.0
    assert snap.pair_address == "pair90000"


def test_both_response_shapes_are_accepted():
    as_list = parse_dexscreener(json.dumps([pair(1000.0)]))
    as_dict = parse_dexscreener(json.dumps({"pairs": [pair(1000.0)]}))
    assert as_list is not None and as_dict is not None
    assert as_list.pair_address == as_dict.pair_address


def test_no_pairs_returns_none_rather_than_a_zeroed_snapshot():
    """A token nobody trades yet is a real state, not a token worth $0."""
    assert parse_dexscreener(json.dumps([])) is None
    assert parse_dexscreener(json.dumps({"pairs": []})) is None


def test_missing_fields_become_none_never_zero():
    """Conflating 'no data' with 'zero' would turn gaps into evidence of death."""
    thin = pair(1000.0)
    del thin["marketCap"]
    thin["volume"] = {}
    thin["txns"] = {}
    snap = parse_dexscreener(json.dumps([thin]))
    assert snap is not None
    assert snap.market_cap_usd is None
    assert snap.volume_5m is None
    assert snap.buys_5m is None
    assert snap.liquidity_usd == 1000.0


def test_unparseable_body_returns_none_rather_than_raising():
    assert parse_dexscreener("not json at all") is None
    assert parse_dexscreener("") is None


def test_numbers_arriving_as_strings_are_parsed():
    snap = parse_dexscreener(json.dumps([pair(1000.0, price="0.00000000456")]))
    assert snap is not None
    assert snap.price_usd == 4.56e-9


# ----------------------------------------------------------------- Jupiter
def test_a_routed_quote_is_recorded_as_sellable():
    body = json.dumps({
        "inAmount": "1000000", "outAmount": "45000000",
        "priceImpactPct": "0.0123", "slippageBps": 300,
        "routePlan": [{"swapInfo": {"label": "Raydium"}},
                      {"swapInfo": {"label": "Orca"}}],
    })
    sim = parse_jupiter_quote(body, 100.0)
    assert sim.succeeded is True
    assert sim.expected_output_raw == "45000000"
    assert sim.route_hops == 2
    assert sim.route_dex == "Raydium > Orca"
    assert sim.price_impact_pct == 0.0123


def test_no_route_is_a_finding_not_an_error():
    """'Cannot sell' is the single most important thing this research records."""
    sim = parse_jupiter_quote(json.dumps({"error": "No routes found"}), 100.0)
    assert sim.succeeded is False
    assert sim.failure_kind == FAILURE_NO_ROUTE
    assert "No routes" in (sim.failure_reason or "")


def test_an_empty_outamount_is_not_sellable():
    sim = parse_jupiter_quote(json.dumps({"outAmount": None}), 100.0)
    assert sim.succeeded is False


def test_a_garbage_response_is_UNKNOWN_not_unsellable():
    """Our failure to read a response says nothing about the token.

    Recording it as "not sellable" would let one bad gateway manufacture
    evidence that a token could not be exited.
    """
    sim = parse_jupiter_quote("<html>502 Bad Gateway</html>", 100.0)
    assert sim.succeeded is None
    assert sim.failure_kind == FAILURE_UNPARSEABLE


def test_no_route_and_transport_failure_are_distinguishable():
    """The whole dataset's integrity rests on this distinction."""
    no_route = parse_jupiter_quote(json.dumps({"error": "No routes found"}), 100.0)
    unreadable = parse_jupiter_quote("gateway timeout", 100.0)
    assert no_route.succeeded is False          # we asked; the answer was no
    assert no_route.failure_kind == FAILURE_NO_ROUTE
    assert unreadable.succeeded is None         # we never got an answer
    assert unreadable.failure_kind == FAILURE_UNPARSEABLE


def test_the_notional_travels_with_the_result():
    """Price impact is meaningless without knowing the size that produced it."""
    sim = parse_jupiter_quote(json.dumps({"outAmount": "1", "routePlan": []}), 500.0)
    assert sim.notional_usd == 500.0
