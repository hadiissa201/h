"""Self-tuning delay between requests to a rate-limited service.

Extracted because this is the third place that needs it. Both Jupiter and
Helius refuse at rates that are not documented and change, so the only reliable
approach is to slow down when refused and speed up when not, rather than
guessing a constant.

Two details are load-bearing, both learned by getting them wrong in production:

RECOVERY MUST OUTPACE BACKOFF. The first version grew the delay 1.6x per refusal
and shrank it 3% per success, which needed about seventy-six clean replies to
return from the ceiling. A throttled run never gets seventy-six, so it was a
one-way ratchet dressed as adaptive pacing, and an hour collected fewer samples
than the preceding fifteen minutes.

A REFUSAL PAYS ITS PENALTY ONCE. Sleeping inside the handler and again in the
caller's loop charged every rate limit twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Pacer:
    delay: float = 1.5
    floor: float = 1.0
    ceiling: float = 4.0
    throttles: int = 0
    _slept: bool = False

    def wait(self) -> None:
        if self._slept:
            self._slept = False
            return
        time.sleep(self.delay)

    def saw_429(self, retry_after: str | None = None) -> None:
        self.throttles += 1
        self.delay = min(self.ceiling, self.delay * 1.4)
        pause = self.delay
        if retry_after:
            try:
                pause = max(pause, min(float(retry_after), 30.0))
            except ValueError:
                pass
        time.sleep(pause)
        self._slept = True

    def saw_success(self) -> None:
        self.delay = max(self.floor, self.delay * 0.85)
