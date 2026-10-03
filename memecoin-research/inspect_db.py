"""Show what is actually in the research database. Read-only.

Every table, how many rows, and a few real rows from the ones that matter, so
the dataset can be inspected rather than taken on trust.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector.models import Base, Observation, PaperPosition, SimulatedExit, Token

# What each table is for, in one line. A row count without this is trivia.
PURPOSE = {
    "tokens": "one row per launch we chose to track",
    "observations": "price, liquidity and buy counts over time -- the raw history",
    "simulated_exits": "could this have been SOLD at that moment, and at what cost",
    "paper_positions": "hypothetical trades: entry, exit, P&L. No orders, ever",
    "token_status": "derived current state: first/peak price, still tradable",
    "pending_detections": "launches seen on the websocket, awaiting mint resolution",
    "work_queue": "the schedule: what to fetch for which token, and when",
    "collection_gaps": "periods the collector was down, so silence is explainable",
    "collector_runs": "one row per start, so downtime has a cause",
    "events": "state changes worth their own row, e.g. became_unsellable",
    "liquidity_events": "pool liquidity added or removed -- how a rug actually happens",
    "creators": "deployer wallets and how many tokens each has launched",
    "holder_snapshots": "holder concentration at a moment, sampled sparsely",
    "pools": "the AMM pools backing each token",
    "raw_payloads": "unparsed API responses, kept so a parse can be re-checked",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=3,
                        help="sample rows to show per key table")
    args = parser.parse_args()

    engine = create_engine(load_settings().database_url, future=True)
    live = set(inspect(engine).get_table_names())

    print("=" * 78)
    print("TABLES")
    print("=" * 78)
    with Session(engine) as session:
        for table in Base.metadata.sorted_tables:
            if table.name not in live:
                print(f"  {table.name:<22}{'MISSING':>10}  (run sync_schema.py)")
                continue
            count = session.scalar(select(func.count()).select_from(table))
            print(f"  {table.name:<22}{count:>10,}  {PURPOSE.get(table.name, '')}")

        print("\n" + "=" * 78)
        print(f"SAMPLE: tokens (newest {args.rows})")
        print("=" * 78)
        for token in session.scalars(
                select(Token).order_by(Token.detected_ts.desc()).limit(args.rows)):
            print(f"  id {token.id}  {token.address}")
            print(f"    detected      {token.detected_ts}")
            print(f"    launchpad     {token.detection_source}")
            print(f"    decimals      {token.decimals}   "
                  f"sampled at rate {token.sample_rate_at_detection}")
            print(f"    mint auth     {token.mint_authority or 'revoked'}")
            print(f"    freeze auth   {token.freeze_authority or 'revoked'}")
            print(f"    creator       {token.creator_address or 'unresolved'}")
            obs = session.scalar(
                select(func.count()).select_from(Observation)
                .where(Observation.token_id == token.id))
            sims = session.scalar(
                select(func.count()).select_from(SimulatedExit)
                .where(SimulatedExit.token_id == token.id))
            print(f"    {obs} observations, {sims} exit checks")

        print("\n" + "=" * 78)
        print(f"SAMPLE: observations (newest {args.rows})")
        print("=" * 78)
        print(f"  {'token':>6}  {'observed':<26}{'price':>14}{'liquidity':>13}"
              f"{'buys 5m':>9}")
        for obs in session.scalars(
                select(Observation).order_by(Observation.observed_ts.desc())
                .limit(args.rows)):
            price = f"{float(obs.price_usd):.4e}" if obs.price_usd else "-"
            liq = f"${float(obs.liquidity_usd):,.0f}" if obs.liquidity_usd else "-"
            print(f"  {obs.token_id:>6}  {str(obs.observed_ts):<26}{price:>14}"
                  f"{liq:>13}{obs.buys_5m or 0:>9}")

        print("\n" + "=" * 78)
        print(f"SAMPLE: simulated_exits (newest {args.rows}) -- the key table")
        print("=" * 78)
        print("  succeeded: True = sellable, False = NO ROUTE, None = we could not ask")
        for sim in session.scalars(
                select(SimulatedExit).order_by(SimulatedExit.simulated_ts.desc())
                .limit(args.rows)):
            impact = (f"{float(sim.price_impact_pct) * 100:.2f}%"
                      if sim.price_impact_pct is not None else "-")
            print(f"  token {sim.token_id}  {sim.simulated_ts}  method={sim.method}")
            print(f"    succeeded={sim.succeeded}  impact={impact}  "
                  f"kind={sim.failure_kind or '-'}")
            if sim.failure_reason:
                print(f"    reason: {sim.failure_reason[:90]}")

        print("\n" + "=" * 78)
        print(f"SAMPLE: paper_positions (newest {args.rows} closed)")
        print("=" * 78)
        for pos in session.scalars(
                select(PaperPosition).where(PaperPosition.is_open.is_(False))
                .order_by(PaperPosition.closed_ts.desc()).limit(args.rows)):
            print(f"  {pos.strategy:<16} token {pos.token_id}  "
                  f"reason={pos.exit_reason}")
            print(f"    stake ${float(pos.notional_usd):,.2f}  "
                  f"entry {float(pos.entry_price_usd):.4e}  "
                  f"exit {float(pos.exit_price_usd or 0):.4e}")
            print(f"    net ${float(pos.net_pnl_usd or 0):+,.2f}  "
                  f"costs ${float(pos.costs_usd or 0):,.2f}  "
                  f"blocked exits {pos.blocked_exits or 0}")

        print("\n" + "=" * 78)
        print("WHAT IS NOT HERE")
        print("=" * 78)
        print("  No private keys. No wallet. No orders. Every 'position' above is")
        print("  arithmetic over recorded prices; nothing was ever bought or sold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
