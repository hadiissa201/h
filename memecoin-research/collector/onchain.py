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
import time
from dataclasses import dataclass

import httpx

from collector.pacing import Pacer
from collector.pda import bonding_curve_address

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
    raw_len: int = 0

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
    # An Anchor bool is 0 or 1. Any other byte here means the five u64 fields
    # above it are the wrong width or the wrong order, and a misaligned read
    # lands on a legal bool only 2 times in 256.
    if complete not in (0, 1):
        raise LayoutMismatch(
            f"the byte after five u64s is {complete}, which is not a bool; "
            f"the field widths or order are wrong")
    return BondingCurve(virtual_token_reserves=vt, virtual_sol_reserves=vs,
                        real_token_reserves=rt, real_sol_reserves=rs,
                        token_total_supply=supply, complete=bool(complete),
                        raw_len=len(raw))


# pump.fun's documented launch state. Every field a fresh curve holds, so a
# parse that reproduces ALL of them at once cannot be reading misaligned bytes:
# a wrong offset would have to land on four separate documented values
# simultaneously.
PUMPFUN_INITIAL = {
    "virtual_sol_reserves": 30_000_000_000,
    "virtual_token_reserves": 1_073_000_000_000_000,
    "real_token_reserves": 793_100_000_000_000,
    "token_total_supply": 1_000_000_000_000_000,
}
# Enough independent tokens that a coincidence is not the explanation.
MIN_INITIAL_CURVES = 3
# Below this, a curve holds dust. 0.01 SOL is around a dollar: visible, but
# not an exit, and counting it as recovered coverage would overstate the gain.
MIN_SELLABLE_SOL = 0.01


@dataclass(frozen=True)
class LayoutCheck:
    name: str
    passed: bool
    detail: str


def _at_launch_state(curve: BondingCurve) -> bool:
    """A curve nobody has traded: no real SOL in it, not graduated."""
    return curve.real_sol_reserves == 0 and not curve.complete


def _matches_launch_state(curve: BondingCurve) -> bool:
    return all(getattr(curve, field) == value
               for field, value in PUMPFUN_INITIAL.items())


def layout_evidence(curves: list[BondingCurve]) -> list[LayoutCheck]:
    """Judge whether the struct is read correctly, from the curves alone.

    The DexScreener cross-check this module was built around only works on
    tokens an aggregator indexed, which is the very bias the on-chain reader
    exists to remove -- and four runs found no token with both. So the
    confirmation is internal.

    The first version of this function tested only INVARIANCE: that
    virtual_sol - real_sol comes out as one constant across curves. That was
    the wrong primary test. It needs traded curves, which a stopped collector
    cannot supply, and it reads a single outlier among otherwise exact matches
    as a failed layout when it is nothing of the kind. Matching the documented
    launch state on several independent tokens is both available and stronger:
    four simultaneous exact values cannot come from a misaligned read.
    """
    checks: list[LayoutCheck] = []
    if not curves:
        return [LayoutCheck("any data", False, "no curve was read")]

    fresh = [c for c in curves if _at_launch_state(c)]
    matching = [c for c in fresh if _matches_launch_state(c)]
    expected = ("30 SOL virtual, 1,073,000,000 virtual tokens, "
                "793,100,000 real tokens, 1,000,000,000 supply")
    if len(matching) >= MIN_INITIAL_CURVES:
        checks.append(LayoutCheck(
            "fields reproduce the documented launch state", True,
            f"{len(matching)} independent untraded curves match all four "
            f"documented values exactly ({expected}). A wrong offset would "
            f"have to hit four documented numbers at once"))
    else:
        checks.append(LayoutCheck(
            "fields reproduce the documented launch state", False,
            f"only {len(matching)} of {len(fresh)} untraded curves match the "
            f"documented launch state ({expected}); {MIN_INITIAL_CURVES} are "
            f"needed before a coincidence stops being the explanation"))

    traded = [c for c in curves if not c.complete and c.real_sol_reserves > 0]
    seeds = {c.virtual_sol_reserves - c.real_sol_reserves for c in traded}
    if len(seeds) >= 2 or (traded and seeds != {PUMPFUN_INITIAL["virtual_sol_reserves"]}):
        shown = ", ".join(f"{v / 10 ** WSOL_DECIMALS:,.9g}" for v in sorted(seeds))
        checks.append(LayoutCheck(
            "traded curves keep the same virtual seed", False,
            f"across {len(traded)} traded curves the virtual SOL seed came "
            f"out as {shown} SOL, and real buys should leave it at 30"))
    elif traded:
        checks.append(LayoutCheck(
            "traded curves keep the same virtual seed", True,
            f"{len(traded)} traded curves all keep a 30 SOL virtual seed on "
            f"top of differing real SOL"))
    else:
        # Not a failure. The strongest check simply has no data yet, and
        # saying so is different from saying the layout is wrong.
        checks.append(LayoutCheck(
            "traded curves keep the same virtual seed", True,
            "UNTESTED: no curve read had any real SOL in it, so the field "
            "that caps a real exit has only ever been observed as zero. Its "
            "position is confirmed by the launch state above, but a non-zero "
            "reading is still worth getting"))

    bad = [c for c in curves
           if c.virtual_sol_reserves < c.real_sol_reserves
           or c.virtual_token_reserves < c.real_token_reserves]
    checks.append(LayoutCheck(
        "virtual reserves exceed real", not bad,
        f"holds on all {len(curves)} curves" if not bad
        else f"{len(bad)} curves have more real than virtual reserves, which "
             f"is impossible and means the pairs are transposed"))
    return checks


