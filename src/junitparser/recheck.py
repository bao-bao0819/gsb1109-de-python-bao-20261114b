"""Worst-case recheck planning from historical JUnit results.

The planner answers a narrow question: given a fixed build window, a number of
parallel device slots, mandatory prerequisite cases and historical per-case
durations, which failing cases should be re-run so that the resulting table
still fits when every case takes the *upper bound* of its recorded duration?

Design rules:

* Only the upper bound of a case's duration interval is used for any decision.
  A mean duration is never consulted.  A case without a recorded upper bound is
  treated as unbounded and cannot be scheduled.
* Selecting a case also selects its whole prerequisite closure.  A case never
  starts before all of its prerequisites finish.
* Each selected case occupies one of ``slots`` parallel lanes.  A case may also
  require a named device; that device can only serve one case at a time.
* Failure-class credit is deduplicated: if two selected entries belong to the
  same class, that class contributes its weight once.

The selector is a greedy maximum-coverage heuristic with a feasibility check
after every addition.  It is not claimed to be optimal, but every returned
selection comes with a concrete schedule that has been proven to fit the
window.
"""

from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Sequence

from .junitparser import Error, Failure, JUnitXml

DEFAULT_WINDOW = 12 * 60.0
"""The default build window in seconds (twelve minutes)."""

UNCLASSIFIED = "unclassified"

_EPSILON = 1e-9
_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_MIN_KEYS = ("duration_min", "time_min", "min_time")
_MAX_KEYS = ("duration_max", "time_max", "max_time")
_CLASS_KEYS = ("failure_class", "failure_classes", "failure_classification")
_DEVICE_KEYS = ("device", "device_slot", "dedicated_device")
_REQUIRED_KEYS = ("required", "mandatory", "must_run")
_PREREQUISITE_KEYS = ("depends_on", "prerequisites", "requires", "after")
_SPLIT_RE = re.compile(r"[,;|\s]+")


class RecheckError(Exception):
    """Base error for invalid recheck planning input."""


class UnknownCaseError(RecheckError):
    """A prerequisite or required case is not present in the history."""


class PrerequisiteCycleError(RecheckError):
    """The prerequisite graph contains a cycle."""


class InvalidDurationError(RecheckError):
    """A duration record cannot be interpreted."""


@dataclass
class CaseRecord:
    """Historical facts about one case.

    ``low`` and ``high`` are the observed duration interval in seconds.
    ``high`` is ``None`` when the history never recorded a duration; such a
    case is *unbounded* and is never scheduled, because doing so would require
    assuming an average.
    """

    case_id: str
    name: str = ""
    classname: str = ""
    low: float | None = None
    high: float | None = None
    samples: list[float] = field(default_factory=list)
    classes: set[str] = field(default_factory=set)
    device: str | None = None
    required: bool = False
    prerequisites: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def merge(self, other: "CaseRecord") -> None:
        if other.samples:
            self.samples.extend(other.samples)
            other_low = min(other.samples)
            other_high = max(other.samples)
            self.low = other_low if self.low is None else min(self.low, other_low)
            self.high = other_high if self.high is None else max(self.high, other_high)
        elif other.high is not None:
            self.low = (
                other.low if self.low is None else min(self.low, other.low or 0.0)
            )
            self.high = other.high if self.high is None else max(self.high, other.high)
        self.classes.update(other.classes)
        if other.device:
            self.device = other.device
        self.required = self.required or other.required
        for prerequisite in other.prerequisites:
            if prerequisite not in self.prerequisites:
                self.prerequisites.append(prerequisite)
        for source in other.sources:
            if source not in self.sources:
                self.sources.append(source)


@dataclass(frozen=True)
class PlanEntry:
    """One row of the recheck table."""

    case_id: str
    lane: int
    start: float
    end: float
    high: float
    device: str | None
    classes: tuple[str, ...]
    mandatory: bool
    credited_classes: tuple[str, ...] = ()
    credit: float = 0.0


@dataclass(frozen=True)
class PlanResult:
    """Outcome of one planning run."""

    feasible: bool
    window: float
    slots: int
    entries: tuple[PlanEntry, ...]
    mandatory: tuple[str, ...]
    covered: Mapping[str, float]
    total_weight: float
    makespan: float
    slack: float
    bottleneck: str | None
    evidence: tuple[str, ...]
    unbounded: tuple[str, ...]
    rejected: tuple[str, ...]
    detail: str = ""
    certified: bool = True


# ---------------------------------------------------------------------------
# History loading
# ---------------------------------------------------------------------------


def case_id(classname: str | None, name: str | None) -> str:
    """Canonical case identifier used throughout the planner."""
    classname = (classname or "").strip()
    name = (name or "").strip()
    if classname and name:
        return f"{classname}::{name}"
    return name or classname


def _parse_seconds(text: str, *, context: str) -> float:
    value = text.strip()
    if value.endswith(("s", "S")):
        value = value[:-1].strip()
    value = value.replace(",", "")
    try:
        seconds = float(value)
    except ValueError as exc:  # pragma: no cover - defensive, covered by tests
        raise InvalidDurationError(
            f"{context}: cannot parse duration {text!r}"
        ) from exc
    if seconds < 0 or not math.isfinite(seconds):
        raise InvalidDurationError(f"{context}: invalid duration {text!r}")
    return seconds


