"""
Aligning external timelines with PGN move nodes.

A game may be observed through more than one recording.  For example, a web
broadcast and an on-site scoresheet each have their own clock, and the
on-site recorder may have been replaced during the game.  :func:`align`
interprets every recording device with a single fixed offset (fitted once per
machine, never per anchor) plus a bounded drift, and uses the confirmed move
nodes to locate comments that have a timestamp but no move annotation.

Only the order of the confirmed moves is known inside a bracket.  Therefore a
comment between two anchors is located to the full range of moves that could
have been played at that moment, not to a single guessed move.

Basic usage::

    import chess.pgn
    import chess.pgn_timeline as timeline

    game = chess.pgn.read_game(open("game.pgn"))
    nodes = list(game.mainline())
    anchors = [
        timeline.Anchor(nodes[0], time=0.0, times={"broadcast": 12.5, "site": 7.0}),
        timeline.Anchor(nodes[9], time=61.0, times={"broadcast": 75.0, "site": 69.5}),
    ]
    comments = [timeline.Comment("A nice resource", {"broadcast": 40.0})]

    report = timeline.align(game, anchors, comments)
    print(timeline.format_report(report))
"""

from __future__ import annotations

import dataclasses
import math

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import chess.pgn


__all__ = [
    "Anchor",
    "Comment",
    "MoveRange",
    "Segment",
    "AnchorIssue",
    "SegmentFit",
    "CommentPlacement",
    "TimelineReport",
    "align",
    "format_report",
    "node_locator",
]


EPSILON = 1e-9


@dataclasses.dataclass(frozen=True)
class Segment:
    """
    One recording machine on one timeline.

    *start* and *end* delimit the timestamps (inclusive start, exclusive end)
    that belong to this machine.  ``None`` means unbounded.

    *offset* is the fixed difference ``game time - recorded time``.  If it is
    ``None``, exactly one offset is fitted from all anchors assigned to this
    segment.  *drift* is the maximum allowed absolute deviation from that
    fixed offset, in seconds.  A machine is never re-fitted per anchor.
    """

    timeline: str
    start: Optional[float] = None
    end: Optional[float] = None
    offset: Optional[float] = None
    drift: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and not self.start < self.end:
            raise ValueError("segment start must be before segment end")
        if self.drift < 0:
            raise ValueError("segment drift must not be negative")
        if self.offset is not None and not math.isfinite(self.offset):
            raise ValueError("segment offset must be finite")

    def contains(self, timestamp: float) -> bool:
        return ((self.start is None or timestamp >= self.start)
                and (self.end is None or timestamp < self.end))


@dataclasses.dataclass(frozen=True)
class Anchor:
    """
    A move node confirmed by both parties.

    *times* maps a timeline name to the timestamp recorded by that device.
    *time* is the position of the move on the shared game timeline, in
    seconds.  It may be omitted when one timeline is used as the reference
    (see the *reference* argument of :func:`align`); in that case the
    reference timestamp is used.  *error* is the symmetric uncertainty of the
    confirmed move time, in seconds.  The same anchor may be used by several
    timelines.
    """

    node: chess.pgn.GameNode
    times: Mapping[str, float]
    time: Optional[float] = None
    error: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.time is not None and not math.isfinite(self.time):
            raise ValueError("anchor time must be finite")
        if self.error < 0:
            raise ValueError("anchor error must not be negative")
        for name, timestamp in self.times.items():
            if not isinstance(name, str) or not name:
                raise ValueError("timeline names must be non-empty strings")
            if not math.isfinite(timestamp):
                raise ValueError("anchor timestamps must be finite")


