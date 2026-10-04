"""Read price and liquidity from the chain instead of from an aggregator.

The oracle run settled why this matters. Of 1,764 tokens, 1,629 were excluded
from analysis and 1,564 of those exclusions were OUR missing data rather than a
market test failing -- 1,282 of them simply had no liquidity figure, because
DexScreener never indexed the pool. We were seeing 7.6% of the tokens we
detected, and that 7.6% is whatever an aggregator thought worth indexing, which
skews toward the ones that grew enough to be noticed.

A pump.fun token's price IS its bonding curve. The curve account holds the
reserves; price is one divided by the other. One getAccountInfo call, available
from the block the token launches, for every token rather than the indexed few.

Two distinctions this module is careful about, because getting either wrong
would quietly flatter the data:

VIRTUAL RESERVES PRICE. REAL RESERVES PAY. The virtual reserves set the quoted
price and are partly fiction -- they exist to shape the curve. The SOL you can
actually extract is capped by real_sol_reserves. Using virtual reserves as
"liquidity" would overstate exit capacity on every token, which is the mistake
this project has made in a dozen other forms.

THE LAYOUT IS AN ASSUMPTION UNTIL IT IS CHECKED. Anchor accounts begin with
sha256("account:<Name>")[:8], so the parser verifies that before reading a
single field. A wrong layout would otherwise produce confident nonsense, and
verify_onchain.py cross-checks the result against DexScreener on tokens where
both exist.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

import httpx

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL_DECIMALS = 9
# Anchor's discriminator for the BondingCurve account, derived rather than
# pasted so it cannot drift from the convention it comes from.
BONDING_CURVE_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]

# discriminator + 5 u64 + 1 bool
_LAYOUT = "<8s5QB"
MIN_ACCOUNT_BYTES = struct.calcsize(_LAYOUT)


class LayoutMismatch(ValueError):
    """The account is not the shape we believe it is. Never parse past this."""


@dataclass(frozen=True)
class BondingCurve:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool

    def price_sol(self, token_decimals: int = 6) -> float | None:
        """SOL per token, from the VIRTUAL reserves, which are what price it."""
        if self.virtual_token_reserves <= 0 or self.virtual_sol_reserves <= 0:
            return None
        sol = self.virtual_sol_reserves / 10 ** WSOL_DECIMALS
        tokens = self.virtual_token_reserves / 10 ** token_decimals
        return sol / tokens if tokens > 0 else None

    def extractable_sol(self) -> float:
        """SOL actually sitting in the curve -- the ceiling on what any sell can
        pay, regardless of what the quoted price implies."""
        return max(0.0, self.real_sol_reserves / 10 ** WSOL_DECIMALS)


def parse_bonding_curve(data_base64: str) -> BondingCurve:
    """Decode a curve account, refusing anything that is not one."""
    raw = base64.b64decode(data_base64)
    if len(raw) < MIN_ACCOUNT_BYTES:
        raise LayoutMismatch(
            f"account is {len(raw)} bytes, need at least {MIN_ACCOUNT_BYTES}")
    discriminator, vt, vs, rt, rs, supply, complete = struct.unpack(
        _LAYOUT, raw[:MIN_ACCOUNT_BYTES])
    if discriminator != BONDING_CURVE_DISCRIMINATOR:
        raise LayoutMismatch(
            f"discriminator {discriminator.hex()} is not a BondingCurve "
            f"({BONDING_CURVE_DISCRIMINATOR.hex()}); the layout assumption is "
            f"wrong and nothing below it can be trusted")
    return BondingCurve(virtual_token_reserves=vt, virtual_sol_reserves=vs,
                        real_token_reserves=rt, real_sol_reserves=rs,
                        token_total_supply=supply, complete=bool(complete))


def _rpc(client: httpx.Client, url: str, method: str, params: list,
         timeout: float = 20.0) -> dict:
    resp = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                  "method": method, "params": params},
                       timeout=timeout)
    return resp.json() or {}


def find_bonding_curve(client: httpx.Client, rpc_url: str, mint: str,
                       timeout: float = 20.0) -> tuple[str | None, str]:
    """The curve's address, via its token account rather than PDA arithmetic.

    Deriving the PDA needs ed25519 point validation; the curve is also simply
    the largest holder of its own token, so its address is the OWNER of the
    biggest token account. Two calls, no cryptography, and it fails loudly
    rather than guessing.

    Resolved once per token and stored, so routine observations cost one call.
    """
    try:
        body = _rpc(client, rpc_url, "getTokenLargestAccounts", [mint], timeout)
        accounts = ((body.get("result") or {}).get("value")) or []
    except Exception as exc:  # noqa: BLE001
        return None, f"largest accounts failed: {type(exc).__name__}"
    if not accounts:
        return None, "no token accounts exist yet"

    try:
        info = _rpc(client, rpc_url, "getAccountInfo",
                    [accounts[0].get("address"), {"encoding": "jsonParsed"}], timeout)
        value = ((info.get("result") or {}).get("value")) or {}
        owner = ((value.get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
    except Exception as exc:  # noqa: BLE001
        return None, f"owner lookup failed: {type(exc).__name__}"
    if not owner:
        return None, "largest token account has no owner"
    return owner, f"curve {owner[:8]}... holds the largest balance"


def read_curve(client: httpx.Client, rpc_url: str, curve_address: str,
               timeout: float = 20.0) -> tuple[BondingCurve | None, str]:
    """One call. Returns (curve, detail); a None curve carries the reason."""
    try:
        body = _rpc(client, rpc_url, "getAccountInfo",
                    [curve_address, {"encoding": "base64"}], timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    value = ((body.get("result") or {}).get("value")) or {}
    data = value.get("data")
    if not data:
        return None, "account does not exist or is empty"
    encoded = data[0] if isinstance(data, list) else data
    try:
        return parse_bonding_curve(encoded), "ok"
    except LayoutMismatch as exc:
        return None, str(exc)


def sol_price_usd(client: httpx.Client, quote_url: str,
                  timeout: float = 20.0) -> float | None:
    """One SOL in USDC, for converting curve reserves into dollars."""
    try:
        resp = client.get(quote_url, params={
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "amount": str(10 ** WSOL_DECIMALS), "slippageBps": "50"},
            timeout=timeout)
        out = (resp.json() or {}).get("outAmount")
        return float(out) / 10 ** 6 if out else None
    except Exception:  # noqa: BLE001
        return None
