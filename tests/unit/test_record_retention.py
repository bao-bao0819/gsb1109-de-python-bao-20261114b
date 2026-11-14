# -*- coding: utf-8 -*-

import itertools
import math

import pytest

from canmatrix.record_retention import (
    CONTINUOUS,
    ENUMERATED,
    SignalSpec,
    compress_records,
    record_numbers,
    signal_minimum_sets,
    verify_selection,
)


def all_feasible_sizes(times, columns, specs, branches=None, missing=None):
    """Brute-force minimum retained-row count over every row subset."""
    n = len(times)
    interiors = list(range(1, n - 1))
    best = None
    for keep_count in range(2, n + 1):
        for combo in itertools.combinations(interiors, keep_count - 2):
            selection = [0] + list(combo) + [n - 1]
            ok, _ = verify_selection(
                times, columns, specs, selection, branches, missing
            )
            if ok:
                return keep_count, selection
    return best, None


def naive_edge_ok(times, values, spec, segment_of, earlier, later):
    """Independent, straightforward per-edge feasibility oracle."""
    if segment_of[earlier] < 0 or segment_of[earlier] != segment_of[later]:
        return True
    if spec.kind == ENUMERATED:
        carried = values[earlier]
        return all(values[i] == carried for i in range(earlier + 1, later))
    t0, t1 = times[earlier], times[later]
    y0, y1 = float(values[earlier]), float(values[later])
    dt = t1 - t0
    for i in range(earlier + 1, later):
        if dt == 0:
            reconstructed = y0
        else:
            reconstructed = y0 + (y1 - y0) * (times[i] - t0) / dt
        if abs(float(values[i]) - reconstructed) > spec.tolerance + 1e-9:
            return False
    return True


def bfs_minimum(times, columns, specs, branches=None, missing=None):
    """Shortest path over the naive edge oracle, independent from the DP."""
    from canmatrix.record_retention import _prepare

    signals, mandatory = _prepare(times, columns, specs, branches, missing)
    n = len(times)
    next_mandatory = [n - 1] * n
    nearest = n - 1
    for i in range(n - 1, -1, -1):
        next_mandatory[i] = nearest
        if mandatory[i]:
            nearest = i
    distance = [-1] * n
    distance[0] = 1
    queue = [0]
    head = 0
    while head < len(queue):
        earlier = queue[head]
        head += 1
        for later in range(earlier + 1, n):
            if next_mandatory[earlier] < later:
                break
            if distance[later] >= 0:
                continue
            if all(
                naive_edge_ok(
                    times,
                    sig.values,
                    sig.spec,
                    sig.segment_of,
                    earlier,
                    later,
                )
                for sig in signals
            ):
                distance[later] = distance[earlier] + 1
                queue.append(later)
    return distance[-1]


def test_single_record_is_kept():
    times = [1.0]
    columns = {"p": [5.0], "v": ["closed"]}
    specs = [
        SignalSpec("p", CONTINUOUS, 0.1),
        SignalSpec("v", ENUMERATED),
    ]
    assert compress_records(times, columns, specs) == [0]


def test_continuous_tolerance_drives_selection():
    # Perfectly linear ramp: endpoints alone reconstruct every row exactly.
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    pressure = [0.0, 2.0, 4.0, 6.0, 8.0]
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    assert compress_records(times, {"p": pressure}, specs) == [0, 4]

    # A bump that no endpoint chord can represent forces an interior anchor.
    pressure = [0.0, 0.0, 9.0, 0.0, 0.0]
    selection = compress_records(times, {"p": pressure}, specs)
    assert 2 in selection
    ok, problems = verify_selection(
        times, {"p": pressure}, specs, selection
    )
    assert ok, problems


def test_enumerated_keeps_every_change_point():
    times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    valve = ["open", "open", "closed", "closed", "open", "open"]
    specs = [SignalSpec("v", ENUMERATED)]
    selection = compress_records(times, {"v": valve}, specs)
    # Rows 2 and 4 are change points and must be retained.
    assert selection == [0, 2, 4, 5]
    ok, problems = verify_selection(times, {"v": valve}, specs, selection)
    assert ok, problems


def test_missing_samples_break_segments():
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    pressure = [0.0, 1.0, None, 7.0, 8.0]
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    selection = compress_records(times, {"p": pressure}, specs)
    # Boundary rows of both present segments are mandatory.
    assert set(selection) >= {0, 1, 3, 4}
    ok, problems = verify_selection(times, {"p": pressure}, specs, selection)
    assert ok, problems

    # Interpolating across the gap must be rejected.
    bad = [0, 4]
    ok, problems = verify_selection(times, {"p": pressure}, specs, bad)
    assert not ok
    assert problems


def test_nan_missing_marker():
    times = [0.0, 1.0, 2.0]
    pressure = [1.0, float("nan"), 3.0]
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    selection = compress_records(
        times, {"p": pressure}, specs, missing=float("nan")
    )
    # The NaN row carries no value; both present singleton rows are mandatory
    # and nothing may interpolate across the gap.
    assert selection == [0, 2]