@dataclasses.dataclass(frozen=True)
class Comment:
    """
    A comment that has no move annotation.

    *times* contains the timestamps at which the comment appeared in the
    available recordings.  *node* is the place where the comment is stored in
    the original PGN, if any; it is only used for reporting.
    """

    text: str
    times: Mapping[str, float]
    error: float = 0.0
    node: Optional[chess.pgn.GameNode] = None

    def __post_init__(self) -> None:
        if self.error < 0:
            raise ValueError("comment error must not be negative")
        for name, timestamp in self.times.items():
            if not isinstance(name, str) or not name:
                raise ValueError("timeline names must be non-empty strings")
            if not math.isfinite(timestamp):
                raise ValueError("comment timestamps must be finite")


def _move_number(node: chess.pgn.GameNode) -> str:
    if node.move is None:
        return "start"
    number = (node.ply() + 1) // 2
    separator = "." if node.ply() % 2 else "..."
    return f"{number}{separator} {node.san()}"


def node_locator(node: chess.pgn.GameNode) -> str:
    """
    Returns an unambiguous locator for a PGN node.

    The locator contains the PGN move number and SAN of the node, its ply and
    the SAN path from the root, so nodes inside variations can be identified
    without relying on the mainline alone.
    """

    if node.move is None:
        return "start position (ply 0)"

    path = []
    current = node
    while current.move is not None:
        path.append(current.san())
        assert current.parent is not None
        current = current.parent
    path.reverse()

    variation = "" if node.is_mainline() else ", variation"
    return "{} (ply {}{}; path: {})".format(
        _move_number(node), node.ply(), variation, " ".join(path))


@dataclasses.dataclass(frozen=True)
class MoveRange:
    """A contiguous range of mainline move nodes."""

    nodes: Tuple[chess.pgn.GameNode, ...]
    start: int
    end: int

    @property
    def first(self) -> chess.pgn.GameNode:
        return self.nodes[0]

    @property
    def last(self) -> chess.pgn.GameNode:
        return self.nodes[-1]

    @property
    def plies(self) -> Tuple[int, int]:
        return self.first.ply(), self.last.ply()

    @property
    def move_count(self) -> int:
        return self.end - self.start + 1

    def __str__(self) -> str:
        if self.first.move is None:
            return "start position"
        if self.move_count == 1:
            return _move_number(self.first)
        return "{} - {} (plies {}-{}, {} moves)".format(
            _move_number(self.first), _move_number(self.last),
            self.first.ply(), self.last.ply(), self.move_count)


@dataclasses.dataclass(frozen=True)
class AnchorIssue:
    """
    A problem with one anchor or a pair of anchors.

    ``reason`` is one of ``not_mainline``, ``no_times``, ``no_game_time``,
    ``conflicting_times``, ``non_monotonic``, ``no_segment``,
    ``offset_conflict`` or ``outside_offset``.
    """

    anchor: Anchor
    timeline: Optional[str]
    reason: str
    other: Optional[Anchor] = None
    detail: str = ""

    @property
    def locator(self) -> str:
        return node_locator(self.anchor.node)

    @property
    def other_locator(self) -> Optional[str]:
        return node_locator(self.other.node) if self.other else None

    def __str__(self) -> str:
        scope = f"[{self.timeline}] " if self.timeline else ""
        message = scope + self.locator
        if self.other:
            message += " <-> " + node_locator(self.other.node)
        message += ": " + self.reason
        if self.detail:
            message += " (" + self.detail + ")"
        return message


@dataclasses.dataclass
class SegmentFit:
    """Result of fitting one recording machine."""

    segment: Segment
    anchors: List[Anchor]
    feasible: bool
    lower: Optional[float]
    upper: Optional[float]
    offset: Optional[float]
    issues: List[AnchorIssue]
    problem: Optional[str] = None

    @property
    def timeline(self) -> str:
        return self.segment.timeline

    @property
    def drift(self) -> float:
        return self.segment.drift


@dataclasses.dataclass
class CommentPlacement:
    """Possible move ranges for one unannotated comment."""

    comment: Comment
    ranges: Dict[str, MoveRange]
    possible: Optional[MoveRange]
    consistent: Optional[MoveRange]
    issues: List[str]

    def __str__(self) -> str:
        label = self.comment.text or "(empty comment)"
        if self.possible is None:
            return f"{label}: unlocatable"
        return f"{label}: {self.possible}"


