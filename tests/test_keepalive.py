"""The keepalive exists to stop Oracle reclaiming the collector's VM.

Oracle stops Always Free instances idle on all three of CPU, network and
memory over a 7-day window. The collector trips all three, so a 20-day run
would be cut short around day 7 and the hole would look exactly like the
power cuts the VM was meant to escape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "liquidations"))

from keepalive import TARGET_FRACTION, busy_slice_s, workers_needed


def test_target_clears_oracle_threshold():
    """Under 20% on all three metrics is what gets an instance stopped."""
    assert TARGET_FRACTION > 0.20


def test_busy_slice_matches_the_target_fraction():
    assert busy_slice_s(0.35, 0.1) == pytest.approx(0.035)
    assert busy_slice_s(0.5, 1.0) == pytest.approx(0.5)


def test_busy_slice_rejects_a_fraction_that_would_pin_the_cpu():
    """1.0 would leave no sleep, starving the collector despite the nice."""
    for bad in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(ValueError):
            busy_slice_s(bad, 0.1)


def test_one_worker_per_cpu():
    """Utilisation is per instance, so a single worker halves on 2 cores."""
    assert workers_needed(1) == 1
    assert workers_needed(4) == 4


def test_worker_count_never_zero():
    """os.cpu_count() can return None; 0 workers would silently do nothing."""
    assert workers_needed(0) == 1
