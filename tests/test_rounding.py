from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest
from fontTools.misc.transform import Transform

from ufoLib2.errors import CircularReferenceError
from ufoLib2.objects import Component, Contour, Font, Glyph, Layer, Point
from ufoLib2.rounding import (
    GlyphRoundingReport,
    LayerRoundingReport,
    PointError,
    analyze_layer_rounding,
    export_rounding_report,
)


def _base_glyph(name: str = "a") -> Glyph:
    glyph = Glyph(name)
    # (1.25, 0) scales to 2.5 under the 500 -> 1000 (x2) conversion used below,
    # i.e. a half-unit value whose rounding is sensitive to when it happens.
    glyph.contours.append(
        Contour([Point(1.25, 0, "line"), Point(0, 0, "line")])
    )
    return glyph


def _font_with_base() -> Font:
    font = Font()
    font.info.unitsPerEm = 500
    font.layers.defaultLayer["a"] = _base_glyph()
    return font


def test_validate_upm_rejects_bad_values() -> None:
    layer = Layer(glyphs=[_base_glyph()])
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            analyze_layer_rounding(layer, 1000, bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            analyze_layer_rounding(layer, bad, 1000)  # type: ignore[arg-type]


def test_export_requires_source_upm() -> None:
    font = Font()
    font.layers.defaultLayer["a"] = _base_glyph()
    assert font.info.unitsPerEm is None
    with pytest.raises(ValueError, match="source_upm"):
        export_rounding_report(font, 1000)


def test_export_coerces_whole_number_font_upm() -> None:
    font = _font_with_base()
    font.info.unitsPerEm = 500.0  # UFO info types unitsPerEm as float
    report = export_rounding_report(font, 1000)
    assert report.sourceUPM == 500
    assert isinstance(report.sourceUPM, int)

    font.info.unitsPerEm = 500.5
    with pytest.raises(ValueError, match="whole number"):
        export_rounding_report(font, 1000)


def test_plain_glyph_pipelines_coincide() -> None:
    report = analyze_layer_rounding(Layer(glyphs=[_base_glyph()]), 1000, 500)
    glyph_report = report.glyphReports[0]
    assert glyph_report.glyphName == "a"
    assert glyph_report.isComposite is False
    assert glyph_report.pointCount == 2
    assert glyph_report.maxPointOffset == 0.0
    assert glyph_report.passes is True
    point = glyph_report.points[0]
    assert point.componentChain == ("a",)
    assert (point.sourceX, point.sourceY) == (1.25, 0)
    assert point.exactX == pytest.approx(2.5)
    assert point.directRoundedX == point.localRoundedX == 3
    assert point.deviation == 0.0


def test_scaled_component_amplifies_local_rounding() -> None:
    font = _font_with_base()
    big = Glyph("big")
    big.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    font.layers.defaultLayer["big"] = big

    report = analyze_layer_rounding(
        font.layers.defaultLayer, 1000, 500, tolerance=0.5
    )
    by_name = {g.glyphName: g for g in report.glyphReports}

    # The base glyph rounds cleanly on its own.
    assert by_name["a"].maxPointOffset == 0.0
    assert by_name["a"].passes is True
    # ... but the same point, magnified 2x through the component, drifts a unit.
    assert by_name["big"].isComposite is True
    assert by_name["big"].maxPointOffset == pytest.approx(1.0)
    assert by_name["big"].passes is False

    worst = by_name["big"].worstPoint
    assert worst is not None
    assert worst.sourceGlyph == "a"
    assert worst.componentChain == ("big", "a")
    assert worst.exactX == pytest.approx(5.0)
    assert worst.directRoundedX == 5
    assert worst.localRoundedX == 6
    assert (worst.deltaX, worst.deltaY) == (1, 0)


def test_base_pass_does_not_cover_instances() -> None:
    font = _font_with_base()
    layer = font.layers.defaultLayer

    passing = Glyph("compPass")
    passing.components.append(Component("a", Transform(1, 0, 0, 1, 4, 4)))
    layer["compPass"] = passing

    failing = Glyph("compFail")
    failing.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    layer["compFail"] = failing

    report = analyze_layer_rounding(layer, 1000, 500, tolerance=0.5)
    by_name = {g.glyphName: g for g in report.glyphReports}

    assert by_name["a"].passes is True
    assert by_name["compPass"].passes is True
    assert by_name["compFail"].passes is False
    assert [g.glyphName for g in report.failingGlyphs] == ["compFail"]
    assert report.allPass is False


def test_tolerance_boundary_is_inclusive() -> None:
    font = _font_with_base()
    big = Glyph("big")
    big.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    font.layers.defaultLayer["big"] = big

    inclusive = analyze_layer_rounding(
        font.layers.defaultLayer, 1000, 500, tolerance=1.0
    )
    assert inclusive.failingGlyphs == []
    strict = analyze_layer_rounding(
        font.layers.defaultLayer, 1000, 500, tolerance=0.999
    )
    assert [g.glyphName for g in strict.failingGlyphs] == ["big"]


def test_nested_components_keep_full_origin_chain() -> None:
    font = _font_with_base()
    layer = font.layers.defaultLayer
    big = Glyph("big")
    big.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    layer["big"] = big
    nest = Glyph("nest")
    nest.components.append(Component("big", Transform(1, 0, 0, 1, 0.3, 0.7)))
    layer["nest"] = nest

    report = analyze_layer_rounding(layer, 1000, 500)
    nest_report = next(g for g in report.glyphReports if g.glyphName == "nest")
    point = nest_report.points[0]

    assert point.componentChain == ("nest", "big", "a")
    assert point.sourceGlyph == "a"
    assert (point.sourceX, point.sourceY) == (1.25, 0)
    assert point.exactX == pytest.approx(5.6)
    assert point.exactY == pytest.approx(1.4)
    assert (point.directRoundedX, point.directRoundedY) == (6, 1)
    assert (point.localRoundedX, point.localRoundedY) == (7, 1)
    assert point.deviation == pytest.approx(1.0)


def test_rotation_produces_two_dimensional_offset() -> None:
    # Point chosen so that, after scaling to (9.6, 2.6), a 3 degree rotation
    # lands both pre-rounding coordinates comfortably inside rounding intervals;
    # snapping the base point before rotating shifts both axes by one unit.
    layer = Layer()
    base = Glyph("a")
    base.contours.append(Contour([Point(4.8, 1.3, "line")]))
    layer["a"] = base
    angle = math.radians(3.0)
    rotated = Glyph("rot")
    rotated.components.append(
        Component(
            "a",
            Transform(
                math.cos(angle),
                math.sin(angle),
                -math.sin(angle),
                math.cos(angle),
                0,
                0,
            ),
        )
    )
    layer["rot"] = rotated

    report = analyze_layer_rounding(layer, 1000, 500)
    point = next(g for g in report.glyphReports if g.glyphName == "rot").points[0]
    assert (point.directRoundedX, point.directRoundedY) == (9, 3)
    assert (point.localRoundedX, point.localRoundedY) == (10, 4)
    assert (point.deltaX, point.deltaY) == (1, 1)
    assert point.deviation == pytest.approx(math.sqrt(2.0))


def test_reflection_keeps_signed_delta() -> None:
    font = _font_with_base()
    mirrored = Glyph("mirror")
    mirrored.components.append(Component("a", Transform(-1, 0, 0, 1, 0, 0)))
    font.layers.defaultLayer["mirror"] = mirrored

    report = analyze_layer_rounding(font.layers.defaultLayer, 1000, 500)
    point = report.glyphReports[1].points[0]
    assert point.directRoundedX == -2  # otRound(-2.5) == -2
    assert point.localRoundedX == -3
    assert point.deltaX == -1
    assert point.deviation == pytest.approx(1.0)
    assert point.componentChain == ("mirror", "a")


def test_circular_component_reference_raises() -> None:
    layer = Layer()
    a = _base_glyph("a")
    b = Glyph("b")
    a.components.append(Component("b", Transform()))
    b.components.append(Component("a", Transform()))
    layer["a"] = a
    layer["b"] = b

    with pytest.raises(CircularReferenceError) as exc_info:
        analyze_layer_rounding(layer, 1000, 500)
    assert exc_info.value.glyph == "a"
    assert exc_info.value.path == ("a", "b", "a")


def test_missing_component_raises_keyerror() -> None:
    layer = Layer()
    ghost = Glyph("ghost")
    ghost.components.append(Component("missing", Transform()))
    layer["ghost"] = ghost
    with pytest.raises(KeyError, match="missing"):
        analyze_layer_rounding(layer, 1000, 500)


def test_empty_glyph_report() -> None:
    layer = Layer(glyphs=[Glyph("empty")])
    report = analyze_layer_rounding(layer, 1000, 500, tolerance=0.0)
    glyph_report = report.glyphReports[0]
    assert glyph_report.pointCount == 0
    assert glyph_report.worstPoint is None
    assert glyph_report.maxPointOffset == 0.0
    assert glyph_report.passes is True


def test_custom_rounding_function_is_used() -> None:
    def trunc(value: float) -> float:
        return float(int(value))

    layer = Layer(glyphs=[_base_glyph()])
    report = analyze_layer_rounding(layer, 1000, 500, rounding=trunc)
    assert report.glyphReports[0].points[0].directRoundedX == 2.0

    default_report = analyze_layer_rounding(layer, 1000, 500)
    assert default_report.glyphReports[0].points[0].directRoundedX == 3


def test_source_upm_override_and_scale() -> None:
    font = _font_with_base()
    font.info.unitsPerEm = 500
    report = export_rounding_report(font, 2000, source_upm=1000)
    assert report.sourceUPM == 1000
    assert report.targetUPM == 2000
    assert report.scale == pytest.approx(2.0)
    # (1.25, 0) at x2 -> 2.5 regardless of the font's own unitsPerEm.
    assert report.glyphReports[0].points[0].exactX == pytest.approx(2.5)


def test_export_selects_named_layer_or_object() -> None:
    font = _font_with_base()
    background = font.newLayer("background")
    background["z"] = _base_glyph("z")

    by_name = export_rounding_report(font, 1000, layer="background")
    assert by_name.layerName == "background"
    assert [g.glyphName for g in by_name.glyphReports] == ["z"]

    by_object = export_rounding_report(font, 1000, layer=background)
    assert by_object.layerName == "background"


def test_export_csv_and_json(tmp_path: Path) -> None:
    font = _font_with_base()
    big = Glyph("big")
    big.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    font.layers.defaultLayer["big"] = big

    csv_path = tmp_path / "errors.csv"
    json_path = tmp_path / "errors.json"
    report = export_rounding_report(
        font,
        1000,
        tolerance=0.5,
        csv_path=csv_path,
        json_path=json_path,
        failing_only=True,
    )

    with open(csv_path, newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["glyphName"] for row in rows] == ["big", "big"]
    assert rows[0]["componentChain"] == "big > a"
    assert rows[0]["sourceGlyph"] == "a"
    assert float(rows[0]["deviation"]) == pytest.approx(1.0)
    assert set(rows[0]) >= {
        "directRoundedX",
        "localRoundedX",
        "deltaX",
        "deltaY",
        "sourceUPM",
        "targetUPM",
        "tolerance",
        "passes",
    }

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["layerName"] == "public.default"
    assert data["sourceUPM"] == 500
    assert data["targetUPM"] == 1000
    assert data["allPass"] is False
    assert data["failingGlyphCount"] == 1
    big_data = next(g for g in data["glyphs"] if g["glyphName"] == "big")
    assert big_data["worstPoint"]["componentChain"] == ["big", "a"]
    assert big_data["maxPointOffset"] == pytest.approx(1.0)

    # A non-filtered CSV contains every analyzed point.
    full_csv = tmp_path / "full.csv"
    report.write_csv(full_csv)
    with open(full_csv, newline="", encoding="utf-8") as file:
        assert sum(1 for _ in csv.DictReader(file)) == sum(
            g.pointCount for g in report.glyphReports
        )


def test_reports_are_serializable() -> None:
    font = _font_with_base()
    big = Glyph("big")
    big.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    font.layers.defaultLayer["big"] = big
    report = analyze_layer_rounding(font.layers.defaultLayer, 1000, 500)

    assert isinstance(report, LayerRoundingReport)
    assert all(isinstance(g, GlyphRoundingReport) for g in report.glyphReports)
    assert all(isinstance(p, PointError) for g in report.glyphReports for p in g.points)
    # JSON must round-trip without raising and keep chain as a plain list.
    encoded = json.dumps(report.to_dict())
    decoded = json.loads(encoded)
    worst = decoded["glyphs"][1]["worstPoint"]
    assert worst["componentChain"] == ["big", "a"]


def test_equal_upm_keeps_integer_coordinates_exact() -> None:
    layer = Layer(glyphs=[_base_glyph()])
    report = analyze_layer_rounding(layer, 500, 500)
    point = report.glyphReports[0].points[0]
    assert report.scale == 1.0
    assert point.exactX == pytest.approx(1.25)
    assert point.deviation == 0.0


def test_mixed_contour_and_component_points_are_separated() -> None:
    font = _font_with_base()
    glyph = Glyph("mixed")
    glyph.contours.append(Contour([Point(2.0, 2.0, "line")]))
    glyph.components.append(Component("a", Transform(2, 0, 0, 2, 0, 0)))
    font.layers.defaultLayer["mixed"] = glyph

    report = analyze_layer_rounding(font.layers.defaultLayer, 1000, 500)
    mixed = next(g for g in report.glyphReports if g.glyphName == "mixed")
    assert mixed.isComposite is True
    assert mixed.pointCount == 3  # one own point + two points reached via component

    own_point = next(p for p in mixed.points if p.componentChain == ("mixed",))
    assert (own_point.sourceX, own_point.sourceY) == (2.0, 2.0)
    assert own_point.deviation == 0.0

    component_points = [p for p in mixed.points if p.componentChain == ("mixed", "a")]
    assert len(component_points) == 2
    assert mixed.maxPointOffset == pytest.approx(1.0)
    assert mixed.worstPoint is component_points[0]
