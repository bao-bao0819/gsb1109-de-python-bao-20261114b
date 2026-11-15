"""Rounding-error analysis for UPM (units-per-em) conversions.

When a font is moved from one units-per-em value to another and stored with
integer coordinates, a point that belongs to a plain (component-free) glyph is
quantized only once. Points reached through components can deviate much more:
the component's affine transformation (scale, rotation, reflection, ...) is
applied *after* the locally rounded coordinates, so a sub-unit rounding error
in the base glyph is carried into every composite that uses it.

Two integer-coordinate pipelines are compared, both expressed in the final
composite glyph coordinate system of the target UPM:

* **direct rounding** -- the exact transformed point is rounded once, directly
  in the final composite coordinate system;
* **local-first rounding** -- coordinates are rounded to integers at every
  nesting level, each component transform is then applied and the result is
  rounded again, mirroring a workflow that snaps each glyph locally before
  reusing it as a component.

The difference between the two pipelines is the error introduced by rounding
locally before transforming instead of rounding the assembled result. Every
record keeps the component chain that placed the point, so the original source
glyph of each displaced point is traceable through nested, rotated or reflected
components.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from attrs import frozen
from fontTools.misc.roundTools import otRound
from fontTools.misc.transform import Identity, Transform

from ufoLib2.errors import CircularReferenceError

if TYPE_CHECKING:
    from ufoLib2.objects.font import Font
    from ufoLib2.objects.layer import Layer

__all__ = [
    "PointError",
    "GlyphRoundingReport",
    "LayerRoundingReport",
    "analyze_layer_rounding",
    "export_rounding_report",
]


class PointError(NamedTuple):
    """The rounding discrepancy of a single point in a composite glyph.

    All coordinate values are expressed in the final composite glyph coordinate
    system of the target UPM, except :attr:`sourceX` and :attr:`sourceY`, which
    are the original coordinates in the source glyph's own (source UPM) space.
    """

    sourceGlyph: str
    """The glyph that actually owns the contour point (the deepest base glyph)."""

    contourIndex: int
    """The index of the contour within :attr:`sourceGlyph`."""

    pointIndex: int
    """The index of the point within its contour."""

    pointType: str | None
    """The UFO point type (``None`` for an off-curve point)."""

    sourceX: float
    """The original x coordinate in source UPM, before any rounding."""

    sourceY: float
    """The original y coordinate in source UPM, before any rounding."""

    exactX: float
    """The exact x coordinate in target UPM, before either rounding pipeline."""

    exactY: float
    """The exact y coordinate in target UPM, before either rounding pipeline."""

    directRoundedX: float
    """The x coordinate produced by rounding the final point directly."""

    directRoundedY: float
    """The y coordinate produced by rounding the final point directly."""

    localRoundedX: float
    """The x coordinate produced by rounding locally at every nesting level."""

    localRoundedY: float
    """The y coordinate produced by rounding locally at every nesting level."""

    deltaX: float
    """``localRoundedX - directRoundedX``."""

    deltaY: float
    """``localRoundedY - directRoundedY``."""

    deviation: float
    """The Euclidean distance between the two pipelines, in target UPM units."""

    componentChain: tuple[str, ...]
    """Glyph names from the analyzed composite down to :attr:`sourceGlyph`.

    A one-element tuple means the point is owned directly by the analyzed glyph
    and passes through no component.
    """

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return {
            "sourceGlyph": self.sourceGlyph,
            "contourIndex": self.contourIndex,
            "pointIndex": self.pointIndex,
            "pointType": self.pointType,
            "sourceX": self.sourceX,
            "sourceY": self.sourceY,
            "exactX": self.exactX,
            "exactY": self.exactY,
            "directRoundedX": self.directRoundedX,
            "directRoundedY": self.directRoundedY,
            "localRoundedX": self.localRoundedX,
            "localRoundedY": self.localRoundedY,
            "deltaX": self.deltaX,
            "deltaY": self.deltaY,
            "deviation": self.deviation,
            "componentChain": list(self.componentChain),
        }


@frozen
class GlyphRoundingReport:
    """Rounding results for one glyph, the unit of acceptance."""

    glyphName: str
    """The name of the analyzed (top-level) glyph."""

    layerName: str
    """The layer the glyph belongs to."""

    isComposite: bool
    """Whether the glyph itself places at least one component."""

    pointCount: int
    """The number of concrete points analyzed, including those in components."""

    maxPointOffset: float
    """The largest :attr:`PointError.deviation` of any point of this glyph, in
    target UPM units."""

    worstPoint: PointError | None
    """The point reaching :attr:`maxPointOffset`, or ``None`` for an empty glyph."""

    points: list[PointError]
    """Every analyzed point with both rounding pipelines and its origin."""

    passes: bool
    """Whether :attr:`maxPointOffset` is within the configured tolerance."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return {
            "glyphName": self.glyphName,
            "layerName": self.layerName,
            "isComposite": self.isComposite,
            "pointCount": self.pointCount,
            "maxPointOffset": self.maxPointOffset,
            "passes": self.passes,
            "worstPoint": (
                None if self.worstPoint is None else self.worstPoint.to_dict()
            ),
            "points": [point.to_dict() for point in self.points],
        }


