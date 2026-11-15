from __future__ import annotations

from ufoLib2.matching import matchPointIdentifiers
from ufoLib2.objects import Contour, Glyph, Point


def _closedContour(types: list[str | None], ids: list[str | None]) -> Contour:
    return Contour(
        points=[
            Point(float(t), float(t), type=type_, identifier=id_)
            for t, (type_, id_) in enumerate(zip(types, ids))
        ]
    )


def _openContour(types: list[str | None], ids: list[str | None]) -> Contour:
    return _closedContour(types, ids)


def _rotate(values: list[str | None], shift: int) -> list[str | None]:
    return values[shift:] + values[:shift]


def test_empty_glyphs() -> None:
    assert matchPointIdentifiers(Glyph("a"), Glyph("a")) == {}


def test_rotated_closed_contour_all_ids_dropped() -> None:
    # Asymmetric type pattern: a single rotation is structurally valid.
    types: list[str | None] = ["line", None, None, "curve", None]
    old_ids = ["a", "b", "c", "d", "e"]
    old = Glyph("a", contours=[_closedContour(types, old_ids)])
    new_types = _rotate(types, 2)
    assert new_types == [None, "curve", None, "line", None]
    new = Glyph("a", contours=[_closedContour(new_types, [None] * 5)])

    mapping = matchPointIdentifiers(old, new)

    new_points = new.contours[0].points
    assert mapping == {
        "a": new_points[3],
        "b": new_points[4],
        "c": new_points[0],
        "d": new_points[1],
        "e": new_points[2],
    }


def test_contour_reordering() -> None:
    line_types: list[str | None] = ["line", "line", "line", "line"]
    curve_types: list[str | None] = ["curve", None, None, "line", "curve", None]
    old = Glyph(
        "a",
        contours=[
            _closedContour(line_types, ["l0", "l1", "l2", "l3"]),
            _closedContour(curve_types, ["c0", "c1", "c2", "c3", "c4", "c5"]),
        ],
    )
    new = Glyph(
        "a",
        contours=[
            _closedContour(curve_types, [None] * 6),
            _closedContour(line_types, [None] * 4),
        ],
    )

    mapping = matchPointIdentifiers(old, new)

    # The all-line contour is symmetric, its points stay ambiguous...
    assert not {"l0", "l1", "l2", "l3"} & mapping.keys()
    # ...but the curve contour has a unique rotation, so it maps fully.
    new_curve_points = new.contours[0].points
    assert {k: mapping[k] for k in ["c0", "c1", "c2", "c3", "c4", "c5"]} == {
        "c0": new_curve_points[0],
        "c1": new_curve_points[1],
        "c2": new_curve_points[2],
        "c3": new_curve_points[3],
        "c4": new_curve_points[4],
        "c5": new_curve_points[5],
    }


def test_surviving_identifiers_anchor_rotation() -> None:
    types: list[str | None] = ["line", "line", "line", "line"]
    old = Glyph("a", contours=[_closedContour(types, ["a", "b", "c", "d"])])
    # Rotated by one, only the identifier of "c" survives.
    new = Glyph(
        "a",
        contours=[_closedContour(_rotate(types, 1), [None, None, None, "c"])],
    )

    mapping = matchPointIdentifiers(old, new)

    new_points = new.contours[0].points
    assert mapping == {
        "a": new_points[1],
        "b": new_points[2],
        "c": new_points[3],
        "d": new_points[0],
    }


def test_symmetric_contour_without_anchors_is_ambiguous() -> None:
    types: list[str | None] = ["line", "line", "line", "line"]
    old = Glyph("a", contours=[_closedContour(types, ["a", "b", "c", "d"])])
    new = Glyph("a", contours=[_closedContour(types, [None] * 4)])

    assert matchPointIdentifiers(old, new) == {}


