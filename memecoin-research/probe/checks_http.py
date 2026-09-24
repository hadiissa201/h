"""HTTP checks: DexScreener, Jupiter, Solana RPC, Helius.

Each check answers one question and records what it saw. Where an endpoint may
have moved, every candidate is tried and the winner is reported by name -- the
collector is later configured from that answer rather than from a guess.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import httpx

from probe.constants import (
    DEXSCREENER_BASE,
    DEXSCREENER_CHECKS,
    HELIUS_RPC_TEMPLATE,
    JUPITER_QUOTE_CANDIDATES,
    JUPITER_SWAP_CANDIDATES,
    SIMULATION_FEE_PAYER,
    USDC_MINT,
    WSOL_MINT,
)
from probe.report import Check, Outcome, Report

TIMEOUT = httpx.Timeout(20.0)


def _timed(fn, *args, **kwargs) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000.0


def _rpc(client: httpx.Client, url: str, method: str,
         params: list | dict) -> httpx.Response:
    """A JSON-RPC call. `params` may be a list OR a dict.

    Standard Solana methods take positional params (a list). Helius DAS methods
    such as getAsset take NAMED params (an object) -- passing a list there gets
    "invalid type: map, expected a string", which reads like a broken endpoint
    when it is really the wrong call shape.
    """
    return client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=TIMEOUT,
    )


# --------------------------------------------------------------- Solana RPC
def check_rpc(report: Report, client: httpx.Client, url: str, label: str) -> None:
    """Liveness, version and slot. If this fails nothing else on-chain works."""
    for method, params in (("getHealth", []), ("getVersion", []), ("getSlot", [])):
        try:
            resp, ms = _timed(_rpc, client, url, method, params)
            body = resp.json()
            if "error" in body:
                report.add(Check(label, method, Outcome.FAILED,
                                 f"rpc error: {body['error']}", url, resp.status_code, ms))
                continue
            report.add(Check(label, method, Outcome.OK, f"{body.get('result')}",
                             url, resp.status_code, ms, {"result": body.get("result")}))
        except Exception as exc:  # noqa: BLE001 -- probe reports, never crashes
            report.add(Check(label, method, Outcome.FAILED, f"{type(exc).__name__}: {exc}", url))


def check_rpc_simulate(report: Report, client: httpx.Client, url: str, label: str) -> None:
    """Can we call simulateTransaction at all?

    Sends a deliberately malformed transaction. We are NOT testing that a swap
    succeeds -- we are testing that the node exposes the method and will run an
    unsigned transaction for us. A decode error proves the method is reachable
    and processing our input; 'method not found' or an auth error does not.
    """
    try:
        resp, ms = _timed(
            _rpc, client, url, "simulateTransaction",
            ["AQAAA", {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}],
        )
        body = resp.json()
        err = body.get("error") or {}
        message = str(err.get("message", "")).lower()
        if "method not found" in message or resp.status_code in (401, 403):
            report.add(Check(label, "simulateTransaction", Outcome.FAILED,
                             f"unavailable: {err}", url, resp.status_code, ms))
        else:
            report.add(Check(label, "simulateTransaction", Outcome.OK,
                             "method reachable (rejected our dummy tx, as expected)",
                             url, resp.status_code, ms, {"response": body}))
    except Exception as exc:  # noqa: BLE001
        report.add(Check(label, "simulateTransaction", Outcome.FAILED,
                         f"{type(exc).__name__}: {exc}", url))


def check_helius(report: Report, client: httpx.Client, api_key: str | None) -> str | None:
    """Helius RPC + DAS. Returns the working RPC url, or None."""
    if not api_key:
        report.add(Check("helius", "api key", Outcome.UNVERIFIED,
                         "HELIUS_API_KEY not set -- cannot verify Helius at all"))
        return None
    url = HELIUS_RPC_TEMPLATE.format(key=api_key)
    check_rpc(report, client, url, "helius")
    check_rpc_simulate(report, client, url, "helius")
    try:
        resp, ms = _timed(_rpc, client, url, "getAsset", {"id": USDC_MINT})
        body = resp.json()
        if "error" in body:
            report.add(Check("helius", "DAS getAsset", Outcome.FAILED,
                             f"{body['error']}", url, resp.status_code, ms))
        else:
            report.add(Check("helius", "DAS getAsset", Outcome.OK,
                             "asset metadata available (holders/metadata path works)",
                             url, resp.status_code, ms))
    except Exception as exc:  # noqa: BLE001
        report.add(Check("helius", "DAS getAsset", Outcome.FAILED, f"{type(exc).__name__}: {exc}"))
    return url


# --------------------------------------------------------------- DexScreener
def check_dexscreener(report: Report, client: httpx.Client) -> list[str]:
    """Which DexScreener endpoints answer, and do they need a key?"""
    working: list[str] = []
    for name, path in DEXSCREENER_CHECKS:
        url = DEXSCREENER_BASE + path.format(mint=WSOL_MINT)
        try:
            resp, ms = _timed(client.get, url, timeout=TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                size = len(data) if isinstance(data, list) else len(str(data))
                working.append(name)
                report.add(Check("dexscreener", name, Outcome.OK,
                                 f"200, payload~{size}", url, resp.status_code, ms,
                                 {"rate_headers": _rate_headers(resp)}))
            else:
                report.add(Check("dexscreener", name, Outcome.FAILED,
                                 f"HTTP {resp.status_code}: {resp.text[:120]}",
                                 url, resp.status_code, ms))
        except Exception as exc:  # noqa: BLE001
            report.add(Check("dexscreener", name, Outcome.FAILED,
                             f"{type(exc).__name__}: {exc}", url))
    return working


def _rate_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items()
            if "rate" in k.lower() or "limit" in k.lower() or "retry" in k.lower()}


def measure_rate_limit(report: Report, client: httpx.Client, service: str,
                       url: str, burst: int) -> None:
    """Find the real rate limit by a bounded burst, not by guessing.

    Deliberately gentle: a small burst, stopping at the first 429. The aim is to
    learn the published limit and whether headers expose it -- not to get the
    collector's IP throttled before it has collected anything.
    """
    sent = 0
    first_429_at: int | None = None
    headers: dict[str, str] = {}
    start = time.perf_counter()
    try:
        for i in range(burst):
            resp = client.get(url, timeout=TIMEOUT)
            sent += 1
            headers = _rate_headers(resp) or headers
            if resp.status_code == 429:
                first_429_at = i + 1
                break
    except Exception as exc:  # noqa: BLE001
        report.add(Check(service, "rate limit", Outcome.FAILED,
                         f"{type(exc).__name__}: {exc}", url))
        return
    elapsed = time.perf_counter() - start
    rate = sent / elapsed if elapsed > 0 else 0.0
    if first_429_at:
        detail = f"429 after {first_429_at} requests (~{rate:.1f} req/s sustained)"
        outcome = Outcome.OK
    else:
        detail = f"{sent} requests in {elapsed:.1f}s (~{rate:.1f} req/s), no 429 hit"
        outcome = Outcome.OK
    if headers:
        detail += f" | headers: {headers}"
    else:
        detail += " | no rate headers exposed"
    report.add(Check(service, "rate limit", outcome, detail, url,
                     evidence={"sent": sent, "first_429_at": first_429_at,
                               "elapsed_s": round(elapsed, 3), "headers": headers}))


# ------------------------------------------------------------------ Jupiter
def check_jupiter_quote(report: Report, client: httpx.Client) -> str | None:
    """Find a working quote endpoint. This is the core of the exit simulation."""
    winner = None
    for name, url in JUPITER_QUOTE_CANDIDATES:
        params = {"inputMint": WSOL_MINT, "outputMint": USDC_MINT,
                  "amount": "100000000", "slippageBps": "100"}
        try:
            resp, ms = _timed(client.get, url, params=params, timeout=TIMEOUT)
            if resp.status_code != 200:
                report.add(Check("jupiter", f"quote {name}", Outcome.FAILED,
                                 f"HTTP {resp.status_code}: {resp.text[:120]}",
                                 url, resp.status_code, ms))
                continue
            body = resp.json()
            out = body.get("outAmount")
            impact = body.get("priceImpactPct")
            routes = len(body.get("routePlan") or [])
            if out is None:
                report.add(Check("jupiter", f"quote {name}", Outcome.FAILED,
                                 f"200 but no outAmount: {str(body)[:140]}",
                                 url, resp.status_code, ms))
                continue
            report.add(Check("jupiter", f"quote {name}", Outcome.OK,
                             f"0.1 SOL -> {int(out) / 1e6:.2f} USDC, "
                             f"impact={impact}, hops={routes}",
                             url, resp.status_code, ms,
                             {"outAmount": out, "priceImpactPct": impact,
                              "routePlan_len": routes,
                              "rate_headers": _rate_headers(resp)}))
            winner = winner or url
        except Exception as exc:  # noqa: BLE001
            report.add(Check("jupiter", f"quote {name}", Outcome.FAILED,
                             f"{type(exc).__name__}: {exc}", url))
    return winner


def check_jupiter_illiquid(report: Report, client: httpx.Client,
                           quote_url: str, mint: str | None) -> None:
    """Quote a real low-liquidity token, not just the SOL/USDC highway.

    SOL to USDC always routes. The question that matters for this research is
    whether a thin, freshly launched token routes -- and what a no-route answer
    looks like, since 'no route' is exactly the signal that an exit was not
    available.
    """
    if not mint:
        report.add(Check("jupiter", "quote illiquid token", Outcome.UNVERIFIED,
                         "no candidate mint supplied (pass --test-mint or run the "
                         "websocket listener first)"))
        return
    params = {"inputMint": mint, "outputMint": WSOL_MINT,
              "amount": "1000000", "slippageBps": "300"}
    try:
        resp, ms = _timed(client.get, quote_url, params=params, timeout=TIMEOUT)
        body = resp.json() if resp.content else {}
        if resp.status_code == 200 and body.get("outAmount"):
            report.add(Check("jupiter", "quote illiquid token", Outcome.OK,
                             f"routed: out={body['outAmount']} "
                             f"impact={body.get('priceImpactPct')}",
                             quote_url, resp.status_code, ms, {"mint": mint}))
        else:
            report.add(Check("jupiter", "quote illiquid token", Outcome.OK,
                             f"NO ROUTE (HTTP {resp.status_code}) -- this is the "
                             f"'cannot sell' signal we need: {str(body)[:120]}",
                             quote_url, resp.status_code, ms, {"mint": mint}))
    except Exception as exc:  # noqa: BLE001
        report.add(Check("jupiter", "quote illiquid token", Outcome.FAILED,
                         f"{type(exc).__name__}: {exc}", quote_url, evidence={"mint": mint}))


def check_jupiter_swap_build(report: Report, client: httpx.Client,
                             quote_url: str) -> None:
    """Can we BUILD an unsigned swap transaction for RPC simulation?

    This is the read-only exit test's second leg. We ask Jupiter to build the
    transaction using a fee payer we do not control and never sign. If this
    works, the full simulate-without-a-wallet path is real.
    """
    params = {"inputMint": WSOL_MINT, "outputMint": USDC_MINT,
              "amount": "10000000", "slippageBps": "100"}
    try:
        quote = client.get(quote_url, params=params, timeout=TIMEOUT)
        if quote.status_code != 200:
            report.add(Check("jupiter", "swap build", Outcome.FAILED,
                             f"quote leg failed HTTP {quote.status_code}", quote_url))
            return
        quote_body = quote.json()
    except Exception as exc:  # noqa: BLE001
        report.add(Check("jupiter", "swap build", Outcome.FAILED,
                         f"quote leg: {type(exc).__name__}: {exc}", quote_url))
        return

    for name, url in JUPITER_SWAP_CANDIDATES:
        try:
            resp, ms = _timed(
                client.post, url,
                json={"quoteResponse": quote_body,
                      "userPublicKey": SIMULATION_FEE_PAYER,
                      "wrapAndUnwrapSol": True},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200 and (resp.json() or {}).get("swapTransaction"):
                tx = resp.json()["swapTransaction"]
                report.add(Check("jupiter", f"swap build {name}", Outcome.OK,
                                 f"unsigned tx returned ({len(tx)} b64 chars) -- "
                                 "RPC simulation path is viable",
                                 url, resp.status_code, ms))
                return
            report.add(Check("jupiter", f"swap build {name}", Outcome.FAILED,
                             f"HTTP {resp.status_code}: {resp.text[:140]}",
                             url, resp.status_code, ms))
        except Exception as exc:  # noqa: BLE001
            report.add(Check("jupiter", f"swap build {name}", Outcome.FAILED,
                             f"{type(exc).__name__}: {exc}", url))


# ------------------------------------------------- resolving a real new mint
def resolve_mint_from_signature(report: Report, client: httpx.Client, rpc_url: str,
                                signatures: list[str]) -> str | None:
    """Turn a creation transaction into the mint address it created.

    The collector needs this anyway: detection gives a signature, and every
    downstream call needs the mint. Verifying it here means the probe proves
    the whole detection chain, not just that messages arrive.

    The new mint is read from postTokenBalances, which lists the token accounts
    the transaction touched. Tries several signatures because a create-like
    instruction does not always mean a mint was born.
    """
    for signature in signatures[:8]:
        try:
            resp, ms = _timed(
                _rpc, client, rpc_url, "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            body = resp.json()
            result = body.get("result")
            if not result:
                continue
            balances = (result.get("meta") or {}).get("postTokenBalances") or []
            mints = {b.get("mint") for b in balances if b.get("mint")}
            mints.discard(WSOL_MINT)
            mints.discard(USDC_MINT)
            if not mints:
                continue
            mint = sorted(mints)[0]
            report.add(Check("solana-rpc", "resolve mint from signature", Outcome.OK,
                             f"{signature[:16]}... -> {mint}", rpc_url,
                             resp.status_code, ms, {"mint": mint, "signature": signature}))
            return mint
        except Exception as exc:  # noqa: BLE001
            report.add(Check("solana-rpc", "resolve mint from signature", Outcome.FAILED,
                             f"{type(exc).__name__}: {exc}", rpc_url))
            return None
    report.add(Check("solana-rpc", "resolve mint from signature", Outcome.UNVERIFIED,
                     f"tried {min(8, len(signatures))} creation signatures, none carried a "
                     "new mint in postTokenBalances", rpc_url))
    return None


def measure_sustained_rate(report: Report, client: httpx.Client, service: str,
                           url: str, target_rps: float, seconds: float) -> None:
    """Hold a steady request rate and see whether it survives.

    A burst of 25 requests in 1.3s says nothing about a limit enforced per
    minute. Sizing the collector off a burst is how you discover the real
    ceiling in production, with a half-collected dataset. This paces requests
    at a target rate for a sustained window and reports the first rejection.
    """
    interval = 1.0 / target_rps if target_rps > 0 else 0.0
    sent = ok = 0
    first_429_at: float | None = None
    other_errors: list[str] = []
    start = time.perf_counter()

    while time.perf_counter() - start < seconds:
        cycle = time.perf_counter()
        try:
            resp = client.get(url, timeout=TIMEOUT)
            sent += 1
            if resp.status_code == 200:
                ok += 1
            elif resp.status_code == 429:
                first_429_at = time.perf_counter() - start
                break
            else:
                other_errors.append(str(resp.status_code))
        except Exception as exc:  # noqa: BLE001
            other_errors.append(type(exc).__name__)
            sent += 1
        nap = interval - (time.perf_counter() - cycle)
        if nap > 0:
            time.sleep(nap)

    elapsed = time.perf_counter() - start
    actual = sent / elapsed if elapsed > 0 else 0.0
    if first_429_at is not None:
        detail = (f"429 after {sent} requests / {first_429_at:.1f}s at "
                  f"{target_rps:.1f} req/s target -- THIS is the real ceiling")
    else:
        detail = (f"held {actual:.1f} req/s for {elapsed:.0f}s, {ok}/{sent} OK, "
                  "no 429 -- sustainable at this rate")
    if other_errors:
        detail += f" | non-200s: {Counter(other_errors).most_common(3)}"
    report.add(Check(service, f"sustained {target_rps:.0f} req/s", Outcome.OK, detail, url,
                     evidence={"target_rps": target_rps, "actual_rps": round(actual, 2),
                               "sent": sent, "ok": ok, "seconds": round(elapsed, 1),
                               "first_429_after_s": first_429_at,
                               "other_errors": Counter(other_errors).most_common(5)}))