@dataclasses.dataclass
class TimelineReport:
    """Complete result of :func:`align`."""

    game: chess.pgn.Game
    segments: List[SegmentFit]
    anchor_issues: List[AnchorIssue]
    comments: List[CommentPlacement]

    @property
    def feasible(self) -> bool:
        """Whether every configured machine could be fitted."""
        return all(segment.feasible for segment in self.segments)

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "segments": [
                {
                    "timeline": fit.timeline,
                    "label": fit.segment.label,
                    "start": fit.segment.start,
                    "end": fit.segment.end,
                    "drift": fit.drift,
                    "offset": fit.offset,
                    "offset_interval": [fit.lower, fit.upper],
                    "feasible": fit.feasible,
                    "anchors": [node_locator(anchor.node) for anchor in fit.anchors],
                    "problem": fit.problem,
                }
                for fit in self.segments
            ],
            "anchor_issues": [
                {
                    "timeline": issue.timeline,
                    "reason": issue.reason,
                    "locator": issue.locator,
                    "other_locator": issue.other_locator,
                    "detail": issue.detail,
                }
                for issue in self.anchor_issues
            ],
            "comments": [
                {
                    "text": placement.comment.text,
                    "original_node": (node_locator(placement.comment.node)
                                      if placement.comment.node else None),
                    "ranges": {
                        name: {
                            "first": _move_number(move_range.first),
                            "last": _move_number(move_range.last),
                            "first_ply": move_range.first.ply(),
                            "last_ply": move_range.last.ply(),
                            "moves": move_range.move_count,
                        }
                        for name, move_range in placement.ranges.items()
                    },
                    "possible": str(placement.possible) if placement.possible else None,
                    "consistent": str(placement.consistent) if placement.consistent else None,
                    "issues": placement.issues,
                }
                for placement in self.comments
            ],
        }


def _timeline_names(anchors: Sequence[Anchor], comments: Sequence[Comment],
                    segments: Sequence[Segment]) -> List[str]:
    names = {segment.timeline for segment in segments}
    for anchor in anchors:
        names.update(anchor.times)
    for comment in comments:
        names.update(comment.times)
    return sorted(names)


def _group_segments(names: Sequence[str], segments: Sequence[Segment]) -> Dict[str, List[Segment]]:
    grouped: Dict[str, List[Segment]] = {name: [] for name in names}
    for segment in segments:
        grouped.setdefault(segment.timeline, []).append(segment)

    for name, timeline_segments in grouped.items():
        if not timeline_segments:
            grouped[name] = [Segment(name)]
            continue

        def sort_key(segment: Segment) -> float:
            return segment.start if segment.start is not None else float("-inf")

        timeline_segments.sort(key=sort_key)
        for previous, segment in zip(timeline_segments, timeline_segments[1:]):
            if previous.end is None:
                raise ValueError(f"overlapping unbounded segments on timeline {name}")
            if segment.start is None:
                raise ValueError(f"ambiguous segment order on timeline {name}")
            if segment.start < previous.end:
                raise ValueError(f"overlapping segments on timeline {name}")

    return grouped


def _segment_for(segments: Sequence[Segment], timestamp: float) -> Optional[Segment]:
    for segment in segments:
        if segment.contains(timestamp):
            return segment
    return None


