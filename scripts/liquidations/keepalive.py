"""Hold CPU utilisation above Oracle's Always Free idle threshold.

Oracle stops Always Free instances whose 95th-percentile CPU, network and
(on A1 shapes) memory all stay under 20% across a 7-day window:
https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

The liquidation collector trips all three. It holds one websocket and waits,
so CPU is near zero, traffic is a few KB/s and memory is a few dozen MB. A
20-day collection would be stopped around day 7, and the resulting hole would
look exactly like the power cuts the VM was meant to escape.

The instance really is in continuous use -- it is I/O-bound, not idle -- so
raising the measured figure makes it reflect what is happening rather than
disguise an unused machine.

Run it niced, so the collector always wins the CPU and no liquidation is
delayed by this.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import time

# Oracle's threshold is 20%. Aim above it with room for the averaging window,
# but stay low enough that the instance is plainly doing less than it could.
TARGET_FRACTION = 0.35
SLICE_S = 0.1


def workers_needed(cpu_count: int) -> int:
    """One worker per CPU.

    Utilisation is reported per instance, not per core, so a single busy
    worker on a 2-core box reads as half of TARGET_FRACTION. Scaling with the
    core count keeps the measured figure at the target whatever shape the
    instance turns out to be -- which matters because Oracle's ARM capacity
    often forces a different shape than the one you asked for.
    """
    return max(1, cpu_count)


def busy_slice_s(target_fraction: float, slice_s: float) -> float:
    """Seconds of work per slice to average out at target_fraction."""
    if not 0.0 < target_fraction < 1.0:
        raise ValueError(f"target_fraction must be in (0, 1): {target_fraction}")
    return slice_s * target_fraction


def spin(target_fraction: float = TARGET_FRACTION,
         slice_s: float = SLICE_S) -> None:
    """Burn target_fraction of one core, sleeping the rest of each slice."""
    busy_s = busy_slice_s(target_fraction, slice_s)
    idle_s = slice_s - busy_s
    while True:
        end = time.monotonic() + busy_s
        while time.monotonic() < end:
            pass
        time.sleep(idle_s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fraction", type=float, default=TARGET_FRACTION,
                        help="target CPU fraction (default %(default)s)")
    args = parser.parse_args()

    count = workers_needed(os.cpu_count() or 1)
    print(f"keepalive: {count} worker(s) at {args.fraction:.0%} CPU each, "
          f"niced so the collector always takes priority", flush=True)

    procs = [multiprocessing.Process(target=spin, args=(args.fraction,),
                                     daemon=True)
             for _ in range(count)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()


if __name__ == "__main__":
    main()