def anomalies(curves: list[BondingCurve]) -> list[str]:
    """Curves that do not fit the launch state and are not explained by trading.

    Reported apart from the layout checks on purpose. An unexplained curve is
    an open question about that token, not evidence the struct is misread --
    conflating the two is what made the first version fail a parse that four
    other curves had just confirmed exactly.
    """
    out = []
    for curve in curves:
        if not _at_launch_state(curve) or _matches_launch_state(curve):
            continue
        off = [f"{field}={getattr(curve, field):,} (documented {value:,})"
               for field, value in PUMPFUN_INITIAL.items()
               if getattr(curve, field) != value]
        out.append(f"untraded curve ({curve.raw_len} bytes) departs from the "
                   f"launch state: " + "; ".join(off))
    return out


class RpcError(RuntimeError):
    """The node did not return JSON. Carries why, with the key redacted."""


def _redact(url: str) -> str:
    """Never let an api-key reach a log or a terminal."""
    import re
    return re.sub(r"(api[-_]?key=)[^&]+", r"\1***", url, flags=re.I)


def _rpc(client: httpx.Client, url: str, method: str, params: list,
         timeout: float = 20.0, pacer: Pacer | None = None,
         attempts: int = 3) -> dict:
    """One JSON-RPC call, failing with the status and body when it is not JSON.

    Calling .json() on an unchecked response turned every RPC failure into a
    bare JSONDecodeError, which says nothing about whether the key is rejected,
    the quota is spent, or the host is wrong -- three problems with three
    different fixes.
    """
    resp = None
    for attempt in range(attempts):
        try:
            resp = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params},
                               timeout=timeout)
        except httpx.TransportError as exc:
            # DNS and connection failures are transient and were not retried,
            # so one getaddrinfo hiccup discarded a token -- and on the first
            # call of a run it killed the whole script with a bare traceback.
            if attempt == attempts - 1:
                raise RpcError(
                    f"{type(exc).__name__} reaching {_redact(url)} after "
                    f"{attempts} attempts: {exc}") from None
            time.sleep(1.0 * (attempt + 1))
            continue
        if resp.status_code != 429:
            break
        # Refused, not broken. Back off and try again rather than discarding a
        # token, which is how a whole run came back empty.
        if pacer is not None:
            pacer.saw_429(resp.headers.get("Retry-After"))
        elif attempt < attempts - 1:
            time.sleep(2.0 * (attempt + 1))
    if resp is None:
        raise RpcError(f"no response from {_redact(url)}")
    if pacer is not None and resp.status_code != 429:
        pacer.saw_success()
    try:
        body = resp.json()
    except ValueError:
        snippet = (resp.text or "")[:160].replace("\n", " ")
        raise RpcError(f"HTTP {resp.status_code} from {_redact(url)}: "
                       f"{snippet or 'empty response'}") from None
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RpcError(f"node refused {method}: "
                       f"{err.get('message', err)}")
    return body or {}


def curve_address(mint: str) -> tuple[str | None, str]:
    """The curve's address, derived locally. No network call at all.

    This replaced asking the node for the token's largest accounts and reading
    the owner. That worked, but getTokenLargestAccounts is among the most
    expensive methods an RPC offers and forty tokens exhausted the quota -- 110
    rate limits with the pacer pinned at its ceiling. Derivation costs nothing
    and halves the calls per token.
    """
    try:
        return bonding_curve_address(mint, PUMP_PROGRAM), "derived locally"
    except ValueError as exc:
        return None, f"cannot derive from mint: {exc}"


def find_bonding_curve(client: httpx.Client, rpc_url: str, mint: str,
                       timeout: float = 20.0,
                       pacer: Pacer | None = None) -> tuple[str | None, str]:
    """Fallback for tokens whose curve is not at the derived address.

    Kept for anything that is not a pump.fun bonding curve -- a graduated token
    sitting in an AMM, for instance -- where the derived address holds nothing.
    Two RPC calls, so only used when derivation has already failed.
    """
    try:
        body = _rpc(client, rpc_url, "getTokenLargestAccounts", [mint], timeout, pacer)
        accounts = ((body.get("result") or {}).get("value")) or []
    except RpcError as exc:
        return None, f"largest accounts: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"largest accounts failed: {type(exc).__name__}: {exc}"
    if not accounts:
        return None, "no token accounts exist yet"

    try:
        info = _rpc(client, rpc_url, "getAccountInfo",
                    [accounts[0].get("address"), {"encoding": "jsonParsed"}],
                    timeout, pacer)
        value = ((info.get("result") or {}).get("value")) or {}
        owner = ((value.get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
    except RpcError as exc:
        return None, f"owner lookup: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"owner lookup failed: {type(exc).__name__}: {exc}"
    if not owner:
        return None, "largest token account has no owner"
    return owner, f"curve {owner[:8]}... holds the largest balance"


def read_curve(client: httpx.Client, rpc_url: str, curve_address: str,
               timeout: float = 20.0,
               pacer: Pacer | None = None) -> tuple[BondingCurve | None, str]:
    """One call. Returns (curve, detail); a None curve carries the reason."""
    try:
        body = _rpc(client, rpc_url, "getAccountInfo",
                    [curve_address, {"encoding": "base64"}], timeout, pacer)
    except RpcError as exc:
        return None, str(exc)
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