def _maximum_consistent_subset(
        intervals: Sequence[Tuple[Anchor, float, float]]) -> Tuple[List[Tuple[Anchor, float, float]], List[Tuple[Anchor, float, float]]]:
    """
    Splits intervals into a largest mutually consistent core and the rest.

    All constraints are one-dimensional intervals for the same fixed offset,
    so a core is consistent exactly when the intervals share a common point.
    The point with the largest coverage is found by testing each interval's
    lower endpoint (an optimal point can always be moved to one).
    """

    best_point: Optional[float] = None
    best_indices: List[int] = []

    for _, candidate, _ in intervals:
        indices = [index for index, (_, low, high) in enumerate(intervals)
                   if low - EPSILON <= candidate <= high + EPSILON]
        if len(indices) > len(best_indices):
            best_point = candidate
            best_indices = indices

    assert best_point is not None
    chosen = set(best_indices)
    core = [interval for index, interval in enumerate(intervals) if index in chosen]
    rejected = [interval for index, interval in enumerate(intervals) if index not in chosen]
    return core, rejected


def _fit_segment(segment: Segment, anchors: Sequence[Anchor]) -> SegmentFit:
    issues: List[AnchorIssue] = []

    intervals: List[Tuple[Anchor, float, float]] = []
    for anchor in anchors:
        timestamp = anchor.times[segment.timeline]
        assert anchor.time is not None
        low = anchor.time - anchor.error - timestamp - segment.drift
        high = anchor.time + anchor.error - timestamp + segment.drift
        intervals.append((anchor, low, high))

    if segment.offset is not None:
        offset = segment.offset
        feasible = True
        for anchor, low, high in intervals:
            if offset < low - EPSILON or offset > high + EPSILON:
                feasible = False
                issues.append(AnchorIssue(
                    anchor, segment.timeline, "outside_offset",
                    detail="required offset {:.3f}..{:.3f}s, fixed offset {:.3f}s".format(
                        low, high, offset)))
        return SegmentFit(segment, list(anchors), feasible, offset, offset,
                          offset, issues)

    if not intervals:
        return SegmentFit(segment, list(anchors), False, None, None, None, issues,
                          problem="no anchors assigned to this machine")

    lower = max(interval[1] for interval in intervals)
    upper = min(interval[2] for interval in intervals)
    feasible = lower <= upper + EPSILON

    if feasible:
        offset = (lower + upper) / 2.0
        return SegmentFit(segment, list(anchors), True, lower, upper, offset, issues)

    core, rejected = _maximum_consistent_subset(intervals)
    core_lower = max(interval[1] for interval in core)
    core_upper = min(interval[2] for interval in core)
    detail = "consistent anchors allow offset {:.3f}..{:.3f}s".format(core_lower, core_upper)

    for anchor, _, _ in rejected:
        issues.append(AnchorIssue(
            anchor, segment.timeline, "offset_conflict", detail=detail))

    return SegmentFit(segment, list(anchors), False, None, None, None, issues,
                      problem="anchors require different fixed offsets")


def _coerce_mainline(game: chess.pgn.Game) -> List[chess.pgn.GameNode]:
    # The root represents the position before the first move. It is included so
    # a comment before move 1 is reported as such instead of being attributed
    # to 1. e4.
    return [game] + list(game.mainline())


def _merge_anchors(anchors: Sequence[Anchor],
                   issues: List[AnchorIssue]) -> List[Anchor]:
    """
    Combines anchors that refer to the same node.

    Supplying one anchor per timeline for the same move is natural, so those
    entries are merged instead of silently overwriting each other.
    """

    merged: List[Anchor] = []
    by_node: Dict[int, int] = {}

    for anchor in anchors:
        key = id(anchor.node)
        index = by_node.get(key)
        if index is None:
            by_node[key] = len(merged)
            merged.append(anchor)
            continue

        existing = merged[index]
        times = dict(existing.times)
        conflicting = False
        for name, timestamp in anchor.times.items():
            if name in times and abs(times[name] - timestamp) > EPSILON:
                conflicting = True
            times[name] = timestamp

        time = existing.time if existing.time is not None else anchor.time
        if existing.time is not None and anchor.time is not None:
            tolerance = max(existing.error, anchor.error) + EPSILON
            if abs(existing.time - anchor.time) > tolerance:
                conflicting = True

        if conflicting:
            issues.append(AnchorIssue(
                anchor, None, "conflicting_times", other=existing,
                detail="repeated anchors for the same node disagree"))
            continue

        merged[index] = dataclasses.replace(
            existing, time=time, times=times,
            error=max(existing.error, anchor.error),
            label=existing.label or anchor.label)

    return merged


