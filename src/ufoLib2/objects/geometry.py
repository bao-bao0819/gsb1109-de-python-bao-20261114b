"""Geometric equivalence checks for :class:`Contour` objects.

Two UFO feeders may submit the same vector geometry using different point
representations. In particular, one feeder habitually splits every cubic
curve into two halves at its parametric midpoint. Exact point-for-point
comparison then fails even though the outlines are identical.

The tools in this module compare contours *segment by segment* rather than
by sampling points:

* closed contours may be aligned after rotating the starting point;
* a cubic segment may be explained by one or more cubics obtained from
  exact recursive midpoint subdivision (de Casteljau at ``t = 0.5``);
* line and curve segments are never considered interchangeable, even when
  they pass through the same on-curve points;
* the comparison precision is the ``tolerance`` declared by the caller and
  is never relaxed by denser sampling.
"""

from __future__ import annotations

import math
from typing import NamedTuple, TypeAlias

from ufoLib2.errors import Error
from ufoLib2.objects.contour import Contour
from ufoLib2.objects.point import Point

PointCoord: TypeAlias = tuple[float, float]

# Maximum number of recursive midpoint splits attempted while explaining a
# single segment. Guarantees termination on genuinely different data.
MAX_SUBDIVISION_DEPTH = 64


class Segment(NamedTuple):
    """A straight line or a cubic Bezier segment.

    ``kind`` is ``"line"`` with two points (start, end) or ``"curve"`` with
    four points (start, control 1, control 2, end).
    """

    kind: str
    points: tuple[PointCoord, ...]

    @property
    def start(self) -> PointCoord:
        return self.points[0]

    @property
    def end(self) -> PointCoord:
        return self.points[-1]


class SegmentMatch(NamedTuple):
    """How one source segment is explained by target contour segments.

    ``target_indices`` are indices into the canonical segment list of the
    target contour (see :func:`contour_segments`). A single index means a
    direct match; several indices mean the source segment was recursively
    split at its midpoint into that many exact sub-curves.
    """

    source_index: int
    target_indices: tuple[int, ...]

    @property
    def parts(self) -> int:
        return len(self.target_indices)


class ContourEquivalence(NamedTuple):
    """Result of :func:`explain_contour_equivalence`."""

    equivalent: bool
    """Whether the two contours describe the same geometry."""

    start_index: int | None
    """Canonical target segment index where the aligned correspondence begins."""

    matches: tuple[SegmentMatch, ...]
    """Segment-to-segment correspondence, in source segment order."""

    mismatch_index: int | None
    """First source segment that could not be explained, if any."""

    reason: str | None
    """Human readable explanation of the mismatch."""


class ContourMismatch(Error):
    """Raised when two contours are not geometrically equivalent.

    Attributes:
        segment_index: Index of the first source segment that could not be
            explained by the target contour.
        reason: Human readable explanation of the mismatch.
    """

    def __init__(self, segment_index: int, reason: str) -> None:
        self.segment_index = segment_index
        self.reason = reason
        super().__init__(
            f"contours differ at source segment {segment_index}: {reason}"
        )


class _RefinedSegment(NamedTuple):
    segment: Segment
    origin: int
    depth: int = 0


def _coord(point: Point) -> PointCoord:
    return (point.x, point.y)


def contour_segments(contour: Contour) -> tuple[Segment, ...]:
    """Return the line and cubic segments that make up a contour.

    UFO point lists store off-curve points between on-curve points. Closed
    contours may start on an off-curve point, in which case the points are
    rotated so that the first point is on-curve and the final segment closes
    back onto it.
    """
    points = contour.points
    if not points:
        return ()

    unsupported = next(
        (p.type for p in points if p.type not in (None, "move", "line", "curve")),
        None,
    )
    if unsupported is not None:
        raise ValueError(
            f"unsupported point type {unsupported!r}; only line and cubic "
            "curve segments are supported"
        )

    if points[0].type == "move":
        ordered = list(points)
        closed = False
    else:
        first_on_curve = next(
            (i for i, point in enumerate(points) if point.type is not None), None
        )
        if first_on_curve is None:
            raise ValueError("closed contour has no on-curve point")
        ordered = points[first_on_curve:] + points[:first_on_curve]
        closed = True

    segments: list[Segment] = []
    start = _coord(ordered[0])
    offcurves: list[PointCoord] = []
    for point in ordered[1:]:
        if point.type is None:
            offcurves.append(_coord(point))
            continue
        end = _coord(point)
        if point.type == "line":
            if offcurves:
                raise ValueError("line segment has off-curve control points")
            segments.append(Segment("line", (start, end)))
        else:
            if len(offcurves) != 2:
                raise ValueError(
                    "cubic curve segment requires exactly two off-curve points"
                )
            segments.append(
                Segment("curve", (start, offcurves[0], offcurves[1], end))
            )
        start = end
        offcurves = []

    if closed:
        end = _coord(ordered[0])
        if offcurves:
            if len(offcurves) != 2:
                raise ValueError(
                    "cubic curve segment requires exactly two off-curve points"
                )
            segments.append(
                Segment("curve", (start, offcurves[0], offcurves[1], end))
            )
        else:
            segments.append(Segment("line", (start, end)))
    elif offcurves:
        raise ValueError("dangling off-curve points at end of open contour")

    return tuple(segments)


