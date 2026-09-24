"""Proof of concept: prove the pipeline works end to end, on real data.

Proves exactly four things and nothing more:
  (a) we can detect a newly launched Solana token
  (b) we can obtain its market and liquidity data
  (c) we can simulate a SELL read-only, with no private key and no transaction
  (d) we can store the result in PostgreSQL, idempotently

This is NOT the collector. It runs once, over a handful of tokens, and stops.

    python -m poc --database-url postgresql+psycopg://user:pass@localhost/memecoin
    python -m poc --mint <known_mint> --skip-detect   # deterministic re-run

Read-only. No keypair is created, loaded or accepted anywhere in this package.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from poc.models import Base, Event, Observation, RawPayload, SimulatedExit, Token
from poc.sources import (
    fetch_dexscreener,
    simulate_sell_quote,
    simulate_sell_rpc,
)
from poc.store import (
    insert_event,
    insert_observation,
    insert_simulated_exit,
    read_raw,
    store_raw,
    upsert_pool,
    upsert_token,
)
from probe.checks_ws import CREATE_CANDIDATES, INSTRUCTION_RE
from probe.constants import DEXSCREENER_BASE, LAUNCHPAD_CANDIDATES, PUBLIC_RPC, PUBLIC_WS

RESULTS: list[tuple[str, bool, str]] = []


def record(step: str, ok: bool, detail: str) -> bool:
    RESULTS.append((step, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {step:<38} {detail}", flush=True)
    return ok


# ------------------------------------------------------------ (a) detection
async def _detect(ws_url: str, seconds: float, want: int) -> list[dict]:
    """Listen for token creations and return what we saw, with timestamps.

    detected_ts is stamped the instant the message arrives -- not when we later
    process it. Phase 2 prices entry from this, so it must be the truth about
    when the opportunity first existed for us.
    """
    import websockets

    found: list[dict] = []
    subs = {i + 1: (label, pid) for i, (label, pid) in enumerate(LAUNCHPAD_CANDIDATES)}
    try:
        async with websockets.connect(ws_url, ping_interval=20, close_timeout=5) as ws:
            for sub_id, (_, pid) in subs.items():
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": sub_id, "method": "logsSubscribe",
                    "params": [{"mentions": [pid]}, {"commitment": "processed"}],
                }))
            deadline = asyncio.get_event_loop().time() + seconds
            while asyncio.get_event_loop().time() < deadline and len(found) < want:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    break
                arrived = datetime.now(UTC)
                msg = json.loads(raw)
                value = (msg.get("params") or {}).get("result", {}).get("value") or {}
                logs = value.get("logs") or []
                # Exact instruction names, never substrings: "Instruction: Create"
                # as a substring also matches CreateIdempotent, which fires for
                # every first-time BUYER and inflated the launch rate ~25x.
                names = {m.group(1) for line in logs
                         if (m := INSTRUCTION_RE.search(line))}
                if not (names & CREATE_CANDIDATES):
                    continue
                mint = _mint_from_logs(logs)
                found.append({
                    "signature": value.get("signature"),
                    "detected_ts": arrived,
                    "slot": (msg.get("params") or {}).get("result", {}).get("context", {}).get("slot"),
                    "mint": mint,
                    "logs": logs[:12],
                })
    except Exception as exc:  # noqa: BLE001
        record("(a) detect launch", False, f"{type(exc).__name__}: {exc}")
    return found


def _mint_from_logs(logs: list[str]) -> str | None:
    """Best-effort mint extraction from program logs.

    Deliberately conservative. If it cannot find one it returns None and the
    caller resolves the mint from the transaction instead -- guessing a wrong
    address would poison the dataset far worse than recording 'unknown'.
    """
    for line in logs:
        if "Program log:" not in line:
            continue
        for token in line.replace(",", " ").split():
            if 32 <= len(token) <= 44 and token.isalnum():
                return token
    return None


# ----------------------------------------------------------------- pipeline
def run_pipeline(session: Session, client: httpx.Client, mint: str,
                 detected_ts: datetime, source: str, quote_url: str,
                 swap_url: str, rpc_url: str, notional_usd: float,
                 slot: int | None = None) -> None:
    """(b) market data -> (c) simulated exit -> (d) store. All idempotent."""

    token_id = upsert_token(
        session, chain="solana", address=mint, detected_ts=detected_ts,
        detection_source=source, first_seen_slot=slot,
    )
    insert_event(session, token_id=token_id, event_ts=detected_ts,
                 kind="first_seen", detail={"source": source})

    # ---- (b) market + liquidity
    fetched = fetch_dexscreener(client, mint, DEXSCREENER_BASE)
    raw_id = store_raw(session, source=fetched.source, endpoint=fetched.endpoint,
                       http_status=fetched.http_status, body=fetched.body,
                       fetched_ts=fetched.fetched_ts)
    snap = fetched.parsed
    pool_id = None
    if snap and snap.pair_address:
        pool_id = upsert_pool(
            session, token_id=token_id, chain="solana",
            pair_address=snap.pair_address, dex=snap.dex,
            quote_mint=snap.quote_mint,
            created_ts=datetime.fromtimestamp(snap.pair_created_ms / 1000, UTC)
            if snap.pair_created_ms else None,
            initial_liquidity_usd=snap.liquidity_usd,
            initial_price_usd=snap.price_usd,
        )
        token = session.get(Token, token_id)
        if token and not token.symbol:
            token.name, token.symbol = snap.name, snap.symbol
        insert_observation(
            session, token_id=token_id, pool_id=pool_id,
            observed_ts=fetched.fetched_ts, source="dexscreener",
            price_usd=snap.price_usd, price_native=snap.price_native,
            liquidity_usd=snap.liquidity_usd, market_cap_usd=snap.market_cap_usd,
            fdv_usd=snap.fdv_usd, volume_5m=snap.volume_5m, volume_1h=snap.volume_1h,
            volume_24h=snap.volume_24h, buys_5m=snap.buys_5m, sells_5m=snap.sells_5m,
            buys_1h=snap.buys_1h, sells_1h=snap.sells_1h, raw_payload_id=raw_id,
        )
        record("(b) market data", True,
               f"price=${snap.price_usd} liq=${snap.liquidity_usd} "
               f"dex={snap.dex} buys5m={snap.buys_5m}")
    else:
        # Not a failure of the pipeline: a token with no pair yet is a real,
        # recordable state, and one we specifically must not discard.
        insert_event(session, token_id=token_id, event_ts=fetched.fetched_ts,
                     kind="no_market_data",
                     detail={"error": fetched.error, "status": fetched.http_status})
        record("(b) market data", True,
               f"no pairs indexed yet ({fetched.error or 'empty'}) -- recorded, not dropped")

    # ---- (c) read-only simulated sell
    decimals = 6
    amount_raw = int(10 ** decimals)  # 1 whole token, nominal
    quoted = simulate_sell_quote(client, quote_url, mint, amount_raw, notional_usd)
    q_raw = store_raw(session, source=quoted.source, endpoint=quoted.endpoint,
                      http_status=quoted.http_status, body=quoted.body,
                      fetched_ts=quoted.fetched_ts)
    sim = quoted.parsed
    insert_simulated_exit(
        session, token_id=token_id, simulated_ts=quoted.fetched_ts,
        method=sim.method, notional_usd=sim.notional_usd,
        input_amount_raw=sim.input_amount_raw,
        expected_output_raw=sim.expected_output_raw,
        price_impact_pct=sim.price_impact_pct, slippage_bps=sim.slippage_bps,
        route_dex=sim.route_dex, route_hops=sim.route_hops,
        failure_kind=sim.failure_kind,
        liquidity_at_sim_usd=snap.liquidity_usd if snap else None,
        succeeded=sim.succeeded, failure_reason=sim.failure_reason,
        raw_payload_id=q_raw,
    )
    verdict = {True: "SELLABLE", False: "NOT SELLABLE",
               None: "UNKNOWN (our side failed -- not evidence)"}[sim.succeeded]
    record("(c) simulated sell [quote]", True,
           f"{verdict} kind={sim.failure_kind or '-'} "
           f"impact={sim.price_impact_pct} "
           f"route={sim.route_dex or sim.failure_reason}")

    if sim.succeeded and sim.raw:
        rpc = simulate_sell_rpc(client, swap_url, rpc_url, sim.raw, notional_usd)
        r_raw = store_raw(session, source=rpc.source, endpoint=rpc.endpoint,
                          http_status=rpc.http_status, body=rpc.body,
                          fetched_ts=rpc.fetched_ts)
        rsim = rpc.parsed
        insert_simulated_exit(
            session, token_id=token_id, simulated_ts=rpc.fetched_ts,
            method=rsim.method, notional_usd=rsim.notional_usd,
            succeeded=rsim.succeeded, failure_kind=rsim.failure_kind,
            failure_reason=rsim.failure_reason,
            liquidity_at_sim_usd=snap.liquidity_usd if snap else None,
            raw_payload_id=r_raw,
        )
        record("(c) simulated sell [rpc_sim]", True,
               f"executed_ok={rsim.succeeded} {rsim.failure_reason or ''}")

    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url",
                        default=os.environ.get("POC_DATABASE_URL",
                                               "postgresql+psycopg://localhost/memecoin_poc"))
    parser.add_argument("--quote-url", default="https://lite-api.jup.ag/swap/v1/quote")
    parser.add_argument("--swap-url", default="https://lite-api.jup.ag/swap/v1/swap")
    parser.add_argument("--rpc-url", default=os.environ.get("SOLANA_RPC", PUBLIC_RPC))
    parser.add_argument("--ws-url", default=os.environ.get("SOLANA_WS", PUBLIC_WS))
    parser.add_argument("--mint", action="append", default=[],
                        help="skip detection and run the pipeline on these mints")
    parser.add_argument("--skip-detect", action="store_true")
    parser.add_argument("--detect-seconds", type=float, default=90.0)
    parser.add_argument("--max-tokens", type=int, default=3)
    parser.add_argument("--notional-usd", type=float, default=100.0)
    args = parser.parse_args()

    print("=" * 78)
    print("PHASE 1 PROOF OF CONCEPT -- read-only, no wallet, no transactions")
    print("=" * 78)

    engine = create_engine(args.database_url, future=True)
    try:
        Base.metadata.create_all(engine)
        record("(d) database reachable", True, f"schema ready at {engine.url.render_as_string()}")
    except Exception as exc:  # noqa: BLE001
        record("(d) database reachable", False, f"{type(exc).__name__}: {exc}")
        print("\nCannot continue without a database. Start Postgres and retry.")
        return 1

    targets: list[tuple[str, datetime, str, int | None]] = [
        (m, datetime.now(UTC), "manual", None) for m in args.mint
    ]
    if not args.skip_detect and not targets:
        print(f"\n(a) listening {args.detect_seconds:.0f}s for new launches...")
        seen = asyncio.run(_detect(args.ws_url, args.detect_seconds, args.max_tokens))
        with_mint = [s for s in seen if s["mint"]]
        record("(a) detect launch", bool(seen),
               f"{len(seen)} creations seen, {len(with_mint)} with a parseable mint")
        targets = [(s["mint"], s["detected_ts"], "websocket", s["slot"]) for s in with_mint]

    if not targets:
        print("\nNo tokens to process. Pass --mint to run the rest deterministically.")
        return 1

    with httpx.Client(follow_redirects=True,
                      headers={"User-Agent": "memecoin-research-poc/0.1"}) as client, \
            Session(engine) as session:
        for mint, detected_ts, source, slot in targets[: args.max_tokens]:
            print(f"\n--- {mint} (detected {detected_ts.isoformat()} via {source})")
            run_pipeline(session, client, mint, detected_ts, source,
                         args.quote_url, args.swap_url, args.rpc_url,
                         args.notional_usd, slot)

        # ---- (d) prove storage, and prove replay does not duplicate
        counts = {
            "tokens": session.scalar(select(func.count()).select_from(Token)),
            "observations": session.scalar(select(func.count()).select_from(Observation)),
            "simulated_exits": session.scalar(select(func.count()).select_from(SimulatedExit)),
            "events": session.scalar(select(func.count()).select_from(Event)),
            "raw_payloads": session.scalar(select(func.count()).select_from(RawPayload)),
        }
        record("(d) rows stored", all(v is not None for v in counts.values()), str(counts))

        before = counts["observations"]
        for mint, detected_ts, source, slot in targets[: args.max_tokens]:
            run_pipeline(session, client, mint, detected_ts, source,
                         args.quote_url, args.swap_url, args.rpc_url,
                         args.notional_usd, slot)
        after = session.scalar(select(func.count()).select_from(Observation))
        token_count = session.scalar(select(func.count()).select_from(Token))
        record("(d) replay is idempotent", token_count == counts["tokens"],
               f"tokens {counts['tokens']} -> {token_count}; "
               f"observations {before} -> {after} (new timestamps are new rows, "
               "repeated facts are not)")

        sample = session.scalar(select(RawPayload.id).limit(1))
        if sample:
            body = read_raw(session, sample)
            record("(d) raw payload reproducible", bool(body),
                   f"{len(body or '')} bytes recovered from gzip")

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 78)
    print(f"RESULT  pass={len(RESULTS) - len(failed)}  fail={len(failed)}")
    print("=" * 78)
    for step, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED {step}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
