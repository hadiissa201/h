"""Deciding which tokens to track, reproducibly.

Sampling is forced by arithmetic, not preference: ~217 DexScreener calls per
token over 7 days against a measured ~300/min ceiling caps us near 1,500
tokens/day, while Solana launches tens of thousands.

The distinction that matters: a DELIBERATE random sample is unbiased and can be
weighted back up. Keeping "whatever we could keep up with" is not a sample at
all -- it silently over-represents quiet periods, because that is exactly when
the collector has spare capacity. One is statistics; the other is a broken
instrument that looks like statistics.

So the decision is a pure function of the mint address: reproducible, uniform,
independent of load, clock, or how busy we happened to be.
"""

from __future__ import annotations

import hashlib

_SPACE = 1 << 32


def sample_score(mint: str, salt: str = "") -> float:
    """A stable, uniform value in [0, 1) for this mint.

    Uses a hash of the address rather than random(): the same token must get the
    same verdict on a restart, on a replayed message, or on a second machine.
    """
    digest = hashlib.sha256(f"{salt}{mint}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / _SPACE


def should_track(mint: str, sample_rate: float, salt: str = "") -> bool:
    """True if this token is in the sample. Deterministic, load-independent."""
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    return sample_score(mint, salt) < sample_rate