def _properties(case) -> dict[str, str]:
    result: dict[str, str] = {}
    for prop in case._elem.findall("properties/property"):
        name = prop.attrib.get("name")
        if name:
            result[name.strip()] = prop.attrib.get("value", "")
    return result


def _split_references(value: str) -> list[str]:
    return [part for part in _SPLIT_RE.split(value) if part]


def _record_from_case(case, source: str) -> CaseRecord:
    identifier = case_id(case.classname, case.name)
    record = CaseRecord(
        case_id=identifier,
        name=case.name or "",
        classname=case.classname or "",
        sources=[source],
    )

    props = _properties(case)

    explicit_min = next((props[k] for k in _MIN_KEYS if k in props), None)
    explicit_max = next((props[k] for k in _MAX_KEYS if k in props), None)

    duration = case.time
    if duration is not None:
        record.samples.append(duration)
        record.low = duration
        record.high = duration
    if explicit_min is not None:
        low = _parse_seconds(explicit_min, context=f"{identifier} {_MIN_KEYS[0]}")
        record.low = low if record.low is None else min(record.low, low)
    if explicit_max is not None:
        high = _parse_seconds(explicit_max, context=f"{identifier} {_MAX_KEYS[0]}")
        record.high = high if record.high is None else max(record.high, high)
    if record.low is not None and record.high is not None and record.low > record.high:
        record.low, record.high = record.high, record.low

    for result in case.result:
        if isinstance(result, (Failure, Error)):
            failure_class = (result.type or "").strip()
            record.classes.add(failure_class or UNCLASSIFIED)
    explicit_classes: set[str] = set()
    for key in _CLASS_KEYS:
        if key in props and props[key].strip():
            explicit_classes.update(_split_references(props[key]))
    if explicit_classes:
        record.classes = explicit_classes

    for key in _DEVICE_KEYS:
        if props.get(key, "").strip():
            record.device = props[key].strip()
            break

    for key in _REQUIRED_KEYS:
        if props.get(key, "").strip().lower() in _TRUE_VALUES:
            record.required = True
            break

    for key in _PREREQUISITE_KEYS:
        if props.get(key, "").strip():
            record.prerequisites.extend(_split_references(props[key]))

    return record


def load_history(
    paths: Iterable[str | object],
    *,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, CaseRecord]:
    """Read historical JUnit XML files into per-case records.

    Multiple records for the same ``classname::name`` are merged: the duration
    interval becomes ``[min(samples), max(samples)]`` and failure classes are
    unioned.  Records may also declare ``duration_min``/``duration_max``
    properties explicitly.
    """
    records: dict[str, CaseRecord] = {}
    for path in paths:
        xml = JUnitXml.fromfile(path)
        source = str(path)
        for suite in xml:
            for case in suite:
                record = _record_from_case(case, source)
                if record.case_id in records:
                    records[record.case_id].merge(record)
                else:
                    records[record.case_id] = record

    for reference, values in (overrides or {}).items():
        record = records.get(reference)
        if record is None:
            matches = [
                item
                for item, candidate in records.items()
                if candidate.name == reference
            ]
            if len(matches) == 1:
                record = records[matches[0]]
            else:
                record = CaseRecord(case_id=reference, name=reference)
                records[reference] = record
        _apply_override(record, values)
    return {record.case_id: record for record in records.values()}


def _apply_override(record: CaseRecord, values: Mapping[str, object]) -> None:
    if "high" in values or "duration_max" in values:
        raw = values.get("high", values.get("duration_max"))
        record.high = float(raw)  # type: ignore[arg-type]
    if "low" in values or "duration_min" in values:
        raw = values.get("low", values.get("duration_min"))
        record.low = float(raw)  # type: ignore[arg-type]
    if record.low is None:
        record.low = record.high
    if record.high is not None and record.low is not None and record.low > record.high:
        record.low, record.high = record.high, record.low
    if "classes" in values:
        raw_classes = values["classes"]
        if isinstance(raw_classes, str):
            record.classes = set(_split_references(raw_classes))
        else:
            record.classes = {str(item).strip() for item in raw_classes}  # type: ignore[union-attr]
        record.classes.discard("")
    if "device" in values:
        device = values["device"]
        record.device = str(device) if device else None
    if "required" in values:
        record.required = bool(values["required"])
    for key in ("prerequisites", "depends_on"):
        if key in values:
            raw_prerequisites = values[key]
            if isinstance(raw_prerequisites, str):
                refs = _split_references(raw_prerequisites)
            else:
                refs = [str(item).strip() for item in raw_prerequisites]  # type: ignore[union-attr]
            for ref in refs:
                if ref and ref not in record.prerequisites:
                    record.prerequisites.append(ref)


# ---------------------------------------------------------------------------
# Graph preparation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Task:
    case_id: str
    high: float
    device: str | None
    classes: tuple[str, ...]
    prerequisites: tuple[str, ...]