def _default_reference(anchors: Sequence[Anchor]) -> Optional[str]:
    """
    Picks a timeline that every anchor with a missing game time can supply.

    Games often have no absolute wall clock at all. In that case either
    recording may serve as the reference; the other machine's offset absorbs
    the difference. Only timelines present on all such anchors are usable.
    """

    needed = [anchor for anchor in anchors if anchor.time is None]
    if not needed:
        return None

    candidates: Optional[Set[str]] = None
    for anchor in needed:
        names = set(anchor.times)
        candidates = names if candidates is None else candidates & names

    if not candidates:
        return None
    return sorted(candidates)[0]


def _check_anchors(mainline: Sequence[chess.pgn.GameNode],
                   anchors: Sequence[Anchor],
                   reference: Optional[str]) -> Tuple[List[Anchor], List[AnchorIssue]]:
    positions = {id(node): index for index, node in enumerate(mainline)}
    issues: List[AnchorIssue] = []

    resolved: List[Anchor] = []
    for anchor in _merge_anchors(anchors, issues):
        if not anchor.times:
            issues.append(AnchorIssue(anchor, None, "no_times",
                                      detail="anchor has no recorded timestamps"))
            continue

        time = anchor.time
        if time is None and reference is not None:
            time = anchor.times.get(reference)
        if time is None:
            issues.append(AnchorIssue(anchor, None, "no_game_time",
                                      detail="anchor has no game time and no reference timeline timestamp"))
            continue
        resolved.append(dataclasses.replace(anchor, time=time))

    ordered: List[Tuple[int, Anchor]] = []
    for anchor in resolved:
        position = positions.get(id(anchor.node))
        if position is None:
            issues.append(AnchorIssue(anchor, None, "not_mainline",
                                      detail="anchor is not part of the game mainline"))
            continue
        ordered.append((position, anchor))

    ordered.sort(key=lambda item: item[0])

    effective: List[Anchor] = []
    previous: Optional[Anchor] = None
    for _, anchor in ordered:
        assert anchor.time is not None
        if previous is not None and anchor.time < previous.time - EPSILON:
            issues.append(AnchorIssue(
                anchor, None, "non_monotonic", other=previous,
                detail="game time precedes the previous confirmed move"))
            continue
        effective.append(anchor)
        previous = anchor

    return effective, issues


def _node_windows(mainline: Sequence[chess.pgn.GameNode],
                  anchors: Sequence[Anchor]) -> List[Tuple[float, float]]:
    """
    Returns the game-time interval in which each node could have been played.

    Confirmed nodes get their own ``time +/- error`` interval. Unconfirmed nodes
    between two confirmed nodes share the whole bracket. Without per-move
    timestamps we cannot justify narrowing a comment inside a bracket to any
    particular move, so every move in the bracket gets the same interval.
    """

    anchor_by_node = {id(anchor.node): anchor for anchor in anchors}
    next_boundary: List[Optional[Tuple[float, float]]] = [None] * len(mainline)

    following: Optional[Tuple[float, float]] = None
    for index in range(len(mainline) - 1, -1, -1):
        next_boundary[index] = following
        anchor = anchor_by_node.get(id(mainline[index]))
        if anchor is not None:
            following = (anchor.time - anchor.error, anchor.time + anchor.error)

    windows: List[Tuple[float, float]] = []
    previous_boundary: Optional[Tuple[float, float]] = None

    for index, node in enumerate(mainline):
        anchor = anchor_by_node.get(id(node))
        if anchor is not None:
            own = (anchor.time - anchor.error, anchor.time + anchor.error)
            windows.append(own)
            previous_boundary = own
            continue

        lower = previous_boundary[0] if previous_boundary is not None else float("-inf")
        upper = next_boundary[index][1] if next_boundary[index] is not None else float("inf")
        windows.append((lower, upper))

    return windows


