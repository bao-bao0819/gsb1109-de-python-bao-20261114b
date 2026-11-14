# -*- coding: utf-8 -*-
from itertools import product

import pytest

from canmatrix.sampling import (
    PRESSURE,
    SIGNALS,
    SPEED,
    VALVE,
    Reading,
    RoundWindow,
    reconstruct_rounds,
)


def make_reading(reading_id, signal, seq, window_start, window_end=None,
                 group="G1", value=None):
    return Reading(
        reading_id=reading_id,
        group=group,
        signal=signal,
        seq=seq,
        window_start=window_start,
        window_end=window_end if window_end is not None else window_start,
        value=value,
    )


def test_clean_two_rounds_reconstruct_both_triplets():
    rounds = [RoundWindow("R1", 0.0, 10.0), RoundWindow("R2", 10.0, 20.0)]
    readings = [
        make_reading("p1", PRESSURE, 0, 2.0),
        make_reading("p2", PRESSURE, 1, 12.0),
        make_reading("s1", SPEED, 0, 3.0),
        make_reading("s2", SPEED, 1, 13.0),
        make_reading("v1", VALVE, 0, 4.0),
        make_reading("v2", VALVE, 1, 14.0),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1"])

    assert report.complete_round_count == 2
    assert report.triplet_count == 2
    assert len(report.optimal_pairings) == 1
    triplets = report.canonical_triplets
    assert [triplet.round_id for triplet in triplets] == ["R1", "R2"]
    assert triplets[0].reading_ids() == ("p1", "s1", "v1")
    assert triplets[1].reading_ids() == ("p2", "s2", "v2")
    assert report.consensus_triplets == triplets
    assert report.retained_candidates == {}
    assert report.contentions == []


def test_shared_pressure_record_yields_all_optimal_pairings():
    # p_mid overlaps both rounds; p_early only fits R1, p_late only fits R2.
    rounds = [RoundWindow("R1", 0.0, 10.0), RoundWindow("R2", 10.0, 20.0)]
    readings = [
        make_reading("p_early", PRESSURE, 0, 5.0, 6.0),
        make_reading("p_mid", PRESSURE, 1, 9.0, 11.0),
        make_reading("p_late", PRESSURE, 2, 15.0, 16.0),
        make_reading("s1", SPEED, 0, 3.0),
        make_reading("s2", SPEED, 1, 13.0),
        make_reading("v1", VALVE, 0, 4.0),
        make_reading("v2", VALVE, 1, 14.0),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1"])

    # All pairings complete both rounds; the shared record must not be
    # consumed by both rounds inside a single pairing.  Leaving the boundary
    # record unused is equally optimal ("at most once" allows idle readings).
    assert report.complete_round_count == 2
    assert len(report.optimal_pairings) == 3
    pressure_placements = set()
    for pairing in report.optimal_pairings:
        used = [reading_id for triplet in pairing
                for reading_id in triplet.reading_ids()]
        assert len(used) == len(set(used))
        by_round = {triplet.round_id: triplet.pressure.reading_id
                    for triplet in pairing}
        pressure_placements.add((by_round["R1"], by_round["R2"]))
    assert pressure_placements == {
        ("p_early", "p_mid"),
        ("p_mid", "p_late"),
        ("p_early", "p_late"),
    }

    # No full triplet is agreed by every optimal pairing.
    assert report.consensus_triplets == []

    # The pressure slot of each round keeps every candidate that appears in
    # any optimal pairing, including the shared record on both sides.
    r1_bucket = report.retained_candidates[("R1", "G1", PRESSURE)]
    r2_bucket = report.retained_candidates[("R2", "G1", PRESSURE)]
    assert [reading.reading_id for reading in r1_bucket] == ["p_early", "p_mid"]
    assert [reading.reading_id for reading in r2_bucket] == ["p_mid", "p_late"]

    # Speed and valve are unambiguous, so no retained buckets for them.
    assert all(key[2] == PRESSURE for key in report.retained_candidates)

    contention = report.contentions
    assert len(contention) == 1
    assert contention[0].reading.reading_id == "p_mid"
    assert contention[0].round_ids == ("R1", "R2")


def test_single_pressure_for_two_rounds_completes_only_one_round():
    rounds = [RoundWindow("R1", 0.0, 10.0), RoundWindow("R2", 10.0, 20.0)]
    readings = [
        make_reading("p_shared", PRESSURE, 0, 9.0, 11.0),
        make_reading("s1", SPEED, 0, 3.0),
        make_reading("s2", SPEED, 1, 13.0),
        make_reading("v1", VALVE, 0, 4.0),
        make_reading("v2", VALVE, 1, 14.0),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1"])

    assert report.complete_round_count == 1
    placements = {
        tuple(sorted(triplet.round_id for triplet in pairing))
        for pairing in report.optimal_pairings
    }
    assert placements == {("R1",), ("R2",)}
    for pairing in report.optimal_pairings:
        pressure_ids = [triplet.pressure.reading_id for triplet in pairing]
        assert pressure_ids.count("p_shared") == 1


def test_sequence_numbers_must_not_go_backwards():
    # Two pressure readings fit both rounds, but seq 0 must precede seq 1.
    rounds = [RoundWindow("R1", 0.0, 10.0), RoundWindow("R2", 10.0, 20.0)]
    readings = [
        make_reading("p_a", PRESSURE, 0, 9.0, 11.0),
        make_reading("p_b", PRESSURE, 1, 9.5, 10.5),
        make_reading("s1", SPEED, 0, 3.0),
        make_reading("s2", SPEED, 1, 13.0),
        make_reading("v1", VALVE, 0, 4.0),
        make_reading("v2", VALVE, 1, 14.0),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1"])
    assert report.complete_round_count == 2
    for pairing in report.optimal_pairings:
        ordered = sorted(pairing, key=lambda triplet: triplet.round_id)
        assert ordered[0].pressure.seq < ordered[1].pressure.seq
    pressure_pairs = {
        tuple(triplet.pressure.reading_id for triplet in
              sorted(pairing, key=lambda triplet: triplet.round_id))
        for pairing in report.optimal_pairings
    }
    assert pressure_pairs == {("p_a", "p_b")}
    assert len(report.optimal_pairings) == 1


def test_contained_policy_excludes_reading_crossing_round_boundary():
    rounds = [RoundWindow("R1", 0.0, 10.0)]
    readings = [
        make_reading("p_cross", PRESSURE, 0, 9.0, 11.0),
        make_reading("s1", SPEED, 0, 3.0),
        make_reading("v1", VALVE, 0, 4.0),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1"], match_policy="contained")
    assert report.canonical_triplets == []
    assert report.complete_round_count == 0

    overlap_report = reconstruct_rounds(readings, rounds, ["G1"])
    assert overlap_report.complete_round_count == 1


def test_multiple_groups_are_independent_and_consensus_keeps_fixed_group():
    rounds = [RoundWindow("R1", 0.0, 10.0), RoundWindow("R2", 10.0, 20.0)]
    readings = []
    # G1: ambiguous pressure as in the headline scenario.
    readings += [
        make_reading("g1_p_early", PRESSURE, 0, 5.0, 6.0, group="G1"),
        make_reading("g1_p_mid", PRESSURE, 1, 9.0, 11.0, group="G1"),
        make_reading("g1_p_late", PRESSURE, 2, 15.0, 16.0, group="G1"),
        make_reading("g1_s1", SPEED, 0, 3.0, group="G1"),
        make_reading("g1_s2", SPEED, 1, 13.0, group="G1"),
        make_reading("g1_v1", VALVE, 0, 4.0, group="G1"),
        make_reading("g1_v2", VALVE, 1, 14.0, group="G1"),
    ]
    # G2: fully determined.
    readings += [
        make_reading("g2_p1", PRESSURE, 0, 2.0, group="G2"),
        make_reading("g2_p2", PRESSURE, 1, 12.0, group="G2"),
        make_reading("g2_s1", SPEED, 0, 3.0, group="G2"),
        make_reading("g2_s2", SPEED, 1, 13.0, group="G2"),
        make_reading("g2_v1", VALVE, 0, 4.0, group="G2"),
        make_reading("g2_v2", VALVE, 1, 14.0, group="G2"),
    ]

    report = reconstruct_rounds(readings, rounds, ["G1", "G2"])

    # Both rounds are complete (each in both groups); only G1 is ambiguous.
    assert report.complete_round_count == 2
    assert report.triplet_count == 4
    assert len(report.optimal_pairings) == 3
    consensus_slots = {
        (triplet.round_id, triplet.group)
        for triplet in report.consensus_triplets
    }
    assert consensus_slots == {("R1", "G2"), ("R2", "G2")}
    for pairing in report.optimal_pairings:
        g2 = sorted(
            (triplet for triplet in pairing if triplet.group == "G2"),
            key=lambda triplet: triplet.round_id,
        )
        assert g2[0].reading_ids() == ("g2_p1", "g2_s1", "g2_v1")
        assert g2[1].reading_ids() == ("g2_p2", "g2_s2", "g2_v2")


def test_validation_errors():
    with pytest.raises(ValueError):
        Reading("x", "G1", "torque", 0, 1.0, 1.0)
    with pytest.raises(ValueError):
        Reading("x", "G1", PRESSURE, 0, 2.0, 1.0)
    with pytest.raises(ValueError):
        RoundWindow("R1", 2.0, 1.0)

    rounds = [RoundWindow("R1", 0.0, 10.0)]
    readings = [
        make_reading("dup", PRESSURE, 0, 1.0),
        make_reading("dup", SPEED, 0, 1.0),
        make_reading("v1", VALVE, 0, 1.0),
    ]
    with pytest.raises(ValueError):
        reconstruct_rounds(readings, rounds, ["G1"])
    with pytest.raises(ValueError):
        reconstruct_rounds([], rounds, ["G1"], match_policy="strict")

    with pytest.raises(ValueError):
        reconstruct_rounds([], rounds, ["G1", "G1"])
    with pytest.raises(ValueError):
        reconstruct_rounds(
            [], [RoundWindow("R1", 0.0, 10.0), RoundWindow("R1", 10.0, 20.0)], ["G1"]
        )


def _brute_force_optima(readings, round_windows, groups, match_policy="overlap"):
    """Independent exhaustive reference: every optimal assignment."""
    ordered_rounds = sorted(round_windows, key=lambda item: (item.start, item.end))
    best_objective = None
    best_solutions = set()

    def fits(reading, round_window):
        if match_policy == "contained":
            return (
                round_window.start <= reading.window_start
                and reading.window_end <= round_window.end
            )
        return (
            reading.window_end >= round_window.start
            and reading.window_start <= round_window.end
        )

    def recurse(index, used, last_seq, assignment):
        nonlocal best_objective, best_solutions
        if index == len(ordered_rounds):
            complete = sum(
                all(
                    (round_window.round_id, group) in assignment
                    for group in groups
                )
                for round_window in ordered_rounds
            )
            objective = (complete, len(assignment))
            if best_objective is None or objective > best_objective:
                best_objective = objective
                best_solutions = {frozenset(assignment.items())}
            elif objective == best_objective:
                best_solutions.add(frozenset(assignment.items()))
            return

        round_window = ordered_rounds[index]
        options_per_group = []
        for group in groups:
            options = [None]
            pools = {}
            for signal in SIGNALS:
                previous = last_seq.get((group, signal))
                pools[signal] = [
                    reading
                    for reading in readings
                    if reading.group == group
                    and reading.signal == signal
                    and fits(reading, round_window)
                    and reading.reading_id not in used
                    and (previous is None or reading.seq > previous)
                ]
            for pressure, speed, valve in product(
                pools[PRESSURE], pools[SPEED], pools[VALVE]
            ):
                ids = (pressure.reading_id, speed.reading_id, valve.reading_id)
                if len(set(ids)) == 3 and not set(ids) & used:
                    options.append((pressure, speed, valve))
            options_per_group.append(options)

        for combo in product(*options_per_group):
            next_used = set(used)
            next_last = dict(last_seq)
            next_assignment = dict(assignment)
            complete = True
            for group, choice in zip(groups, combo):
                if choice is None:
                    complete = False
                    continue
                for reading in choice:
                    next_used.add(reading.reading_id)
                    next_last[(group, reading.signal)] = reading.seq
                next_assignment[(round_window.round_id, group)] = tuple(
                    reading.reading_id for reading in choice
                )
            if not any(choice is not None for choice in combo):
                complete = False
            recurse(index + 1, frozenset(next_used), next_last, next_assignment)

    recurse(0, frozenset(), {}, {})
    return best_objective, best_solutions


def test_randomized_matches_brute_force():
    import random

    rng = random.Random(20261114)
    for seed in range(40):
        rng.seed(1000 + seed)
        match_policy = rng.choice(["overlap", "contained"])
        group_count = rng.randint(1, 2)
        groups = ["G%d" % index for index in range(group_count)]
        round_windows = [
            RoundWindow("R%d" % index, float(index * 10), float(index * 10 + 10))
            for index in range(rng.randint(1, 3))
        ]
        readings = []
        counter = 0
        for group in groups:
            for signal in SIGNALS:
                count = rng.randint(1, 3)
                base = rng.randint(0, 2)
                for offset in range(count):
                    start = rng.uniform(-2.0, 26.0)
                    width = rng.uniform(0.0, 4.0)
                    readings.append(
                        Reading(
                            reading_id="r%d" % counter,
                            group=group,
                            signal=signal,
                            seq=base + offset,
                            window_start=start,
                            window_end=start + width,
                        )
                    )
                    counter += 1

        report = reconstruct_rounds(
            readings, round_windows, groups, match_policy=match_policy
        )
        expected_objective, expected_solutions = _brute_force_optima(
            readings, round_windows, groups, match_policy=match_policy
        )

        assert (report.complete_round_count, report.triplet_count) == expected_objective

        actual_solutions = set()
        by_id = {reading.reading_id: reading for reading in readings}
        for pairing in report.optimal_pairings:
            seen_ids = []
            last_seen = {}
            normalized = {}
            for triplet in pairing:
                normalized[(triplet.round_id, triplet.group)] = triplet.reading_ids()
                for reading in triplet.as_tuple():
                    seen_ids.append(reading.reading_id)
                    key = (triplet.group, reading.signal)
                    previous = last_seen.get(key)
                    assert previous is None or reading.seq > previous
                    last_seen[key] = reading.seq
            # every reading is claimed at most once
            assert len(seen_ids) == len(set(seen_ids))
            actual_solutions.add(frozenset(normalized.items()))
        assert len(actual_solutions) == len(report.optimal_pairings)
        assert actual_solutions == expected_solutions

        # every retained candidate is actually used in some optimal pairing
        used_ids = {
            reading.reading_id
            for pairing in report.optimal_pairings
            for triplet in pairing
            for reading in triplet.as_tuple()
        }
        for bucket in report.retained_candidates.values():
            for reading in bucket:
                assert reading.reading_id in used_ids

        # contentions are exactly the readings landing on multiple rounds
        usage = {}
        for pairing in report.optimal_pairings:
            for triplet in pairing:
                for reading in triplet.as_tuple():
                    usage.setdefault(reading.reading_id, set()).add(triplet.round_id)
        expected_contended = {
            reading_id for reading_id, round_ids in usage.items()
            if len(round_ids) > 1
        }
        assert {
            contention.reading.reading_id for contention in report.contentions
        } == expected_contended
        for contention in report.contentions:
            assert contention.reading.reading_id in by_id

