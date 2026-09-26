"""Source adapters: detection, market data, exit simulation.

Each adapter returns a parsed dataclass AND the raw body, so the caller can
archive what was actually received. Parsing is kept separate from fetching so
the parsers can be tested offline against fixtures -- which is how the PoC's
parsing is verified without touching the network.

NOTHING in this module can sign or send a transaction. There is no keypair, no
signing import, and no send path. The exit simulation uses a public key we do
not control, which is all `simulateTransaction` requires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from probe.constants import SIMULATION_FEE_PAYER, WSOL_MINT

TIMEOUT = httpx.Timeout(20.0)


# --------------------------------------------------------------------- types
@dataclass
class Fetched:
    """A response plus everything needed to archive and audit it."""
    source: str
    endpoint: str
    http_status: int | None
    body: str | None
    fetched_ts: datetime
    parsed: Any = None
    error: str | None = None


@dataclass
class MarketSnapshot:
    pair_address: str | None = None
    dex: str | None = None
    quote_mint: str | None = None
    name: str | None = None
    symbol: str | None = None
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    volume_5m: float | None = None
    volume_1h: float | None = None
    volume_24h: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    pair_created_ms: int | None = None


# Why an exit attempt produced no answer. 'transport' is the critical one: it
# means OUR side failed, so the row is not evidence about the token at all.
FAILURE_NO_ROUTE = "no_route"
FAILURE_TRANSPORT = "transport"
FAILURE_BUILD = "build_failed"
FAILURE_REVERTED = "reverted"
FAILURE_UNPARSEABLE = "unparseable"
FAILURE_BAD_REQUEST = "bad_request"

# Jupiter says "there is genuinely no way to sell this" with one of these.
# Anything else non-200 is OUR problem -- a malformed amount, a rate limit, an
# outage -- and must never be recorded as a fact about the token. A 400 Bad
# Request was being parsed as "no route", which turned every bad request we
# sent into evidence that a token was unsellable.
_NO_ROUTE_MARKERS = (
    "could_not_find_any_route", "could not find any route",
    "no_routes_found", "no routes found", "no route found",
    "routenotfound", "no_route",
)


def says_no_route(body: str | None) -> bool:
    """True only when the response explicitly reports a routing failure."""
    if not body:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _NO_ROUTE_MARKERS)


@dataclass
class ExitSimulation:
    method: str
    notional_usd: float
    succeeded: bool | None
    input_amount_raw: str | None = None
    expected_output_raw: str | None = None
    price_impact_pct: float | None = None
    slippage_bps: int | None = None
    route_dex: str | None = None
    route_hops: int | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ parsing
def _f(value: Any) -> float | None:
    """Parse a number, preserving the difference between missing and zero."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_dexscreener(body: str) -> MarketSnapshot | None:
    """Pull one snapshot from a DexScreener response.

    Accepts both response shapes: a bare list (token-pairs) and {"pairs": [...]}
    (latest/dex/tokens). Picks the deepest pool, because that is the one an exit
    would realistically route through -- not the first in an arbitrary order.

    Returns None when there are no pairs at all. That is a real and important
    state: the token exists on-chain but nothing is trading it yet.
    """
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None

    pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    if not pairs:
        return None

    def depth(pair: dict) -> float:
        return _f((pair.get("liquidity") or {}).get("usd")) or 0.0

    pair = max(pairs, key=depth)
    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}

    return MarketSnapshot(
        pair_address=pair.get("pairAddress"),
        dex=pair.get("dexId"),
        quote_mint=quote.get("address"),
        name=base.get("name"),
        symbol=base.get("symbol"),
        price_usd=_f(pair.get("priceUsd")),
        price_native=_f(pair.get("priceNative")),
        liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
        market_cap_usd=_f(pair.get("marketCap")),
        fdv_usd=_f(pair.get("fdv")),
        volume_5m=_f(volume.get("m5")),
        volume_1h=_f(volume.get("h1")),
        volume_24h=_f(volume.get("h24")),
        buys_5m=_i((txns.get("m5") or {}).get("buys")),
        sells_5m=_i((txns.get("m5") or {}).get("sells")),
        buys_1h=_i((txns.get("h1") or {}).get("buys")),
        sells_1h=_i((txns.get("h1") or {}).get("sells")),
        pair_created_ms=_i(pair.get("pairCreatedAt")),
    )


def parse_jupiter_quote(body: str, notional_usd: float) -> ExitSimulation:
    """Turn a Jupiter response into a sellable / not-sellable verdict.

    A missing route is not an error to swallow -- it IS the finding. It means
    no path existed to convert this token back into SOL at that moment, which
    is precisely the condition that makes a paper gain unrealisable.
    """
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        # A body we cannot read is our problem, not the token's.
        return ExitSimulation("quote", notional_usd, None,
                              failure_kind=FAILURE_UNPARSEABLE,
                              failure_reason="unparseable response")

    out = data.get("outAmount")
    if not out:
        reason = data.get("error") or data.get("errorCode") or "no route returned"
        # We asked and got a real answer: there is no way out. THE finding.
        return ExitSimulation("quote", notional_usd, False,
                              failure_kind=FAILURE_NO_ROUTE,
                              failure_reason=str(reason)[:500], raw=data)

    route = data.get("routePlan") or []
    labels = [str((hop.get("swapInfo") or {}).get("label", "")) for hop in route]
    return ExitSimulation(
        method="quote",
        notional_usd=notional_usd,
        succeeded=True,
        input_amount_raw=str(data.get("inAmount")) if data.get("inAmount") else None,
        expected_output_raw=str(out),
        price_impact_pct=_f(data.get("priceImpactPct")),
        slippage_bps=_i(data.get("slippageBps")),
        route_dex=" > ".join(x for x in labels if x) or None,
        route_hops=len(route),
        raw=data,
    )


