"""Result records for the probe.

A probe that says "OK" without evidence is worthless. Every check carries the
request it made, the status it got, and the latency, so a later disagreement
can be settled by reading the record instead of re-running from memory.

Three outcomes, never two. UNVERIFIED is a first-class result: it means the
check could not be performed (no API key, host unreachable), which is different
from the check running and failing. Collapsing those two into "failed" is how a
missing credential gets mistaken for a broken service.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


# Helius and friends carry the key in the query string, so any URL recorded by
# a check would put a live credential into probe_report.json -- a file meant to
# be shared. Every endpoint is masked on the way in. The project's rule from day
# one has been that secrets are never logged; a report is a log.
_SECRET_PARAMS = ("api-key", "api_key", "apikey", "key", "token", "access_token")
_SECRET_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(p) for p in _SECRET_PARAMS) + r")=([^&\s]+)"
)


def redact(text: str | None) -> str:
    """Mask credentials in a URL or message. Keeps enough to tell keys apart."""
    if not text:
        return text or ""

    def mask(match: re.Match[str]) -> str:
        value = match.group(2)
        tail = value[-4:] if len(value) > 8 else ""
        return f"{match.group(1)}=***REDACTED{('...' + tail) if tail else ''}"

    return _SECRET_RE.sub(mask, text)


class Outcome(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class Check:
    service: str
    name: str
    outcome: Outcome
    detail: str = ""
    endpoint: str = ""
    http_status: int | None = None
    latency_ms: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        # Redact at construction, so no code path can record a raw key even by
        # forgetting to call something.
        self.endpoint = redact(self.endpoint)
        self.detail = redact(self.detail)

    @property
    def symbol(self) -> str:
        return {Outcome.OK: "PASS", Outcome.FAILED: "FAIL", Outcome.UNVERIFIED: "????"}[
            self.outcome
        ]


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.started_at = datetime.now(UTC)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        line = f"  [{check.symbol}] {check.service:<14} {check.name:<28} {check.detail}"
        print(line, flush=True)
        return check

    def counts(self) -> dict[str, int]:
        out = {o.value: 0 for o in Outcome}
        for check in self.checks:
            out[check.outcome.value] += 1
        return out

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.outcome is Outcome.FAILED]

    def unverified(self) -> list[Check]:
        return [c for c in self.checks if c.outcome is Outcome.UNVERIFIED]

    def to_json(self) -> str:
        payload = {
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": self.counts(),
            "checks": [asdict(c) | {"outcome": c.outcome.value} for c in self.checks],
        }
        return json.dumps(payload, indent=2, default=str)
