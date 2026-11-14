# -*- coding: utf-8 -*-
"""Retention (down-selection) of sorted bench records as whole rows.

The bench log is a time-sorted table: one row per sampled instant, one column
per signal.  A row may only be kept or dropped as a whole -- it is not allowed
to keep individual cells.  When a row is dropped the value of a signal is
reconstructed from the surrounding kept rows:

* continuous signals are reconstructed by linear interpolation in time
  between the two adjacent kept rows, and must stay within the signal's
  error limit at every dropped row;
* enumerated signals keep carrying the last known value forward, so a kept
  row is required whenever the value changes.

Missing samples and multiplexer branch switches break a signal into
independent segments: interpolation and value carry-forward must never cross
such a break, and the rows at both ends of every segment are mandatory.

The joint dynamic program in :func:`compress_records` minimises the number of
kept *rows*.  Solving each signal separately and merging the two shortest
lists is not equivalent: independently shortest lists may avoid sharing rows
while a different pair of equally short lists shares more rows and therefore
occupies fewer whole rows.  The joint DP optimises the shared row count
directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, SupportsFloat, Tuple, cast

CONTINUOUS = "continuous"
ENUMERATED = "enumerated"

_EPS = 1e-12


def _as_float(value: object) -> float:
    return float(cast(SupportsFloat, value))


@dataclass(frozen=True)
class SignalSpec:
    """Description of one signal column.

    ``kind`` is either :data:`CONTINUOUS` or :data:`ENUMERATED`.
    ``tolerance`` is the maximum absolute interpolation error (only used for
    continuous signals).
    """

    name: str
    kind: str
    tolerance: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in (CONTINUOUS, ENUMERATED):
            raise ValueError("unknown signal kind: %r" % self.kind)
        if self.kind == CONTINUOUS and self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")


def _is_missing(value: object, missing: object) -> bool:
    if value is None:
        return True
    if missing is None:
        return False
    if value is missing or value == missing:
        return True
    if isinstance(value, float) and isinstance(missing, float):
        if math.isnan(value) and math.isnan(missing):
            return True
    return False


def _build_segments(
    values: Sequence[object],
    branch_column: Optional[Sequence[object]],
    missing: object,
) -> Tuple[List[Tuple[int, int]], List[int]]:
    """Split one signal into maximal present, same-branch segments.

    Returns the list of ``(start, end_exclusive)`` index ranges and an array
    mapping every row index to its segment id (``-1`` when the signal has no
    usable value on that row).
    """
    segments: List[Tuple[int, int]] = []
    start: Optional[int] = None
    current_branch: object = None
    for index, value in enumerate(values):
        if _is_missing(value, missing):
            if start is not None:
                segments.append((start, index))
                start = None
            current_branch = None
            continue
        branch = branch_column[index] if branch_column is not None else 0
        if start is None or branch != current_branch:
            if start is not None:
                segments.append((start, index))
            start = index
            current_branch = branch
    if start is not None:
        segments.append((start, len(values)))
    segment_of = [-1] * len(values)
    for segment_id, (seg_start, seg_end) in enumerate(segments):
        for index in range(seg_start, seg_end):
            segment_of[index] = segment_id
    return segments, segment_of


def _segment_scale(values: Sequence[object], start: int, end: int) -> float:
    scale = 1.0
    for index in range(start, end):
        scale = max(scale, abs(_as_float(values[index])))
    return scale


class _Signal:
    """Preprocessed per-signal view used by the dynamic program."""

    def __init__(
        self,
        spec: SignalSpec,
        times: Sequence[float],
        columns: Dict[str, Sequence[object]],
        branches: Optional[Dict[str, Sequence[object]]],
        missing: object,
    ) -> None:
        self.spec = spec
        self.values = columns[spec.name]
        if len(self.values) != len(times):
            raise ValueError("column %r length does not match time axis" % spec.name)
        branch_column = branches.get(spec.name) if branches else None
        self.segments, self.segment_of = _build_segments(
            self.values, branch_column, missing
        )

    def _effective_tolerance(self, start: int, end: int) -> float:
        return self.spec.tolerance + _EPS * _segment_scale(
            self.values, start, end
        )

    def reach_masks(self, times: Sequence[float]) -> List[int]:
        """Bit ``j`` of mask ``i`` is set when edge ``i -> j`` is tolerable.

        Edges crossing a missing-sample or branch-switch break carry no
        constraint for this signal and are therefore set by default; the
        mandatory boundary rows in the DP prevent crossing the break itself.
        Only edges whose endpoints share a segment are restricted here.
        """
        row_count = len(times)
        all_bits = (1 << row_count) - 1
        # Every forward edge starts allowed, including edges that leave or
        # skip segments in which this signal has no usable value.
        masks = [all_bits & ~((1 << (index + 1)) - 1) for index in range(row_count)]
        for start, end in self.segments:
            allowed = [0] * row_count
            if self.spec.kind == ENUMERATED:
                self._enum_masks(start, end, allowed)
            else:
                self._continuous_masks(times, start, end, allowed)
            for earlier in range(start, end):
                within_bits = ((1 << end) - 1) ^ ((1 << (earlier + 1)) - 1)
                masks[earlier] = (masks[earlier] & ~within_bits) | allowed[earlier]
        return masks

    def _enum_masks(
        self,
        start: int,
        end: int,
        masks: List[int],
    ) -> None:
        values = self.values
        for earlier in range(start, end):
            carried = values[earlier]
            mask = 0
            for later in range(earlier + 1, end):
                # The newly interior row is ``later - 1``; it must still
                # equal the carried value.  The later kept row itself may
                # differ -- that is a retained enum change point.
                if values[later - 1] != carried:
                    break
                mask |= 1 << later
            masks[earlier] |= mask

    def _continuous_masks(
        self,
        times: Sequence[float],
        start: int,
        end: int,
        masks: List[int],
    ) -> None:
        values = self.values
        tolerance = self._effective_tolerance(start, end)
        for earlier in range(start, end):
            t_earlier = times[earlier]
            y_earlier = _as_float(values[earlier])
            slope_lo = -math.inf
            slope_hi = math.inf
            mask = 0
            for later in range(earlier + 1, end):
                interior = later - 1
                if interior > earlier:
                    dt_interior = times[interior] - t_earlier
                    y_interior = _as_float(values[interior])
                    if dt_interior == 0:
                        # Equal timestamps: interpolation collapses to the
                        # earlier value, so the interior row must fit on its
                        # own and every longer edge inherits the failure.
                        if abs(y_interior - y_earlier) > tolerance:
                            break
                    else:
                        deviation = y_interior - y_earlier
                        point_lo = (deviation - tolerance) / dt_interior
                        point_hi = (deviation + tolerance) / dt_interior
                        slope_lo = max(slope_lo, point_lo)
                        slope_hi = min(slope_hi, point_hi)
                        if slope_lo > slope_hi:
                            break
                delta_t = times[later] - t_earlier
                if delta_t == 0:
                    # Whole span shares one timestamp; only equal-time
                    # interior checks above apply.
                    mask |= 1 << later
                    continue
                slope = (_as_float(values[later]) - y_earlier) / delta_t
                if slope_lo <= slope <= slope_hi:
                    mask |= 1 << later
            masks[earlier] |= mask


def _combined_reach(signals: Sequence[_Signal], times: Sequence[float]) -> List[int]:
    row_count = len(times)
    full_mask = (1 << row_count) - 1
    reach = [full_mask] * row_count
    for signal in signals:
        signal_masks = signal.reach_masks(times)
        reach = [edges & signal_masks[index] for index, edges in enumerate(reach)]
    return reach


def _path_less(left: int, right: int, parent: Sequence[int]) -> bool:
    """Whether the retained path to ``left`` is lexicographically smaller.

    Tied candidates always have paths of equal length, so the parent chains
    are walked from the shared root and compared element by element.
    """
    if left == right:
        return False
    left_chain: List[int] = []
    right_chain: List[int] = []
    node = left
    while node >= 0:
        left_chain.append(node)
        node = parent[node]
    node = right
    while node >= 0:
        right_chain.append(node)
        node = parent[node]
    left_chain.reverse()
    right_chain.reverse()
    for left_node, right_node in zip(left_chain, right_chain):
        if left_node != right_node:
            return left_node < right_node
    return False


def _run_dp(
    times: Sequence[float],
    signals: Sequence[_Signal],
    mandatory: Sequence[bool],
    prefer_latest: bool,
) -> List[int]:
    row_count = len(times)
    if row_count == 1:
        return [0]

    # Nearest mandatory row at or after each index, so a mandatory boundary
    # row can never be skipped by an edge.
    next_mandatory = [row_count - 1] * row_count
    nearest = row_count - 1
    for index in range(row_count - 1, -1, -1):
        next_mandatory[index] = nearest
        if mandatory[index]:
            nearest = index

    reach = _combined_reach(signals, times)
    infinity = row_count + 1
    dp = [infinity] * row_count
    parent = [-1] * row_count
    dp[0] = 1

    for later in range(1, row_count):
        for earlier in range(later):
            if dp[earlier] == infinity:
                continue
            if not (reach[earlier] & (1 << later)):
                continue
            if next_mandatory[earlier] < later:
                continue
            candidate = dp[earlier] + 1
            if candidate > dp[later]:
                continue
            if candidate < dp[later] or parent[later] < 0:
                dp[later] = candidate
                parent[later] = earlier
            elif candidate == dp[later]:
                smaller = _path_less(earlier, parent[later], parent)
                if (not smaller) if prefer_latest else smaller:
                    parent[later] = earlier

    if parent[-1] < 0:  # pragma: no cover - adjacent rows always connect
        raise RuntimeError("no feasible retention set exists")

    selection: List[int] = []
    node = row_count - 1
    while node >= 0:
        selection.append(node)
        if node == 0:
            break
        node = parent[node]
    selection.reverse()
    return selection


def _prepare(
    times: Sequence[float],
    columns: Dict[str, Sequence[object]],
    specs: Sequence[SignalSpec],
    branches: Optional[Dict[str, Sequence[object]]],
    missing: object,
) -> Tuple[List[_Signal], List[bool]]:
    if not times:
        raise ValueError("at least one record is required")
    for spec in specs:
        if spec.name not in columns:
            raise ValueError("missing column for signal %r" % spec.name)
    signals = [_Signal(spec, times, columns, branches, missing) for spec in specs]
    row_count = len(times)
    mandatory = [False] * row_count
    mandatory[0] = True
    mandatory[-1] = True
    for signal in signals:
        for start, end in signal.segments:
            mandatory[start] = True
            mandatory[end - 1] = True
    return signals, mandatory


def compress_records(
    times: Sequence[float],
    columns: Dict[str, Sequence[object]],
    specs: Sequence[SignalSpec],
    branches: Optional[Dict[str, Sequence[object]]] = None,
    missing: object = None,
    prefer_latest: bool = False,
) -> List[int]:
    """Return the minimum set of rows to keep (zero-based row indices).

    Parameters
    ----------
    times:
        Non-decreasing sampling timestamps, one entry per record.
    columns:
        Signal name to value sequence; missing samples are ``None`` or the
        caller supplied ``missing`` marker.
    specs:
        One :class:`SignalSpec` per signal to honour.
    branches:
        Optional signal name to multiplexer branch id sequence.  A branch
        change breaks that signal's continuity.
    missing:
        Additional value that marks a missing sample.
    prefer_latest:
        When several minimum-size solutions exist, keep the lexicographically
        largest row list (later rows preferred); the default keeps the
        lexicographically smallest list.

    The returned indices are sorted and always include the first and last
    record.  Use :func:`record_numbers` to convert to one-based record
    numbers, and :func:`verify_selection` to validate a selection.
    """
    signals, mandatory = _prepare(times, columns, specs, branches, missing)
    return _run_dp(times, signals, mandatory, prefer_latest)


def record_numbers(selected: Sequence[int]) -> List[int]:
    """Convert zero-based row indices to one-based original record numbers."""
    return [index + 1 for index in selected]


def signal_minimum_sets(
    times: Sequence[float],
    values: Sequence[object],
    spec: SignalSpec,
    branch_column: Optional[Sequence[object]] = None,
    missing: object = None,
) -> Tuple[List[int], List[int]]:
    """Shortest retention sets for a single signal.

    Returns a ``(earliest_preferred, latest_preferred)`` pair of equally
    short index lists, illustrating the ties a signal may have on its own.
    These lists are diagnostic only; merging them across signals does not
    yield the global optimum -- use :func:`compress_records` for that.
    """
    columns = {spec.name: values}
    branches = {spec.name: branch_column} if branch_column is not None else None
    signals, mandatory = _prepare(times, columns, [spec], branches, missing)
    earliest = _run_dp(times, signals, mandatory, False)
    latest = _run_dp(times, signals, mandatory, True)
    return earliest, latest


def verify_selection(
    times: Sequence[float],
    columns: Dict[str, Sequence[object]],
    specs: Sequence[SignalSpec],
    selected: Sequence[int],
    branches: Optional[Dict[str, Sequence[object]]] = None,
    missing: object = None,
) -> Tuple[bool, List[str]]:
    """Check that a selection satisfies every signal's error limit.

    Returns ``(ok, problems)`` where ``problems`` describes every violated
    constraint.  Segment boundary rows must be selected and no interpolation
    may cross a missing-sample or branch-switch break.
    """
    selected_set = set(selected)
    problems: List[str] = []
    row_count = len(times)

    if sorted(selected) != list(selected) or any(
        index < 0 or index >= row_count for index in selected
    ):
        problems.append("selection must be sorted indices inside the record range")
        return False, problems
    if 0 not in selected_set or row_count - 1 not in selected_set:
        problems.append("first and last record must be selected")

    for spec in specs:
        signal = _Signal(spec, times, columns, branches, missing)
        values = signal.values
        for start, end in signal.segments:
            if start not in selected_set or end - 1 not in selected_set:
                problems.append(
                    "signal %r: segment boundary rows %d/%d are not selected"
                    % (spec.name, start, end - 1)
                )
                continue
            anchors = [index for index in selected if start <= index < end]
            for left, right in zip(anchors, anchors[1:]):
                if spec.kind == ENUMERATED:
                    carried = values[left]
                    if any(
                        values[index] != carried
                        for index in range(left + 1, right)
                    ):
                        problems.append(
                            "signal %r: enum value changes between kept rows "
                            "%d and %d without a kept row" % (spec.name, left, right)
                        )
                    continue
                tolerance = signal._effective_tolerance(start, end)
                y_left = _as_float(values[left])
                y_right = _as_float(values[right])
                t_left = times[left]
                t_right = times[right]
                delta_t = t_right - t_left
                for index in range(left + 1, right):
                    if delta_t == 0:
                        reconstructed = y_left
                    else:
                        fraction = (times[index] - t_left) / delta_t
                        reconstructed = y_left + (y_right - y_left) * fraction
                    error = abs(_as_float(values[index]) - reconstructed)
                    if error > tolerance:
                        problems.append(
                            "signal %r: row %d error %.6g exceeds tolerance %g"
                            % (spec.name, index, error, spec.tolerance)
                        )
    return not problems, problems
