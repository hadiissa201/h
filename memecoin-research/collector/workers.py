"""The workers that actually fetch and record: market data, exit simulation, holders.

Every one of them obeys the same three rules, which are the whole point of the
project and not merely good hygiene:

  1. A source that omitted a field records NULL, never 0. "No data" and "zero
     liquidity" mean opposite things, and conflating them turns our own gaps
     into evidence that a token was dead.
  2. A failure on OUR side records succeeded=NULL, not False. "We could not ask"
     is not "it could not be sold". Collapsing them makes a rate-limit spike
     look like a wave of rugs.
  3. Nothing is dropped. Work that fails is requeued with backoff and its error
     recorded, because an abandoned task and a missing observation are the same
     hole in the dataset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from collector.models import HolderSnapshot, Token, TokenStatus
from collector.ratelimit import Limiters
from poc.sources import (
    FAILURE_BAD_REQUEST,
    FAILURE_TRANSPORT,
    ExitSimulation,
    fetch_dexscreener,
    parse_jupiter_quote,
    says_no_route,
)
from poc.store import (
    insert_event,
    insert_observation,
    insert_simulated_exit,
    store_raw,
    upsert_pool,
)
from probe.constants import WSOL_MINT

log = logging.getLogger("collector.workers")


@dataclass
class WorkResult:
    ok: bool
    detail: str = ""
    requeue: bool = True


def _rate_limited(limiters: Limiters, service: str) -> bool:
    bucket = limiters.get(service)
    return bucket is not None and not bucket.acquire(max_wait=15.0)


def _note_429(limiters: Limiters, service: str, status: int | None) -> None:
    if status == 429:
        bucket = limiters.get(service)
        if bucket is not None:
            bucket.penalise(60.0)


# --------------------------------------------------------------- market data
def observe_market(session: Session, client: httpx.Client, limiters: Limiters,
                   settings, token: Token) -> WorkResult:  # noqa: ANN001
    """One market+liquidity snapshot. Records the no-pairs state as data."""
    if _rate_limited(limiters, "dexscreener"):
        # Not a failure of the token -- a failure of our budget. Requeue.
        return WorkResult(False, "rate limited, requeued")

    fetched = fetch_dexscreener(client, token.address, settings.dexscreener_base)
    _note_429(limiters, "dexscreener", fetched.http_status)

    raw_id = None
    if settings.store_raw_payloads:
        raw_id = store_raw(session, source=fetched.source, endpoint=fetched.endpoint,
                           http_status=fetched.http_status, body=fetched.body,
                           fetched_ts=fetched.fetched_ts)

    snap = fetched.parsed
    if snap is None:
        # A token nobody has listed yet is a REAL state and a common one. It is
        # recorded as an event, not discarded and not written as zero liquidity.
        insert_event(session, token_id=token.id, event_ts=fetched.fetched_ts,
                     kind="no_market_data",
                     detail={"error": fetched.error, "status": fetched.http_status})
        _bump_dead_counter(session, token, settings, dead=True)
        return WorkResult(True, "no pairs indexed yet (recorded)")

    pool_id = None
    if snap.pair_address:
        pool_id = upsert_pool(
            session, token_id=token.id, chain=token.chain,
            pair_address=snap.pair_address, dex=snap.dex,
            quote_mint=snap.quote_mint,
            created_ts=(datetime.fromtimestamp(snap.pair_created_ms / 1000, UTC)
                        if snap.pair_created_ms else None),
            initial_liquidity_usd=snap.liquidity_usd,
            initial_price_usd=snap.price_usd,
        )
    if not token.symbol and snap.symbol:
        token.name, token.symbol = snap.name, snap.symbol

    insert_observation(
        session, token_id=token.id, pool_id=pool_id,
        observed_ts=fetched.fetched_ts, source="dexscreener",
        price_usd=snap.price_usd, price_native=snap.price_native,
        liquidity_usd=snap.liquidity_usd, market_cap_usd=snap.market_cap_usd,
        fdv_usd=snap.fdv_usd, volume_5m=snap.volume_5m, volume_1h=snap.volume_1h,
        volume_24h=snap.volume_24h, buys_5m=snap.buys_5m, sells_5m=snap.sells_5m,
        buys_1h=snap.buys_1h, sells_1h=snap.sells_1h, raw_payload_id=raw_id,
    )
    _update_status(session, token, snap, fetched.fetched_ts, settings)
    return WorkResult(True, f"price={snap.price_usd} liq={snap.liquidity_usd}")


def _update_status(session: Session, token: Token, snap, observed_ts,  # noqa: ANN001
                   settings) -> None:  # noqa: ANN001
    """Maintain the derived peak / drawdown cache.

    Peak is tracked live rather than computed later because Phase 2 needs
    "what was the best this token ever did" for tens of thousands of tokens,
    and re-scanning every series for it is slower than the collection.
    """
    status = session.get(TokenStatus, token.id)
    if status is None:
        status = TokenStatus(token_id=token.id, observations_count=0)
        session.add(status)
        session.flush()

    status.observations_count = (status.observations_count or 0) + 1
    price = snap.price_usd
    if price:
        if status.first_price_usd is None:
            status.first_price_usd = price
        if status.peak_price_usd is None or price > float(status.peak_price_usd):
            status.peak_price_usd = price
            status.peak_ts = observed_ts
            if status.first_price_usd:
                status.peak_multiple = price / float(status.first_price_usd)
        if status.peak_price_usd:
            drop = (float(status.peak_price_usd) - price) / float(status.peak_price_usd)
            if (status.max_drawdown_from_peak_pct is None
                    or drop > float(status.max_drawdown_from_peak_pct)):
                status.max_drawdown_from_peak_pct = drop

    liquidity = snap.liquidity_usd
    dead = liquidity is not None and liquidity < settings.dormant_liquidity_usd
    _bump_dead_counter(session, token, settings, dead=dead, status=status)


def _bump_dead_counter(session: Session, token: Token, settings,  # noqa: ANN001
                       dead: bool, status: TokenStatus | None = None) -> None:
    """Track consecutive dead checks; retire (never delete) when persistent."""
    status = status or session.get(TokenStatus, token.id)
    if status is None:
        status = TokenStatus(token_id=token.id)
        session.add(status)
        session.flush()
    if dead:
        status.consecutive_dead_checks = (status.consecutive_dead_checks or 0) + 1
        if (status.consecutive_dead_checks >= settings.dormant_after_consecutive
                and status.retired_ts is None):
            status.retired_ts = datetime.now(UTC)
            status.retire_reason = "liquidity_below_threshold"
            insert_event(session, token_id=token.id, event_ts=status.retired_ts,
                         kind="went_dormant",
                         detail={"consecutive_dead_checks": status.consecutive_dead_checks})
    else:
        status.consecutive_dead_checks = 0


# ------------------------------------------------------------ exit simulation
def simulate_exit(session: Session, client: httpx.Client, limiters: Limiters,
                  settings, token: Token, notionals: list[float] | None = None,
                  ) -> WorkResult:  # noqa: ANN001
    """Ask, read-only, whether this position could be sold right now.

    A failure here is classified, never flattened. succeeded=False means we
    asked and there was no way out -- the single most valuable fact this project
    records. succeeded=NULL means our own request failed and we learned nothing.
    """
    sizes = notionals or [settings.exit_notional_usd]
    last = "no sizes"
    for notional in sizes:
        if _rate_limited(limiters, "jupiter"):
            return WorkResult(False, "rate limited, requeued")

        amount_raw = _amount_for_notional(session, token, notional)
        params = {"inputMint": token.address, "outputMint": WSOL_MINT,
                  "amount": str(amount_raw),
                  "slippageBps": str(settings.exit_slippage_bps)}
        now = datetime.now(UTC)
        try:
            resp = client.get(settings.jupiter_quote_url, params=params,
                              timeout=settings.http_timeout_s)
            _note_429(limiters, "jupiter", resp.status_code)
            sim = parse_jupiter_quote(resp.text, notional)
            if resp.status_code != 200 and not says_no_route(resp.text):
                # Non-200 without an explicit routing failure is OUR problem:
                # a malformed amount, a rate limit, an outage. Recording it as
                # "cannot be sold" would turn our own bad requests into
                # evidence about the market, which is exactly what a 400 Bad
                # Request was doing.
                kind = (FAILURE_TRANSPORT if resp.status_code in (403, 429)
                        or resp.status_code >= 500 else FAILURE_BAD_REQUEST)
                sim = ExitSimulation("quote", notional, None,
                                     failure_kind=kind,
                                     failure_reason=f"HTTP {resp.status_code}: "
                                                    f"{resp.text[:160]}")
            body, status = resp.text, resp.status_code
        except Exception as exc:  # noqa: BLE001
            sim = ExitSimulation("quote", notional, None,
                                 failure_kind=FAILURE_TRANSPORT,
                                 failure_reason=f"{type(exc).__name__}: {exc}")
            body, status = None, None

        raw_id = None
        if settings.store_raw_payloads and body:
            raw_id = store_raw(session, source="jupiter",
                               endpoint=settings.jupiter_quote_url,
                               http_status=status, body=body, fetched_ts=now)

        insert_simulated_exit(
            session, token_id=token.id, simulated_ts=now, method=sim.method,
            notional_usd=sim.notional_usd, input_amount_raw=sim.input_amount_raw,
            expected_output_raw=sim.expected_output_raw,
            price_impact_pct=sim.price_impact_pct, slippage_bps=sim.slippage_bps,
            route_dex=sim.route_dex, route_hops=sim.route_hops,
            succeeded=sim.succeeded, failure_kind=sim.failure_kind,
            failure_reason=sim.failure_reason, raw_payload_id=raw_id,
        )
        _record_tradability(session, token, sim, now)
        last = f"${notional:.0f}: {_verdict(sim.succeeded)}"
    return WorkResult(True, last)


def _verdict(succeeded: bool | None) -> str:
    return {True: "sellable", False: "NOT SELLABLE", None: "unknown"}[succeeded]


def _record_tradability(session: Session, token: Token, sim: ExitSimulation,
                        now: datetime) -> None:
    """Transitions into and out of sellability are events worth their own row.

    The moment a paper gain stops being realisable is invisible on any price
    chart, and it is precisely what a strategy replay has to respect.
    """
    if sim.succeeded is None:
        return  # learned nothing; do not move the state
    status = session.get(TokenStatus, token.id)
    if status is None:
        status = TokenStatus(token_id=token.id)
        session.add(status)
        session.flush()
    previous = status.is_tradable
    status.is_tradable = sim.succeeded
    if sim.succeeded:
        status.last_tradable_ts = now
    if previous is not None and previous != sim.succeeded:
        insert_event(session, token_id=token.id, event_ts=now,
                     kind="became_sellable_again" if sim.succeeded else "became_unsellable",
                     detail={"failure_kind": sim.failure_kind,
                             "reason": (sim.failure_reason or "")[:300]})


# Jupiter rejects amounts outside a sane range with a 400. Those rejections
# were being recorded as "no route", so the bounds are not cosmetic.
_MIN_QUOTE_AMOUNT = 1_000
_MAX_QUOTE_AMOUNT = 10 ** 18


def _amount_for_notional(session: Session, token: Token, notional_usd: float) -> int:
    """Raw token units approximating a dollar notional at the last known price.

    Falls back to one whole token when no price is known yet. The notional is
    stored alongside the result either way, so Phase 2 always knows what size
    produced a given price impact -- impact without size is meaningless.

    Clamped: a memecoin priced at 1e-11 turns $100 into an astronomically large
    raw amount, and a near-zero one into a value below Jupiter's minimum. Both
    return 400, and a 400 used to be filed as "this cannot be sold".
    """
    decimals = token.decimals if token.decimals is not None else 6
    status = session.get(TokenStatus, token.id)
    price = None
    if status is not None and status.first_price_usd is not None:
        price = float(status.first_price_usd)
    elif status is not None and status.peak_price_usd is not None:
        price = float(status.peak_price_usd)
    if not price or price <= 0:
        raw = 10 ** decimals
    else:
        raw = int((notional_usd / price) * (10 ** decimals))
    return max(_MIN_QUOTE_AMOUNT, min(_MAX_QUOTE_AMOUNT, raw))


# ------------------------------------------------------------------- holders
def observe_holders(session: Session, client: httpx.Client, limiters: Limiters,
                    settings, token: Token) -> WorkResult:  # noqa: ANN001
    """Holder count and concentration via the RPC's largest-accounts view.

    Snapshotted at observation time, never re-fetched later: providers drop data
    for dead tokens, and a dead token's final distribution is exactly what we
    most need to keep.
    """
    if _rate_limited(limiters, "helius"):
        return WorkResult(False, "rate limited, requeued")
    now = datetime.now(UTC)
    try:
        resp = client.post(settings.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
            "params": [token.address],
        }, timeout=settings.http_timeout_s)
        _note_429(limiters, "helius", resp.status_code)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return WorkResult(False, f"{type(exc).__name__}: {exc}")

    holders = ((body.get("result") or {}).get("value")) or []
    if not holders:
        insert_event(session, token_id=token.id, event_ts=now,
                     kind="no_holder_data", detail={"error": body.get("error")})
        return WorkResult(True, "no holder data (recorded)")

    amounts = [float(h.get("uiAmount") or 0.0) for h in holders]
    total = sum(amounts) or 1.0
    session.add(HolderSnapshot(
        token_id=token.id, observed_ts=now, holder_count=len(holders),
        top1_pct=(amounts[0] / total if amounts else None),
        top10_pct=(sum(amounts[:10]) / total if amounts else None),
        top50_pct=(sum(amounts[:50]) / total if amounts else None),
    ))
    return WorkResult(True, f"{len(holders)} largest accounts, top1={amounts[0]/total:.1%}")
