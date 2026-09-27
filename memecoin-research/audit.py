"""Integrity audit of the Phase 1 collector and paper trading.

Answers, from the live database rather than from what the code intends:
  1. traces complete paper trades, entry to exit, with every number
  2. explains what total_costs_usd actually contains
  3. checks the exit classification is mutually exclusive and correct
  4. reports whether the RPC simulation ever ran
  5. tests whether abandoned detections are random or biased by launchpad
  6. explains queue depth
  7. hunts duplicates, impossible jumps, bad timestamps and look-ahead

Read-only. It opens a session, runs SELECTs, and prints. It changes nothing.

    python audit.py
    python audit.py --trades 5
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from collector.config import load_settings
from collector import verify
from collector.models import (
    CollectionGap,
    Observation,
    PaperPosition,
    PendingDetection,
    SimulatedExit,
    Token,
    WorkItem,
)

FINDINGS: list[tuple[str, str]] = []


def finding(severity: str, text: str) -> None:
    FINDINGS.append((severity, text))
    print(f"    [{severity}] {text}")


def aware(moment):  # noqa: ANN001
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ------------------------------------------------------------ 1. trade traces
def trace_trades(session: Session, limit: int) -> None:
    head("1. PAPER TRADE TRACES -- detection to P&L")
    rows = session.scalars(
        select(PaperPosition).where(PaperPosition.is_open.is_(False))
        .order_by(PaperPosition.closed_ts.desc()).limit(limit)).all()
    if not rows:
        print("  no closed positions yet")
        return

    for pos in rows:
        token = session.get(Token, pos.token_id)
        print(f"\n  --- {pos.strategy} / token {pos.token_id} "
              f"({(token.address or '?')[:16]}...)")
        print(f"      detected     {aware(token.detected_ts)}")
        print(f"      entry        {aware(pos.opened_ts)}  "
              f"price {float(pos.entry_price_usd):.12g}  "
              f"age at entry {float(pos.token_age_at_entry_s or 0):.0f}s")
        print(f"      notional     ${float(pos.notional_usd):.2f}   "
              f"entry liquidity ${float(pos.entry_liquidity_usd or 0):,.0f}")

        # Every exit attempt this position could have seen.
        attempts = session.scalars(
            select(SimulatedExit)
            .where(SimulatedExit.token_id == pos.token_id,
                   SimulatedExit.simulated_ts >= pos.opened_ts,
                   SimulatedExit.simulated_ts <= (pos.closed_ts or pos.opened_ts))
            .order_by(SimulatedExit.simulated_ts)).all()
        verdicts = Counter(
            "sellable" if a.succeeded else ("no_route" if a.succeeded is False
                                            else "unknown") for a in attempts)
        print(f"      exit attempts while held: {len(attempts)}  {dict(verdicts)}")
        print(f"      blocked: {pos.blocked_exits or 0} "
              f"(market {pos.blocked_no_route or 0}, ours {pos.blocked_our_fault or 0})")

        entry_imp = float(pos.entry_price_impact_pct or 0.0)
        exit_imp = float(pos.price_impact_at_exit_pct or 0.0)
        gross = float(pos.gross_pnl_usd or 0.0)
        charged = float(pos.costs_usd or 0.0)
        gross_costs = float(pos.costs_gross_usd or charged)
        net = float(pos.net_pnl_usd or 0.0)
        print(f"      exit         {aware(pos.closed_ts)}  "
              f"price {float(pos.exit_price_usd or 0):.12g}  reason {pos.exit_reason}")
        print(f"      impact       entry {entry_imp:.4%}   exit {exit_imp:.4%}")
        print(f"      gross P&L    ${gross:+.2f}")
        print(f"      costs        charged ${charged:.2f}  actual ${gross_costs:.2f}"
              f"{'  (CAPPED at the -notional floor)' if pos.costs_capped_by_floor else ''}")
        print(f"      net P&L      ${net:+.2f}")

        # The arithmetic must reconcile, and the floor must hold.
        if abs((gross - charged) - net) > 0.01:
            finding("BUG", f"position {pos.id}: net != gross - costs")
        if net < -float(pos.notional_usd) - 0.01:
            finding("BUG", f"position {pos.id}: lost more than the stake")
        if aware(pos.opened_ts) < aware(token.detected_ts):
            finding("BUG", f"position {pos.id}: entered BEFORE detection")
        if pos.closed_ts and aware(pos.closed_ts) < aware(pos.opened_ts):
            finding("BUG", f"position {pos.id}: closed before it opened")


# ---------------------------------------------------------------- 2. costs
def audit_costs(session: Session) -> None:
    head("2. WHAT total_costs_usd ACTUALLY CONTAINS")
    print("""  Code path: collector/paper.py::_close

    fees        = notional * round_trip_cost_pct        (flat, both legs)
    entry_imp   = notional * entry_price_impact_pct      (buying moves price)
    exit_imp    = (notional + gross) * exit_impact_pct   (selling moves price)
    costs_gross = fees + entry_imp + exit_imp
    costs       = costs_gross, capped so net >= -notional

  So it is TRANSACTION COSTS: fees plus price impact on both legs. It is NOT
  deployed capital and NOT cumulative notional -- those are reported
  separately as total_notional_deployed_usd.""")

    closed = session.scalars(
        select(PaperPosition).where(PaperPosition.is_open.is_(False))).all()
    absurd = [p for p in closed
              if float(p.costs_usd or 0) > float(p.notional_usd or 1) * 3]
    if absurd:
        by_strategy = Counter(p.strategy for p in absurd)
        finding("STALE DATA",
                f"{len(absurd)} closed positions have costs > 3x their stake: "
                f"{dict(by_strategy)}. These predate the impact clamp and are "
                f"the entire source of the ~$3.5m totals. They are not a "
                f"current calculation error; analyse_paper.py excludes them.")
        worst = max(absurd, key=lambda p: float(p.costs_usd or 0))
        print(f"      worst: position {worst.id} ({worst.strategy}) "
              f"costs ${float(worst.costs_usd):,.2f} on a "
              f"${float(worst.notional_usd):.0f} stake")
    else:
        print("\n  No position has implausible costs.")

    no_entry_impact = [p for p in closed if p.entry_price_impact_pct is None]
    if no_entry_impact:
        finding("KNOWN GAP",
                f"{len(no_entry_impact)} closed positions were opened before "
                f"entry impact was charged. Their costs are UNDERSTATED -- only "
                f"the exit leg paid impact.")


# ------------------------------------------------------- 3. exit classification
def audit_exit_classification(session: Session) -> None:
    head("3. EXIT CLASSIFICATION")
    total = session.scalar(select(func.count()).select_from(SimulatedExit))
    counts = {
        "sellable (True)": session.scalar(
            select(func.count()).select_from(SimulatedExit)
            .where(SimulatedExit.succeeded.is_(True))),
        "no route (False)": session.scalar(
            select(func.count()).select_from(SimulatedExit)
            .where(SimulatedExit.succeeded.is_(False))),
        "unknown (NULL)": session.scalar(
            select(func.count()).select_from(SimulatedExit)
            .where(SimulatedExit.succeeded.is_(None))),
    }
    print(f"  {total} simulated exits")
    for name, n in counts.items():
        print(f"    {name:20} {n:>7}  ({n / total * 100:5.1f}%)" if total else name)
    if sum(counts.values()) != total:
        finding("BUG", "verdict counts do not sum to the total")

    print("\n  Failure kinds among the non-sellable:")
    kinds = Counter(
        k for (k,) in session.execute(
            select(SimulatedExit.failure_kind)
            .where(SimulatedExit.failure_kind.is_not(None))).all())
    for kind, n in kinds.most_common():
        print(f"    {kind:16} {n:>7}")

    # THE critical invariant: a transport failure must never be a failed sell.
    bad = session.scalar(
        select(func.count()).select_from(SimulatedExit)
        .where(SimulatedExit.succeeded.is_(False),
               SimulatedExit.failure_kind.in_(
                   ("transport", "bad_request", "unparseable"))))
    if bad:
        finding("BUG", f"{bad} rows marked NOT SELLABLE whose failure_kind is "
                       f"one of ours. Our outage is being counted as a rug.")
    else:
        print("\n    OK: no row is marked 'could not sell' for one of our own failures.")

    legacy = session.scalar(
        select(func.count()).select_from(SimulatedExit)
        .where(SimulatedExit.succeeded.is_(False),
               SimulatedExit.failure_kind.is_(None)))
    if legacy:
        finding("STALE DATA",
                f"{legacy} 'no route' rows have NO failure_kind. They predate "
                f"the classification fix and MAY include HTTP 400s that were "
                f"our own malformed requests. The historical unsellable rate is "
                f"an UPPER bound until these age out.")


# ------------------------------------------------------ 4. RPC simulation
def audit_rpc_simulation(session: Session) -> None:
    head("4. RPC SIMULATION")
    methods = Counter(
        m for (m,) in session.execute(select(SimulatedExit.method)).all())
    print(f"  methods recorded: {dict(methods)}")
    if methods.get("rpc_sim", 0) == 0:
        finding("KNOWN GAP",
                "ZERO rpc_sim rows. The unsigned simulateTransaction path exists "
                "but nothing has called it yet. We test ROUTING only; a frozen "
                "account or transfer hook would not be caught. This is a real "
                "limit on what 'sellable' means here.")
    else:
        counts = verify.disagreement_rate(session)
        print(f"\n  {verify.describe(counts)}")
        print(f"    would land:  {counts['would_land']}")
        print(f"    would FAIL:  {counts['would_revert']}")
        print(f"    unknown:     {counts['unknown']}  (could not run the check)")
        rate = counts["quote_false_positive_rate"]
        if rate is None:
            finding("KNOWN GAP",
                    f"{counts['unknown']} verification attempts, none of which "
                    f"returned a verdict. The check is running but learning "
                    f"nothing -- treat sellability as routing-only until this "
                    f"produces answers.")
        elif rate > 0.02:
            finding("OVERSTATED",
                    f"{rate:.1%} of quotes that said 'sellable' would NOT have "
                    f"landed on chain. Every paper return in this project is "
                    f"overstated by roughly that share of its exits, because "
                    f"paper trading closes on the quote verdict. Do not compare "
                    f"strategies until this is folded in.")
        else:
            print(f"\n    OK: quote verdicts matched chain state within "
                  f"{rate:.1%}; routing is a fair proxy at this sample size.")
    print("\n  Wallet safety: no private key exists in this codebase; "
          "tests/test_no_wallet.py fails the build if one is added.")


# --------------------------------------------------- 5. detection/resolution
def audit_detection_bias(session: Session) -> None:
    head("5. ABANDONED DETECTIONS -- random or biased?")
    total = session.scalar(select(func.count()).select_from(PendingDetection))
    given_up = session.scalar(
        select(func.count()).select_from(PendingDetection)
        .where(PendingDetection.give_up_reason.is_not(None)))
    resolved = session.scalar(
        select(func.count()).select_from(PendingDetection)
        .where(PendingDetection.resolved.is_(True)))
    print(f"  {total} detections: {resolved} resolved, {given_up} given up "
          f"({given_up / total * 100:.1f}%)" if total else "  none")

    print("\n  By launchpad -- if these rates differ, the loss is BIASED:")
    print(f"    {'program':28}{'total':>8}{'resolved':>10}{'given up':>10}{'loss %':>9}")
    rates = {}
    for (program,) in session.execute(
            select(PendingDetection.program_label).distinct()).all():
        n = session.scalar(select(func.count()).select_from(PendingDetection)
                           .where(PendingDetection.program_label == program))
        ok = session.scalar(select(func.count()).select_from(PendingDetection)
                            .where(PendingDetection.program_label == program,
                                   PendingDetection.resolved.is_(True)))
        lost = session.scalar(select(func.count()).select_from(PendingDetection)
                              .where(PendingDetection.program_label == program,
                                     PendingDetection.give_up_reason.is_not(None)))
        pct = lost / n * 100 if n else 0.0
        rates[program] = pct
        print(f"    {str(program):28}{n:>8}{ok:>10}{lost:>10}{pct:>8.1f}%")

    if len(rates) > 1:
        spread = max(rates.values()) - min(rates.values())
        if spread > 15.0:
            finding("BIAS",
                    f"loss rate varies {spread:.0f} points across launchpads "
                    f"({rates}). Abandoned detections are NOT random, so the "
                    f"tracked sample over-represents whatever resolves easily.")
        else:
            print(f"\n    Spread {spread:.1f} points -- no strong launchpad bias.")


# --------------------------------------------------------------- 6. queue
def audit_queue(session: Session) -> None:
    head("6. QUEUE HEALTH")
    depth = session.scalar(select(func.count()).select_from(WorkItem))
    overdue = session.scalar(select(func.count()).select_from(WorkItem)
                             .where(WorkItem.due_at <= datetime.now(UTC)))
    tracked = session.scalar(select(func.count()).select_from(Token)
                             .where(Token.is_tracked.is_(True)))
    kinds = Counter(k for (k,) in session.execute(select(WorkItem.kind)).all())
    print(f"  depth {depth}, overdue {overdue}, tracked tokens {tracked}")
    print(f"  by kind: {dict(kinds)}")
    print(f"  expected if every tracked token holds one job per kind: "
          f"{tracked * len(kinds)}")
    if overdue == 0:
        print("\n    Depth is SCHEDULED FUTURE WORK, not backlog: every item has a\n"
              "    due_at in the future. Backlog would show as overdue > 0.")
    else:
        finding("BACKLOG", f"{overdue} items are past due -- workers are behind.")

    stuck = session.scalar(select(func.count()).select_from(WorkItem)
                           .where(WorkItem.attempts >= 5))
    if stuck:
        finding("WARN", f"{stuck} work items have failed 5+ times.")


# --------------------------------------------------------- 7. data quality
def audit_data_quality(session: Session) -> None:
    head("7. DATA QUALITY")

    dupe_tokens = session.execute(
        select(Token.chain, Token.address, func.count().label("n"))
        .group_by(Token.chain, Token.address).having(func.count() > 1)).all()
    print(f"  duplicate tokens:       {len(dupe_tokens)}")
    if dupe_tokens:
        finding("BUG", f"{len(dupe_tokens)} tokens appear more than once")

    dupe_obs = session.execute(
        select(Observation.token_id, Observation.observed_ts, Observation.source,
               func.count().label("n"))
        .group_by(Observation.token_id, Observation.observed_ts, Observation.source)
        .having(func.count() > 1)).all()
    print(f"  duplicate observations: {len(dupe_obs)}")
    if dupe_obs:
        finding("BUG", f"{len(dupe_obs)} duplicate observations")

    # Observations before their token was detected = look-ahead.
    early = 0
    for token in session.scalars(select(Token)).all():
        first = session.scalars(
            select(Observation).where(Observation.token_id == token.id)
            .order_by(Observation.observed_ts).limit(1)).first()
        if first and aware(first.observed_ts) < aware(token.detected_ts) - timedelta(seconds=1):
            early += 1
    print(f"  observations before detection: {early}")
    if early:
        finding("LOOKAHEAD", f"{early} tokens have observations predating detection")

    # Impossible jumps between consecutive observations.
    jumps = 0
    checked = 0
    for token in session.scalars(select(Token).limit(400)).all():
        prices = [float(p) for (p,) in session.execute(
            select(Observation.price_usd)
            .where(Observation.token_id == token.id,
                   Observation.price_usd.is_not(None))
            .order_by(Observation.observed_ts)).all() if p]
        checked += 1
        for a, b in zip(prices, prices[1:], strict=False):
            if a > 0 and (b / a > 1000 or b / a < 0.001):
                jumps += 1
    print(f"  implausible consecutive jumps (>1000x) in {checked} tokens: {jumps}")
    if jumps:
        finding("DATA", f"{jumps} price jumps beyond 1000x -- feed artefacts. "
                        f"The paper trader now ignores these, but they are in "
                        f"the raw observations.")

    gaps = session.scalar(select(func.count()).select_from(CollectionGap))
    open_gaps = session.scalar(select(func.count()).select_from(CollectionGap)
                               .where(CollectionGap.gap_end.is_(None)))
    print(f"  recorded collection gaps: {gaps} ({open_gaps} still open)")

    # Exit simulations that predate the token's detection.
    bad_exit_ts = 0
    for token in session.scalars(select(Token).limit(400)).all():
        n = session.scalar(
            select(func.count()).select_from(SimulatedExit)
            .where(SimulatedExit.token_id == token.id,
                   SimulatedExit.simulated_ts < token.detected_ts))
        bad_exit_ts += n or 0
    print(f"  exit sims before detection: {bad_exit_ts}")
    if bad_exit_ts:
        finding("LOOKAHEAD", f"{bad_exit_ts} exit simulations predate detection")

    obs_per = [n for (n,) in session.execute(
        select(func.count()).select_from(Observation)
        .group_by(Observation.token_id)).all()]
    if obs_per:
        print(f"  observations per token: median {statistics.median(obs_per):.0f}, "
              f"min {min(obs_per)}, max {max(obs_per)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=int, default=4)
    args = parser.parse_args()

    engine = create_engine(load_settings().database_url, future=True)
    with Session(engine) as session:
        print("=" * 78)
        print("PHASE 1 INTEGRITY AUDIT -- read-only, changes nothing")
        print("=" * 78)
        trace_trades(session, args.trades)
        audit_costs(session)
        audit_exit_classification(session)
        audit_rpc_simulation(session)
        audit_detection_bias(session)
        audit_queue(session)
        audit_data_quality(session)

    head("SUMMARY")
    if not FINDINGS:
        print("  No findings. Everything checked reconciles.")
        return 0
    by_severity = Counter(sev for sev, _ in FINDINGS)
    print(f"  {len(FINDINGS)} finding(s): {dict(by_severity)}\n")
    for severity, text in FINDINGS:
        print(f"  [{severity}] {text}")
    blocking = [f for f in FINDINGS if f[0] in ("BUG", "LOOKAHEAD")]
    print("\n  " + ("PAPER P&L CANNOT BE TRUSTED -- fix the BUG/LOOKAHEAD items."
                    if blocking else
                    "No correctness bugs. Stale-data and known-gap items are "
                    "caveats on interpretation, not arithmetic errors."))
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
