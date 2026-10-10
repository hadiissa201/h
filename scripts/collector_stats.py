"""Wilson interval, standalone so scripts/ does not import the memecoin package."""

from __future__ import annotations


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI for a proportion, correct at small n where the normal one is not."""
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    d = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / d
    margin = (z / d) * ((p * (1 - p) / trials
                         + z * z / (4 * trials * trials)) ** 0.5)
    return (max(0.0, centre - margin), min(1.0, centre + margin))
