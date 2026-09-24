"""Rate limiting against measured ceilings, and honest accounting when we fail.

Jupiter returned 429 after 121 requests in 24.9 seconds. That is a ~120/minute
budget, and a collector that ignores it does not merely get throttled -- it goes
blind during exactly the windows it was throttled in, and those launches are
gone for good. There is no backfill for "what was this token doing at 14:32".

So two rules hold everywhere below:

  1. Refusals are COUNTED, never silent. A dropped observation that nobody
     recorded is indistinguishable from a token that was quiet.
  2. A 429 makes the limiter slower for a while. Hammering a limit that just
     rejected you spends the budget on rejections.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class BucketStats:
    """What actually happened, so the dataset's gaps are explainable."""
    acquired: int = 0
    waited_s: float = 0.0
    refused: int = 0          # caller would not wait; the work was requeued
    throttle_events: int = 0  # a 429 we were told about
    last_throttle_ts: float | None = None


class TokenBucket:
    """A steady-rate limiter with a penalty box.

    `acquire` blocks until a slot is free, or returns False immediately if that
    would take longer than `max_wait`. The caller then requeues rather than
    dropping -- the difference between a delayed observation and a missing one.
    """

    def __init__(self, rate_per_s: float, name: str, burst: float | None = None) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self.name = name
        self._rate = rate_per_s
        self._capacity = burst if burst is not None else max(1.0, rate_per_s)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()
        self._penalty_until = 0.0
        self.stats = BucketStats()

    @property
    def rate(self) -> float:
        return self._rate

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        # No refill while serving a penalty: a 429 means the server's window is
        # already spent, so spending ours on rejections helps nobody.
        if now < self._penalty_until:
            self._updated = now
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    def acquire(self, max_wait: float = 30.0) -> bool:
        deadline = time.monotonic() + max_wait
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if now >= self._penalty_until and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.stats.acquired += 1
                    self.stats.waited_s += slept
                    return True
                if now >= self._penalty_until:
                    wait = (1.0 - self._tokens) / self._rate
                else:
                    wait = self._penalty_until - now
            if time.monotonic() + wait > deadline:
                with self._lock:
                    self.stats.refused += 1
                return False
            nap = min(wait, 0.25)
            time.sleep(nap)
            slept += nap

    def penalise(self, seconds: float = 60.0) -> None:
        """Called on a 429. Stops traffic until the server's window resets."""
        with self._lock:
            now = time.monotonic()
            self._penalty_until = max(self._penalty_until, now + seconds)
            self._tokens = 0.0
            self.stats.throttle_events += 1
            self.stats.last_throttle_ts = time.time()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.monotonic()
            return {
                "name": self.name,
                "rate_per_s": self._rate,
                "tokens_available": round(self._tokens, 2),
                "penalised_for_s": round(max(0.0, self._penalty_until - now), 1),
                "acquired": self.stats.acquired,
                "refused": self.stats.refused,
                "throttle_events": self.stats.throttle_events,
                "total_wait_s": round(self.stats.waited_s, 1),
            }


@dataclass
class Limiters:
    """One bucket per service, because their ceilings differ by 3x."""
    dexscreener: TokenBucket
    jupiter: TokenBucket
    helius: TokenBucket
    _by_name: dict[str, TokenBucket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_name = {b.name: b for b in
                         (self.dexscreener, self.jupiter, self.helius)}

    def get(self, name: str) -> TokenBucket | None:
        return self._by_name.get(name)

    def snapshot(self) -> list[dict[str, object]]:
        return [b.snapshot() for b in self._by_name.values()]


def build_limiters(settings) -> Limiters:  # noqa: ANN001 -- CollectorSettings
    return Limiters(
        dexscreener=TokenBucket(settings.dexscreener_rps, "dexscreener"),
        jupiter=TokenBucket(settings.jupiter_rps, "jupiter"),
        helius=TokenBucket(settings.helius_rps, "helius"),
    )