def test_identical_contours_are_ambiguous_until_anchored() -> None:
    # Asymmetric type pattern, so each pairing allows a single rotation; with
    # two structurally identical contours the pairing itself stays ambiguous.
    types: list[str | None] = ["line", None, "curve"]
    old = Glyph(
        "a",
        contours=[
            _closedContour(types, ["a", "b", "c"]),
            _closedContour(types, ["d", "e", "f"]),
        ],
    )
    new = Glyph(
        "a",
        contours=[
            _closedContour(types, [None] * 3),
            _closedContour(types, [None] * 3),
        ],
    )

    assert matchPointIdentifiers(old, new) == {}

    # One surviving identifier pins down which new contour is which, and with
    # it the rotation inside both; everything else follows.
    new.contours[1].points[1].identifier = "e"
    mapping = matchPointIdentifiers(old, new)

    first, second = new.contours
    assert mapping == {
        "a": first.points[0],
        "b": first.points[1],
        "c": first.points[2],
        "d": second.points[0],
        "e": second.points[1],
        "f": second.points[2],
    }


def test_open_contour_is_not_rotated() -> None:
    # A "move" first point makes the contour open; its ends are fixed.
    types: list[str | None] = ["move", "line", None, None, "curve"]
    old_ids = ["a", "b", "c", "d", "e"]
    old = Glyph("a", contours=[_openContour(types, old_ids)])
    new = Glyph("a", contours=[_openContour(types, [None] * 5)])

    mapping = matchPointIdentifiers(old, new)

    new_points = new.contours[0].points
    assert mapping == {id_: new_points[t] for t, id_ in enumerate(old_ids)}


def test_open_contour_does_not_match_closed_contour() -> None:
    open_types: list[str | None] = ["move", "line", "line"]
    closed_types: list[str | None] = ["line", "line", "line"]
    old = Glyph("a", contours=[_openContour(open_types, ["a", "b", "c"])])
    new = Glyph("a", contours=[_closedContour(closed_types, [None] * 3)])

    assert matchPointIdentifiers(old, new) == {}


def test_conflicting_anchor_removes_interpretation() -> None:
    # The surviving identifier "a" sits on a curve point in the new revision,
    # but "a" was a line point in the old one: no rotation can honor both the
    # type pattern and the anchor, so no correspondence can be confirmed.
    types: list[str | None] = ["line", "curve", "curve"]
    old = Glyph("a", contours=[_closedContour(types, ["a", "b", "c"])])
    new = Glyph("a", contours=[_closedContour(types, [None, "a", None])])

    assert matchPointIdentifiers(old, new) == {}


def test_point_count_mismatch() -> None:
    old = Glyph(
        "a",
        contours=[_closedContour(["line", "line", "line"], ["a", "b", "c"])],
    )
    new = Glyph(
        "a",
        contours=[
            _closedContour(["line", "line", "line", "line"], [None] * 4),
        ],
    )

    assert matchPointIdentifiers(old, new) == {}


def test_contour_identifier_anchors_pairing() -> None:
    old = Glyph(
        "a",
        contours=[
            Contour(
                points=[
                    Point(0, 0, "line", identifier="a"),
                    Point(1, 1, identifier="b"),
                    Point(2, 2, "curve", identifier="c"),
                ],
                identifier="contour1",
            ),
            Contour(
                points=[
                    Point(3, 3, "line", identifier="d"),
                    Point(4, 4, identifier="e"),
                    Point(5, 5, "curve", identifier="f"),
                ],
                identifier="contour2",
            ),
        ],
    )
    # Contours swapped, identifiers kept on contours, dropped on points.
    new = Glyph(
        "a",
        contours=[
            Contour(
                points=[Point(3, 3, "line"), Point(4, 4), Point(5, 5, "curve")],
                identifier="contour2",
            ),
            Contour(
                points=[Point(0, 0, "line"), Point(1, 1), Point(2, 2, "curve")],
                identifier="contour1",
            ),
        ],
    )

    mapping = matchPointIdentifiers(old, new)

    first, second = new.contours
    assert mapping == {
        "a": second.points[0],
        "b": second.points[1],
        "c": second.points[2],
        "d": first.points[0],
        "e": first.points[1],
        "f": first.points[2],
    }


def test_extra_contour_in_new_glyph() -> None:
    types: list[str | None] = ["line", None, None, "curve"]
    old = Glyph("a", contours=[_closedContour(types, ["a", "b", "c", "d"])])
    new = Glyph(
        "a",
        contours=[
            _closedContour(types, [None] * 4),
            _closedContour(["line", "line"], [None, None]),
        ],
    )

    mapping = matchPointIdentifiers(old, new)

    new_points = new.contours[0].points
    assert mapping == {
        "a": new_points[0],
        "b": new_points[1],
        "c": new_points[2],
        "d": new_points[3],
    }
