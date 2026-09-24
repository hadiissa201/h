"""The collector loop: detect, schedule, collect, record -- forever.

Structure is deliberately boring. A detector thread feeds a Postgres-backed
queue; worker threads drain it against per-service rate limits; a status server
exposes what is happening. Nothing clever, because this has to run unattended
for weeks and the failure mode we cannot tolerate is the quiet one.

What it will not do:
  - trade, sign, or hold a key of any kind
  - drop work silently (failures are requeued with the error recorded)
  - pretend it was collecting while it was down (collection_gaps says otherwise)
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from collector.config import CollectorSettings
from collector.detector import Detection, LaunchDetector
from collector.models import Base, CollectorRun, Observation, SimulatedExit, Token, WorkItem
from collector.ratelimit import Limiters, build_limiters
from collector.sampling import sample_score, should_track
from collector.schedule import WorkKind, backoff_due, capacity, next_due
from collector.workers import observe_holders, observe_market, simulate_exit
from poc.store import close_gap, insert_event, open_gap, upsert_token
from probe.checks_http import resolve_mint_from_signature
from probe.report import Report

log = logging.getLogger("collector")
VERSION = "phase1-0.1"


class Collector:
    def __init__(self, settings: CollectorSettings) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url, future=True, pool_size=10,
                                    max_overflow=20, pool_pre_ping=True)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.limiters: Limiters = build_limiters(settings)
        self.stop_event = threading.Event()
        self.run_id: int | None = None
        self.started_at = time.time()
        self.stats = {"detected": 0, "admitted": 0, "skipped_by_sampling": 0,
                      "mint_unresolved": 0, "work_done": 0, "work_failed": 0}
        self._detector: LaunchDetector | None = None
        self._client = httpx.Client(
            follow_redirects=True, timeout=settings.http_timeout_s,
            headers={"User-Agent": "memecoin-research-collector/0.1"},
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.Session() as session:
            run = CollectorRun(started_ts=datetime.now(UTC), version=VERSION,
                               sample_rate=self.settings.sample_rate,
                               config_note=json.dumps(capacity(self.settings)))
            session.add(run)
            session.commit()
            self.run_id = run.id

        plan = capacity(self.settings)
        log.info("capacity: %s concurrent tokens, %s/day intake, binding=%s",
                 plan["concurrent_tokens"], plan["intake_tokens_per_day"],
                 plan["binding_service"])
        if self.settings.using_fallback:
            log.warning("NO HELIUS KEY: falling back to the public RPC, which was "
                        "measured dropping ~30%% of launch messages WITHOUT erroring. "
                        "Detection will silently under-count.")

    def shutdown(self, reason: str) -> None:
        self.stop_event.set()
        if self._detector is not None:
            self._detector.stop()
        with self.Session() as session:
            if self.run_id is not None:
                run = session.get(CollectorRun, self.run_id)
                if run is not None:
                    run.ended_ts = datetime.now(UTC)
                    run.shutdown_reason = reason[:128]
                    session.commit()
        self._client.close()
        log.info("collector stopped: %s", reason)

    # -------------------------------------------------------------- detection
    def on_detection(self, detection: Detection) -> None:
        """Admit or reject a launch. Sampling decides, and the decision is stored.

        A rejected token is never written: storing every launch we chose not to
        track would be a second dataset with entirely different coverage. What
        IS stored, on every admitted token, is the sample rate and score that
        admitted it -- which is what lets Phase 2 weight back to the population.
        """
        self.stats["detected"] += 1
        try:
            with self.Session() as session:
                report = Report()  # resolve_mint_from_signature records into this
                mint = resolve_mint_from_signature(
                    report, self._client, self.settings.rpc_url, [detection.signature])
                if not mint:
                    self.stats["mint_unresolved"] += 1
                    return

                score = sample_score(mint)
                if not should_track(mint, self.settings.sample_rate):
                    self.stats["skipped_by_sampling"] += 1
                    return

                token_id = upsert_token(
                    session, chain="solana", address=mint,
                    detected_ts=detection.detected_ts,
                    detection_source=f"ws:{detection.program_label}",
                    first_seen_slot=detection.slot,
                    sample_score=score,
                    sample_rate_at_detection=self.settings.sample_rate,
                    is_tracked=True,
                )
                insert_event(session, token_id=token_id,
                             event_ts=detection.detected_ts, kind="first_seen",
                             detail={"program": detection.program_label,
                                     "signature": detection.signature,
                                     "instructions": list(detection.instructions)})
                for kind in WorkKind:
                    self._enqueue(session, token_id, kind, datetime.now(UTC))
                session.commit()
                self.stats["admitted"] += 1
        except Exception as exc:  # noqa: BLE001 -- a bad detection must not kill the loop
            log.exception("detection handling failed: %s", exc)

    def _enqueue(self, session: Session, token_id: int, kind: WorkKind,
                 due_at: datetime) -> None:
        existing = session.scalar(
            select(WorkItem).where(WorkItem.token_id == token_id,
                                   WorkItem.kind == kind.value))
        if existing is None:
            session.add(WorkItem(token_id=token_id, kind=kind.value, due_at=due_at))
        else:
            existing.due_at = due_at

    # ----------------------------------------------------------------- workers
    def worker_loop(self, worker_id: int) -> None:
        while not self.stop_event.is_set():
            try:
                if not self._process_one():
                    self.stop_event.wait(1.0)
            except Exception as exc:  # noqa: BLE001 -- a worker must never die
                log.exception("worker %d error: %s", worker_id, exc)
                self.stop_event.wait(2.0)

    def _process_one(self) -> bool:
        """Claim the most overdue task and run it. False when nothing is due."""
        with self.Session() as session:
            item = session.scalars(
                select(WorkItem)
                .where(WorkItem.due_at <= datetime.now(UTC))
                .order_by(WorkItem.due_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first() if self.engine.dialect.name == "postgresql" else session.scalars(
                select(WorkItem).where(WorkItem.due_at <= datetime.now(UTC))
                .order_by(WorkItem.due_at).limit(1)).first()
            if item is None:
                return False

            token = session.get(Token, item.token_id)
            if token is None:
                session.delete(item)
                session.commit()
                return True

            # Claim it immediately so a sibling worker cannot pick up the same
            # row while this one is out on the network.
            item.due_at = datetime.now(UTC) + timedelta(seconds=120)
            session.commit()

            kind = WorkKind(item.kind)
            result = self._run_work(session, kind, token)

            fresh = session.get(WorkItem, item.id)
            if fresh is None:
                session.commit()
                return True
            fresh.last_run_ts = datetime.now(UTC)
            if result.ok:
                fresh.attempts = 0
                fresh.last_error = None
                self.stats["work_done"] += 1
                due = next_due(self.settings, kind, token.detected_ts)
                if due is None:
                    session.delete(fresh)          # retired: stop scheduling
                    insert_event(session, token_id=token.id,
                                 event_ts=datetime.now(UTC), kind="tracking_ended",
                                 detail={"reason": "max_age", "work": kind.value})
                else:
                    fresh.due_at = due
            else:
                fresh.attempts += 1
                fresh.last_error = result.detail[:500]
                fresh.due_at = backoff_due(fresh.attempts)
                self.stats["work_failed"] += 1
            session.commit()
            return True

    def _run_work(self, session: Session, kind: WorkKind, token: Token):
        if kind is WorkKind.OBSERVE:
            return observe_market(session, self._client, self.limiters,
                                  self.settings, token)
        if kind is WorkKind.EXIT:
            return simulate_exit(session, self._client, self.limiters,
                                 self.settings, token)
        return observe_holders(session, self._client, self.limiters,
                               self.settings, token)

    # ------------------------------------------------------------------ gaps
    def gap_open(self, cause: str) -> int:
        with self.Session() as session:
            gap_id = open_gap(session, gap_start=datetime.now(UTC), cause=cause,
                              affected_sources="launchpad_websocket")
            session.commit()
            log.warning("BLIND: collection gap opened (%s)", cause)
            return gap_id

    def gap_close(self, gap_id: int) -> None:
        with self.Session() as session:
            close_gap(session, gap_id, datetime.now(UTC))
            session.commit()
            log.info("collection gap closed")

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        with self.Session() as session:
            counts = {
                "tokens": session.scalar(select(func.count()).select_from(Token)),
                "observations": session.scalar(select(func.count()).select_from(Observation)),
                "simulated_exits": session.scalar(select(func.count()).select_from(SimulatedExit)),
                "queue_depth": session.scalar(select(func.count()).select_from(WorkItem)),
                "queue_overdue": session.scalar(
                    select(func.count()).select_from(WorkItem)
                    .where(WorkItem.due_at <= datetime.now(UTC))),
                # Rows where we actually learned something. NULL means we could
                # not ask, and must never be counted as "could not sell".
                "exits_with_a_verdict": session.scalar(
                    select(func.count()).select_from(SimulatedExit)
                    .where(SimulatedExit.succeeded.is_not(None))),
                "exits_not_sellable": session.scalar(
                    select(func.count()).select_from(SimulatedExit)
                    .where(SimulatedExit.succeeded.is_(False))),
            }
        return {
            "version": VERSION,
            "uptime_s": round(time.time() - self.started_at, 1),
            "sample_rate": self.settings.sample_rate,
            "using_public_rpc_fallback": self.settings.using_fallback,
            "capacity": capacity(self.settings),
            "detector": self._detector.stats.snapshot() if self._detector else {},
            "pipeline": self.stats,
            "database": counts,
            "rate_limits": self.limiters.snapshot(),
        }


class _StatusHandler(BaseHTTPRequestHandler):
    collector: Collector = None  # set on the class before serving

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        if self.path not in ("/status", "/health", "/"):
            self.send_error(404)
            return
        payload = json.dumps(self.collector.status(), indent=2, default=str)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args) -> None:  # noqa: ANN002 -- silence access logs
        pass


def serve_status(collector: Collector, port: int) -> HTTPServer:
    _StatusHandler.collector = collector
    server = HTTPServer(("127.0.0.1", port), _StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("status on http://127.0.0.1:%d/status", port)
    return server


def run(settings: CollectorSettings, workers: int = 4) -> int:
    collector = Collector(settings)
    collector.start()
    server = serve_status(collector, settings.status_port)

    detector = LaunchDetector(
        settings.ws_url, collector.on_detection,
        on_gap_open=collector.gap_open, on_gap_close=collector.gap_close,
        base_delay=settings.reconnect_base_delay_s,
        max_delay=settings.reconnect_max_delay_s,
    )
    collector._detector = detector

    for index in range(workers):
        threading.Thread(target=collector.worker_loop, args=(index,),
                         daemon=True, name=f"worker-{index}").start()

    reason = "normal"

    def _signal(signum, _frame) -> None:  # noqa: ANN001
        nonlocal reason
        reason = f"signal {signum}"
        collector.stop_event.set()
        detector.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal)

    try:
        asyncio.run(detector.run())
    except KeyboardInterrupt:
        reason = "keyboard interrupt"
    finally:
        server.shutdown()
        collector.shutdown(reason)
    return 0
