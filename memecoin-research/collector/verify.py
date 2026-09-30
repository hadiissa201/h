"""Verifying a routing quote against real chain state.

A Jupiter quote proves a *route* exists and what it would pay. It does not
prove the transaction would land. A token can quote perfectly and still be
impossible to sell: a freeze authority activated after launch, a transfer hook
that rejects sells, a token program that fails on transfer. Those are among the
more interesting ways a memecoin position becomes worthless, and a quote cannot
see any of them.

So a sampled fraction of successful quotes is re-checked by building the real
swap transaction and running it through `simulateTransaction`.

Two things make this honest, and both are limitations as much as features:

WHOSE ACCOUNT. Simulating a sell requires an account that actually holds the
token, and we hold nothing. A zero-balance fee payer reverts for "insufficient
funds" on every token, which would look exactly like an untradeable one -- worse
than collecting no data at all. So the simulation runs as an existing holder,
read from the chain. That measures whether *that holder* can sell. If they are
blacklisted by a transfer hook and a fresh wallet would not be, we record a
failure that would not have happened to us; if they are whitelisted, we miss a
restriction that would. It is an imperfect proxy, stated plainly, and still far
closer to the truth than a routing calculation alone.

NO KEY, EVER. `sigVerify: false` means the node neither requires nor receives a
signature, and the holder's address is public information. Nothing here can be
submitted to the network, and no private key exists anywhere in this codebase.

These rows are deliberately NOT fed back into paper trading. They are a
measurement of how often the quote-based pipeline is wrong, and a check cannot
also be part of the thing it checks.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.models import SimulatedExit, Token
from collector.ratelimit import Limiters
from poc.sources import METHOD_RPC_SIM, ExitSimulation, simulate_sell_rpc
from poc.store import insert_simulated_exit

log = logging.getLogger("collector.verify")

# A fee payer must be a plain wallet: system-owned, with lamports. The largest
# holders of a memecoin are pool vaults whose owner is a PDA derived from the
# launchpad program -- not the program id itself, so an id blocklist does not
# catch them. A PDA cannot pay fees, and Solana rejects the transaction with
# InvalidAccountForFee before it ever reaches the token. That produced four
# "would revert" rows that said nothing whatsoever about sellability.
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Enough SOL to cover fees and any account rent the swap needs. A wallet below
# this cannot be simulated from even though it holds the token.
MIN_FEE_PAYER_LAMPORTS = 10_000_000          # 0.01 SOL

# Kept as a cheap first pass; the system-owner check below is the real filter.
_PROGRAM_OWNERS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
})

FAILURE_NO_HOLDER = "no_simulatable_holder"


def should_verify(mint: str, sample_rate: float, bucket: str = "") -> bool:
    """Deterministic sampling, so the sample is unbiased and reproducible.

    Sampling by "whatever we had spare capacity for" would over-represent quiet
    periods, which is when unsellability is least likely -- the exact bias this
    check exists to detect.
    """
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    digest = hashlib.sha256(f"{mint}:{bucket}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < sample_rate


def find_holder(client: httpx.Client, rpc_url: str, mint: str,
                min_amount_raw: int, timeout: float = 20.0) -> tuple[str | None, str]:
    """A wallet holding at least `min_amount_raw` of this mint.

    getTokenLargestAccounts returns token ACCOUNTS; the swap needs their OWNER.
    The largest is almost always the bonding curve or AMM vault, so candidates
    are walked in order and program-owned accounts skipped.

    Returns (owner, note). A None owner is not a verdict about the token -- it
    means we could not run the check, and it must be recorded as unknown.
    """
    try:
        resp = client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
            "params": [mint],
        }, timeout=timeout)
        accounts = ((resp.json() or {}).get("result") or {}).get("value") or []
    except Exception as exc:  # noqa: BLE001
        return None, f"largest accounts failed: {type(exc).__name__}"

    if not accounts:
        return None, "no holders returned"

    rejected: list[str] = []

    for account in accounts[:5]:
        try:
            if int(account.get("amount") or 0) < min_amount_raw:
                continue          # sorted descending: nothing below will fit
        except (TypeError, ValueError):
            continue
        address = account.get("address")
        if not address:
            continue
        try:
            info = client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [address, {"encoding": "jsonParsed"}],
            }, timeout=timeout)
            parsed = (((info.json() or {}).get("result") or {}).get("value") or {})
            owner = ((parsed.get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
        except Exception as exc:  # noqa: BLE001
            return None, f"owner lookup failed: {type(exc).__name__}"
        if not owner or owner in _PROGRAM_OWNERS:
            continue
        ok, why = _can_pay_fees(client, rpc_url, owner, timeout)
        if ok:
            return owner, f"holder {owner[:8]}... holding {account.get('amount')}"
        rejected.append(f"{owner[:8]}...{why}")

    detail = "; ".join(rejected) if rejected else f"{len(accounts)} accounts"
    return None, f"no holder that can pay fees ({detail})"


def _can_pay_fees(client: httpx.Client, rpc_url: str, address: str,
                  timeout: float) -> tuple[bool, str]:
    """Is this a plain wallet with enough SOL to be a fee payer?

    Without this check the simulation reverts with InvalidAccountForFee on any
    PDA -- which is most large memecoin holders -- and the revert gets recorded
    as though the token could not be sold.
    """
    try:
        resp = client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [address, {"encoding": "jsonParsed"}],
        }, timeout=timeout)
        value = (((resp.json() or {}).get("result") or {}).get("value")) or {}
    except Exception as exc:  # noqa: BLE001
        return False, f" lookup failed ({type(exc).__name__})"

    if not value:
        return False, " account does not exist"
    if value.get("owner") != SYSTEM_PROGRAM:
        return False, " not a wallet (PDA or program account); cannot pay fees"
    lamports = value.get("lamports") or 0
    if lamports < MIN_FEE_PAYER_LAMPORTS:
        return False, f" only {lamports} lamports"
    return True, ""


def verify_exit(session: Session, client: httpx.Client, limiters: Limiters,
                settings, token: Token, quote_body: dict,  # noqa: ANN001
                notional_usd: float, amount_raw: int) -> str:
    """Re-check one successful quote against chain state. Records its own row."""
    now = datetime.now(UTC)

    owner, note = find_holder(client, settings.rpc_url, token.address, amount_raw,
                              settings.http_timeout_s)
    if owner is None:
        # We could not ask. NULL, never False: absence of a simulatable holder
        # says nothing about whether the token can be sold.
        insert_simulated_exit(
            session, token_id=token.id, simulated_ts=now, method=METHOD_RPC_SIM,
            notional_usd=notional_usd, succeeded=None,
            failure_kind=FAILURE_NO_HOLDER, failure_reason=note[:500])
        return f"rpc_sim skipped: {note}"

    fetched = simulate_sell_rpc(client, settings.jupiter_swap_url, settings.rpc_url,
                                quote_body, notional_usd, user_public_key=owner)
    sim: ExitSimulation = fetched.parsed
    insert_simulated_exit(
        session, token_id=token.id, simulated_ts=now, method=METHOD_RPC_SIM,
        notional_usd=notional_usd, succeeded=sim.succeeded,
        failure_kind=sim.failure_kind,
        failure_reason=(sim.failure_reason or note)[:500],
        price_impact_pct=sim.price_impact_pct)
    verdict = {True: "would land", False: "WOULD REVERT", None: "unknown"}[sim.succeeded]
    if sim.succeeded is False:
        # A quote said sellable and the chain disagreed. This is the whole
        # point of the check, so it is logged loudly rather than buried.
        log.warning("QUOTE WRONG: %s quoted sellable but simulation reverted: %s",
                    token.address[:12], (sim.failure_reason or "")[:200])
    return f"rpc_sim {verdict} ({note})"


def disagreement_rate(session: Session) -> dict:
    """How often the quote pipeline was wrong, on the verified sample.

    This is the number that says whether every other result in the project is
    overstated, and by roughly how much.
    """
    rows = session.execute(
        select(SimulatedExit.succeeded, func.count())
        .where(SimulatedExit.method == METHOD_RPC_SIM)
        .group_by(SimulatedExit.succeeded)).all()
    counts = {verdict: n for verdict, n in rows}
    landed = counts.get(True, 0)
    reverted = counts.get(False, 0)
    answered = landed + reverted
    return {
        "verified": answered,
        "would_land": landed,
        "would_revert": reverted,
        "unknown": counts.get(None, 0),
        # None, not 0.0, when nothing has been verified: an unmeasured rate is
        # not a rate of zero.
        "quote_false_positive_rate": (reverted / answered) if answered else None,
    }


# Below this many answered checks, the rate is not reportable as a rate. Two
# reverts out of two is 100% only in the sense that a coin landing heads twice
# is a 100%-heads coin. It is also the size at which a systematic fault in our
# own method -- simulating as a holder who could not have sold anyway -- looks
# exactly like a finding about the market.
MIN_VERIFIED_FOR_A_RATE = 20


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI for a proportion, correct at small n where the normal one is not.

    Reported alongside every rate so a number resting on a handful of checks
    cannot be read as a measurement.
    """
    if trials <= 0:
        return (0.0, 1.0)
    phat = successes / trials
    denom = 1 + z**2 / trials
    centre = (phat + z**2 / (2 * trials)) / denom
    margin = z * ((phat * (1 - phat) / trials + z**2 / (4 * trials**2)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def describe(counts: dict) -> str:
    rate = counts["quote_false_positive_rate"]
    n = counts["verified"]
    if rate is None:
        return "no quotes verified against chain state yet -- routing only"
    low, high = wilson_interval(counts["would_revert"], n)
    body = (f"{n} quotes verified, {counts['would_revert']} would have reverted "
            f"({rate:.0%}, 95% CI {low:.0%}-{high:.0%})")
    if n < MIN_VERIFIED_FOR_A_RATE:
        return (f"{body} -- TOO FEW TO BE A RATE (need {MIN_VERIFIED_FOR_A_RATE}); "
                f"at this size our own method failing looks identical to a finding")
    return body


__all__ = ["FAILURE_NO_HOLDER", "describe", "disagreement_rate", "find_holder",
           "should_verify", "verify_exit"]
