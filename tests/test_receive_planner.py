import random
import unittest

from tftpy.receive_planner import (
    FileJob,
    InfeasibleScheduleError,
    Move,
    ReceivePlanner,
    devices_from_groups,
    format_report,
)


def brute_force(planner, jobs, free):
    """Independent exhaustive reference solver for small instances.

    At every step any remaining job may be placed next (on any of its
    stations) or rejected, so every feasible per-device ordering is explored.
    """

    best = 0.0

    def search(remaining, free_state, score):
        nonlocal best
        best = max(best, score)
        for index in list(remaining):
            job = jobs[index]
            for station in job.stations:
                candidate = planner._candidate(job, free_state, station)
                if candidate is None:
                    continue
                device_index, _start, end = candidate
                next_free = list(free_state)
                next_free[device_index] = end
                rest = set(remaining)
                rest.remove(index)
                search(rest, next_free, score + job.weight)

    search(set(range(len(jobs))), list(free), 0.0)
    return best


def make_planner(groups):
    return ReceivePlanner(devices_from_groups(groups))


class ReceivePlannerTests(unittest.TestCase):
    def test_single_device_picks_higher_weight(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("lo", (0,), 0, 5, 4, weight=1),
            FileJob("hi", (0,), 0, 5, 4, weight=9),
        ]
        plan = planner.plan(jobs)
        self.assertTrue(plan.optimal)
        self.assertEqual([e.fid for e in plan.entries], ["hi"])
        self.assertEqual(plan.total_weight, 9)

    def test_two_files_chain_on_one_device(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("a", (0,), 0, 10, 4, weight=5),
            FileJob("b", (0,), 0, 10, 4, weight=5),
        ]
        plan = planner.plan(jobs)
        self.assertEqual(plan.total_weight, 10)
        self.assertEqual([(e.fid, e.start, e.end) for e in plan.entries],
                         [("a", 0, 4), ("b", 4, 8)])

    def test_release_time_respected(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("late", (0,), 5, 9, 3, weight=5),
            FileJob("early", (0,), 0, 6, 3, weight=5),
        ]
        plan = planner.plan(jobs)
        by_id = {e.fid: e for e in plan.entries}
        self.assertEqual((by_id["early"].start, by_id["early"].end), (0, 3))
        self.assertEqual((by_id["late"].start, by_id["late"].end), (5, 8))

    def test_flexible_file_reserved_once(self):
        planner = make_planner([[0], [1]])
        jobs = [
            FileJob("flex", (0, 1), 0, 10, 4, weight=8),
            FileJob("s0", (0,), 0, 10, 4, weight=3),
            FileJob("s1", (1,), 0, 10, 4, weight=3),
        ]
        plan = planner.plan(jobs)
        fids = [e.fid for e in plan.entries]
        self.assertEqual(sorted(fids), ["flex", "s0", "s1"])
        self.assertEqual(len([e for e in fids if e == "flex"]), 1)
        flex_entry = next(e for e in plan.entries if e.fid == "flex")
        self.assertIn(flex_entry.station, (0, 1))
        self.assertEqual(plan.total_weight, 14)

    def test_shared_head_serialises_stations(self):
        planner = make_planner([[0, 1]])
        jobs = [
            FileJob("a", (0,), 0, 10, 4, weight=5),
            FileJob("b", (1,), 0, 10, 4, weight=5),
            FileJob("c", (0,), 0, 10, 4, weight=5),
        ]
        plan = planner.plan(jobs)
        self.assertEqual(plan.total_weight, 10)  # only two fit on the shared head
        planner.validate(plan, jobs)

    def test_mandatory_infeasible_raises(self):
        planner = make_planner([[0]])
        jobs = [FileJob("m", (0,), 0, 3, 5, weight=1, mandatory=True)]
        with self.assertRaises(InfeasibleScheduleError):
            planner.plan(jobs)

    def test_infeasible_optional_file_rejected(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("ok", (0,), 0, 10, 4, weight=5),
            FileJob("bad", (0,), 0, 2, 4, weight=5),
        ]
        plan = planner.plan(jobs)
        self.assertEqual(plan.rejected, ["bad"])
        self.assertEqual([e.fid for e in plan.entries], ["ok"])

    def test_started_transfer_is_frozen(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("x", (0,), 0, 10, 10, weight=1, started=True, station=0, start=0),
            FileJob("n", (0,), 3, 6, 2, weight=9, mandatory=True),
        ]
        with self.assertRaises(InfeasibleScheduleError):
            planner.plan(jobs, now=3)

    def test_started_transfer_blocks_but_optional_dropped(self):
        planner = make_planner([[0]])
        jobs = [
            FileJob("x", (0,), 0, 10, 10, weight=1, started=True, station=0, start=0),
            FileJob("n", (0,), 3, 6, 2, weight=9),
        ]
        plan = planner.plan(jobs, now=3)
        self.assertEqual([e.fid for e in plan.entries], ["x"])
        self.assertTrue(plan.entries[0].started)

    def test_replan_narrow_window_names_the_moves(self):
        planner = make_planner([[0]])
        original_jobs = [
            FileJob("a", (0,), 0, 10, 3, weight=5),
            FileJob("b", (0,), 3, 13, 3, weight=5),
        ]
        original = planner.plan(original_jobs)
        self.assertEqual([(e.fid, e.start, e.end) for e in original.entries],
                         [("a", 0, 3), ("b", 3, 6)])

        new_jobs = original_jobs + [
            FileJob("n", (0,), 0, 2, 2, weight=9, mandatory=True)
        ]
        result = planner.replan(new_jobs, original.entries, now=0)
        self.assertTrue(result.plan.optimal)
        self.assertEqual(result.added, ["n"])
        # a and b have equal weight: exactly one of them has to move, the
        # other keeps its slot. The planner guarantees the minimal move set.
        self.assertEqual(len(result.moved), 1)
        self.assertEqual(len(result.unchanged), 1)
        moved_fid = result.moved[0].fid
        self.assertIn(moved_fid, ("a", "b"))
        kept_fid = result.unchanged[0]
        kept = next(e for e in result.plan.entries if e.fid == kept_fid)
        self.assertEqual((kept.start, kept.end), (3, 6))
        self.assertFalse(any(m.changed_station for m in result.moved))
        planner.validate(result.plan, new_jobs)
        self.assertIn("must move", format_report(result))

    def test_replan_can_move_file_to_other_station(self):
        from tftpy.receive_planner import PlanEntry

        planner = make_planner([[0, 1], [2]])
        jobs = [
            FileJob("x", (0,), 0, 10, 6, weight=1),
            FileJob("flex", (1, 2), 0, 10, 4, weight=5),
        ]
        # An existing reservation (from an earlier planning round) keeps flex
        # on station 2, while x occupies station 0's shared head until 6.
        previous = [
            PlanEntry("x", 0, 0, 0, 6, weight=1),
            PlanEntry("flex", 2, 1, 0, 4, weight=5),
        ]

        # n occupies station 2's head until 7; flex can no longer finish there
        # before its deadline, so it must cross back to station 1 [6, 10).
        narrow = FileJob("n", (2,), 0, 7, 7, weight=9, mandatory=True)
        result = planner.replan(jobs + [narrow], previous)
        new_flex = next(e for e in result.plan.entries if e.fid == "flex")
        new_n = next(e for e in result.plan.entries if e.fid == "n")
        self.assertEqual((new_n.station, new_n.start, new_n.end), (2, 0, 7))
        self.assertEqual(new_flex.station, 1)
        self.assertEqual((new_flex.start, new_flex.end), (6, 10))
        move = next(m for m in result.moved if m.fid == "flex")
        self.assertTrue(move.changed_station)
        planner.validate(result.plan, jobs + [narrow])

    def test_replan_keeps_unchanged_when_possible(self):
        planner = make_planner([[0]])
        original_jobs = [
            FileJob("a", (0,), 0, 20, 5, weight=5),
            FileJob("b", (0,), 0, 20, 5, weight=5),
        ]
        original = planner.plan(original_jobs)
        # A narrow job that fits entirely inside the idle tail: nothing moves.
        narrow = FileJob("n", (0,), 12, 15, 3, weight=9, mandatory=True)
        result = planner.replan(original_jobs + [narrow], original.entries)
        self.assertEqual(result.moved, [])
        self.assertEqual(sorted(result.unchanged), ["a", "b"])
        self.assertEqual(result.added, ["n"])

    def test_replan_frozen_file_never_moves(self):
        planner = make_planner([[0], [1]])
        original_jobs = [
            FileJob("frozen", (0,), 0, 10, 6, weight=5, started=True,
                    station=0, start=0),
            FileJob("loose", (1,), 0, 10, 4, weight=5),
        ]
        original = planner.plan(original_jobs, now=2)
        new_jobs = [
            FileJob("frozen", (0,), 0, 10, 6, weight=5, started=True,
                    station=0, start=0),
            FileJob("loose", (1,), 0, 10, 4, weight=5),
            FileJob("n", (1,), 0, 6, 3, weight=9, mandatory=True),
        ]
        result = planner.replan(new_jobs, original.entries, now=2)
        self.assertEqual(result.frozen, ["frozen"])
        frozen_entry = next(e for e in result.plan.entries if e.fid == "frozen")
        self.assertEqual((frozen_entry.station, frozen_entry.start), (0, 0))
        # n takes the head first [2, 5); loose is pushed to [5, 9).
        new_n = next(e for e in result.plan.entries if e.fid == "n")
        new_loose = next(e for e in result.plan.entries if e.fid == "loose")
        self.assertEqual((new_n.start, new_n.end), (2, 5))
        self.assertEqual((new_loose.start, new_loose.end), (5, 9))
        self.assertEqual([m.fid for m in result.moved], ["loose"])
        planner.validate(result.plan, new_jobs, now=2)

    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(20261114)
        groups = [[0, 1], [2], [3, 4]]
        planner = make_planner(groups)
        stations = list(devices_from_groups(groups))
        for _ in range(40):
            count = rng.randint(1, 8)
            jobs = []
            for i in range(count):
                release = rng.randint(0, 6)
                deadline = release + rng.randint(2, 10)
                duration = rng.randint(1, 5)
                options = rng.sample(stations, rng.randint(1, 2))
                jobs.append(
                    FileJob("f%d" % i, tuple(options), release, deadline,
                            duration, weight=rng.randint(1, 10))
                )
            plan = planner.plan(jobs)
            self.assertTrue(plan.optimal)
            planner.validate(plan, jobs)
            self.assertAlmostEqual(
                plan.total_weight, brute_force(planner, jobs, [0.0] * 3)
            )

    def test_greedy_fallback_still_valid(self):
        planner = make_planner([[0], [1]])
        jobs = [
            FileJob("a", (0,), 0, 10, 4, weight=5),
            FileJob("b", (1,), 0, 10, 4, weight=5),
        ]
        plan = planner.plan(jobs, node_budget=0)
        self.assertFalse(plan.optimal)
        planner.validate(plan, jobs)
        self.assertEqual(plan.total_weight, 10)

    def test_dict_input_and_duplicate_station_rejected(self):
        planner = ReceivePlanner({0: "d0", 1: "d1"})
        plan = planner.plan([
            {"fid": "a", "stations": [0, 1], "release": 0, "deadline": 8,
             "duration": 3, "weight": 7, "priority": 2},
        ])
        self.assertEqual(plan.entries[0].fid, "a")
        self.assertEqual(plan.entries[0].weight, 7)
        with self.assertRaises(ValueError):
            devices_from_groups([[0, 1], [1, 2]])

    def test_twenty_station_scenario_with_narrow_arrival(self):
        groups = [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9],
                  [10], [11], [12], [13], [14],
                  [15, 16], [17], [18], [19]]
        planner = make_planner(groups)
        rng = random.Random(7)
        jobs = []
        for station in range(20):
            release = rng.randint(0, 30)
            duration = rng.randint(2, 6)
            jobs.append(
                FileJob("j%02d" % station, (station,), release,
                        release + rng.randint(duration + 2, 24), duration,
                        weight=rng.randint(1, 10))
            )
        jobs.append(FileJob("flex", (3, 12), 0, 40, 5, weight=10, priority=9))

        plan = planner.plan(jobs)
        self.assertTrue(plan.optimal)
        planner.validate(plan, jobs)
        self.assertEqual(len([e for e in plan.entries if e.fid == "flex"]), 1)
        baseline_weight = plan.total_weight
        self.assertGreater(baseline_weight, 0)

        narrow = FileJob("urgent", (5,), 8, 11, 3, weight=10, mandatory=True)
        result = planner.replan(jobs + [narrow], plan.entries)
        planner.validate(result.plan, jobs + [narrow])
        self.assertIn("urgent", [e.fid for e in result.plan.entries])
        urgent = next(e for e in result.plan.entries if e.fid == "urgent")
        self.assertGreaterEqual(urgent.start, 8 - 1e-9)
        self.assertLessEqual(urgent.end, 11 + 1e-9)
        # Every reported move must describe a real change of reservation.
        for move in result.moved:
            self.assertIsInstance(move, Move)
            self.assertTrue(
                move.changed_station or abs(move.old_start - move.new_start) > 1e-9
            )
        report = format_report(result)
        self.assertIn("urgent", report)


if __name__ == "__main__":
    unittest.main()
