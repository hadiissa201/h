"""Websocket check: can we detect launches, and how many are there really?

This does double duty. It confirms the detection mechanism works, and it
measures the ACTUAL launch rate -- which is the number that decides whether
full collection is affordable or sampling is forced. Guessing that rate would
have picked the architecture on a number nobody measured.

Every candidate program is subscribed separately so a program that has moved
shows up as "zero events" against its own name, not as a silent gap.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import websockets

from probe.constants import LAUNCHPAD_CANDIDATES
from probe.report import Check, Outcome, Report

# Log substrings that indicate a token creation rather than an ordinary trade.
# Deliberately broad: over-matching is visible in the sample we print, whereas
# under-matching would look like "no launches are happening".
CREATE_HINTS = ("Instruction: Create", "Instruction: Initialize", "InitializeMint",
                "Instruction: CreatePool", "initialize2", "Instruction: MintTo")


@dataclass
class ProgramActivity:
    label: str
    program_id: str
    notifications: int = 0
    create_like: int = 0
    signatures: set[str] = field(default_factory=set)
    sample_logs: list[str] = field(default_factory=list)
    error: str | None = None


async def _listen(ws_url: str, label: str, program_id: str,
                  duration_s: float) -> ProgramActivity:
    activity = ProgramActivity(label=label, program_id=program_id)
    sub = {
        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
        "params": [{"mentions": [program_id]}, {"commitment": "processed"}],
    }
    try:
        async with websockets.connect(ws_url, ping_interval=20, close_timeout=5) as ws:
            await ws.send(json.dumps(sub))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if "error" in ack:
                activity.error = f"subscribe rejected: {ack['error']}"
                return activity
            deadline = time.monotonic() + duration_s
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                value = (msg.get("params") or {}).get("result", {}).get("value") or {}
                if not value:
                    continue
                activity.notifications += 1
                sig = value.get("signature")
                if sig:
                    activity.signatures.add(sig)
                logs = value.get("logs") or []
                if any(hint in line for line in logs for hint in CREATE_HINTS):
                    activity.create_like += 1
                    if len(activity.sample_logs) < 3:
                        hit = next(ln for ln in logs
                                   if any(h in ln for h in CREATE_HINTS))
                        activity.sample_logs.append(f"{sig}: {hit}")
    except Exception as exc:  # noqa: BLE001 -- the probe reports, never crashes
        activity.error = f"{type(exc).__name__}: {exc}"
    return activity


async def probe_launchpads(report: Report, ws_url: str, duration_s: float) -> str | None:
    """Subscribe to every candidate program; report rates. Returns a live mint if seen."""
    print(f"\n  listening on {ws_url} for {duration_s:.0f}s per program "
          f"({len(LAUNCHPAD_CANDIDATES)} programs, concurrent)...", flush=True)

    results = await asyncio.gather(*[
        _listen(ws_url, label, pid, duration_s) for label, pid in LAUNCHPAD_CANDIDATES
    ], return_exceptions=True)

    total_creates = 0
    for item in results:
        if isinstance(item, BaseException):
            report.add(Check("launchpad", "listen", Outcome.FAILED, repr(item), ws_url))
            continue
        if item.error:
            report.add(Check("launchpad", item.label, Outcome.FAILED, item.error, ws_url,
                             evidence={"program_id": item.program_id}))
            continue
        if item.notifications == 0:
            report.add(Check("launchpad", item.label, Outcome.UNVERIFIED,
                             "subscribed fine but ZERO events -- program id is likely "
                             "stale or inactive; find the current address before relying on it",
                             ws_url, evidence={"program_id": item.program_id}))
            continue
        per_min = item.create_like / duration_s * 60.0
        total_creates += item.create_like
        report.add(Check(
            "launchpad", item.label, Outcome.OK,
            f"{item.notifications} events, {len(item.signatures)} unique txs, "
            f"{item.create_like} create-like (~{per_min:.1f}/min)",
            ws_url,
            evidence={"program_id": item.program_id,
                      "notifications": item.notifications,
                      "unique_signatures": len(item.signatures),
                      "create_like": item.create_like,
                      "create_per_min": round(per_min, 2),
                      "sample_logs": item.sample_logs},
        ))

    rate = total_creates / duration_s * 60.0
    report.add(Check(
        "launchpad", "MEASURED LAUNCH RATE", Outcome.OK if total_creates else Outcome.UNVERIFIED,
        f"~{rate:.1f} create-like events/min across all programs "
        f"(~{rate * 60 * 24:,.0f}/day) -- this is the number that decides "
        "whether full collection is affordable",
        evidence={"creates_per_min": round(rate, 2),
                  "projected_per_day": round(rate * 60 * 24),
                  "window_seconds": duration_s},
    ))
    return None


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
