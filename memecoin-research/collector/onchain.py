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
                        token_total_supply=supply, complete=bool(complete))


# pump.fun seeds every curve with the same fictional reserves and the same
# supply. These are the documented values, but the check below does not rest on
# them being right: what carries the proof is that the SAME number comes out of
# every token, which a misread field cannot do.
PUMPFUN_VIRTUAL_SOL_SEED = 30_000_000_000
PUMPFUN_TOKEN_TOTAL_SUPPLY = 1_000_000_000_000_000


@dataclass(frozen=True)
class LayoutCheck:
    name: str
    passed: bool
    detail: str


def layout_evidence(curves: list[BondingCurve]) -> list[LayoutCheck]:
    """Judge whether the struct is read correctly, from the curves alone.

    verify_onchain.py was built to cross-check the parse against DexScreener,
    which only works on tokens the aggregator indexed -- and the first run
    found no token with both, so it proved nothing at all. These checks need no
    second source, no price oracle and no extra RPC call.

    The load-bearing one is INVARIANCE. pump.fun seeds every curve with the
    same fictional reserves and real trades add on top, so
    virtual_sol - real_sol is the same constant on every live curve. A u64 read
    at the wrong offset cannot produce one identical constant across tokens
    that differ in every other respect; it would scatter.
    """
    checks: list[LayoutCheck] = []
    if not curves:
        return [LayoutCheck("any data", False, "no curve was read")]

    live = [c for c in curves if not c.complete]

    def invariant(name: str, values: list[int], expected: int | None,
                  scale: int, unit: str) -> None:
        uniq = sorted(set(values))
        shown = ", ".join(f"{v / scale:,.9g}" for v in uniq[:3])
        if len(uniq) != 1:
            checks.append(LayoutCheck(
                name, False,
                f"{len(uniq)} different values across {len(values)} curves "
                f"({shown}{', ...' if len(uniq) > 3 else ''}) {unit} -- a "
                f"constant was expected, so the offset is wrong"))
            return
        got = uniq[0]
        if expected is not None and got != expected:
            checks.append(LayoutCheck(
                name, False,
                f"constant across all {len(values)} curves at {got / scale:,.9g} "
                f"{unit}, but pump.fun documents {expected / scale:,.9g} -- "
                f"one constant means the offset is probably right and the "
                f"documented figure stale, so confirm before trusting it"))
            return
        checks.append(LayoutCheck(
            name, True,
            f"{got / scale:,.9g} {unit} on all {len(values)} curves"))

    # The invariant only has teeth if the curves actually differ. A curve
    # nobody has bought sits at the initial state with real_sol == 0, so
    # virtual - real is trivially identical across any number of untouched
    # tokens no matter where the fields are read from. The first live run
    # returned exactly that: three curves, 0.00 SOL in every one.
    traded = sorted({c.real_sol_reserves for c in live})
    if len(traded) >= 2:
        invariant("virtual SOL seed is one constant",
                  [c.virtual_sol_reserves - c.real_sol_reserves for c in live],
                  PUMPFUN_VIRTUAL_SOL_SEED, 10 ** WSOL_DECIMALS, "SOL")
        invariant("virtual token seed is one constant",
                  [c.virtual_token_reserves - c.real_token_reserves
                   for c in live],
                  None, 10 ** 6, "tokens")
    elif not live:
        checks.append(LayoutCheck(
            "virtual SOL seed is one constant", False,
            f"all {len(curves)} curves read were already graduated, so the "
            f"seed invariant cannot be tested"))
    else:
        checks.append(LayoutCheck(
            "virtual SOL seed is one constant", False,
            f"all {len(live)} live curves hold the same "
            f"{traded[0] / 10 ** WSOL_DECIMALS:,.4f} SOL, so they are at one "
            f"state and the difference is identical whatever the offsets are. "
            f"The check needs curves with DIFFERENT real SOL -- tokens "
            f"somebody actually bought"))

    invariant("total supply is one constant",
              [c.token_total_supply for c in curves],
              PUMPFUN_TOKEN_TOTAL_SUPPLY, 10 ** 6, "tokens")

    bad = [c for c in curves
           if c.virtual_sol_reserves < c.real_sol_reserves
           or c.virtual_token_reserves < c.real_token_reserves]
    checks.append(LayoutCheck(
        "virtual reserves exceed real", not bad,
        f"holds on all {len(curves)} curves" if not bad
        else f"{len(bad)} curves have more real than virtual reserves, which "
             f"is impossible and means the pairs are transposed"))
    return checks


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