def test_branch_switch_breaks_segment():
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    # Same raw value, but rows 0-1 and 2-4 come from different mux branches.
    values = [10.0, 10.0, 10.0, 10.0, 10.0]
    branches = [1, 1, 2, 2, 2]
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    selection = compress_records(
        times,
        {"p": values},
        specs,
        branches={"p": branches},
    )
    assert set(selection) >= {0, 1, 2, 4}
    ok, problems = verify_selection(
        times, {"p": values}, specs, selection, branches={"p": branches}
    )
    assert ok, problems


def test_duplicate_timestamps_do_not_crash():
    times = [0.0, 0.0, 1.0]
    columns = {"p": [1.0, 1.0, 2.0]}
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    selection = compress_records(times, columns, specs)
    ok, problems = verify_selection(times, columns, specs, selection)
    assert ok, problems


def test_naive_union_of_independent_shortest_lists_is_not_optimal():
    # Step near the end: two equally short size-3 lists exist per signal,
    # but a size-2 list does not.
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    pressure = [0.0, 0.0, 0.0, 0.0, 10.0]
    valve_pos = [0.0, 0.0, 0.0, 0.0, 1000.0]
    specs = [
        SignalSpec("p", CONTINUOUS, 5.0),
        SignalSpec("v", CONTINUOUS, 500.0),
    ]

    p_early, p_late = signal_minimum_sets(
        times, pressure, specs[0]
    )
    v_early, v_late = signal_minimum_sets(
        times, valve_pos, specs[1]
    )
    assert len(p_early) == len(p_late) == 3
    assert p_early != p_late
    assert len(v_early) == len(v_late) == 3

    # The naive move: earliest list for one signal, latest for the other.
    naive_union = sorted(set(p_early) | set(v_late))
    assert len(naive_union) == 4

    # The joint DP keeps the shared size-3 list instead.
    joint = compress_records(
        times, {"p": pressure, "v": valve_pos}, specs
    )
    assert len(joint) < len(naive_union)
    ok, problems = verify_selection(
        times, {"p": pressure, "v": valve_pos}, specs, joint
    )
    assert ok, problems


def test_tie_break_earliest_vs_latest():
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    values = [0.0, 0.0, 0.0, 0.0, 10.0]
    spec = SignalSpec("p", CONTINUOUS, 5.0)
    earliest = compress_records(times, {"p": values}, [spec])
    latest = compress_records(
        times, {"p": values}, [spec], prefer_latest=True
    )
    assert earliest == [0, 2, 4]
    assert latest == [0, 3, 4]
    assert len(earliest) == len(latest)


def test_record_numbers_are_one_based():
    assert record_numbers([0, 2, 4]) == [1, 3, 5]


def test_verify_rejects_unknown_and_unsorted_selection():
    times = [0.0, 1.0, 2.0]
    columns = {"p": [0.0, 1.0, 2.0]}
    specs = [SignalSpec("p", CONTINUOUS, 0.0)]
    ok, _ = verify_selection(times, columns, specs, [2, 0])
    assert not ok
    ok, _ = verify_selection(times, columns, specs, [0, 1])
    assert not ok


def test_invalid_arguments():
    with pytest.raises(ValueError):
        SignalSpec("p", "bogus", 1.0)
    with pytest.raises(ValueError):
        SignalSpec("p", CONTINUOUS, -1.0)
    with pytest.raises(ValueError):
        compress_records([], {"p": []}, [SignalSpec("p", CONTINUOUS, 1.0)])
    with pytest.raises(ValueError):
        compress_records(
            [0.0, 1.0],
            {"q": [0.0, 1.0]},
            [SignalSpec("p", CONTINUOUS, 1.0)],
        )


@pytest.mark.parametrize("seed", range(40))
def test_dp_matches_brute_force(seed):
    import random

    rng = random.Random(seed)
    n = rng.randint(2, 9)
    times = [float(i) for i in range(n)]
    pressure = [rng.uniform(0.0, 10.0) for _ in range(n)]
    valve = [rng.choice([0, 1, 2]) for _ in range(n)]

    # Sprinkle missing samples independently per signal.
    p_col = [
        None if rng.random() < 0.18 else value
        for value in pressure
    ]
    v_col = [
        None if rng.random() < 0.18 else value
        for value in valve
    ]
    # Guard against fully missing signals.
    p_col[0] = pressure[0]
    p_col[-1] = pressure[-1]
    v_col[0] = valve[0]
    v_col[-1] = valve[-1]

    branches = None
    if rng.random() < 0.5:
        cut = rng.randint(1, n - 1)
        branches = {"p": [1] * cut + [2] * (n - cut)}

    specs = [
        SignalSpec("p", CONTINUOUS, rng.uniform(0.0, 2.0)),
        SignalSpec("v", ENUMERATED),
    ]
    columns = {"p": p_col, "v": v_col}

    selection = compress_records(times, columns, specs, branches)
    ok, problems = verify_selection(
        times, columns, specs, selection, branches
    )
    assert ok, problems

    brute_size, brute_set = all_feasible_sizes(
        times, columns, specs, branches
    )
    assert brute_set is not None
    assert len(selection) == brute_size

    latest = compress_records(
        times, columns, specs, branches, prefer_latest=True
    )
    assert len(latest) == brute_size
    assert latest >= selection