def _locate(mainline: Sequence[chess.pgn.GameNode],
            windows: Sequence[Tuple[float, float]],
            low: float, high: float) -> MoveRange:
    matching = [index for index, (node_low, node_high) in enumerate(windows)
                if node_low <= high + EPSILON and node_high >= low - EPSILON]
    if not matching:
        # Every move is confirmed and the comment falls strictly between two
        # confirmed times. Attribute it to the closest confirmed node instead
        # of inventing an interval. Ties go to the later move: a comment after
        # the last confirmed move is more likely about that move than about an
        # earlier one.
        def distance(window: Tuple[float, float]) -> float:
            return max(window[0] - high, low - window[1])

        nearest = min(range(len(windows)),
                      key=lambda index: (distance(windows[index]), -index))
        matching = [nearest]

    start, end = matching[0], matching[-1]
    return MoveRange(tuple(mainline[start:end + 1]), start, end)


def _union(mainline: Sequence[chess.pgn.GameNode],
           ranges: Sequence[MoveRange]) -> Optional[MoveRange]:
    if not ranges:
        return None
    # All ranges are contiguous slices of the same mainline, so their union is
    # the slice from the smallest start to the largest end. Disjoint ranges
    # remain a single honest interval: every move in between is possible at
    # some time when the comment could have been made.
    start = min(move_range.start for move_range in ranges)
    end = max(move_range.end for move_range in ranges)
    return MoveRange(tuple(mainline[start:end + 1]), start, end)


def _intersection(mainline: Sequence[chess.pgn.GameNode],
                  ranges: Sequence[MoveRange]) -> Optional[MoveRange]:
    if not ranges:
        return None
    start = max(move_range.start for move_range in ranges)
    end = min(move_range.end for move_range in ranges)
    if start > end:
        return None
    return MoveRange(tuple(mainline[start:end + 1]), start, end)