# ------------------------------------------------------------------ fetching
def fetch_dexscreener(client: httpx.Client, mint: str, base: str) -> Fetched:
    url = f"{base}/token-pairs/v1/solana/{mint}"
    now = datetime.now(UTC)
    try:
        resp = client.get(url, timeout=TIMEOUT)
        return Fetched("dexscreener", url, resp.status_code, resp.text, now,
                       parsed=parse_dexscreener(resp.text) if resp.status_code == 200 else None,
                       error=None if resp.status_code == 200 else f"HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        return Fetched("dexscreener", url, None, None, now,
                       error=f"{type(exc).__name__}: {exc}")


def simulate_sell_quote(client: httpx.Client, quote_url: str, mint: str,
                        amount_raw: int, notional_usd: float,
                        slippage_bps: int = 300) -> Fetched:
    """Ask Jupiter what selling `amount_raw` of `mint` back to SOL would yield.

    Read-only. This is a routing calculation against live liquidity -- no
    wallet, no signature, no transaction. A non-200 or a missing route is
    recorded as a failed exit, not discarded.
    """
    params = {"inputMint": mint, "outputMint": WSOL_MINT,
              "amount": str(amount_raw), "slippageBps": str(slippage_bps)}
    now = datetime.now(UTC)
    try:
        resp = client.get(quote_url, params=params, timeout=TIMEOUT)
        parsed = parse_jupiter_quote(resp.text, notional_usd)
        if resp.status_code >= 500 or resp.status_code in (403, 429):
            # Rate limited, blocked or the API is down -- says nothing about
            # the token. Recorded as unknown so it cannot be miscounted.
            parsed.succeeded = None
            parsed.failure_kind = FAILURE_TRANSPORT
            parsed.failure_reason = f"HTTP {resp.status_code}"
        elif resp.status_code != 200 and parsed.succeeded:
            parsed.succeeded = False
            parsed.failure_kind = FAILURE_NO_ROUTE
            parsed.failure_reason = f"HTTP {resp.status_code}"
        return Fetched("jupiter", str(resp.request.url), resp.status_code,
                       resp.text, now, parsed=parsed)
    except Exception as exc:  # noqa: BLE001
        return Fetched("jupiter", quote_url, None, None, now,
                       parsed=ExitSimulation("quote", notional_usd, None,
                                             failure_kind=FAILURE_TRANSPORT,
                                             failure_reason=f"{type(exc).__name__}: {exc}"),
                       error=f"{type(exc).__name__}: {exc}")


def simulate_sell_rpc(client: httpx.Client, swap_url: str, rpc_url: str,
                      quote_body: dict, notional_usd: float) -> Fetched:
    """Build an unsigned swap and run it through simulateTransaction.

    Stronger than a quote: this executes against real chain state and catches
    frozen accounts, transfer hooks and failing token programs that a routing
    calculation cannot see.

    The fee payer is a public address we do not control. `sigVerify: false`
    means the node does not require -- and we never produce -- a signature.
    No private key exists anywhere in this codebase.
    """
    now = datetime.now(UTC)
    try:
        built = client.post(swap_url, json={
            "quoteResponse": quote_body,
            "userPublicKey": SIMULATION_FEE_PAYER,
            "wrapAndUnwrapSol": True,
        }, timeout=TIMEOUT)
        if built.status_code != 200:
            return Fetched("jupiter-swap", swap_url, built.status_code, built.text, now,
                           parsed=ExitSimulation("rpc_sim", notional_usd, None,
                                                 failure_kind=FAILURE_BUILD,
                                                 failure_reason=f"build failed "
                                                                f"HTTP {built.status_code}"))
        tx = (built.json() or {}).get("swapTransaction")
        if not tx:
            return Fetched("jupiter-swap", swap_url, built.status_code, built.text, now,
                           parsed=ExitSimulation("rpc_sim", notional_usd, None,
                                                 failure_kind=FAILURE_BUILD,
                                                 failure_reason="no swapTransaction returned"))

        sim = client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [tx, {"encoding": "base64", "sigVerify": False,
                            "replaceRecentBlockhash": True}],
        }, timeout=TIMEOUT)
        body = sim.json() if sim.content else {}
        value = ((body.get("result") or {}).get("value")) or {}
        err = value.get("err")
        return Fetched("solana-rpc", rpc_url, sim.status_code, sim.text, now,
                       parsed=ExitSimulation(
                           method="rpc_sim", notional_usd=notional_usd,
                           succeeded=err is None and "result" in body,
                           failure_kind=None if err is None else FAILURE_REVERTED,
                           failure_reason=None if err is None else json.dumps(err)[:500],
                           raw=value))
    except Exception as exc:  # noqa: BLE001
        return Fetched("solana-rpc", rpc_url, None, None, now,
                       parsed=ExitSimulation("rpc_sim", notional_usd, None,
                                             failure_kind=FAILURE_TRANSPORT,
                                             failure_reason=f"{type(exc).__name__}: {exc}"),
                       error=f"{type(exc).__name__}: {exc}")