def _resolve(reference: str, records: Mapping[str, CaseRecord]) -> str:
    reference = reference.strip()
    if reference in records:
        return reference
    if "::" in reference:
        raise UnknownCaseError(f"unknown case {reference!r}")
    matches = [
        identifier for identifier, record in records.items() if record.name == reference
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UnknownCaseError(f"unknown case {reference!r}")
    raise UnknownCaseError(
        f"ambiguous case {reference!r}; matches: {', '.join(sorted(matches))}"
    )


def _prepare(
    records: Mapping[str, CaseRecord],
    *,
    required: Sequence[str],
    prerequisites: Mapping[str, Sequence[str]],
    devices: Mapping[str, str],
) -> tuple[dict[str, _Task], set[str]]:
    extra_required: set[str] = set()

    def add_extra(reference: str) -> None:
        extra_required.add(_resolve(reference, records))

    for reference in required:
        add_extra(reference)
    for record in records.values():
        if record.required:
            add_extra(record.case_id)

    resolved_prerequisites: dict[str, list[str]] = {
        identifier: [] for identifier in records
    }
    for record in records.values():
        target = _resolve(record.case_id, records)
        for reference in record.prerequisites:
            prerequisite = _resolve(reference, records)
            if prerequisite not in resolved_prerequisites[target]:
                resolved_prerequisites[target].append(prerequisite)
    for reference, dependencies in prerequisites.items():
        target = _resolve(reference, records)
        for dependency in dependencies:
            prerequisite = _resolve(dependency, records)
            if prerequisite not in resolved_prerequisites[target]:
                resolved_prerequisites[target].append(prerequisite)
    resolved_devices = {
        _resolve(reference, records): device
        for reference, device in devices.items()
        if device
    }
    for record in records.values():
        if record.device:
            resolved_devices.setdefault(record.case_id, record.device)

    for identifier in extra_required:
        records[identifier].required = True

    tasks: dict[str, _Task] = {}
    for identifier, record in records.items():
        tasks[identifier] = _Task(
            case_id=identifier,
            high=record.high if record.high is not None else math.inf,
            device=resolved_devices.get(identifier),
            classes=tuple(sorted(record.classes)),
            prerequisites=tuple(sorted(resolved_prerequisites[identifier])),
        )
    _check_cycles(tasks)
    return tasks, extra_required


def _children(tasks: Mapping[str, _Task], ids: Iterable[str]) -> dict[str, list[str]]:
    id_set = set(ids)
    children: dict[str, list[str]] = {identifier: [] for identifier in id_set}
    for identifier in id_set:
        for prerequisite in tasks[identifier].prerequisites:
            if prerequisite in id_set:
                children[prerequisite].append(identifier)
    for value in children.values():
        value.sort()
    return children


def _topological(tasks: Mapping[str, _Task], ids: Iterable[str]) -> list[str]:
    id_set = set(ids)
    children = _children(tasks, id_set)
    indegree = {
        identifier: sum(
            1
            for prerequisite in tasks[identifier].prerequisites
            if prerequisite in id_set
        )
        for identifier in id_set
    }
    ready = sorted(identifier for identifier in id_set if indegree[identifier] == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    if len(order) != len(id_set):
        remaining = sorted(id_set - set(order))
        raise PrerequisiteCycleError(
            "prerequisite cycle involving: " + ", ".join(remaining)
        )
    return order


def _check_cycles(tasks: Mapping[str, _Task]) -> None:
    _topological(tasks, tasks.keys())


def _closure(tasks: Mapping[str, _Task], seeds: Iterable[str]) -> set[str]:
    selected: set[str] = set()
    stack = list(seeds)
    while stack:
        identifier = stack.pop()
        if identifier in selected:
            continue
        selected.add(identifier)
        stack.extend(tasks[identifier].prerequisites)
    return selected


def _longest_paths(
    tasks: Mapping[str, _Task], ids: Iterable[str]
) -> tuple[dict[str, float], dict[str, str | None]]:
    """Longest path to a sink using upper-bound durations."""
    id_set = set(ids)
    order = _topological(tasks, id_set)
    children = _children(tasks, id_set)
    distance: dict[str, float] = {}
    successor: dict[str, str | None] = {}
    for identifier in reversed(order):
        best = 0.0
        best_child: str | None = None
        for child in children[identifier]:
            if distance[child] > best:
                best = distance[child]
                best_child = child
        distance[identifier] = tasks[identifier].high + best
        successor[identifier] = best_child
    return distance, successor


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _earliest(intervals: list[tuple[float, float]], earliest: float, duration: float):
    current = earliest
    for start, end in intervals:
        if end <= current:
            continue
        if current + duration <= start + _EPSILON:
            return current
        current = max(current, end)
    return current


def _place(
    lane: list[tuple[float, float]],
    device: list[tuple[float, float]] | None,
    base: float,
    duration: float,
) -> float:
    """Earliest start satisfying both a lane and an optional device."""
    start = _earliest(lane, base, duration)
    if device is None:
        return start
    for _ in range(len(device) + 2):
        device_start = _earliest(device, start, duration)
        if device_start <= start + _EPSILON:
            return start
        rediscovered = _earliest(lane, device_start, duration)
        if rediscovered <= device_start + _EPSILON:
            return device_start
        start = rediscovered
    return max(start, _earliest(device, start, duration))


def _insert(intervals: list[tuple[float, float]], start: float, end: float) -> None:
    bisect.insort(intervals, (start, end))


@dataclass(frozen=True)
class _Schedule:
    entries: tuple[PlanEntry, ...]
    makespan: float


def _schedule(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    slots: int,
    rule: str,
) -> _Schedule:
    id_set = set(ids)
    if slots < 1:
        raise RecheckError("slots must be at least 1")
    if not id_set:
        return _Schedule(entries=(), makespan=0.0)

    order = _topological(tasks, id_set)
    distance, _ = _longest_paths(tasks, id_set)
    del order

    device_load: dict[str, float] = {}
    for identifier in id_set:
        device = tasks[identifier].device
        if device:
            device_load[device] = device_load.get(device, 0.0) + tasks[identifier].high

    lanes: list[list[tuple[float, float]]] = [[] for _ in range(slots)]
    devices: dict[str, list[tuple[float, float]]] = {}
    finishes: dict[str, float] = {}
    unscheduled = set(id_set)
    entries: list[PlanEntry] = []

    def priority(identifier: str):
        task = tasks[identifier]
        remaining = distance[identifier]
        if rule == "longest":
            return (-task.high, -remaining, identifier)
        if rule == "device":
            return (
                task.device is None,
                -device_load.get(task.device or "", 0.0),
                -remaining,
                identifier,
            )
        if rule == "id":
            return (identifier,)
        return (-remaining, -task.high, identifier)

    while unscheduled:
        ready = [
            identifier
            for identifier in unscheduled
            if all(
                prerequisite in finishes
                for prerequisite in tasks[identifier].prerequisites
                if prerequisite in id_set
            )
        ]
        if not ready:  # pragma: no cover - cycles are rejected earlier
            raise PrerequisiteCycleError("prerequisite cycle detected while scheduling")
        current = min(ready, key=priority)
        task = tasks[current]

        base = max(
            (
                finishes[prerequisite]
                for prerequisite in task.prerequisites
                if prerequisite in id_set
            ),
            default=0.0,
        )

        if task.device is None:
            choices = [
                (_earliest(lanes[index], base, task.high), index)
                for index in range(slots)
            ]
            start, lane_index = min(choices)
            device_intervals = None
        else:
            device_intervals = devices.setdefault(task.device, [])
            probe = base
            for _ in range(len(device_intervals) + 2):
                device_start = _earliest(device_intervals, probe, task.high)
                choices = [
                    (_earliest(lanes[index], device_start, task.high), index)
                    for index in range(slots)
                ]
                lane_start, lane_index = min(choices)
                confirmed = _earliest(device_intervals, lane_start, task.high)
                if confirmed <= lane_start + _EPSILON:
                    start = lane_start
                    break
                probe = confirmed
            else:  # pragma: no cover - defensive
                raise RecheckError("could not place case on its device")

        end = start + task.high
        _insert(lanes[lane_index], start, end)
        if device_intervals is not None:
            _insert(device_intervals, start, end)

        finishes[current] = end
        entries.append(
            PlanEntry(
                case_id=current,
                lane=lane_index,
                start=start,
                end=end,
                high=task.high,
                device=task.device,
                classes=task.classes,
                mandatory=False,
            )
        )
        unscheduled.remove(current)

    entries.sort(key=lambda entry: (entry.start, entry.lane, entry.case_id))
    makespan = max((entry.end for entry in entries), default=0.0)
    return _Schedule(entries=tuple(entries), makespan=makespan)


_RULES = ("critical", "longest", "device", "id")


def _fits(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    window: float,
    slots: int,
    node_limit: int = 200_000,
    solution: list | None = None,
) -> bool | None:
    """Exact feasibility check by branch and bound.

    Tasks are placed at the earliest start that is free both in their lane and
    on their dedicated device (if any).  Left-shifting a task within a fixed
    lane/device order never delays another task, so this is complete; every
    lane is explored and identical lanes are explored once.

    Returns ``True`` if a fitting schedule was found, ``False`` only when the
    search space was exhausted, and ``None`` if the node limit was reached (in
    which case infeasibility is *not* proven).
    """
    id_set = set(ids)
    if not id_set:
        return True

    order = _topological(tasks, id_set)
    position = {identifier: index for index, identifier in enumerate(order)}
    children = _children(tasks, id_set)
    for identifiers in children.values():
        identifiers.sort(key=lambda item: (-tasks[item].high, position[item]))
    roots = tuple(
        sorted(
            (
                identifier
                for identifier in id_set
                if not any(
                    prerequisite in id_set
                    for prerequisite in tasks[identifier].prerequisites
                )
            ),
            key=lambda item: (-tasks[item].high, position[item]),
        )
    )

    start_lanes: tuple[tuple[tuple[float, float], ...], ...] = tuple(
        () for _ in range(slots)
    )
    seen: set[tuple] = set()
    nodes = 0

    def search(
        ready: tuple[str, ...],
        lanes: tuple[tuple[tuple[float, float], ...], ...],
        devices: tuple[tuple[str, tuple[tuple[float, float], ...]], ...],
        finishes: tuple[tuple[str, float], ...],
    ) -> bool | None:
        nonlocal nodes
        if not ready:
            return True
        nodes += 1
        if nodes > node_limit:
            return None

        key = (frozenset(ready), lanes, devices, finishes)
        if key in seen:
            return False
        seen.add(key)
        finish_map = dict(finishes)
        device_map = dict(devices)

        exhausted = True
        for identifier in ready:
            task = tasks[identifier]
            base = max(
                (
                    finish_map[prerequisite]
                    for prerequisite in task.prerequisites
                    if prerequisite in id_set
                ),
                default=0.0,
            )
            device_intervals = device_map.get(task.device) if task.device else None

            visited_lanes: set[tuple[tuple[float, float], ...]] = set()
            for lane_index in sorted(
                range(slots),
                key=lambda index: (lanes[index][-1][1] if lanes[index] else 0.0, index),
            ):
                lane = lanes[lane_index]
                if lane in visited_lanes:
                    continue
                visited_lanes.add(lane)

                start = _place(
                    list(lane), list(device_intervals or ()), base, task.high
                )
                end = start + task.high
                if end > window + _EPSILON:
                    continue

                updated_lane = tuple(sorted(lane + ((start, end),)))
                new_lanes = (
                    lanes[:lane_index] + (updated_lane,) + lanes[lane_index + 1 :]
                )
                new_devices = dict(device_map)
                if task.device:
                    new_devices[task.device] = tuple(
                        sorted((device_intervals or ()) + ((start, end),))
                    )
                new_finishes = dict(finish_map)
                new_finishes[identifier] = end

                newly_ready = tuple(
                    child
                    for child in children[identifier]
                    if all(
                        prerequisite in new_finishes
                        for prerequisite in tasks[child].prerequisites
                        if prerequisite in id_set
                    )
                )
                outcome = search(
                    tuple(item for item in ready if item != identifier) + newly_ready,
                    new_lanes,
                    tuple(sorted(new_devices.items())),
                    tuple(sorted(new_finishes.items())),
                )
                if outcome is True:
                    if solution is not None:
                        solution.append((identifier, lane_index, start, end))
                    return True
                if outcome is None:
                    exhausted = False
        return False if exhausted else None

    return search(roots, start_lanes, (), ())


def _exact_schedule(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    window: float,
    slots: int,
    node_limit: int = 50_000,
) -> tuple[bool | None, _Schedule | None]:
    """Try to find a fitting schedule exactly; report proof status.

    Returns ``(True, schedule)`` when a schedule was found,
    ``(False, None)`` when no schedule exists, and ``(None, None)`` when the
    search was cut off before a conclusion.
    """
    placements: list[tuple[str, int, float, float]] = []
    status = _fits(
        tasks,
        ids,
        window=window,
        slots=slots,
        node_limit=node_limit,
        solution=placements,
    )
    if status is not True:
        return status, None
    entries = tuple(
        sorted(
            (
                PlanEntry(
                    case_id=identifier,
                    lane=lane,
                    start=start,
                    end=end,
                    high=tasks[identifier].high,
                    device=tasks[identifier].device,
                    classes=tasks[identifier].classes,
                    mandatory=False,
                )
                for identifier, lane, start, end in placements
            ),
            key=lambda entry: (entry.start, entry.lane, entry.case_id),
        )
    )
    makespan = max((entry.end for entry in entries), default=0.0)
    return True, _Schedule(entries=entries, makespan=makespan)


def _lower_bound_infeasible(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    window: float,
    slots: int,
) -> bool:
    """Cheap proof that no schedule can fit, using necessary conditions."""
    id_set = set(ids)
    if not id_set:
        return False
    distance, _ = _longest_paths(tasks, id_set)
    if max(distance.values()) > window + _EPSILON:
        return True
    total = sum(tasks[identifier].high for identifier in id_set)
    if total > slots * window + _EPSILON:
        return True
    device_load: dict[str, float] = {}
    for identifier in id_set:
        device = tasks[identifier].device
        if device:
            device_load[device] = device_load.get(device, 0.0) + tasks[identifier].high
    return any(load > window + _EPSILON for load in device_load.values())


def _try_schedule(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    window: float,
    slots: int,
    exact: bool = True,
    node_limit: int = 50_000,
) -> tuple[_Schedule | None, bool]:
    """Find a fitting schedule if one exists.

    Returns ``(schedule, proven)``.  ``schedule`` is ``None`` when nothing was
    found; ``proven`` then says whether the absence is a proof (necessary
    bounds or an exhausted exact search) or merely the heuristic's opinion.
    """
    id_set = set(ids)
    heuristic = _best_schedule(tasks, id_set, slots=slots)
    if heuristic.makespan <= window + _EPSILON:
        return heuristic, True
    if _lower_bound_infeasible(tasks, id_set, window=window, slots=slots):
        return None, True
    if not exact:
        return None, False
    status, exact_schedule = _exact_schedule(
        tasks, id_set, window=window, slots=slots, node_limit=node_limit
    )
    if exact_schedule is not None:
        return exact_schedule, True
    return None, status is False


def _best_schedule(
    tasks: Mapping[str, _Task], ids: Iterable[str], *, slots: int
) -> _Schedule:
    id_set = set(ids)
    if not id_set:
        return _Schedule(entries=(), makespan=0.0)
    schedules = [_schedule(tasks, id_set, slots=slots, rule=rule) for rule in _RULES]
    return min(schedules, key=lambda schedule: schedule.makespan)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def _diagnose(
    tasks: Mapping[str, _Task],
    ids: Iterable[str],
    *,
    window: float,
    slots: int,
) -> tuple[str | None, list[str], bool]:
    """Identify the constraint that makes a fixed task set infeasible.

    Necessary lower bounds (longest prerequisite chain, dedicated-device load,
    total slot capacity) are checked first because they are provable.  When
    none of them binds, the constraints are relaxed one at a time and each
    relaxation is checked with the exact solver; whichever relaxation restores
    a fitting schedule is reported as the practical blocker.

    The last return value says whether infeasibility is *proven*.
    """
    id_set = set(ids)
    if not id_set:
        return None, [], True

    distance, successor = _longest_paths(tasks, id_set)
    path_start = max(id_set, key=lambda identifier: (distance[identifier], identifier))
    critical = distance[path_start]
    evidence: list[str] = []
    bottleneck: str | None = None

    if critical > window + _EPSILON:
        path = [path_start]
        while successor[path[-1]] is not None:
            path.append(successor[path[-1]])  # type: ignore[arg-type]
        evidence.append(
            "prerequisite chain {} = {:.3f}s > window {:.3f}s".format(
                " -> ".join(path), critical, window
            )
        )
        bottleneck = "prerequisite_chain"

    device_load: dict[str, float] = {}
    for identifier in id_set:
        device = tasks[identifier].device
        if device:
            device_load[device] = device_load.get(device, 0.0) + tasks[identifier].high
    if device_load:
        device, load = max(device_load.items(), key=lambda item: (item[1], item[0]))
        if load > window + _EPSILON:
            evidence.append(
                f"dedicated device {device!r} mandatory load = {load:.3f}s "
                f"> window {window:.3f}s"
            )
            bottleneck = bottleneck or "dedicated_device"

    total = sum(tasks[identifier].high for identifier in id_set)
    capacity = slots * window
    if total > capacity + _EPSILON:
        evidence.append(
            f"mandatory load {total:.3f}s > {slots} slot(s) x {window:.3f}s "
            f"= {capacity:.3f}s"
        )
        bottleneck = bottleneck or "slot_capacity"

    if bottleneck is not None:
        return bottleneck, evidence, True

    if _fits(tasks, id_set, window=window, slots=slots, node_limit=50_000) is True:
        return None, [], True

    without_devices = {
        identifier: replace(task, device=None)
        for identifier, task in tasks.items()
        if identifier in id_set
    }
    without_prerequisites = {
        identifier: replace(task, prerequisites=())
        for identifier, task in tasks.items()
        if identifier in id_set
    }
    fits_without_devices = (
        _fits(without_devices, id_set, window=window, slots=slots, node_limit=50_000)
        is True
    )
    fits_without_prerequisites = (
        _fits(
            without_prerequisites, id_set, window=window, slots=slots, node_limit=50_000
        )
        is True
    )

    if fits_without_devices and not fits_without_prerequisites:
        evidence.append(
            "relaxing dedicated-device exclusivity yields a fitting schedule; "
            "the device occupancy is the blocker"
        )
        bottleneck = "dedicated_device"
    elif fits_without_prerequisites and not fits_without_devices:
        evidence.append(
            "relaxing prerequisite ordering yields a fitting schedule; "
            "the prerequisite chain is the blocker"
        )
        bottleneck = "prerequisite_chain"
    elif fits_without_devices and fits_without_prerequisites:
        evidence.append(
            "both relaxing device exclusivity and relaxing prerequisite ordering "
            "yield fitting schedules; the two constraints interact"
        )
        bottleneck = "prerequisite_chain+dedicated_device"
    else:
        evidence.append(
            "no necessary lower bound exceeds the window and relaxing either "
            "constraint still does not fit; the mandatory mix exceeds slot capacity"
        )
        bottleneck = "slot_capacity"

    proven = all(
        _fits(candidate, id_set, window=window, slots=slots, node_limit=50_000)
        is not None
        for candidate in (tasks, without_devices, without_prerequisites)
    )
    return bottleneck, evidence, proven


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _weight_of(classes: Iterable[str], weights: Mapping[str, float], default: float):
    return sum(weights.get(failure_class, default) for failure_class in classes)


def _greedy(
    tasks: Mapping[str, _Task],
    selected: set[str],
    candidates: Sequence[str],
    *,
    weights: Mapping[str, float],
    default_weight: float,
    window: float,
    slots: int,
    strategy: str,
    max_trials: int = 12,
) -> set[str]:
    selected = set(selected)

    def covered_classes(chosen: Iterable[str]) -> set[str]:
        covered: set[str] = set()
        for identifier in chosen:
            covered.update(tasks[identifier].classes)
        return covered

    covered = covered_classes(selected)
    while True:
        proposals = []
        for identifier in candidates:
            if identifier in selected:
                continue
            addition = _closure(tasks, [identifier]) - selected
            if any(math.isinf(tasks[item].high) for item in addition):
                continue
            new_classes = {
                failure_class
                for item in addition
                for failure_class in tasks[item].classes
                if failure_class not in covered
            }
            gain = _weight_of(new_classes, weights, default_weight)
            if gain <= 0:
                continue
            cost = sum(tasks[item].high for item in addition)
            cost = max(cost, _EPSILON)
            density = gain / cost
            if strategy == "value":
                key = (-gain, cost, identifier)
            elif strategy == "cost":
                key = (cost, -gain, identifier)
            else:
                key = (-density, -gain, cost, identifier)
            proposals.append((key, identifier, addition))

        proposals.sort(key=lambda item: item[0])
        chosen: tuple[str, set[str]] | None = None
        for _, identifier, addition in proposals[:max_trials]:
            trial = selected | addition
            schedule, _ = _try_schedule(
                tasks, trial, window=window, slots=slots, exact=False
            )
            if schedule is not None:
                chosen = (identifier, addition)
                break
        if chosen is None:
            break
        selected |= chosen[1]
        covered = covered_classes(selected)
    return selected


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_recheck(
    paths: Iterable[str | object],
    *,
    window: float = DEFAULT_WINDOW,
    slots: int = 1,
    weights: Mapping[str, float] | None = None,
    default_weight: float = 1.0,
    required: Sequence[str] = (),
    prerequisites: Mapping[str, Sequence[str]] | None = None,
    devices: Mapping[str, str] | None = None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> PlanResult:
    """Build the worst-case recheck table.

    Args:
        paths: Historical JUnit XML files.
        window: Build window in seconds.
        slots: Number of parallel lanes/slots.
        weights: Weight per failure class.  Unlisted classes use
            ``default_weight``.
        default_weight: Weight for classes absent from ``weights``.
        required: Cases that must run even if they add no coverage.
        prerequisites: Extra prerequisite edges, ``{case: [case, ...]}``.
        devices: Extra dedicated-device assignments, ``{case: device}``.
        overrides: Synthetic or corrected case data, merged after history.
    """
    if slots < 1:
        raise RecheckError("slots must be at least 1")
    if window <= 0 or not math.isfinite(window):
        raise RecheckError("window must be a positive number of seconds")

    weights = dict(weights or {})
    for failure_class, weight in weights.items():
        if not math.isfinite(weight) or weight < 0:
            raise RecheckError(
                f"weight for class {failure_class!r} must be finite and non-negative"
            )
    if not math.isfinite(default_weight) or default_weight < 0:
        raise RecheckError("default_weight must be finite and non-negative")
    records = load_history(paths, overrides=overrides)
    tasks, explicit_required = _prepare(
        records,
        required=required,
        prerequisites=prerequisites or {},
        devices=devices or {},
    )

    required_ids = _closure(tasks, explicit_required)
    unbounded = tuple(
        sorted(
            identifier for identifier, task in tasks.items() if math.isinf(task.high)
        )
    )

    mandatory_unbounded = sorted(
        identifier for identifier in required_ids if math.isinf(tasks[identifier].high)
    )
    if mandatory_unbounded:
        evidence = tuple(
            f"case {identifier!r} has no duration upper bound; "
            "refusing to substitute an average"
            for identifier in mandatory_unbounded
        )
        return PlanResult(
            feasible=False,
            window=window,
            slots=slots,
            entries=(),
            mandatory=tuple(sorted(required_ids)),
            covered={},
            total_weight=0.0,
            makespan=0.0,
            slack=0.0,
            bottleneck="unknown_duration",
            evidence=evidence,
            unbounded=unbounded,
            rejected=(),
            detail="cannot certify the mandatory plan without worst-case durations",
        )

    base_bottleneck, base_evidence, proven = _diagnose(
        tasks, required_ids, window=window, slots=slots
    )
    base_schedule, base_proven = _try_schedule(
        tasks,
        required_ids,
        window=window,
        slots=slots,
        exact=True,
        node_limit=20_000 if len(required_ids) > 30 else 100_000,
    )
    proven = proven and base_proven
    if base_schedule is None:
        base_schedule = _best_schedule(tasks, required_ids, slots=slots)
    if base_bottleneck is not None or base_schedule.makespan > window + _EPSILON:
        bottleneck = base_bottleneck or "scheduling_conflict"
        evidence = list(base_evidence)
        if base_schedule.makespan > window + _EPSILON:
            if base_proven:
                evidence.append(
                    "exact search proved that no schedule of the mandatory cases "
                    f"fits {window:.3f}s with {slots} slot(s); best heuristic "
                    f"makespan {base_schedule.makespan:.3f}s"
                )
            else:
                evidence.append(
                    "no heuristic schedule fits; the exact search hit its node "
                    "limit, so this infeasibility is not proven"
                )
        return PlanResult(
            feasible=False,
            window=window,
            slots=slots,
            entries=(),
            mandatory=tuple(sorted(required_ids)),
            covered={},
            total_weight=0.0,
            makespan=base_schedule.makespan,
            slack=window - base_schedule.makespan,
            bottleneck=bottleneck,
            evidence=tuple(evidence),
            unbounded=unbounded,
            rejected=(),
            detail="the mandatory cases alone do not fit the window",
            certified=proven,
        )

    candidate_ids = sorted(
        identifier
        for identifier, task in tasks.items()
        if identifier not in required_ids and task.classes and not math.isinf(task.high)
    )

    best_selection = set(required_ids)
    best_weight = -math.inf
    for strategy in ("density", "value", "cost"):
        selection = _greedy(
            tasks,
            required_ids,
            candidate_ids,
            weights=weights,
            default_weight=default_weight,
            window=window,
            slots=slots,
            strategy=strategy,
        )
        covered = {
            failure_class
            for identifier in selection
            for failure_class in tasks[identifier].classes
        }
        total = _weight_of(covered, weights, default_weight)
        if total > best_weight + _EPSILON:
            best_weight = total
            best_selection = selection

    schedule, _ = _try_schedule(
        tasks, best_selection, window=window, slots=slots, exact=False
    )
    if schedule is None:  # pragma: no cover - selection only admits fitting trials
        raise RecheckError("selected plan does not fit the window")

    entries: list[PlanEntry] = []
    credited: set[str] = set()
    covered_weights: dict[str, float] = {}
    # Mandatory rows claim their classes first: they run anyway, so their
    # credit should not be stolen by an optional row that covers the same class.
    ordered = sorted(
        schedule.entries,
        key=lambda entry: (
            entry.case_id not in required_ids,
            entry.start,
            entry.lane,
            entry.case_id,
        ),
    )
    for entry in ordered:
        new_classes = tuple(
            failure_class
            for failure_class in entry.classes
            if failure_class not in credited
        )
        credited.update(new_classes)
        credit = _weight_of(new_classes, weights, default_weight)
        for failure_class in new_classes:
            covered_weights[failure_class] = weights.get(failure_class, default_weight)
        entries.append(
            replace(
                entry,
                mandatory=entry.case_id in required_ids,
                credited_classes=new_classes,
                credit=credit,
            )
        )
    entries.sort(key=lambda entry: (entry.start, entry.lane, entry.case_id))

    makespan = schedule.makespan
    selected_ids = {entry.case_id for entry in entries}
    rejected = tuple(
        identifier for identifier in candidate_ids if identifier not in selected_ids
    )
    return PlanResult(
        feasible=True,
        window=window,
        slots=slots,
        entries=tuple(entries),
        mandatory=tuple(sorted(required_ids)),
        covered=dict(sorted(covered_weights.items())),
        total_weight=sum(covered_weights.values()),
        makespan=makespan,
        slack=window - makespan,
        bottleneck=None,
        evidence=(),
        unbounded=unbounded,
        rejected=rejected,
        detail="all durations use their recorded upper bound",
    )


def parse_duration(text: str) -> float:
    """Parse ``"90s"``, ``"12m"``, ``"250ms"`` or plain seconds into a float."""
    value = text.strip().lower()
    for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if value.endswith(suffix):
            return _parse_seconds(value[: -len(suffix)], context="duration") * factor
    return _parse_seconds(value, context="duration")


def load_spec(path: str | object) -> dict:
    """Load planning options from a JSON spec file.

    The spec is a JSON object with any of ``window`` (seconds, or a string such
    as ``"12m"``), ``slots``, ``weights``, ``default_weight``, ``required``,
    ``prerequisites``, ``devices`` and ``overrides``.
    """
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict):
        raise RecheckError(f"{path}: spec must be a JSON object")
    if isinstance(spec.get("window"), str):
        spec["window"] = parse_duration(spec["window"])
    return spec


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_plan(result: PlanResult) -> str:
    """Render a plan as a human-readable table."""
    lines: list[str] = []
    header = f"Recheck plan: window {result.window:.3f}s, {result.slots} slot(s)"
    lines.append(header)
    if not result.feasible:
        lines.append(f"INFEASIBLE: {result.detail}")
        if result.bottleneck:
            lines.append(f"Bottleneck: {result.bottleneck}")
        for item in result.evidence:
            lines.append(f"  - {item}")
        if result.mandatory:
            lines.append("Mandatory cases: " + ", ".join(result.mandatory))
        if result.unbounded:
            lines.append("Unbounded cases: " + ", ".join(result.unbounded))
        if not result.certified:
            lines.append(
                "Not certified: no exhaustive proof was possible for this input."
            )
        return "\n".join(lines)

    lines.append(
        "Fit: makespan {:.3f}s, slack {:.3f}s, covered weight {:.3f}".format(
            result.makespan, result.slack, result.total_weight
        )
    )
    if result.covered:
        lines.append("Covered classes:")
        for failure_class, weight in result.covered.items():
            lines.append(f"  {failure_class:<28} {weight:.3f}")
    lines.append("")
    lines.append(
        f"{'start':>8} {'end':>8} {'slot':>4}  {'case':<44} "
        f"{'high':>8}  {'credit':>6}  classes"
    )
    for entry in result.entries:
        classes = ", ".join(entry.classes) if entry.classes else "-"
        marker = " (required)" if entry.mandatory else ""
        lines.append(
            f"{entry.start:>8.3f} {entry.end:>8.3f} {entry.lane:>4}  "
            f"{entry.case_id + marker:<44} {entry.high:>8.3f}  "
            f"{entry.credit:>6.3f}  {classes}"
        )
    if result.rejected:
        shown = ", ".join(result.rejected[:12])
        if len(result.rejected) > 12:
            shown += f", ... (+{len(result.rejected) - 12})"
        lines.append("")
        lines.append(f"Not selected ({len(result.rejected)}): {shown}")
    if result.unbounded:
        lines.append(
            "Unbounded, never scheduled (no duration upper bound): "
            + ", ".join(result.unbounded)
        )
    if not result.certified:
        lines.append(
            "Note: the shortfall was not proven by exhaustive search; treat it "
            "as a heuristic result."
        )
    return "\n".join(lines)


def plan_as_dict(result: PlanResult) -> dict:
    """Serialise a plan to JSON-compatible data."""
    return {
        "feasible": result.feasible,
        "window": result.window,
        "slots": result.slots,
        "makespan": result.makespan,
        "slack": result.slack,
        "total_weight": result.total_weight,
        "bottleneck": result.bottleneck,
        "certified": result.certified,
        "detail": result.detail,
        "evidence": list(result.evidence),
        "mandatory": list(result.mandatory),
        "covered": dict(result.covered),
        "unbounded": list(result.unbounded),
        "rejected": list(result.rejected),
        "entries": [
            {
                "case": entry.case_id,
                "slot": entry.lane,
                "start": entry.start,
                "end": entry.end,
                "upper_bound": entry.high,
                "device": entry.device,
                "classes": list(entry.classes),
                "credited_classes": list(entry.credited_classes),
                "credit": entry.credit,
                "mandatory": entry.mandatory,
            }
            for entry in result.entries
        ],
    }


def plan_as_json(result: PlanResult) -> str:
    """Serialise a plan to a JSON string."""
    return json.dumps(plan_as_dict(result), indent=2, sort_keys=False)