@frozen
class LayerRoundingReport:
    """Rounding results for every glyph in one source layer."""

    layerName: str
    """The name of the analyzed layer."""

    sourceUPM: int
    """The units-per-em the layer coordinates were authored in."""

    targetUPM: int
    """The units-per-em the integer coordinates are stored in after scaling."""

    scale: float
    """``targetUPM / sourceUPM``."""

    tolerance: float
    """The maximum accepted point offset, in target UPM units."""

    glyphReports: list[GlyphRoundingReport]
    """One acceptance report per glyph in the layer."""

    @property
    def maxPointOffset(self) -> float:
        """The worst offset reached by any glyph in the layer."""
        if not self.glyphReports:
            return 0.0
        return max(report.maxPointOffset for report in self.glyphReports)

    @property
    def failingGlyphs(self) -> list[GlyphRoundingReport]:
        """The glyph reports whose offset exceeds the tolerance."""
        return [report for report in self.glyphReports if not report.passes]

    @property
    def allPass(self) -> bool:
        """Whether every glyph in the layer is within the tolerance."""
        return not self.failingGlyphs

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return {
            "layerName": self.layerName,
            "sourceUPM": self.sourceUPM,
            "targetUPM": self.targetUPM,
            "scale": self.scale,
            "tolerance": self.tolerance,
            "maxPointOffset": self.maxPointOffset,
            "allPass": self.allPass,
            "glyphCount": len(self.glyphReports),
            "failingGlyphCount": len(self.failingGlyphs),
            "glyphs": [report.to_dict() for report in self.glyphReports],
        }

    def write_json(self, path: str | os.PathLike[str], *, indent: int = 2) -> None:
        """Write the full report as JSON to ``path``."""
        with open(path, "w", encoding="utf-8") as file:
            json.dump(self.to_dict(), file, indent=indent)
            file.write("\n")

    def write_csv(
        self, path: str | os.PathLike[str], *, failing_only: bool = False
    ) -> None:
        """Write one row per analyzed point as CSV to ``path``.

        Args:
            path: The file to write.
            failing_only: If True, only points of glyphs that exceed the
                tolerance are written; otherwise every point is written.
        """
        fieldnames = [
            "glyphName",
            "layerName",
            "sourceUPM",
            "targetUPM",
            "tolerance",
            "passes",
            "isComposite",
            "maxPointOffset",
            "componentChain",
            "sourceGlyph",
            "contourIndex",
            "pointIndex",
            "pointType",
            "sourceX",
            "sourceY",
            "exactX",
            "exactY",
            "directRoundedX",
            "directRoundedY",
            "localRoundedX",
            "localRoundedY",
            "deltaX",
            "deltaY",
            "deviation",
        ]
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for report in self.glyphReports:
                if failing_only and report.passes:
                    continue
                for point in report.points:
                    writer.writerow(
                        {
                            "glyphName": report.glyphName,
                            "layerName": self.layerName,
                            "sourceUPM": self.sourceUPM,
                            "targetUPM": self.targetUPM,
                            "tolerance": self.tolerance,
                            "passes": report.passes,
                            "isComposite": report.isComposite,
                            "maxPointOffset": report.maxPointOffset,
                            "componentChain": " > ".join(point.componentChain),
                            "sourceGlyph": point.sourceGlyph,
                            "contourIndex": point.contourIndex,
                            "pointIndex": point.pointIndex,
                            "pointType": point.pointType,
                            "sourceX": point.sourceX,
                            "sourceY": point.sourceY,
                            "exactX": point.exactX,
                            "exactY": point.exactY,
                            "directRoundedX": point.directRoundedX,
                            "directRoundedY": point.directRoundedY,
                            "localRoundedX": point.localRoundedX,
                            "localRoundedY": point.localRoundedY,
                            "deltaX": point.deltaX,
                            "deltaY": point.deltaY,
                            "deviation": point.deviation,
                        }
                    )


