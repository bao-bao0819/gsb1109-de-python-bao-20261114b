from __future__ import annotations

import pytest

from ufoLib2.objects import Contour, Glyph, Point
from ufoLib2.objects.geometry import (
    ContourMismatch,
    Segment,
    SegmentMatch,
    assert_contours_equivalent,
    contour_segments,
    contours_equivalent,
    explain_contour_equivalence,
    split_curve_half,
)

CUBIC = Segment(
    "curve", ((0.0, 0.0), (10.0, 10.0), (10.0, 20.0), (0.0, 20.0))
)


def _lerp(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def draw_contour(commands: list[tuple], closed: bool = True) -> Contour:
    """Build a Contour from segment-pen commands.

    Commands are ``("M", point)``, ``("L", point)``,
    ``("C", control1, control2, point)`` and optionally ``("Z",)``.
    """
    glyph = Glyph("sample")
    pen = glyph.getPen()
    for command in commands:
        if command[0] == "M":
            pen.moveTo(command[1])
        elif command[0] == "L":
            pen.lineTo(command[1])
        elif command[0] == "C":
            pen.curveTo(command[1], command[2], command[3])
        elif command[0] == "Z":
            pen.closePath()
        else:
            raise AssertionError(f"unknown command {command[0]!r}")
    if closed and commands[-1][0] != "Z":
        pen.closePath()
    if not closed:
        pen.endPath()
    return glyph.contours[0]


def reference_contour() -> Contour:
    # One cubic side plus a straight closing side.
    return draw_contour(
        [
            ("M", CUBIC.points[0]),
            ("C", CUBIC.points[1], CUBIC.points[2], CUBIC.points[3]),
            ("Z",)
        ]
    )


def midpoint_split_contour() -> Contour:
    # The same cubic, split into two exact halves at t = 0.5.
    left, right = split_curve_half(CUBIC)
    return draw_contour(
        [
            ("M", left.start),
            ("C", left.points[1], left.points[2], left.end),
            ("C", right.points[1], right.points[2], right.end),
            ("Z",)
        ]
    )


def test_split_curve_half_is_exact_de_casteljau() -> None:
    left, right = split_curve_half(CUBIC)
    assert left.kind == right.kind == "curve"
    # The two halves join at the shared parametric midpoint.
    assert left.end == right.start
    # Known de Casteljau values for the chosen cubic.
    assert left.points == (
        (0.0, 0.0),
        (5.0, 5.0),
        (7.5, 10.0),
        (7.5, 13.75),
    )
    assert right.points == (
        (7.5, 13.75),
        (7.5, 17.5),
        (5.0, 20.0),
        (0.0, 20.0),
    )


def test_positive_midpoint_split_gives_segment_correspondence() -> None:
    source = reference_contour()
    target = midpoint_split_contour()

    result = explain_contour_equivalence(source, target)
    assert result.equivalent
    assert result.start_index == 0
    assert result.mismatch_index is None

    source_segments = contour_segments(source)
    target_segments = contour_segments(target)
    assert len(source_segments) == 2
    assert len(target_segments) == 3

    # Source cubic 0 is explained by target cubics 0 and 1 in order.
    assert result.matches[0] == SegmentMatch(0, (0, 1))
    # The closing line maps one-to-one.
    assert result.matches[1] == SegmentMatch(1, (2,))

    # The reported target indices really are the two halves of the source
    # segment and join at its parametric midpoint.
    halves = [target_segments[i] for i in result.matches[0].target_indices]
    assert halves[0].end == halves[1].start == split_curve_half(CUBIC)[0].end
    assert contours_equivalent(source, target)
    assert assert_contours_equivalent(source, target) == result.matches


def test_positive_closed_contour_rotated_start_point() -> None:
    source = reference_contour()
    target = midpoint_split_contour()
    # Rotate the target point list so it starts on an off-curve point, i.e.
    # it neither shares the source start point nor its segment ordering.
    points = target.points
    offcurve_index = next(i for i, point in enumerate(points) if point.type is None)
    target.points = points[offcurve_index:] + points[:offcurve_index]
    assert target.points[0].type is None

    result = explain_contour_equivalence(source, target)
    assert result.equivalent
    target_segments = contour_segments(target)
    # The canonical segment list starts on the midpoint on-curve point.
    assert target_segments[result.start_index].start == CUBIC.points[0]
    # Indices refer to the canonical target segment list; the source cubic
    # wraps around it, taking the second half first and the first half last.
    assert result.matches[0].target_indices == (2, 0)
    assert result.matches[1].target_indices == (1,)
    covered = [
        target_segments[i] for i in result.matches[0].target_indices
    ]
    left_half, right_half = split_curve_half(CUBIC)
    assert covered[0].start == left_half.start == CUBIC.start
    assert covered[0].end == covered[1].start == left_half.end
    assert covered[-1].end == right_half.end == CUBIC.end

    # Same geometry, opposite rotation direction of the argument.
    reverse = explain_contour_equivalence(target, source)
    assert reverse.equivalent


def test_positive_recursive_subdivision_four_quarters() -> None:
    left, right = split_curve_half(CUBIC)
    quarters = [*split_curve_half(left), *split_curve_half(right)]
    commands = [("M", quarters[0].start)]
    for quarter in quarters:
        commands.append(
            ("C", quarter.points[1], quarter.points[2], quarter.end)
        )
    commands.append(("Z",))
    target = draw_contour(commands)

    result = explain_contour_equivalence(reference_contour(), target)
    assert result.equivalent
    assert result.matches[0] == SegmentMatch(0, (0, 1, 2, 3))
    assert result.matches[1] == SegmentMatch(1, (4,))


def test_positive_mixed_lines_and_curves() -> None:
    source = draw_contour(
        [
            ("M", (0, 0)),
            ("L", (10, 0)),
            ("C", (20, 0), (20, 10), (10, 10)),
            ("L", (0, 10)),
            ("Z",)
        ]
    )
    left, right = split_curve_half(
        Segment("curve", ((10, 0), (20, 0), (20, 10), (10, 10)))
    )
    target = draw_contour(
        [
            ("M", (0, 0)),
            ("L", (10, 0)),
            ("C", left.points[1], left.points[2], left.end),
            ("C", right.points[1], right.points[2], right.end),
            ("L", (0, 10)),
            ("Z",)
        ]
    )

    result = explain_contour_equivalence(source, target)
    assert result.equivalent
    assert [match.target_indices for match in result.matches] == [
        (0,),
        (1, 2),
        (3,),
        (4,),
    ]


def test_positive_open_contour_split_curve() -> None:
    source = draw_contour(
        [("M", (0, 0)), ("C", (2, 4), (8, 4), (10, 0))], closed=False
    )
    left, right = split_curve_half(
        Segment("curve", ((0, 0), (2, 4), (8, 4), (10, 0)))
    )
    target = draw_contour(
        [
            ("M", left.start),
            ("C", left.points[1], left.points[2], left.end),
            ("C", right.points[1], right.points[2], right.end),
        ],
        closed=False,
    )
    assert source.open and target.open
    result = explain_contour_equivalence(source, target)
    assert result.equivalent
    assert result.matches[0].target_indices == (0, 1)


def test_negative_line_and_curve_through_same_points_are_not_equivalent() -> None:
    # A straight side expressed once as a line and once as a cubic whose
    # control points sit on that same line, so every sampled point coincides.
    line_side = draw_contour(
        [("M", (0, 0)), ("L", (10, 0)), ("L", (10, 10)), ("Z",)]
    )
    curve_side = draw_contour(
        [
            ("M", (0, 0)),
            ("C", (10 / 3, 0.0), (20 / 3, 0.0), (10, 0)),
            ("L", (10, 10)),
            ("Z",)
        ]
    )

    result = explain_contour_equivalence(line_side, curve_side)
    assert not result.equivalent
    assert result.mismatch_index == 0
    assert "line and curve" in (result.reason or "")

    with pytest.raises(ContourMismatch) as exc_info:
        assert_contours_equivalent(line_side, curve_side)
    assert exc_info.value.segment_index == 0

    # The reverse direction must reject it as well.
    assert not contours_equivalent(curve_side, line_side)


def test_negative_first_unexplained_segment_is_localized() -> None:
    source = reference_contour()
    target = midpoint_split_contour()
    # Nudge one control point of the first half beyond any reasonable slack.
    target.points[1].x += 0.5

    result = explain_contour_equivalence(source, target, tolerance=0.01)
    assert not result.equivalent
    assert result.mismatch_index == 0
    assert result.matches == ()

    # A later segment failure still reports the already matched prefix.
    source = draw_contour(
        [
            ("M", (0, 0)),
            ("C", (10, 10), (10, 20), (0, 20)),
            ("C", (0, 30), (10, 30), (10, 20)),
            ("Z",)
        ]
    )
    good_target = draw_contour(
        [
            ("M", (0, 0)),
            ("C", (5, 5), (7.5, 10), (7.5, 13.75)),
            ("C", (7.5, 17.5), (5, 20), (0, 20)),
            ("C", (0, 30), (10, 30), (10, 20)),
            ("Z",)
        ]
    )
    # Nudge an off-curve control of the second (unsplit) cubic only.
    broken_control = next(p for p in good_target.points if (p.x, p.y) == (0, 30))
    broken_control.x += 2.0
    later = explain_contour_equivalence(source, good_target, tolerance=0.01)
    assert not later.equivalent
    assert later.mismatch_index == 1
    assert later.matches == (SegmentMatch(0, (0, 1)),)


def test_tolerance_is_declared_not_hidden_by_sampling() -> None:
    source = reference_contour()
    target = midpoint_split_contour()
    target.points[1].y += 0.05  # 0.05 font unit deviation

    # There is no sampling-density argument: the declared tolerance alone
    # decides whether the deviation is acceptable.
    assert not contours_equivalent(source, target, tolerance=0.01)
    assert contours_equivalent(source, target, tolerance=0.1)

    rejected = explain_contour_equivalence(source, target, tolerance=0.01)
    assert rejected.mismatch_index == 0
    accepted = explain_contour_equivalence(source, target, tolerance=0.1)
    assert accepted.equivalent
    assert accepted.matches[0].target_indices == (0, 1)


def test_negative_extra_and_missing_segments() -> None:
    extended = draw_contour(
        [
            ("M", CUBIC.points[0]),
            ("C", (10, 10), (10, 20), CUBIC.points[3]),
            ("L", (-5, 20)),
            ("Z",)
        ]
    )
    result = explain_contour_equivalence(reference_contour(), extended)
    assert not result.equivalent
    # The source cubic is explained first; the detour breaks the closing line.
    assert result.mismatch_index == 1
    assert result.matches == (SegmentMatch(0, (0,)),)

    # Open contours make the count mismatch unambiguous: one side simply has
    # more or fewer segments after the common prefix.
    short_source = draw_contour(
        [("M", (0, 0)), ("L", (10, 0))], closed=False
    )
    long_target = draw_contour(
        [("M", (0, 0)), ("L", (10, 0)), ("L", (20, 0))], closed=False
    )
    extra = explain_contour_equivalence(short_source, long_target)
    assert not extra.equivalent
    assert extra.mismatch_index == 1
    assert "extra" in (extra.reason or "")

    missing = explain_contour_equivalence(long_target, short_source)
    assert not missing.equivalent
    assert missing.mismatch_index == 1
    assert "missing" in (missing.reason or "")

    shortened = draw_contour([("M", CUBIC.points[0]), ("Z",)])
    result = explain_contour_equivalence(reference_contour(), shortened)
    assert not result.equivalent
    assert result.mismatch_index == 0


def test_negative_closed_contour_without_matching_start() -> None:
    source = reference_contour()
    target = draw_contour(
        [
            ("M", (100, 100)),
            ("C", (110, 110), (110, 120), (100, 120)),
            ("Z",)
        ]
    )
    result = explain_contour_equivalence(source, target)
    assert not result.equivalent
    assert result.mismatch_index == 0


def test_negative_open_contour_cannot_rotate_start() -> None:
    source = draw_contour(
        [("M", (0, 0)), ("C", (2, 4), (8, 4), (10, 0))], closed=False
    )
    target = draw_contour(
        [("M", (5, 0)), ("C", (8, 4), (2, 4), (0, 0))], closed=False
    )
    result = explain_contour_equivalence(source, target)
    assert not result.equivalent
    assert result.mismatch_index == 0


def test_open_vs_closed_contours_are_different() -> None:
    source = draw_contour([("M", (0, 0)), ("L", (10, 0))], closed=False)
    target = draw_contour([("M", (0, 0)), ("L", (10, 0)), ("Z",)])
    result = explain_contour_equivalence(source, target)
    assert not result.equivalent
    assert "open" in (result.reason or "")


def test_empty_contours() -> None:
    empty_a = Contour()
    empty_b = Contour()
    result = explain_contour_equivalence(empty_a, empty_b)
    assert result.equivalent
    assert result.matches == ()

    non_empty = reference_contour()
    assert not explain_contour_equivalence(empty_a, non_empty).equivalent


def test_contour_starting_on_offcurve_point() -> None:
    target = midpoint_split_contour()
    rotated = Contour(
        points=target.points[1:] + [target.points[0]]
    )
    assert rotated.points[0].type is None
    # Segment extraction rotates onto an on-curve start, producing a cyclic
    # rotation of the same canonical segment list.
    canonical = contour_segments(target)
    extracted = contour_segments(rotated)
    assert len(extracted) == len(canonical)
    assert any(
        extracted == canonical[shift:] + canonical[:shift]
        for shift in range(len(canonical))
    )
    assert contours_equivalent(reference_contour(), rotated)


def test_positive_line_split_at_exact_midpoint() -> None:
    source = draw_contour(
        [("M", (0, 0)), ("L", (10, 0)), ("L", (10, 10)), ("Z",)]
    )
    target = draw_contour(
        [
            ("M", (0, 0)),
            ("L", (5, 0)),
            ("L", (10, 0)),
            ("L", (10, 10)),
            ("Z",)
        ]
    )
    result = explain_contour_equivalence(source, target)
    assert result.equivalent
    assert result.matches[0].target_indices == (0, 1)
    assert result.matches[1].target_indices == (2,)
    assert result.matches[2].target_indices == (3,)

    # A line cut anywhere but its midpoint cannot be explained by halving.
    uneven = draw_contour(
        [
            ("M", (0, 0)),
            ("L", (3, 0)),
            ("L", (10, 0)),
            ("L", (10, 10)),
            ("Z",)
        ]
    )
    rejected = explain_contour_equivalence(source, uneven)
    assert not rejected.equivalent
    assert rejected.mismatch_index == 0


def test_negative_split_at_non_midpoint_is_not_accepted() -> None:
    # The target traces the exact same curve but cuts it at t = 1/3, which
    # recursive midpoint subdivision can never reproduce. Sampling would hide
    # this; the segment-level explanation must reject it.
    p0, p1, p2, p3 = CUBIC.points
    t = 1.0 / 3.0
    p01 = _lerp(p0, p1, t)
    p12 = _lerp(p1, p2, t)
    p23 = _lerp(p2, p3, t)
    p012 = _lerp(p01, p12, t)
    p123 = _lerp(p12, p23, t)
    split_point = _lerp(p012, p123, t)
    first = Segment("curve", (p0, p01, p012, split_point))
    second = Segment("curve", (split_point, p123, p23, p3))
    target = draw_contour(
        [
            ("M", first.start),
            ("C", first.points[1], first.points[2], first.end),
            ("C", second.points[1], second.points[2], second.end),
            ("Z",)
        ]
    )

    result = explain_contour_equivalence(reference_contour(), target)
    assert not result.equivalent
    assert result.mismatch_index == 0


def test_negative_reversed_winding_is_not_equivalent() -> None:
    source = draw_contour(
        [
            ("M", (0, 0)),
            ("C", (10, 10), (10, 20), (0, 20)),
            ("L", (0, 0)),
            ("Z",)
        ]
    )
    # Same on-curve points, traversed in the opposite direction.
    reversed_contour = draw_contour(
        [
            ("M", (0, 0)),
            ("L", (0, 20)),
            ("C", (10, 20), (10, 10), (0, 0)),
            ("Z",)
        ]
    )
    result = explain_contour_equivalence(source, reversed_contour)
    assert not result.equivalent
    assert result.mismatch_index == 0


def test_positive_three_levels_of_subdivision() -> None:
    pieces = [CUBIC]
    for _ in range(3):
        pieces = [
            half
            for piece in pieces
            for half in split_curve_half(piece)
        ]
    assert len(pieces) == 8
    commands = [("M", pieces[0].start)]
    for piece in pieces:
        commands.append(("C", piece.points[1], piece.points[2], piece.end))
    commands.append(("Z",))
    target = draw_contour(commands)

    result = explain_contour_equivalence(reference_contour(), target)
    assert result.equivalent
    assert result.matches[0].target_indices == tuple(range(8))


def test_invalid_tolerance_and_point_types() -> None:
    source = reference_contour()
    target = midpoint_split_contour()
    with pytest.raises(ValueError):
        contours_equivalent(source, target, tolerance=-1.0)

    broken = Contour(points=[Point(0, 0, type="qcurve"), Point(1, 1, type="line")])
    with pytest.raises(ValueError):
        contour_segments(broken)
