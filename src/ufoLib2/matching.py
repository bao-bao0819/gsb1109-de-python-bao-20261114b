from __future__ import annotations

from ufoLib2.objects.contour import Contour
from ufoLib2.objects.glyph import Glyph
from ufoLib2.objects.point import Point

__all__ = ["matchPointIdentifiers"]


def matchPointIdentifiers(old: Glyph, new: Glyph) -> dict[str, Point]:
    """Matches point identifiers of an old glyph revision to points of a new one.

    This is meant for the situation where a quality assurance tool has stored
    the point identifiers of a previous version of a glyph, and a new revision
    of the outline has since lost some of the identifiers, reordered its
    contours and rewritten closed contours to start at a different point.

    Two outlines are considered compatible interpretations of each other when
    one can be turned into the other by only

    - reordering contours,
    - rotating the start point of closed contours (open contours keep their
      fixed start and end points),
    - dropping or adding point/contour identifiers,

    while every point keeps its role within its segment (its ``type``, where
    ``None`` means "offcurve") and identifiers that survived in the new
    revision stay anchored to the same logical points and contours.

    Symmetric outlines can allow several equally valid correspondences. Only
    the points that map to the same new point under *every* valid
    interpretation are reported; the rest is left for the designer to
    disambiguate.

    Args:
        old: The previous revision of the glyph, holding the identifiers that
            were saved by the quality assurance tool.
        new: The new revision of the glyph.

    Returns:
        A dict mapping each confirmable point identifier of ``old`` to the
        :class:`.Point` object of ``new`` that it corresponds to. Points whose
        identifier survived map to the point carrying that same identifier;
        for deleted identifiers the value is the new point that should
        re-receive the old identifier. Identifiers with ambiguous
        correspondences are omitted.

    .. doctest::

        >>> from ufoLib2.objects import Glyph, Contour, Point
        >>> old = Glyph("a", contours=[Contour(points=[
        ...     Point(0, 0, "line", identifier="p0"),
        ...     Point(10, 0, "line", identifier="p1"),
        ...     Point(10, 10, "line", identifier="p2"),
        ...     Point(0, 10, "line", identifier="p3"),
        ... ])])
        >>> # Same square, rotated to start at "p1", identifiers dropped
        >>> # except for one anchor.
        >>> new = Glyph("a", contours=[Contour(points=[
        ...     Point(10, 0, "line", identifier="p1"),
        ...     Point(10, 10, "line"),
        ...     Point(0, 10, "line"),
        ...     Point(0, 0, "line"),
        ... ])])
        >>> mapping = matchPointIdentifiers(old, new)
        >>> sorted(mapping)
        ['p0', 'p1', 'p2', 'p3']
        >>> mapping["p1"] is new.contours[0].points[0]
        True
        >>> mapping["p0"] is new.contours[0].points[3]
        True
    """
    oldContours = list(old.contours)
    newContours = list(new.contours)

    oldPointIds = {
        point.identifier
        for contour in oldContours
        for point in contour.points
        if point.identifier is not None
    }
    newPointIds = {
        point.identifier
        for contour in newContours
        for point in contour.points
        if point.identifier is not None
    }
    oldContourIds = {
        contour.identifier for contour in oldContours if contour.identifier is not None
    }
    newContourIds = {
        contour.identifier for contour in newContours if contour.identifier is not None
    }

    # rotations[i][j] holds the start-point shifts under which old contour i
    # can correspond to new contour j.
    rotations: list[list[list[int]]] = []
    adjacency: list[list[int]] = []
    for oldContour in oldContours:
        rotationRow: list[list[int]] = []
        adjacent: list[int] = []
        for j, newContour in enumerate(newContours):
            valid = _validRotations(
                oldContour,
                newContour,
                oldPointIds,
                newPointIds,
                oldContourIds,
                newContourIds,
            )
            rotationRow.append(valid)
            if valid:
                adjacent.append(j)
        rotations.append(rotationRow)
        adjacency.append(adjacent)

    total = _maximumMatchingSize(adjacency, len(newContours))
    if total == 0:
        return {}

    result: dict[str, Point] = {}
    for i, oldContour in enumerate(oldContours):
        if not any(point.identifier is not None for point in oldContour.points):
            continue
        if _maximumMatchingSize(adjacency, len(newContours), excludedLeft=i) == total:
            # The contour is left unmatched by some valid interpretation, so
            # nothing about its points can be confirmed.
            continue
        # candidates[t] collects every new point (as (contour, index) pairs)
        # that old point t maps to under some valid interpretation.
        candidates: list[set[tuple[int, int]]] = [
            set() for _ in range(len(oldContour.points))
        ]
        for j in adjacency[i]:
            if (
                _maximumMatchingSize(
                    adjacency, len(newContours), excludedLeft=i, excludedRight=j
                )
                != total - 1
            ):
                # No maximum matching pairs old contour i with new contour j.
                continue
            for shift in rotations[i][j]:
                for t in range(len(oldContour.points)):
                    candidates[t].add((j, (t + shift) % len(oldContour.points)))
        for t, point in enumerate(oldContour.points):
            if point.identifier is not None and len(candidates[t]) == 1:
                ((j, k),) = candidates[t]
                result[point.identifier] = newContours[j].points[k]
    return result


