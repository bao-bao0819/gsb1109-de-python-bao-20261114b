"""Receive-window scheduler for shared collection heads.

Twenty sampling stations upload files, but some stations share one collection
head (a "device"). A device can only receive one file at a time. Each file has
a readable time window (release / deadline), an upper-bound transfer duration
and a priority weight. The scheduler assigns each accepted file to one of its
allowed stations and a non-overlapping start time so that the total weight of
files received in full before their deadline is maximised.

Rules implemented here:

* a reservation may be moved freely until the transfer starts;
* once the first block has been received (``started=True``) the reservation is
  frozen and can never move;
* a file readable at two stations is reserved on at most one of them (never
  double booked);
* a newly arriving narrow-window file is handled by :meth:`ReceivePlanner.replan`,
  which reports exactly which previously made reservations have to move.

The solver is pure stdlib. With few multi-station files (the usual case here)
it enumerates their station choices and solves each shared head independently
as an exact memoised single-machine selection problem; otherwise a dominance
branch-and-bound search is used. Every result is marked ``optimal`` only when
the search was exhaustive within the node budget, and a deterministic greedy
schedule is returned (flagged non-optimal) if the budget is exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

EPS = 1e-9


class InfeasibleScheduleError(RuntimeError):
    """Raised when a mandatory file cannot be placed within its window."""


class _BudgetExceeded(RuntimeError):
    """Internal signal that the exact search node budget was exhausted."""


@dataclass
class FileJob:
    """One file offered by a sampling station.

    ``stations`` holds every station the file may be received at. A flexible
    high-priority file readable at two stations simply lists both; the planner
    reserves exactly one.
    """

    fid: str
    stations: Tuple[int, ...]
    release: float
    deadline: float
    duration: float
    weight: float = 1.0
    priority: int = 0
    mandatory: bool = False
    started: bool = False
    station: Optional[int] = None
    start: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileJob":
        stations = tuple(data["stations"]) if "stations" in data else (data["station"],)
        return cls(
            fid=str(data["fid"]),
            stations=stations,
            release=float(data["release"]),
            deadline=float(data["deadline"]),
            duration=float(data["duration"]),
            weight=float(data.get("weight", 1.0)),
            priority=int(data.get("priority", 0)),
            mandatory=bool(data.get("mandatory", False)),
            started=bool(data.get("started", False)),
            station=data.get("station"),
            start=None if data.get("start") is None else float(data["start"]),
        )


@dataclass
class PlanEntry:
    """One confirmed reservation: file, station/device and half-open interval."""

    fid: str
    station: int
    device: Any
    start: float
    end: float
    weight: float
    priority: int = 0
    started: bool = False


@dataclass
class Move:
    """A reservation that the replan had to relocate."""

    fid: str
    old_station: int
    old_start: float
    old_end: float
    new_station: int
    new_start: float
    new_end: float

    @property
    def changed_station(self) -> bool:
        return self.old_station != self.new_station


@dataclass
class Plan:
    entries: List[PlanEntry]
    rejected: List[str]
    total_weight: float
    optimal: bool
    nodes: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entries": [e.__dict__.copy() for e in self.entries],
            "rejected": list(self.rejected),
            "total_weight": self.total_weight,
            "optimal": self.optimal,
            "nodes": self.nodes,
        }


@dataclass
class ReplanResult:
    plan: Plan
    moved: List[Move] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    frozen: List[str] = field(default_factory=list)


def devices_from_groups(groups: Sequence[Sequence[int]]) -> Dict[int, int]:
    """Build a station -> device map from groups of stations sharing a head."""
    mapping: Dict[int, int] = {}
    for device, group in enumerate(groups):
        for station in group:
            if station in mapping:
                raise ValueError("station %s listed in two device groups" % station)
            mapping[station] = device
    return mapping


class ReceivePlanner:
    """Builds conflict-free receive plans and replans around new arrivals."""

    # Above this many device-assignment combinations the general branch-and-bound
    # solver is used instead of the flexible-job decomposition.
    FLEX_ENUM_LIMIT = 4096

    def __init__(self, station_device: Mapping[int, Any]):
        self.station_device = dict(station_device)
        self.devices = sorted(set(self.station_device.values()), key=str)
        self._device_index = {device: i for i, device in enumerate(self.devices)}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def plan(
        self,
        files: Iterable[Any],
        now: float = 0.0,
        node_budget: int = 2_000_000,
        previous: Optional[Mapping[str, Tuple[int, float]]] = None,
    ) -> Plan:
        """Return the best-weight conflict-free plan for ``files``.

        ``previous`` maps file id to ``(station, start)`` of an existing
        reservation; among equal-weight solutions the one that keeps the most
        previous reservations in place is preferred (used by :meth:`replan`).
        """

        jobs = [self._coerce(job) for job in files]
        previous = dict(previous or {})
        n_devices = len(self.devices)

        free, frozen_entries, _frozen_weight = self._freeze_started(jobs, now)
        open_jobs, rejected = self._filter_open(jobs)

        if open_jobs:
            try:
                score, placements, nodes, optimal = self._solve(
                    open_jobs, free, previous, node_budget
                )
            except _BudgetExceeded:
                placements, nodes = self._greedy(open_jobs, free, previous)
                optimal = False
                missing = [
                    job.fid for index, job in enumerate(open_jobs)
                    if job.mandatory and index not in placements
                ]
                if missing:
                    raise InfeasibleScheduleError(
                        "cannot place mandatory file(s): %s" % ", ".join(missing)
                    )
        else:
            placements, nodes, optimal = {}, 0, True

        entries = list(frozen_entries)
        for index, (station, start) in placements.items():
            job = open_jobs[index]
            device = self.station_device[station]
            entries.append(
                PlanEntry(
                    fid=job.fid,
                    station=station,
                    device=device,
                    start=start,
                    end=start + job.duration,
                    weight=job.weight,
                    priority=job.priority,
                )
            )

        entries.sort(key=lambda entry: (entry.start, str(entry.device), entry.fid))
        total_weight = round(sum(entry.weight for entry in entries), 12)
        plan = Plan(
            entries=entries,
            rejected=rejected,
            total_weight=total_weight,
            optimal=optimal,
            nodes=nodes,
        )
        self.validate(plan, jobs, now)
        return plan

    def replan(
        self,
        files: Iterable[Any],
        previous: Sequence[PlanEntry],
        now: float = 0.0,
        node_budget: int = 2_000_000,
    ) -> ReplanResult:
        """Re-optimise while honouring every already-started reservation.

        Returns the new plan together with the precise list of old
        reservations that had to move to accommodate the new request set.
        """

        jobs = [self._coerce(job) for job in files]
        started = {job.fid for job in jobs if job.started}
        old_map = {
            entry.fid: (entry.station, entry.start)
            for entry in previous
            if entry.fid not in started
        }
        plan = self.plan(jobs, now=now, node_budget=node_budget, previous=old_map)
        new_map = {entry.fid: entry for entry in plan.entries if not entry.started}
        old_entries = {entry.fid: entry for entry in previous if entry.fid not in started}

        moved: List[Move] = []
        unchanged: List[str] = []
        for fid, old_entry in old_entries.items():
            new_entry = new_map.get(fid)
            if new_entry is None:
                continue
            if (
                new_entry.station == old_entry.station
                and abs(new_entry.start - old_entry.start) <= EPS
            ):
                unchanged.append(fid)
            else:
                moved.append(
                    Move(
                        fid=fid,
                        old_station=old_entry.station,
                        old_start=old_entry.start,
                        old_end=old_entry.end,
                        new_station=new_entry.station,
                        new_start=new_entry.start,
                        new_end=new_entry.end,
                    )
                )

        added = [fid for fid in new_map if fid not in old_entries]
        dropped = [fid for fid in old_entries if fid not in new_map]
        frozen = sorted(started)
        return ReplanResult(
            plan=plan,
            moved=moved,
            unchanged=sorted(unchanged),
            added=sorted(added),
            dropped=sorted(dropped),
            frozen=frozen,
        )

    def validate(self, plan: Plan, jobs: Sequence[FileJob], now: float = 0.0) -> None:
        """Assert that ``plan`` honours every scheduling rule."""

        by_id = {job.fid: job for job in jobs}
        seen: set = set()
        device_intervals: Dict[Any, List[Tuple[float, float, str]]] = {}

        for entry in plan.entries:
            if entry.fid in seen:
                raise AssertionError("file %s reserved twice" % entry.fid)
            seen.add(entry.fid)
            job = by_id[entry.fid]
            if entry.station not in job.stations:
                raise AssertionError("file %s placed at disallowed station" % entry.fid)
            if self.station_device[entry.station] != entry.device:
                raise AssertionError("device mismatch for file %s" % entry.fid)
            if entry.start < job.release - EPS:
                raise AssertionError("file %s starts before its release" % entry.fid)
            if entry.end > job.deadline + EPS:
                raise AssertionError("file %s misses its deadline" % entry.fid)
            if abs((entry.end - entry.start) - job.duration) > EPS:
                raise AssertionError("file %s uses wrong duration" % entry.fid)
            if not job.started and entry.start < now - EPS:
                raise AssertionError("file %s scheduled in the past" % entry.fid)
            device_intervals.setdefault(entry.device, []).append(
                (entry.start, entry.end, entry.fid)
            )

        for device, intervals in device_intervals.items():
            intervals.sort()
            for (start_a, end_a, fid_a), (start_b, end_b, fid_b) in zip(
                intervals, intervals[1:]
            ):
                if start_b < end_a - EPS:
                    raise AssertionError(
                        "device %s overlap: %s vs %s" % (device, fid_a, fid_b)
                    )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _coerce(self, job: Any) -> FileJob:
        if isinstance(job, FileJob):
            return job
        if isinstance(job, Mapping):
            return FileJob.from_dict(job)
        raise TypeError("unsupported job type: %r" % type(job))

    def _freeze_started(
        self, jobs: Sequence[FileJob], now: float
    ) -> Tuple[List[float], List[PlanEntry], float]:
        free = [now] * len(self.devices)
        frozen: List[PlanEntry] = []
        per_device: Dict[int, List[Tuple[float, float, str]]] = {}

        for job in jobs:
            if not job.started:
                continue
            if job.station is None or job.start is None:
                raise ValueError("started file %s needs station and start" % job.fid)
            if job.station not in self.station_device:
                raise ValueError("unknown station %s" % job.station)
            device = self.station_device[job.station]
            device_index = self._device_index[device]
            end = job.start + job.duration
            if end <= now + EPS:
                # Already fully received; nothing to schedule.
                continue
            frozen.append(
                PlanEntry(
                    fid=job.fid,
                    station=job.station,
                    device=device,
                    start=job.start,
                    end=end,
                    weight=job.weight,
                    priority=job.priority,
                    started=True,
                )
            )
            per_device.setdefault(device_index, []).append((job.start, end, job.fid))
            free[device_index] = max(free[device_index], end)

        for device_index, intervals in per_device.items():
            intervals.sort()
            for (start_a, end_a, fid_a), (start_b, _, fid_b) in zip(
                intervals, intervals[1:]
            ):
                if start_b < end_a - EPS:
                    raise InfeasibleScheduleError(
                        "frozen transfers %s and %s overlap on one device"
                        % (fid_a, fid_b)
                    )
        return free, frozen, sum(entry.weight for entry in frozen)

    def _filter_open(
        self, jobs: Sequence[FileJob]
    ) -> Tuple[List[FileJob], List[str]]:
        open_jobs: List[FileJob] = []
        rejected: List[str] = []
        for job in jobs:
            if job.started:
                continue
            if job.stations and any(
                station not in self.station_device for station in job.stations
            ):
                raise ValueError("file %s references an unknown station" % job.fid)
            if not job.stations or job.duration > (job.deadline - job.release) + EPS:
                rejected.append(job.fid)
                if job.mandatory:
                    raise InfeasibleScheduleError(
                        "mandatory file %s cannot fit its own window" % job.fid
                    )
                continue
            open_jobs.append(job)
        return open_jobs, rejected

    def _candidate(
        self, job: FileJob, free: Sequence[float], station: int
    ) -> Optional[Tuple[int, float, float]]:
        device_index = self._device_index[self.station_device[station]]
        start = max(job.release, free[device_index])
        end = start + job.duration
        if end <= job.deadline + EPS:
            return device_index, start, end
        return None

    def _solve(
        self,
        jobs: Sequence[FileJob],
        free: Sequence[float],
        previous: Mapping[str, Tuple[int, float]],
        node_budget: int,
    ) -> Tuple[float, Dict[int, Tuple[int, float]], int, bool]:
        scale = len(jobs) + 1
        flexible = [
            (index, job)
            for index, job in enumerate(jobs)
            if len({self.station_device[station] for station in job.stations}) > 1
        ]
        # Enumeration space: every device choice per flexible job, plus the
        # option of rejecting an optional one.
        enumeration_size = 1
        for _index, job in flexible:
            choices = {self.station_device[s] for s in job.stations}
            enumeration_size *= len(choices) + (0 if job.mandatory else 1)

        if enumeration_size <= self.FLEX_ENUM_LIMIT:
            return self._solve_decomposed(
                jobs, free, previous, scale, flexible, node_budget
            )
        return self._solve_general(jobs, free, previous, node_budget)

    def _solve_decomposed(
        self,
        jobs: Sequence[FileJob],
        free: Sequence[float],
        previous: Mapping[str, Tuple[int, float]],
        scale: float,
        flexible: Sequence[Tuple[int, FileJob]],
        node_budget: int,
    ) -> Tuple[float, Dict[int, Tuple[int, float]], int, bool]:
        n_devices = len(self.devices)
        flex_indices = {index for index, _job in flexible}
        fixed: List[List[int]] = [[] for _ in range(n_devices)]
        for index, job in enumerate(jobs):
            if index in flex_indices:
                continue
            station = job.stations[0]
            fixed[self._device_index[self.station_device[station]]].append(index)

        option_sets = []
        for index, job in flexible:
            device_stations: Dict[Any, int] = {}
            for station in job.stations:
                device_stations.setdefault(self.station_device[station], station)
            choices = list(device_stations.items())
            if not job.mandatory:
                choices.append((None, None))  # reject this flexible file
            option_sets.append(choices)

        incumbent, _ = self._greedy(jobs, free, previous)
        best_placement = dict(incumbent)
        best_score = self._score_placements(jobs, incumbent, previous, scale)
        nodes = 0

        for combo in product(*option_sets):
            groups = [list(group) for group in fixed]
            chosen_station: Dict[int, int] = {}
            for (index, _job), (device, station) in zip(flexible, combo):
                if device is None:
                    continue  # this optional flexible file is rejected
                groups[self._device_index[device]].append(index)
                chosen_station[index] = station

            total_score = 0.0
            placements: Dict[int, Tuple[int, float]] = {}
            feasible = True
            for device_index, group in enumerate(groups):
                score, sub_placements, sub_nodes, sub_feasible = self._solve_device(
                    jobs, group, free[device_index], previous, scale, chosen_station
                )
                nodes += sub_nodes
                if nodes > node_budget:
                    raise _BudgetExceeded()
                if not sub_feasible:
                    feasible = False
                    break
                total_score += score
                placements.update(sub_placements)

            if feasible and total_score > best_score + EPS:
                best_score = total_score
                best_placement = placements

        missing = [
            jobs[index].fid
            for index, job in enumerate(jobs)
            if job.mandatory and index not in best_placement
        ]
        if missing:
            raise InfeasibleScheduleError(
                "cannot place mandatory file(s): %s" % ", ".join(missing)
            )
        return best_score, best_placement, nodes, True

    def _solve_device(
        self,
        jobs: Sequence[FileJob],
        group: Sequence[int],
        t0: float,
        previous: Mapping[str, Tuple[int, float]],
        scale: float,
        chosen_station: Mapping[int, int],
    ) -> Tuple[float, Dict[int, Tuple[int, float]], int, bool]:
        """Optimal single-machine weighted selection with release/deadline."""

        k = len(group)
        full_mask = (1 << k) - 1
        memo: Dict[Tuple[int, float], float] = {}
        nodes = 0

        def station_for(index: int) -> int:
            if index in chosen_station:
                return chosen_station[index]
            job = jobs[index]
            prior = previous.get(job.fid)
            if prior is not None and prior[0] in job.stations:
                return prior[0]
            return job.stations[0]

        def dfs(mask: int, clock: float) -> float:
            """Best additional score obtainable from the remaining jobs."""

            nonlocal nodes
            nodes += 1
            key = (mask, clock)
            cached = memo.get(key)
            if cached is not None:
                return cached
            best_here = 0.0
            probe = mask
            while probe:
                bit = probe & -probe
                position = bit.bit_length() - 1
                probe ^= bit
                index = group[position]
                job = jobs[index]
                station = station_for(index)
                start = max(job.release, clock)
                end = start + job.duration
                if end > job.deadline + EPS:
                    if job.mandatory:
                        memo[key] = float("-inf")
                        return float("-inf")
                    continue
                penalty = 0.0 if previous.get(job.fid) == (station, start) else 1.0
                suffix = dfs(mask ^ bit, end)
                if suffix == float("-inf"):
                    if job.mandatory:
                        memo[key] = float("-inf")
                        return float("-inf")
                    continue
                best_here = max(best_here, job.weight * scale - penalty + suffix)
            memo[key] = best_here
            return best_here

        best_score = dfs(full_mask, t0)
        if best_score == float("-inf"):
            return 0.0, {}, nodes, False

        placements: Dict[int, Tuple[int, float]] = {}
        mask, clock = full_mask, t0
        while mask:
            target = memo[(mask, clock)]
            if target <= EPS:
                break
            advanced = False
            probe = mask
            while probe:
                bit = probe & -probe
                position = bit.bit_length() - 1
                probe ^= bit
                index = group[position]
                job = jobs[index]
                station = station_for(index)
                start = max(job.release, clock)
                end = start + job.duration
                if end > job.deadline + EPS:
                    continue
                penalty = 0.0 if previous.get(job.fid) == (station, start) else 1.0
                gain = job.weight * scale - penalty
                if abs(gain + dfs(mask ^ bit, end) - target) <= 1e-6:
                    placements[index] = (station, start)
                    mask ^= bit
                    clock = end
                    advanced = True
                    break
            if not advanced:
                break
        return best_score, placements, nodes, True

    def _solve_general(
        self,
        jobs: Sequence[FileJob],
        free: Sequence[float],
        previous: Mapping[str, Tuple[int, float]],
        node_budget: int,
    ) -> Tuple[float, Dict[int, Tuple[int, float]], int, bool]:
        n = len(jobs)
        scale = n + 1
        full_mask = (1 << n) - 1

        # Total remaining weight for every subset, used as the optimistic bound.
        remaining_weight = [0.0] * (1 << n)
        for mask in range(1, 1 << n):
            low = mask & -mask
            remaining_weight[mask] = (
                remaining_weight[mask ^ low] + jobs[low.bit_length() - 1].weight
            )

        # Greedy gives an incumbent immediately, so the branch-and-bound search
        # prunes most branches before exploring them.
        incumbent, _ = self._greedy(jobs, free, previous)
        best_placement = dict(incumbent)
        best_score = self._score_placements(jobs, incumbent, previous, scale)
        nodes = 0
        exhausted = True
        # Dominance memo: best gained score with which a (mask, free) state was
        # reached. A later, lower-scored arrival cannot improve anything.
        seen: Dict[Tuple[int, Tuple[float, ...]], float] = {}

        def dfs(
            mask: int,
            free_state: List[float],
            gained: float,
            placement: Dict[int, Tuple[int, float]],
        ) -> None:
            nonlocal nodes, best_score, best_placement, exhausted
            nodes += 1
            if nodes > node_budget:
                exhausted = False
                return
            state = (mask, tuple(free_state))
            prior = seen.get(state)
            if prior is not None and prior >= gained - EPS:
                return
            seen[state] = gained

            # Stopping here is itself a valid choice (reject every remaining
            # job), so every visited node is a candidate solution.
            if gained > best_score + EPS:
                best_score = gained
                best_placement = dict(placement)

            # Optimistic bound: every remaining job is accepted with no move
            # penalty. Never understates the achievable score.
            if gained + remaining_weight[mask] * scale <= best_score + EPS:
                return

            branches = []
            probe = mask
            while probe:
                bit = probe & -probe
                index = bit.bit_length() - 1
                probe ^= bit
                job = jobs[index]
                options = []
                for station in job.stations:
                    candidate = self._candidate(job, free_state, station)
                    if candidate is None:
                        continue
                    device_index, start, end = candidate
                    kept = previous.get(job.fid) == (station, start)
                    options.append((device_index, end, station, start, kept))
                if not options:
                    if job.mandatory:
                        # Device availability only moves forward, so a
                        # mandatory file that does not fit now never will.
                        return
                    continue
                for device_index, end, station, start, kept in options:
                    gain = job.weight * scale - (0.0 if kept else 1.0)
                    branches.append(
                        (not kept, not job.mandatory, job.deadline, -gain,
                         bit, index, device_index, end, station, start, gain)
                    )

            # Keep old reservations first, then mandatory / urgent / valuable
            # placements, so strong incumbents surface early.
            branches.sort()
            for (_moved, _optional, _deadline, _gain, bit, index,
                 device_index, end, station, start, gain) in branches:
                old_free = free_state[device_index]
                free_state[device_index] = end
                placement[index] = (station, start)
                dfs(mask ^ bit, free_state, gained + gain, placement)
                del placement[index]
                free_state[device_index] = old_free
                if not exhausted:
                    return

        dfs(full_mask, list(free), 0.0, {})
        if not exhausted:
            raise _BudgetExceeded()
        missing = [
            jobs[index].fid
            for index, job in enumerate(jobs)
            if job.mandatory and index not in best_placement
        ]
        if missing:
            raise InfeasibleScheduleError(
                "cannot place mandatory file(s): %s" % ", ".join(missing)
            )
        return best_score, best_placement, nodes, True

    @staticmethod
    def _score_placements(
        jobs: Sequence[FileJob],
        placements: Mapping[int, Tuple[int, float]],
        previous: Mapping[str, Tuple[int, float]],
        scale: float,
    ) -> float:
        score = 0.0
        for index, (station, start) in placements.items():
            job = jobs[index]
            penalty = 0.0 if previous.get(job.fid) == (station, start) else 1.0
            score += job.weight * scale - penalty
        return score

    def _greedy(
        self,
        jobs: Sequence[FileJob],
        free: Sequence[float],
        previous: Mapping[str, Tuple[int, float]],
    ) -> Tuple[Dict[int, Tuple[int, float]], int]:
        orderings = (
            lambda job: (job.deadline, job.release, -job.weight),
            lambda job: (-job.weight, job.deadline),
            lambda job: (-job.priority, job.deadline, -job.weight),
            lambda job: (-(job.weight / job.duration), job.deadline),
        )
        best: Optional[Tuple[float, Dict[int, Tuple[int, float]]]] = None
        scale = len(jobs) + 1

        for order in orderings:
            free_state = list(free)
            placements: Dict[int, Tuple[int, float]] = {}
            score = 0.0
            for index, job in sorted(enumerate(jobs), key=lambda pair: order(pair[1])):
                options = []
                for station in job.stations:
                    candidate = self._candidate(job, free_state, station)
                    if candidate is not None:
                        options.append((candidate[1], station, candidate[0]))
                if not options:
                    # Greedy is only an incumbent heuristic; the branch-and-bound
                    # search decides whether mandatory files are truly infeasible.
                    continue
                start, station, device_index = min(options)
                free_state[device_index] = start + job.duration
                placements[index] = (station, start)
                penalty = 0.0 if previous.get(job.fid) == (station, start) else 1.0
                score += job.weight * scale - penalty
            if best is None or score > best[0]:
                best = (score, placements)
        assert best is not None
        return best[1], 0


def format_report(result: ReplanResult) -> str:
    """Render a :class:`ReplanResult` as a human-readable explanation."""

    lines = ["Receive plan (total weight %.3f, optimal=%s)"
             % (result.plan.total_weight, result.plan.optimal)]
    lines.append("  device  station  file  start-end  weight")
    for entry in result.plan.entries:
        flag = " [frozen]" if entry.started else ""
        lines.append(
            "  %-6s  %-7s  %-5s %6.1f-%-6.1f %g%s"
            % (entry.device, entry.station, entry.fid, entry.start, entry.end,
               entry.weight, flag)
        )
    if result.moved:
        lines.append("Reservations that must move:")
        for move in result.moved:
            kind = "station+time" if move.changed_station else "time"
            lines.append(
                "  %s: station %s %.1f-%.1f -> station %s %.1f-%.1f (%s)"
                % (move.fid, move.old_station, move.old_start, move.old_end,
                   move.new_station, move.new_start, move.new_end, kind)
            )
    else:
        lines.append("No existing reservation has to move.")
    if result.added:
        lines.append("Added:     %s" % ", ".join(result.added))
    if result.unchanged:
        lines.append("Unchanged: %s" % ", ".join(result.unchanged))
    if result.dropped:
        lines.append("Dropped:   %s" % ", ".join(result.dropped))
    if result.frozen:
        lines.append("Frozen (first block received): %s" % ", ".join(result.frozen))
    if result.plan.rejected:
        lines.append("Rejected (no feasible window): %s"
                     % ", ".join(result.plan.rejected))
    return "\n".join(lines)
