#!/usr/bin/env python3
"""Demo: rebuild per-round (pressure, speed, valve) triplets.

The readings arrive on disk in per-signal columns; this script reconstructs
which pressure/speed/valve readings belong to the same bench round and shows
how a pressure record overlapping two rounds keeps both optimal pairings.
"""

from canmatrix.sampling import (
    PRESSURE,
    SPEED,
    VALVE,
    Reading,
    RoundWindow,
    reconstruct_rounds,
)


def reading(reading_id, signal, seq, start, end=None):
    return Reading(
        reading_id=reading_id,
        group="G1",
        signal=signal,
        seq=seq,
        window_start=start,
        window_end=start if end is None else end,
    )


rounds = [
    RoundWindow("R1", 0.0, 10.0),
    RoundWindow("R2", 10.0, 20.0),
]

readings = [
    # p_mid's sampling window [9, 11] overlaps both rounds.
    reading("p_early", PRESSURE, 0, 5.0, 6.0),
    reading("p_mid", PRESSURE, 1, 9.0, 11.0),
    reading("p_late", PRESSURE, 2, 15.0, 16.0),
    reading("s1", SPEED, 0, 3.0),
    reading("s2", SPEED, 1, 13.0),
    reading("v1", VALVE, 0, 4.0),
    reading("v2", VALVE, 1, 14.0),
]

report = reconstruct_rounds(readings, rounds, ["G1"])

print("complete rounds: %d, triplets: %d" % (
    report.complete_round_count, report.triplet_count))
for index, pairing in enumerate(report.optimal_pairings, start=1):
    print("optimal pairing %d:" % index)
    for triplet in pairing:
        print("  %s/%s -> pressure=%s speed=%s valve=%s" % (
            triplet.round_id,
            triplet.group,
            triplet.pressure.reading_id,
            triplet.speed.reading_id,
            triplet.valve.reading_id,
        ))

print("consensus triplets: %d" % len(report.consensus_triplets))
for key, bucket in report.retained_candidates.items():
    round_id, group, signal = key
    print("retain for %s/%s %s: %s" % (
        round_id, group, signal, [item.reading_id for item in bucket]))
for contention in report.contentions:
    print("contended reading %s may belong to rounds %s" % (
        contention.reading.reading_id, contention.round_ids))
