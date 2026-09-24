"""Launch detection: a websocket that stays up, and an honest record of when it didn't.

Two things this file refuses to do.

It will not count instruction occurrences. A single pump.fun creation logs
Create AND InitializeMint2 AND Initialize, and a graduation transaction mentions
both the bonding curve and the AMM. Counting either way inflated the measured
launch rate by more than 20x before it was caught. Detection is per TRANSACTION,
deduplicated across programs.

It will not disappear quietly. Every disconnect opens a row in collection_gaps
and every reconnect closes it, because a collector that was offline and a market
that was silent look identical in the data otherwise -- and the first one makes
every base rate Phase 2 computes too low.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import websockets

from probe.constants import LAUNCHPAD_CANDIDATES
from probe.report import redact

log = logging.getLogger("collector.detector")

INSTRUCTION_RE = re.compile(r"Program log: Instruction: (\w+)")

CREATE_CANDIDATES = frozenset({
    "Create", "CreatePool", "CreateEvent", "Initialize", "Initialize2",
    "InitializeMint", "InitializeMint2", "InitializeVirtualPoolWithSplToken",
    "InitializeConfig", "Migrate", "Launch",
})

SUB_BASE = 1000
# Signatures already emitted, so a graduation seen on two subscriptions -- or a
# message redelivered after a reconnect -- is not a second launch. Bounded: the
# collector runs for weeks and an unbounded set is a slow memory leak.
_SEEN_MAX = 200_000


@dataclass
class Detection:
    signature: str
    detected_ts: datetime
    slot: int | None
    program_label: str
    instructions: tuple[str, ...]


@dataclass
class DetectorStats:
    connected_since: float | None = None
    reconnects: int = 0
    messages: int = 0
    detections: int = 0
    duplicates_suppressed: int = 0
    last_message_ts: float | None = None
    last_error: str | None = None
    open_gap_id: int | None = None
    seen_signatures: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, object]:
        return {
            "connected": self.connected_since is not None,
            "connected_for_s": (round(time.time() - self.connected_since, 1)
                                if self.connected_since else 0),
            "reconnects": self.reconnects,
            "messages": self.messages,
            "detections": self.detections,
            "duplicates_suppressed": self.duplicates_suppressed,
            "seconds_since_last_message": (round(time.time() - self.last_message_ts, 1)
                                           if self.last_message_ts else None),
            "last_error": self.last_error,
            "blind_right_now": self.open_gap_id is not None,
        }


class LaunchDetector:
    """Subscribes to every launchpad over ONE connection and reconnects forever.

    One connection is not an optimisation: opening four got the fourth rejected
    by the RPC with a protocol error, which looked exactly like a dead program id.
    """

    def __init__(self, ws_url: str, on_detection: Callable[[Detection], None],
                 on_gap_open: Callable[[str], int] | None = None,
                 on_gap_close: Callable[[int], None] | None = None,
                 base_delay: float = 1.0, max_delay: float = 60.0) -> None:
        self._ws_url = ws_url
        self._on_detection = on_detection
        self._on_gap_open = on_gap_open
        self._on_gap_close = on_gap_close
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._stop = asyncio.Event()
        self.stats = DetectorStats()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Reconnect loop. Only a stop request ends it."""
        delay = self._base_delay
        # We start blind: nothing is being collected until the socket is up.
        self._open_gap("startup")
        while not self._stop.is_set():
            try:
                await self._session()
                delay = self._base_delay  # a clean session resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- must never exit the loop
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("detector disconnected: %s", self.stats.last_error)
            self.stats.connected_since = None
            self._open_gap(self.stats.last_error or "disconnected")
            if self._stop.is_set():
                break
            self.stats.reconnects += 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(self._max_delay, delay * 2)
        self._close_gap()

    def _open_gap(self, cause: str) -> None:
        if self.stats.open_gap_id is None and self._on_gap_open is not None:
            self.stats.open_gap_id = self._on_gap_open(cause[:120])

    def _close_gap(self) -> None:
        if self.stats.open_gap_id is not None and self._on_gap_close is not None:
            self._on_gap_close(self.stats.open_gap_id)
        self.stats.open_gap_id = None

    async def _session(self) -> None:
        log.info("detector connecting to %s", redact(self._ws_url))
        async with websockets.connect(self._ws_url, ping_interval=20,
                                      ping_timeout=20, close_timeout=5,
                                      max_size=8_000_000) as ws:
            labels = {}
            for index, (label, program_id) in enumerate(LAUNCHPAD_CANDIDATES):
                request_id = SUB_BASE + index
                labels[request_id] = label
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": request_id, "method": "logsSubscribe",
                    "params": [{"mentions": [program_id]}, {"commitment": "processed"}],
                }))

            sub_to_request: dict[int, int] = {}
            self.stats.connected_since = time.time()
            # Connected: whatever gap we were in is over.
            self._close_gap()
            log.info("detector connected, %d subscriptions", len(labels))

            while not self._stop.is_set():
                raw = await ws.recv()
                self.stats.messages += 1
                self.stats.last_message_ts = time.time()
                self._handle(json.loads(raw), sub_to_request, labels)

    def _handle(self, msg: dict, sub_to_request: dict[int, int],
                labels: dict[int, str]) -> None:
        if "id" in msg and isinstance(msg.get("result"), int):
            sub_to_request[msg["result"]] = msg["id"]
            return
        params = msg.get("params") or {}
        result = params.get("result") or {}
        value = result.get("value") or {}
        if not value:
            return
        signature = value.get("signature")
        if not signature:
            return

        names = {m.group(1) for line in (value.get("logs") or [])
                 if (m := INSTRUCTION_RE.search(line))}
        creating = names & CREATE_CANDIDATES
        if not creating:
            return

        # Per TRANSACTION, deduplicated globally: one token, one detection,
        # however many create instructions it logged or programs it mentioned.
        if signature in self.stats.seen_signatures:
            self.stats.duplicates_suppressed += 1
            return
        if len(self.stats.seen_signatures) >= _SEEN_MAX:
            self.stats.seen_signatures.clear()
        self.stats.seen_signatures.add(signature)

        request_id = sub_to_request.get(params.get("subscription"), -1)
        self.stats.detections += 1
        self._on_detection(Detection(
            signature=signature,
            detected_ts=datetime.now(UTC),   # when WE saw it, never launch time
            slot=(result.get("context") or {}).get("slot"),
            program_label=labels.get(request_id, "unknown"),
            instructions=tuple(sorted(creating)),
        ))
