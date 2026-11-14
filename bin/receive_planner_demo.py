#!/usr/bin/env python
"""End-to-end demonstration of the shared-head receive planner.

Builds a twenty-station network where some stations share a collection head,
creates one upload reservation per station plus a flexible high-priority file,
then injects a narrow-window urgent file and prints exactly which existing
reservations have to move.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tftpy.receive_planner import (  # noqa: E402
    FileJob,
    ReceivePlanner,
    ReplanResult,
    devices_from_groups,
    format_report,
)


def build_scenario(seed):
    # Stations 0..19; paired groups share one collection head, the rest own one.
    groups = [
        [0, 1], [2, 3], [4, 5], [6, 7], [8, 9],
        [10], [11], [12], [13], [14],
        [15, 16], [17], [18], [19],
    ]
    planner = ReceivePlanner(devices_from_groups(groups))
    rng = random.Random(seed)
    jobs = []
    for station in range(20):
        release = rng.randint(0, 30)
        duration = rng.randint(2, 6)
        jobs.append(
            FileJob(
                fid="s%02d" % station,
                stations=(station,),
                release=release,
                deadline=release + rng.randint(duration + 2, 24),
                duration=duration,
                weight=rng.randint(1, 10),
                priority=rng.randint(0, 3),
            )
        )
    # High-priority file readable at either of two stations on different heads;
    # the planner reserves exactly one of them.
    jobs.append(
        FileJob("flex-hi", (3, 12), release=0, deadline=40, duration=5,
                weight=10, priority=9)
    )
    return planner, jobs


def main():
    planner, jobs = build_scenario(seed=7)
    base_plan = planner.plan(jobs)
    planner.validate(base_plan, jobs)
    print("== Initial receive plan ==")
    print(format_report(ReplanResult(plan=base_plan)))

    # A narrow-window mandatory file aimed at the start of an existing
    # reservation whose own window has enough slack to be pushed later, so the
    # planner must explicitly move that reservation rather than drop it.
    job_by_id = {job.fid: job for job in jobs}
    target = None
    for entry in base_plan.entries:
        source_job = job_by_id[entry.fid]
        slack = source_job.deadline - entry.end
        if slack >= 3:
            target = entry
            break
    if target is None:  # pragma: no cover - the random scenario always has one
        raise SystemExit("no reschedulable reservation found; change the seed")
    narrow = FileJob(
        "urgent", (target.station,),
        release=target.start, deadline=target.start + 3, duration=3,
        weight=12, priority=10, mandatory=True,
    )
    result = planner.replan(jobs + [narrow], base_plan.entries)
    planner.validate(result.plan, jobs + [narrow])
    print()
    print("== After the narrow-window arrival ==")
    print(format_report(result))


if __name__ == "__main__":
    main()