def test_end_to_end_pressure_and_valve():
    times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    columns = {
        "pressure": [1.0, 1.2, 1.4, 5.0, 5.1, 5.0, 4.9],
        "valve": ["open", "open", "open", "open", "closed", "closed", "closed"],
    }
    specs = [
        SignalSpec("pressure", CONTINUOUS, 0.35),
        SignalSpec("valve", ENUMERATED),
    ]
    selection = compress_records(times, columns, specs)
    ok, problems = verify_selection(times, columns, specs, selection)
    assert ok, problems
    # Valve change at row 4 must be retained alongside pressure anchors.
    assert 4 in selection
    assert record_numbers(selection) == [index + 1 for index in selection]
    assert math.isfinite(float(times[0]))


@pytest.mark.parametrize("seed", range(30))
def test_reach_masks_match_direct_oracle(seed):
    import random

    from canmatrix.record_retention import _prepare

    rng = random.Random(1000 + seed)
    n = rng.randint(2, 30)
    times = sorted(rng.uniform(0.0, 5.0) for _ in range(n))
    pressure = [rng.uniform(0.0, 10.0) for _ in range(n)]
    valve = [rng.choice([0, 1]) for _ in range(n)]
    p_col = [None if rng.random() < 0.15 else value for value in pressure]
    v_col = [None if rng.random() < 0.15 else value for value in valve]
    p_col[0], p_col[-1] = pressure[0], pressure[-1]
    v_col[0], v_col[-1] = valve[0], valve[-1]
    specs = [
        SignalSpec("p", CONTINUOUS, rng.uniform(0.0, 1.5)),
        SignalSpec("v", ENUMERATED),
    ]
    columns = {"p": p_col, "v": v_col}
    branches = None
    if rng.random() < 0.5:
        cut = rng.randint(1, n - 1)
        branches = {"p": [1] * cut + [2] * (n - cut)}

    signals, _ = _prepare(times, columns, specs, branches, None)
    for signal in signals:
        masks = signal.reach_masks(times)
        for earlier in range(n):
            for later in range(earlier + 1, n):
                expected = naive_edge_ok(
                    times,
                    signal.values,
                    signal.spec,
                    signal.segment_of,
                    earlier,
                    later,
                )
                assert bool(masks[earlier] & (1 << later)) == expected


@pytest.mark.parametrize("seed", range(20))
def test_dp_optimum_at_larger_scale(seed):
    import random

    rng = random.Random(2000 + seed)
    n = rng.randint(60, 150)
    times = [float(i) + rng.uniform(-0.05, 0.05) for i in range(n)]
    pressure = [
        5.0 + 2.0 * math.sin(i / 7.0) + rng.uniform(-0.4, 0.4)
        for i in range(n)
    ]
    valve = [0 if (i // 11) % 2 == 0 else 1 for i in range(n)]
    p_col = [None if rng.random() < 0.05 else value for value in pressure]
    p_col[0], p_col[-1] = pressure[0], pressure[-1]
    specs = [
        SignalSpec("p", CONTINUOUS, rng.uniform(0.2, 0.8)),
        SignalSpec("v", ENUMERATED),
    ]
    columns = {"p": p_col, "v": valve}

    selection = compress_records(times, columns, specs)
    ok, problems = verify_selection(times, columns, specs, selection)
    assert ok, problems
    assert len(selection) == bfs_minimum(times, columns, specs)

    latest = compress_records(times, columns, specs, prefer_latest=True)
    ok, problems = verify_selection(times, columns, specs, latest)
    assert ok, problems
    assert len(latest) == len(selection)
    assert latest >= selection
    if latest != selection:
        assert latest > selection


def test_performance_long_log():
    import time

    n = 4000
    times = [float(i) for i in range(n)]
    pressure = [5.0 + math.sin(i / 50.0) for i in range(n)]
    valve = [i // 250 for i in range(n)]
    columns = {"p": pressure, "v": valve}
    specs = [
        SignalSpec("p", CONTINUOUS, 0.05),
        SignalSpec("v", ENUMERATED),
    ]
    started = time.perf_counter()
    selection = compress_records(times, columns, specs)
    elapsed = time.perf_counter() - started
    ok, problems = verify_selection(times, columns, specs, selection)
    assert ok, problems
    assert elapsed < 20.0


def test_record_numbers_round_trip_on_realistic_log():
    times = [float(i) for i in range(10)]
    columns = {
        "pressure": [0.0, 0.1, 0.2, 4.0, 4.1, 4.2, 4.3, 2.0, 2.1, 2.2],
        "valve": [0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
    }
    specs = [
        SignalSpec("pressure", CONTINUOUS, 0.3),
        SignalSpec("valve", ENUMERATED),
    ]
    selection = compress_records(times, columns, specs)
    numbers = record_numbers(selection)
    assert numbers == [index + 1 for index in selection]
    assert all(1 <= number <= len(times) for number in numbers)