def _validRotations(
    oldContour: Contour,
    newContour: Contour,
    oldPointIds: set[str],
    newPointIds: set[str],
    oldContourIds: set[str],
    newContourIds: set[str],
) -> list[int]:
    """Returns the start-point shifts under which two contours can correspond.

    A shift of ``s`` means that point ``t`` of ``oldContour`` corresponds to
    point ``(t + s) % len`` of ``newContour``.
    """
    if (
        oldContour.identifier is not None
        and oldContour.identifier in newContourIds
        and newContour.identifier != oldContour.identifier
    ):
        return []
    if (
        newContour.identifier is not None
        and newContour.identifier in oldContourIds
        and newContour.identifier != oldContour.identifier
    ):
        return []
    count = len(oldContour.points)
    if count != len(newContour.points) or oldContour.open != newContour.open:
        return []
    # Open contours have fixed start and end points and must not be rotated.
    shifts: tuple[int, ...] | range = (0,) if oldContour.open else range(count)
    return [
        shift
        for shift in shifts
        if _rotationValid(oldContour, newContour, shift, oldPointIds, newPointIds)
    ]


def _rotationValid(
    oldContour: Contour,
    newContour: Contour,
    shift: int,
    oldPointIds: set[str],
    newPointIds: set[str],
) -> bool:
    count = len(oldContour.points)
    for t, oldPoint in enumerate(oldContour.points):
        newPoint = newContour.points[(t + shift) % count]
        if oldPoint.type != newPoint.type:
            return False
        oldId = oldPoint.identifier
        newId = newPoint.identifier
        # An identifier that survived in the other revision anchors its point:
        # it may only be paired with the point carrying that same identifier.
        if oldId is not None and oldId in newPointIds and newId != oldId:
            return False
        if newId is not None and newId in oldPointIds and newId != oldId:
            return False
    return True


def _maximumMatchingSize(
    adjacency: list[list[int]],
    rightCount: int,
    excludedLeft: int | None = None,
    excludedRight: int | None = None,
) -> int:
    """Returns the size of a maximum matching in a bipartite graph.

    ``adjacency`` maps each left vertex to the right vertices it may pair
    with. ``excludedLeft`` and ``excludedRight`` optionally remove one vertex
    on each side, which allows testing whether a given edge or vertex takes
    part in some maximum matching.
    """
    matchRight: list[int | None] = [None] * rightCount

    def augment(left: int, seen: list[bool]) -> bool:
        for right in adjacency[left]:
            if seen[right] or right == excludedRight:
                continue
            seen[right] = True
            owner = matchRight[right]
            if owner is None or augment(owner, seen):
                matchRight[right] = left
                return True
        return False

    size = 0
    for left in range(len(adjacency)):
        if left == excludedLeft:
            continue
        if augment(left, [False] * rightCount):
            size += 1
    return size