def _validate_upm(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _coerce_upm(name: str, value: float) -> int:
    """Coerce a font-info UPM (typed as float) into a positive integer."""
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    coerced = int(value)
    if coerced != value:
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return coerced


def _target_space_transform(
    transformation: Transform, scale: float
) -> Transform:
    """Re-express a source-UPM component transform in target-UPM space.

    Under a uniform scale the linear part (rotation, scale, reflection, shear)
    is unchanged and only the translation is multiplied by the scale.
    """
    xx, xy, yx, yy, dx, dy = transformation
    return Transform(xx, xy, yx, yy, dx * scale, dy * scale)


def _walk_components(
    glyph_name: str,
    layer: Layer,
    scale: float,
    rounder: Callable[[float], float],
    source_matrix: Transform,
    target_transforms: tuple[Transform, ...],
    stack: tuple[str, ...],
    records: list[PointError],
) -> None:
    """Append :class:`PointError` records for every point reachable from
    ``glyph_name``.

    Args:
        source_matrix: Composition of the source-UPM component transforms from
            the analyzed glyph down to ``glyph_name`` (Identity at the root).
        target_transforms: The same component transforms re-expressed in target
            UPM, ordered from the outermost component to the innermost one.
        stack: Glyph names from the analyzed glyph down to ``glyph_name``; used
            both for origin tracing and circular-reference detection.
    """
    if glyph_name in stack:
        raise CircularReferenceError(glyph_name, stack + (glyph_name,))

    glyph = layer[glyph_name]
    current_stack = stack + (glyph_name,)

    for contour_index, contour in enumerate(glyph.contours):
        for point_index, point in enumerate(contour.points):
            exact_source_x, exact_source_y = source_matrix.transformPoint(
                (point.x, point.y)
            )
            exact_x = exact_source_x * scale
            exact_y = exact_source_y * scale
            direct_x = rounder(exact_x)
            direct_y = rounder(exact_y)

            # Local-first pipeline: snap the base point after scaling, then walk
            # the component chain from the innermost transform outwards,
            # re-rounding after every transform.
            local_x = rounder(point.x * scale)
            local_y = rounder(point.y * scale)
            for transformation in reversed(target_transforms):
                local_x, local_y = transformation.transformPoint(
                    (local_x, local_y)
                )
                local_x = rounder(local_x)
                local_y = rounder(local_y)

            delta_x = local_x - direct_x
            delta_y = local_y - direct_y
            records.append(
                PointError(
                    sourceGlyph=glyph_name,
                    contourIndex=contour_index,
                    pointIndex=point_index,
                    pointType=point.type,
                    sourceX=point.x,
                    sourceY=point.y,
                    exactX=exact_x,
                    exactY=exact_y,
                    directRoundedX=direct_x,
                    directRoundedY=direct_y,
                    localRoundedX=local_x,
                    localRoundedY=local_y,
                    deltaX=delta_x,
                    deltaY=delta_y,
                    deviation=math.hypot(delta_x, delta_y),
                    componentChain=current_stack,
                )
            )

    for component in glyph.components:
        transformation = component.transformation
        _walk_components(
            component.baseGlyph,
            layer,
            scale,
            rounder,
            source_matrix.transform(transformation),
            target_transforms
            + (_target_space_transform(transformation, scale),),
            current_stack,
            records,
        )


def analyze_layer_rounding(
    layer: Layer,
    target_upm: int,
    source_upm: int,
    *,
    tolerance: float = 0.0,
    rounding: Callable[[float], float] = otRound,
) -> LayerRoundingReport:
    """Analyze integer-rounding discrepancies for every glyph in a layer.

    Args:
        layer: The source layer whose glyphs are analyzed.
        target_upm: The units-per-em the coordinates are converted to.
        source_upm: The units-per-em the layer coordinates are authored in.
        tolerance: The maximum accepted point offset in target UPM units. A
            glyph passes when its :attr:`GlyphRoundingReport.maxPointOffset`
            does not exceed this value.
        rounding: The rounding function applied to coordinates, taking and
            returning a number. Defaults to fontTools' OpenType rounding.

    Returns:
        A :class:`LayerRoundingReport` with a per-glyph acceptance verdict and
        every point's direct-vs-local rounding difference with its origin.

    Raises:
        ValueError: If either UPM value is not a positive integer.
        CircularReferenceError: If component references form a cycle.
        KeyError: If a component references a glyph missing from the layer.
    """
    source_upm = _validate_upm("source_upm", source_upm)
    target_upm = _validate_upm("target_upm", target_upm)
    scale = target_upm / source_upm

    glyph_reports: list[GlyphRoundingReport] = []
    for glyph_name in layer.keys():
        records: list[PointError] = []
        glyph = layer[glyph_name]
        _walk_components(
            glyph_name,
            layer,
            scale,
            rounding,
            Identity,
            (),
            (),
            records,
        )
        worst_point: PointError | None = None
        max_offset = 0.0
        if records:
            worst_point = max(records, key=lambda record: record.deviation)
            max_offset = worst_point.deviation
        glyph_reports.append(
            GlyphRoundingReport(
                glyphName=glyph_name,
                layerName=layer.name,
                isComposite=bool(glyph.components),
                pointCount=len(records),
                maxPointOffset=max_offset,
                worstPoint=worst_point,
                points=records,
                passes=max_offset <= tolerance,
            )
        )

    return LayerRoundingReport(
        layerName=layer.name,
        sourceUPM=source_upm,
        targetUPM=target_upm,
        scale=scale,
        tolerance=tolerance,
        glyphReports=glyph_reports,
    )


def export_rounding_report(
    font: Font,
    target_upm: int,
    *,
    layer: str | Layer | None = None,
    source_upm: int | None = None,
    tolerance: float = 0.0,
    rounding: Callable[[float], float] = otRound,
    csv_path: str | os.PathLike[str] | None = None,
    json_path: str | os.PathLike[str] | None = None,
    failing_only: bool = False,
) -> LayerRoundingReport:
    """Build a rounding report for a font layer and optionally export it.

    The source UPM is taken from ``font.info.unitsPerEm`` unless ``source_upm``
    is given explicitly.

    Args:
        font: The font containing the source layer.
        target_upm: The units-per-em the integer coordinates are stored in.
        layer: The layer to analyze, given by name or as a :class:`.Layer`.
            Defaults to the font's default layer.
        source_upm: Overrides the source units-per-em when the font info does
            not provide one.
        tolerance: Maximum accepted point offset in target UPM units.
        rounding: The rounding function, defaults to OpenType rounding.
        csv_path: If given, writes the per-point error sheet to this file.
        json_path: If given, writes the full structured report to this file.
        failing_only: When writing CSV, limit the rows to failing glyphs.

    Returns:
        The produced :class:`LayerRoundingReport`.
    """
    if source_upm is None:
        info_upm = font.info.unitsPerEm
        if info_upm is None:
            raise ValueError(
                "source_upm is required because the font info has no unitsPerEm"
            )
        source_upm = _coerce_upm("unitsPerEm", info_upm)

    if layer is None:
        source_layer = font.layers.defaultLayer
    elif isinstance(layer, str):
        source_layer = font.layers[layer]
    else:
        source_layer = layer

    report = analyze_layer_rounding(
        source_layer,
        target_upm,
        source_upm,
        tolerance=tolerance,
        rounding=rounding,
    )
    if csv_path is not None:
        report.write_csv(csv_path, failing_only=failing_only)
    if json_path is not None:
        report.write_json(json_path)
    return report
