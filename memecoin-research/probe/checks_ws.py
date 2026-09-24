"""Websocket check: can we detect launches, and how many are there really?

The first version of this file guessed. It matched any log line containing
"Instruction: Create" and reported ~1,025,280 launches/day -- roughly 20-35x
any plausible rate. The substring also matches `CreateIdempotent`, the
associated-token-account instruction that fires whenever a NEW BUYER touches a
token, and `MintTo`, which on a bonding curve fires on every single buy. It was
counting buyers, not launches.

So this version does not guess. It counts every `Instruction: <Name>` it sees,
per program, and reports the distribution. The data then says which instruction
means "a token was created" -- instead of a hard-coded hint list deciding it in
advance and being confidently wrong.

It also multiplexes every subscription over ONE connection. Opening four killed
the fourth: the public RPC rejected it with a 1002, which the old version
reported as a dead program id rather than as our own concurrency limit.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import websockets

from probe.constants import LAUNCHPAD_CANDIDATES
from probe.report import Check, Outcome, Report

INSTRUCTION_RE = re.compile(r"Program log: Instruction: (\w+)")

# Instructions that fire on ordinary trading, not on a token being born.
# Counted and reported separately rather than silently dropped, so the
# over-count that produced the million-a-day figure stays visible.
TRADING_NOISE = frozenset({
    "CreateIdempotent",      # associated token account for a first-time buyer
    "InitializeAccount", "InitializeAccount2", "InitializeAccount3",
    "InitializeImmutableOwner",
    "MintTo", "MintToChecked",
    "Transfer", "TransferChecked", "SyncNative", "CloseAccount",
    "Buy", "Sell", "Swap", "Approve", "Revoke",
})

# Instructions that plausibly mean a new token or pool. Still provisional --
# the reported distribution is what settles it.
CREATE_CANDIDATES = frozenset({
    "Create", "CreatePool", "CreateEvent", "Initialize", "Initialize2",
    "InitializeMint", "InitializeMint2", "InitializeVirtualPoolWithSplToken",
    "InitializeConfig", "Migrate", "Launch",
})

# One subscription id per program, so a notification can be attributed to the
# program it came from when they all share a single socket.
SUB_BASE = 1000


@dataclass
class ProgramActivity:
    label: str
    program_id: str
    notifications: int = 0
    signatures: set[str] = field(default_factory=set)
    instructions: Counter = field(default_factory=Counter)
    samples: dict[str, str] = field(default_factory=dict)

    def create_like(self) -> int:
        return sum(n for name, n in self.instructions.items()
                   if name in CREATE_CANDIDATES)

    def noise(self) -> int:
        return sum(n for name, n in self.instructions.items()
                   if name in TRADING_NOISE)


async def _listen_all(ws_url: str, duration_s: float) -> dict[int, ProgramActivity]:
    """Subscribe to every candidate program over a SINGLE connection."""
    activity = {
        SUB_BASE + i: ProgramActivity(label=label, program_id=pid)
        for i, (label, pid) in enumerate(LAUNCHPAD_CANDIDATES)
    }
    # Maps the server's subscription number back to our request id.
    sub_to_request: dict[int, int] = {}

    async with websockets.connect(ws_url, ping_interval=20, close_timeout=5,
                                  max_size=4_000_000) as ws:
        for request_id, item in activity.items():
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": request_id, "method": "logsSubscribe",
                "params": [{"mentions": [item.program_id]}, {"commitment": "processed"}],
            }))

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)

            # Subscription acknowledgement: {"result": <sub_id>, "id": <request_id>}
            if "id" in msg and isinstance(msg.get("result"), int):
                sub_to_request[msg["result"]] = msg["id"]
                continue

            params = msg.get("params") or {}
            item = activity.get(sub_to_request.get(params.get("subscription"), -1))
            if item is None:
                continue
            value = (params.get("result") or {}).get("value") or {}
            if not value:
                continue

            item.notifications += 1
            if value.get("signature"):
                item.signatures.add(value["signature"])
            for line in value.get("logs") or []:
                match = INSTRUCTION_RE.search(line)
                if not match:
                    continue
                name = match.group(1)
                item.instructions[name] += 1
                if name not in item.samples and name in CREATE_CANDIDATES:
                    item.samples[name] = f"{value.get('signature', '?')[:16]}... {line[:90]}"
    return activity


async def probe_launchpads(report: Report, ws_url: str, duration_s: float) -> None:
    print(f"\n  listening on {ws_url} for {duration_s:.0f}s "
          f"({len(LAUNCHPAD_CANDIDATES)} programs, ONE multiplexed connection)...",
          flush=True)
    try:
        activity = await _listen_all(ws_url, duration_s)
    except Exception as exc:  # noqa: BLE001 -- the probe reports, never crashes
        report.add(Check("launchpad", "listen", Outcome.FAILED,
                         f"{type(exc).__name__}: {exc}", ws_url))
        return

    total_create = 0
    for item in activity.values():
        if item.notifications == 0:
            report.add(Check("launchpad", item.label, Outcome.UNVERIFIED,
                             "subscribed, but ZERO events -- program id may be stale "
                             "or the program may simply be quiet",
                             ws_url, evidence={"program_id": item.program_id}))
            continue

        create = item.create_like()
        total_create += create
        top = item.instructions.most_common(12)
        report.add(Check(
            "launchpad", item.label, Outcome.OK,
            f"{item.notifications} events | create-like {create} "
            f"(~{create / duration_s * 60:.1f}/min) | trading noise {item.noise()}",
            ws_url,
            evidence={"program_id": item.program_id,
                      "notifications": item.notifications,
                      "unique_signatures": len(item.signatures),
                      "create_like": create,
                      "instruction_histogram": dict(top),
                      "create_samples": item.samples},
        ))
        print(f"         top instructions: "
              f"{', '.join(f'{n}={c}' for n, c in top[:8])}", flush=True)

    rate = total_create / duration_s * 60.0
    report.add(Check(
        "launchpad", "MEASURED LAUNCH RATE", Outcome.OK if total_create else Outcome.UNVERIFIED,
        f"~{rate:.1f} create-like/min (~{rate * 60 * 24:,.0f}/day) -- "
        "read the instruction histogram before trusting this; "
        "trading instructions are excluded but the candidate list is provisional",
        evidence={"creates_per_min": round(rate, 2),
                  "projected_per_day": round(rate * 60 * 24),
                  "window_seconds": duration_s},
    ))


def run_launchpad_probe(report: Report, ws_url: str, duration_s: float) -> None:
    try:
        asyncio.run(probe_launchpads(report, ws_url, duration_s))
    except Exception as exc:  # noqa: BLE001
        report.add(Check("launchpad", "websocket", Outcome.FAILED,
                         f"{type(exc).__name__}: {exc}", ws_url))


async def _ws_reachable(ws_url: str) -> tuple[bool, str]:
    try:
        async with websockets.connect(ws_url, ping_interval=None, close_timeout=5):
            return True, "connected"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def check_ws_reachable(report: Report, ws_url: str, label: str) -> bool:
    ok, detail = asyncio.run(_ws_reachable(ws_url))
    report.add(Check(label, "websocket connect",
                     Outcome.OK if ok else Outcome.FAILED, detail, ws_url))
    return ok