def align(game: chess.pgn.Game,
          anchors: Iterable[Anchor],
          comments: Iterable[Comment] = (),
          *,
          segments: Iterable[Segment] = (),
          reference: Optional[str] = None) -> TimelineReport:
    """
    Fits every recording machine once and locates unannotated comments.

    *anchors* are confirmed move nodes.  Each anchor carries the timestamp
    observed on each timeline.  *segments* describe the recording machines;
    omitted timelines get one unbounded machine with a fitted offset and zero
    drift.

    Each machine gets exactly one offset, fitted from all of its anchors, and
    the given drift is the only additional slack.  Anchors that require
    mutually exclusive offsets, that are outside a fixed offset, that fall
    outside every segment, or that contradict the move order are returned in
    ``anchor_issues`` together with their original PGN node locators.

    If *reference* names a timeline, anchors without an absolute game time use
    that timeline's timestamp, so the game timeline can be expressed relative
    to one known-good recording.

    A comment is located on every timeline whose machine is feasible.  When
    more than one alignment is available, ``possible`` is the union of the
    per-timeline move ranges (moves compatible with at least one alignment)
    and ``consistent`` is their intersection (moves compatible with all of
    them).  ``consistent`` is ``None`` when the alignments disagree.
    """

    anchor_list = list(anchors)
    comment_list = list(comments)
    segment_list = list(segments)

    if reference is None:
        reference = _default_reference(anchor_list)

    mainline = _coerce_mainline(game)
    effective, anchor_issues = _check_anchors(mainline, anchor_list, reference)

    names = _timeline_names(anchor_list, comment_list, segment_list)
    grouped = _group_segments(names, segment_list)

    fits: List[SegmentFit] = []
    fits_by_segment: Dict[Tuple[str, int], SegmentFit] = {}

    for name in names:
        timeline_segments = grouped[name]
        assigned: Dict[int, List[Anchor]] = {id(segment): [] for segment in timeline_segments}

        for anchor in effective:
            timestamp = anchor.times.get(name)
            if timestamp is None:
                continue
            segment = _segment_for(timeline_segments, timestamp)
            if segment is None:
                anchor_issues.append(AnchorIssue(
                    anchor, name, "no_segment",
                    detail="timestamp {:.3f}s is outside every recording segment".format(timestamp)))
                continue
            assigned[id(segment)].append(anchor)

        for segment in timeline_segments:
            fit = _fit_segment(segment, assigned[id(segment)])
            fits.append(fit)
            fits_by_segment[(name, id(segment))] = fit
            anchor_issues.extend(fit.issues)

    windows = _node_windows(mainline, effective)

    placements: List[CommentPlacement] = []
    for comment in comment_list:
        ranges: Dict[str, MoveRange] = {}
        placement_issues: List[str] = []

        for name, timestamp in comment.times.items():
            if not mainline:
                placement_issues.append("game has no moves to locate the comment in")
                break

            timeline_segments = grouped.get(name, [Segment(name)])
            segment = _segment_for(timeline_segments, timestamp)
            if segment is None:
                placement_issues.append(
                    f"{name}: timestamp {timestamp:.3f}s is outside every recording segment")
                continue

            fit = fits_by_segment.get((name, id(segment)))
            if fit is None or not fit.feasible or fit.lower is None or fit.upper is None:
                placement_issues.append(f"{name}: no usable offset for this machine")
                continue

            low = timestamp + fit.lower - fit.drift - comment.error
            high = timestamp + fit.upper + fit.drift + comment.error
            ranges[name] = _locate(mainline, windows, low, high)

        if not mainline:
            placement_issues.append("game has no moves to locate the comment in")
        elif not comment.times:
            placement_issues.append("comment has no recorded timestamps")

        ordered_ranges = list(ranges.values())
        possible = _union(mainline, ordered_ranges)
        if set(ranges) == set(comment.times) and ranges:
            consistent = _intersection(mainline, ordered_ranges)
        else:
            # A timeline that could not be aligned says nothing about which
            # moves are compatible with all recordings.
            consistent = None

        placements.append(CommentPlacement(
            comment, ranges, possible, consistent, placement_issues))

    return TimelineReport(game, fits, anchor_issues, placements)


def format_report(report: TimelineReport) -> str:
    """Formats a report as human-readable text, including PGN locators."""

    lines: List[str] = []

    for fit in report.segments:
        segment = fit.segment
        label = f" {segment.label!r}" if segment.label else ""
        scope = "{}..{}".format(
            "start" if segment.start is None else f"{segment.start:.3f}",
            "end" if segment.end is None else f"{segment.end:.3f}")
        if fit.lower is None or fit.upper is None:
            offset_text = "offset unavailable"
        elif segment.offset is not None:
            offset_text = f"fixed offset {fit.offset:.3f}s"
        else:
            offset_text = "fitted offset {:.3f}s (allowed {:.3f}..{:.3f}s)".format(
                fit.offset, fit.lower, fit.upper)
        lines.append("timeline {}{} [{}]: {}, drift +/-{:.3f}s, {}".format(
            fit.timeline, label, scope, offset_text, fit.drift,
            "feasible" if fit.feasible else "INFEASIBLE"))
        if fit.problem:
            lines.append("  problem: " + fit.problem)

    if report.anchor_issues:
        lines.append("anchor issues:")
        for issue in report.anchor_issues:
            lines.append("  - " + str(issue))

    if report.comments:
        lines.append("comments:")
        for placement in report.comments:
            lines.append("  - " + str(placement))
            for name, move_range in placement.ranges.items():
                lines.append(f"      {name}: {move_range}")
            if placement.consistent is not None and len(placement.ranges) > 1:
                lines.append(f"      consistent: {placement.consistent}")
            for message in placement.issues:
                lines.append("      issue: " + message)

    return "\n".join(lines)
