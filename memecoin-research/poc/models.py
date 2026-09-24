"""The proof of concept uses the collector's schema.

Deliberately a re-export, not a copy. Two schemas for one database is how the
pipeline that was verified and the collector that runs for weeks quietly drift
apart -- and the drift only shows up as data that will not load.
"""

from __future__ import annotations

from collector.models import (  # noqa: F401
    Base,
    CollectionGap,
    CollectorRun,
    Creator,
    Event,
    HolderSnapshot,
    LiquidityEvent,
    Observation,
    PaperPosition,
    PendingDetection,
    Pool,
    RawPayload,
    SimulatedExit,
    Token,
    TokenStatus,
    WorkItem,
)

__all__ = [
    "Base", "Token", "Pool", "Observation", "SimulatedExit", "Event",
    "RawPayload", "CollectionGap", "Creator", "TokenStatus", "WorkItem",
    "HolderSnapshot", "LiquidityEvent", "PaperPosition", "PendingDetection",
    "CollectorRun",
]
