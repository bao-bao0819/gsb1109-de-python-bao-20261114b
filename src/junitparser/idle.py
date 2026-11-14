"""Bounds on machine idle time derived from JUnit timing records.

Test machine rental is billed by wall-clock time, but JUnit reports only
record per-suite and per-case durations.  Given the maximum concurrency of
a machine, the start/end of each suite, and optionally the recorded start
times of some cases, this module computes lower and upper bounds for the
total machine idle time that are consistent with those records.

Model:

- Each top-level testsuite is one rented machine.  Its rental window is
  ``[timestamp, timestamp + time]``.
- Nested testsuites under the same parent share the parent's machine; they
  never occupy an extra machine.  Their own ``timestamp``/``time`` (when
  recorded) only narrow the window in which their cases may run.
- A machine runs at most ``concurrency`` cases at any moment.
- A case runs contiguously for ``case.time`` seconds inside the
  intersection of all its ancestor suite windows.  A case with a known
  start time (see ``case_starts``) is fixed at that instant.

When the records admit no feasible schedule (e.g. two long cases pinned
to the same narrow window with concurrency 1), :func:`idle_bounds` raises
:class:`InfeasibleScheduleError` instead of reporting a negative idle
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .junitparser import JUnitXml, JUnitXmlError, TestCase, TestSuite

__all__ = [
    "IdleBounds",
    "InfeasibleScheduleError",
    "MachineIdleBounds",
    "idle_bounds",
]

_EPS = 1e-6


class InfeasibleScheduleError(JUnitXmlError):
    """Raised when the recorded timings admit no feasible schedule."""


@dataclass(frozen=True)
class MachineIdleBounds:
    """Idle-time bounds (seconds) for one machine, i.e. one top-level suite.

    Attributes:
        suite: Name of the top-level testsuite.
        hostname: Recorded hostname of the suite, if any.
        window: Rental wall-clock length of the suite, in seconds.
        min_idle: Lower bound of idle time consistent with the records.
        max_idle: Upper bound of idle time consistent with the records.
    """

    suite: str | None
    hostname: str | None
    window: float
    min_idle: float
    max_idle: float


@dataclass(frozen=True)
class IdleBounds:
    """Total idle-time bounds (seconds) across all machines.

    Attributes:
        min_idle: Sum of per-machine lower bounds.
        max_idle: Sum of per-machine upper bounds.
        machines: Per-machine breakdown.
    """

    min_idle: float
    max_idle: float
    machines: tuple[MachineIdleBounds, ...]


@dataclass
class _CaseRun:
    """A testcase to place on a machine."""

    duration: float
    window_start: float
    window_end: float
    fixed_start: float | None


def _parse_instant(value) -> float:
    """Convert a datetime, ISO-8601 string, or epoch number to epoch seconds.

    Naive datetimes are treated as UTC; only relative differences matter.
    """
    if isinstance(value, bool):
        raise TypeError(f"Invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise JUnitXmlError(f"Invalid timestamp: {value!r}") from None
    else:
        raise TypeError(f"Invalid timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_duration(raw: str) -> float:
    return float(raw.replace(",", ""))


def _sweep(intervals) -> tuple[float, int]:
    """Return (union measure, max overlap) of ``(start, end)`` intervals."""
    events = []
    for start, end in intervals:
        if end > start:
            events.append((start, 1))
            events.append((end, -1))
    events.sort()
    union = 0.0
    overlap = 0
    max_overlap = 0
    prev = None
    for point, delta in events:
        if overlap > 0 and prev is not None:
            union += point - prev
        overlap += delta
        max_overlap = max(max_overlap, overlap)
        prev = point
    return union, max_overlap


class _CaseStarts:
    """Resolves known case start times from a user mapping or the XML."""

    def __init__(self, mapping: Mapping | None):
        self._by_object = {}
        self._by_key = {}
        self._by_name = {}
        for key, value in (mapping or {}).items():
            instant = _parse_instant(value)
            if isinstance(key, TestCase):
                self._by_object[id(key)] = instant
            elif isinstance(key, tuple):
                self._by_key[tuple(key)] = instant
            else:
                self._by_name[str(key)] = instant

    def lookup(self, case: TestCase) -> float | None:
        if id(case) in self._by_object:
            return self._by_object[id(case)]
        key = (case.classname, case.name)
        if key in self._by_key:
            return self._by_key[key]
        if case.name in self._by_name:
            return self._by_name[case.name]
        raw = case._elem.attrib.get("timestamp")
        if raw is not None:
            return _parse_instant(raw)
        return None


def _collect(
    suite: TestSuite,
    win_start: float,
    win_end: float,
    starts: _CaseStarts,
    runs: list[_CaseRun],
    constraints: list[tuple[float, float, str]],
) -> float:
    """Gather cases of *suite* (recursively) and per-window work constraints.

    Returns the total case work in the suite subtree.
    """
    own_ts = suite._elem.attrib.get("timestamp")
    own_time = suite._elem.attrib.get("time")
    if own_ts is not None and own_time is not None:
        start = _parse_instant(own_ts)
        end = start + _parse_duration(own_time)
        win_start = max(win_start, start)
        win_end = min(win_end, end)

    work = 0.0
    for case in suite.iterchildren(suite.testcase):
        duration = case.time
        duration = 0.0 if duration is None else duration
        label = f"testcase {case.name!r}"
        if duration < 0:
            raise InfeasibleScheduleError(f"{label} has a negative duration")
        fixed = starts.lookup(case)
        if fixed is not None:
            if fixed < win_start - _EPS or fixed + duration > win_end + _EPS:
                raise InfeasibleScheduleError(
                    f"{label} (start {fixed:g}, duration {duration:g}s) does not "
                    f"fit in the window [{win_start:g}, {win_end:g}]"
                )
        elif duration > win_end - win_start + _EPS:
            raise InfeasibleScheduleError(
                f"{label} ({duration:g}s) does not fit in the window "
                f"[{win_start:g}, {win_end:g}]"
            )
        runs.append(_CaseRun(duration, win_start, win_end, fixed))
        work += duration
    for child in suite.testsuites():
        work += _collect(child, win_start, win_end, starts, runs, constraints)
    constraints.append((work, win_end - win_start, suite.name))
    return work


def _machine_idle_bounds(
    suite: TestSuite, concurrency: int, starts: _CaseStarts
) -> MachineIdleBounds:
    ts_raw = suite._elem.attrib.get("timestamp")
    time_raw = suite._elem.attrib.get("time")
    if ts_raw is None:
        raise JUnitXmlError(
            f"testsuite {suite.name!r} has no timestamp; the rental window "
            "start is unknown"
        )
    if time_raw is None:
        raise JUnitXmlError(
            f"testsuite {suite.name!r} has no time; the rental window length is unknown"
        )
    start = _parse_instant(ts_raw)
    end = start + _parse_duration(time_raw)
    if end < start:
        raise JUnitXmlError(f"testsuite {suite.name!r} has a negative duration")

    runs: list[_CaseRun] = []
    constraints: list[tuple[float, float, str]] = []
    _collect(suite, start, end, starts, runs, constraints)

    # Feasibility: the work of every suite subtree must fit its window.
    for work, length, name in constraints:
        if work > concurrency * length + _EPS:
            raise InfeasibleScheduleError(
                f"testsuite {name!r}: {work:g}s of testcase time cannot be "
                f"scheduled in a {length:g}s window with concurrency "
                f"{concurrency}"
            )
    # Feasibility: cases with known starts must not exceed the concurrency.
    fixed_intervals = [
        (run.fixed_start, run.fixed_start + run.duration)
        for run in runs
        if run.fixed_start is not None
    ]
    fixed_union, max_overlap = _sweep(fixed_intervals)
    if max_overlap > concurrency:
        raise InfeasibleScheduleError(
            f"testsuite {suite.name!r}: {max_overlap} cases with known start "
            f"times overlap, exceeding concurrency {concurrency}"
        )

    total_work = sum(run.duration for run in runs)
    fixed_work = sum(end_ - start_ for start_, end_ in fixed_intervals)
    free_work = total_work - fixed_work
    longest = max((run.duration for run in runs), default=0.0)
    window = end - start

    # Busy time is at least the fixed cases' union, at least total work
    # spread over full concurrency, and at least the longest single case.
    busy_min = max(fixed_union, total_work / concurrency, longest)
    # Busy time is at most the fixed union plus every free case spread out
    # without any overlap, and never more than the rental window itself.
    busy_max = min(window, fixed_union + free_work)

    return MachineIdleBounds(
        suite=suite.name,
        hostname=suite.hostname,
        window=window,
        min_idle=max(0.0, window - busy_max),
        max_idle=max(0.0, window - busy_min),
    )


def idle_bounds(
    obj: JUnitXml | TestSuite,
    concurrency: int = 1,
    case_starts: Mapping | None = None,
) -> IdleBounds:
    """Compute idle-time bounds consistent with the JUnit timing records.

    Args:
        obj: A :class:`JUnitXml` or a single top-level :class:`TestSuite`.
            Each top-level testsuite is one rented machine; nested suites
            share their parent's machine.
        concurrency: Maximum number of cases running simultaneously on one
            machine.  Must be at least 1.
        case_starts: Optional mapping of known case start times.  Keys may
            be :class:`TestCase` objects, ``(classname, name)`` tuples, or
            plain case names; values may be ``datetime`` objects, ISO-8601
            strings, or epoch seconds.  A ``timestamp`` attribute on a
            ``<testcase>`` element is used when no mapping entry matches.

    Returns:
        An :class:`IdleBounds` with total and per-machine idle bounds in
        seconds.

    Raises:
        InfeasibleScheduleError: If the records admit no feasible schedule
            (e.g. cases pinned to a window too narrow for the concurrency).
        JUnitXmlError: If a top-level suite lacks ``timestamp`` or ``time``.
        ValueError: If ``concurrency`` is less than 1.
    """
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be an integer of at least 1")
    if isinstance(obj, JUnitXml):
        suites = list(obj)
    elif isinstance(obj, TestSuite):
        suites = [obj]
    else:
        raise TypeError("expected a JUnitXml or TestSuite object")

    starts = _CaseStarts(case_starts)
    machines = tuple(
        _machine_idle_bounds(suite, concurrency, starts) for suite in suites
    )
    return IdleBounds(
        min_idle=sum(machine.min_idle for machine in machines),
        max_idle=sum(machine.max_idle for machine in machines),
        machines=machines,
    )