def split_curve_half(segment: Segment) -> tuple[Segment, Segment]:
    """Split a cubic segment into two exact halves at ``t = 0.5``.

    The two returned cubics reproduce the original curve exactly, joined at
    its parametric midpoint.
    """
    if segment.kind != "curve" or len(segment.points) != 4:
        raise ValueError("only cubic curve segments can be split")
    p0, p1, p2, p3 = segment.points
    p01 = _midpoint(p0, p1)
    p12 = _midpoint(p1, p2)
    p23 = _midpoint(p2, p3)
    p012 = _midpoint(p01, p12)
    p123 = _midpoint(p12, p23)
    midpoint = _midpoint(p012, p123)
    left = Segment("curve", (p0, p01, p012, midpoint))
    right = Segment("curve", (midpoint, p123, p23, p3))
    return left, right


def _midpoint(a: PointCoord, b: PointCoord) -> PointCoord:
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def _points_close(a: PointCoord, b: PointCoord, tolerance: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def segments_close(a: Segment, b: Segment, tolerance: float) -> bool:
    """Return whether two segments coincide within the declared tolerance.

    Segments of different kinds never match: a line and a cubic sharing their
    endpoints are not equivalent.
    """
    if a.kind != b.kind or len(a.points) != len(b.points):
        return False
    return all(
        _points_close(pa, pb, tolerance)
        for pa, pb in zip(a.points, b.points)
    )


def _validate_tolerance(tolerance: float) -> None:
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")


def _halve(piece: _RefinedSegment) -> tuple[_RefinedSegment, _RefinedSegment]:
    segment = piece.segment
    if segment.kind == "curve":
        left, right = split_curve_half(segment)
    else:
        start, end = segment.points
        midpoint = _midpoint(start, end)
        left = Segment("line", (start, midpoint))
        right = Segment("line", (midpoint, end))
    return (
        _RefinedSegment(left, piece.origin, piece.depth + 1),
        _RefinedSegment(right, piece.origin, piece.depth + 1),
    )


def _within_box(
    point: PointCoord, box_points: tuple[PointCoord, ...], tolerance: float
) -> bool:
    min_x = min(coord[0] for coord in box_points) - tolerance
    max_x = max(coord[0] for coord in box_points) + tolerance
    min_y = min(coord[1] for coord in box_points) - tolerance
    max_y = max(coord[1] for coord in box_points) + tolerance
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def _point_on_line(
    point: PointCoord, segment: Segment, tolerance: float
) -> bool:
    start, end = segment.points
    if not _within_box(point, segment.points, tolerance):
        return False
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return _points_close(point, start, tolerance)
    cross = abs(
        (point[1] - start[1]) * dx - (point[0] - start[0]) * dy
    )
    return cross / math.sqrt(length_squared) <= tolerance


def _point_on_cubic(
    point: PointCoord, segment: Segment, tolerance: float, depth: int = 0
) -> bool:
    """Whether ``point`` is a node of the cubic's midpoint subdivision tree.

    The convex-hull control box prunes subtrees that cannot contain the
    point; a reachable vertex coincides with one of the exact midpoint nodes
    produced by :func:`split_curve_half`. A point on the curve at a non-dyadic
    parameter (e.g. ``t = 1/3``) is never a node and is rejected once the
    depth bound is reached.
    """
    if depth >= MAX_SUBDIVISION_DEPTH or not _within_box(
        point, segment.points, tolerance
    ):
        return False
    left, right = split_curve_half(segment)
    if _points_close(point, left.end, tolerance):
        return True
    return _point_on_cubic(
        point, left, tolerance, depth + 1
    ) or _point_on_cubic(point, right, tolerance, depth + 1)


def _point_interior_to_segment(
    point: PointCoord, segment: Segment, tolerance: float
) -> bool:
    if _points_close(point, segment.start, tolerance) or _points_close(
        point, segment.end, tolerance
    ):
        return False
    if segment.kind == "line":
        return _point_on_line(point, segment, tolerance)
    return _point_on_cubic(point, segment, tolerance, 0)


def _split_at_node(
    piece: _RefinedSegment, point: PointCoord, tolerance: float, depth: int = 0
) -> tuple[list[_RefinedSegment], list[_RefinedSegment]] | None:
    """Split a segment at a node of its midpoint subdivision tree.

    Returns the sub-segments before and after ``point``, all retaining the
    original segment index. Returns ``None`` when ``point`` is not a midpoint
    node (for a line, only its exact midpoint is a node).
    """
    segment = piece.segment
    if depth >= MAX_SUBDIVISION_DEPTH:
        return None
    if segment.kind == "line":
        midpoint = _midpoint(segment.points[0], segment.points[1])
        if not _points_close(point, midpoint, tolerance):
            return None
        left, right = _halve(piece)
        return [left], [right]

    left, right = _halve(piece)
    if _points_close(point, left.segment.end, tolerance):
        return [left], [right]
    if _within_box(point, left.segment.points, tolerance):
        sub = _split_at_node(left, point, tolerance, depth + 1)
        if sub is None:
            return None
        before, after = sub
        return before, after + [right]
    if _within_box(point, right.segment.points, tolerance):
        sub = _split_at_node(right, point, tolerance, depth + 1)
        if sub is None:
            return None
        before, after = sub
        return [left] + before, after
    return None


def _pairings_to_matches(
    pairings: list[tuple[int, int]],
) -> tuple[SegmentMatch, ...]:
    grouped: dict[int, list[int]] = {}
    for source_index, target_index in pairings:
        indices = grouped.setdefault(source_index, [])
        if not indices or indices[-1] != target_index:
            indices.append(target_index)
    return tuple(
        SegmentMatch(source_index, tuple(indices))
        for source_index, indices in sorted(grouped.items())
    )


def _merge_contours(
    source_segments: tuple[Segment, ...],
    target_pieces: list[_RefinedSegment],
    tolerance: float,
) -> tuple[list[tuple[int, int]], int | None, str | None]:
    """Walk both contours in parallel.

    When one side reaches an interior vertex of the other side's current
    segment, the current segment is split into exact midpoint halves. A
    vertex that is not a recursive midpoint, or a line that would have to be
    split, ends the walk with the first unexplained source segment index.
    """
    source: list[_RefinedSegment] = [
        _RefinedSegment(segment, index)
        for index, segment in enumerate(source_segments)
    ]
    pairings: list[tuple[int, int]] = []

    while source or target_pieces:
        if not source:
            return (
                pairings,
                len(source_segments),
                "target contour has unexplained extra segments",
            )
        if not target_pieces:
            return (
                pairings,
                source[0].origin,
                "target contour is missing segments",
            )

        cur_source = source[0]
        cur_target = target_pieces[0]

        if not _points_close(
            cur_source.segment.start,
            cur_target.segment.start,
            tolerance,
        ):
            return (
                pairings,
                cur_source.origin,
                "no target segment starts at this point",
            )
        if cur_source.segment.kind != cur_target.segment.kind:
            return (
                pairings,
                cur_source.origin,
                "line and curve segments passing through the same points "
                "are not equivalent",
            )

        if _points_close(
            cur_source.segment.end, cur_target.segment.end, tolerance
        ):
            if not segments_close(
                cur_source.segment, cur_target.segment, tolerance
            ):
                return (
                    pairings,
                    cur_source.origin,
                    "segment points differ by more than the declared "
                    "tolerance",
                )
            pairings.append((cur_source.origin, cur_target.origin))
            source.pop(0)
            target_pieces.pop(0)
            continue

        split_source, reason = _can_split_towards(
            cur_source, cur_target.segment.end, tolerance
        )
        if split_source:
            if cur_source.depth >= MAX_SUBDIVISION_DEPTH:
                return (
                    pairings,
                    cur_source.origin,
                    "midpoint subdivision depth limit exceeded",
                )
            source[0:1] = list(_halve(cur_source))
            continue
        if reason is not None:
            return pairings, cur_source.origin, reason

        split_target, reason = _can_split_towards(
            cur_target, cur_source.segment.end, tolerance
        )
        if split_target:
            if cur_target.depth >= MAX_SUBDIVISION_DEPTH:
                return (
                    pairings,
                    cur_source.origin,
                    "midpoint subdivision depth limit exceeded",
                )
            target_pieces[0:1] = list(_halve(cur_target))
            continue
        if reason is not None:
            return pairings, cur_source.origin, reason

        return (
            pairings,
            cur_source.origin,
            "the other contour's vertex is not reachable by midpoint "
            "subdivision, or the segment endpoints diverge",
        )

    return pairings, None, None


def _can_split_towards(
    piece: _RefinedSegment, vertex: PointCoord, tolerance: float
) -> tuple[bool, str | None]:
    """Whether splitting ``piece`` helps reach an interior ``vertex``.

    Returns ``(True, None)`` when the vertex lies inside the piece and is a
    node of its midpoint subdivision tree, ``(False, reason)`` when it lies
    inside but can never be reached, and ``(False, None)`` when it is not on
    the piece at all.
    """
    segment = piece.segment
    if not _point_interior_to_segment(vertex, segment, tolerance):
        return False, None
    if segment.kind == "line":
        midpoint = _midpoint(segment.points[0], segment.points[1])
        if not _points_close(vertex, midpoint, tolerance):
            return False, "a line can only be split at its exact midpoint"
        return True, None
    if not _point_on_cubic(vertex, segment, tolerance):
        return (
            False,
            "the vertex is not a node of this cubic's midpoint "
            "subdivision tree",
        )
    return True, None


def explain_contour_equivalence(
    source: Contour, target: Contour, tolerance: float = 0.0
) -> ContourEquivalence:
    """Compare two contours and explain their segment correspondence.

    Args:
        source: Reference contour.
        target: Contour to explain against ``source``; closed contours may
            choose a different starting point and either side may use
            midpoint-split cubics.
        tolerance: Maximum permitted distance between corresponding points,
            in font units, as declared by the caller. It is never tightened
            or loosened internally; a deviation larger than ``tolerance``
            fails the comparison regardless of sampling density.
    """
    _validate_tolerance(tolerance)
    source_segments = contour_segments(source)
    target_segments = contour_segments(target)

    if source.open != target.open:
        return ContourEquivalence(
            False,
            None,
            (),
            0,
            "one contour is open and the other is closed",
        )
    if not source_segments and not target_segments:
        return ContourEquivalence(True, 0, (), None, None)
    if not source_segments or not target_segments:
        return ContourEquivalence(
            False, None, (), 0, "one contour is empty while the other is not"
        )

    count = len(target_segments)

    def _boundary_rotation(index: int) -> list[_RefinedSegment]:
        ordered = target_segments[index:] + target_segments[:index]
        return [
            _RefinedSegment(segment, (index + offset) % count)
            for offset, segment in enumerate(ordered)
        ]

    if source.open:
        candidates = [(0, _boundary_rotation(0))]
    else:
        source_start = source_segments[0].start
        candidates = []
        for index, segment in enumerate(target_segments):
            if _points_close(segment.start, source_start, tolerance):
                candidates.append((index, _boundary_rotation(index)))
                continue
            # The source start may lie inside a target cubic at one of its
            # recursive midpoint nodes; rotate by splitting that segment there.
            piece = _RefinedSegment(segment, index)
            split = _split_at_node(piece, source_start, tolerance)
            if split is None:
                continue
            before, after = split
            rotated = (
                after
                + [
                    _RefinedSegment(
                        target_segments[other], other
                    )
                    for other in (*range(index + 1, count), *range(0, index))
                ]
                + before
            )
            candidates.append((index, rotated))
        if not candidates:
            return ContourEquivalence(
                False,
                None,
                (),
                0,
                "no target segment contains the source contour's start point",
            )

    best: ContourEquivalence | None = None
    for start_index, rotated_target in candidates:
        pairings, mismatch_index, reason = _merge_contours(
            source_segments, rotated_target, tolerance
        )
        if mismatch_index is None:
            return ContourEquivalence(
                True,
                start_index,
                _pairings_to_matches(pairings),
                None,
                None,
            )
        candidate = ContourEquivalence(
            False,
            None,
            _pairings_to_matches(pairings),
            mismatch_index,
            reason,
        )
        if best is None or (candidate.mismatch_index or 0) > (
            best.mismatch_index or 0
        ):
            best = candidate

    assert best is not None
    return best


def contours_equivalent(
    source: Contour, target: Contour, tolerance: float = 0.0
) -> bool:
    """Return whether two contours describe the same vector geometry.

    See :func:`explain_contour_equivalence` for the matching rules.
    """
    return explain_contour_equivalence(source, target, tolerance).equivalent


def assert_contours_equivalent(
    source: Contour, target: Contour, tolerance: float = 0.0
) -> tuple[SegmentMatch, ...]:
    """Assert two contours are equivalent and return the segment mapping.

    Raises:
        ContourMismatch: With ``segment_index`` pointing at the first source
            segment that the target contour cannot explain.
    """
    result = explain_contour_equivalence(source, target, tolerance)
    if not result.equivalent:
        raise ContourMismatch(
            result.mismatch_index if result.mismatch_index is not None else 0,
            result.reason or "contours differ",
        )
    return result.matches
