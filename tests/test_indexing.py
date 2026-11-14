from __future__ import annotations

import copy

import pytest
from fontTools.misc.transform import Identity

from ufoLib2.indexing import (
    AffectedRecord,
    ComponentSummary,
    GlyphChange,
    GlyphIndex,
    GlyphRecord,
    Hop,
)
from ufoLib2.objects import Component, Font, Glyph

PRIORITY = ("Bold", "public.default")


def plain_glyph(name: str) -> Glyph:
    return Glyph(name)


def composite_glyph(name: str, *base_glyphs: str) -> Glyph:
    glyph = Glyph(name)
    for base_glyph in base_glyphs:
        glyph.components.append(Component(base_glyph))
    return glyph


@pytest.fixture
def font() -> Font:
    font = Font()
    default = font.layers.defaultLayer
    default.addGlyph(plain_glyph("A"))
    default.addGlyph(composite_glyph("B", "A"))
    default.addGlyph(composite_glyph("Bacute", "B"))
    default.addGlyph(composite_glyph("Bacutemacron", "Bacute"))
    default.addGlyph(composite_glyph("Aacute", "A"))
    default.addGlyph(composite_glyph("Aacutemacron", "Aacute"))
    default.addGlyph(plain_glyph("C"))
    default.addGlyph(composite_glyph("E", "F"))  # dangling reference to F
    bold = font.layers.newLayer("Bold")
    bold.addGlyph(plain_glyph("A"))
    bold.addGlyph(composite_glyph("Aacute", "A"))
    bold.addGlyph(plain_glyph("D"))
    font.layers.newLayer("Thin")  # not part of the priority
    return font


@pytest.fixture
def index(font: Font) -> GlyphIndex:
    return GlyphIndex.build(font, PRIORITY)


def test_build_picks_first_priority_layer(index: GlyphIndex) -> None:
    assert index.records["A"] == GlyphRecord("A", "Bold", ())
    assert index.records["Aacute"] == GlyphRecord(
        "Aacute", "Bold", (ComponentSummary("A", Identity),)
    )
    assert index.records["B"].layer == "public.default"
    assert index.records["C"] == GlyphRecord("C", "public.default", ())
    assert index.records["D"] == GlyphRecord("D", "Bold", ())
    assert set(index.records) == {
        "A",
        "Aacute",
        "Aacutemacron",
        "B",
        "Bacute",
        "Bacutemacron",
        "C",
        "D",
        "E",
    }


def test_build_carries_component_summary_of_effective_version(
    index: GlyphIndex,
) -> None:
    # Aacutemacron only exists in the default layer and references Aacute there.
    assert index.records["Aacutemacron"] == GlyphRecord(
        "Aacutemacron",
        "public.default",
        (ComponentSummary("Aacute", Identity),),
    )


def test_build_validates_priority(font: Font) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        GlyphIndex.build(font, ())
    with pytest.raises(ValueError, match="duplicates"):
        GlyphIndex.build(font, ("Bold", "Bold"))
    with pytest.raises(KeyError, match="Missing"):
        GlyphIndex.build(font, ("Missing",))


def test_modified_in_effective_layer(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("Bold", "A", "modified")])
    assert plan.affected["A"] == AffectedRecord(
        "A", "rebuild", (Hop("Bold", "A", "modified"),)
    )
    # Aacute lives in Bold and resolves its component A in Bold.
    assert plan.affected["Aacute"] == AffectedRecord(
        "Aacute",
        "rebuild",
        (Hop("Bold", "A", "modified"), Hop("Bold", "Aacute", "component")),
    )
    # B resolves A in the default layer, which did not change.
    assert len(plan) == 2


def test_modified_shadowed_lower_layer_notices_only_same_layer_referrers(
    index: GlyphIndex,
) -> None:
    # A in the default layer is shadowed by Bold; changing it must not touch
    # the record for A, but records resolving components in the default layer
    # still see a changed target.
    plan = index.plan_rebuild([GlyphChange("public.default", "A", "modified")])
    assert "A" not in plan.affected
    assert "Aacute" not in plan.affected
    assert plan.names == ["B", "Bacute", "Bacutemacron"]


def test_indirect_dependencies_across_multiple_glyphs(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("public.default", "A", "modified")])
    assert plan.affected["B"].path == (
        Hop("public.default", "A", "modified"),
        Hop("public.default", "B", "component"),
    )
    assert plan.affected["Bacute"].path == (
        Hop("public.default", "A", "modified"),
        Hop("public.default", "B", "component"),
        Hop("public.default", "Bacute", "component"),
    )
    assert plan.affected["Bacutemacron"].path == (
        Hop("public.default", "A", "modified"),
        Hop("public.default", "B", "component"),
        Hop("public.default", "Bacute", "component"),
        Hop("public.default", "Bacutemacron", "component"),
    )


def test_removed_top_layer_falls_back_to_lower_layer(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("Bold", "A", "removed")])
    assert plan.affected["A"] == AffectedRecord(
        "A",
        "rebuild",
        (Hop("Bold", "A", "removed"), Hop("public.default", "A", "fallback")),
    )
    # Aacute in Bold referenced A in Bold, which is gone now.
    assert plan.affected["Aacute"].path == (
        Hop("Bold", "A", "removed"),
        Hop("Bold", "Aacute", "component"),
    )
    # B references A in the default layer, which is untouched.
    assert "B" not in plan.affected


def test_removed_from_all_layers_removes_record(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("public.default", "C", "removed")])
    assert plan.affected["C"] == AffectedRecord(
        "C",
        "removed",
        (
            Hop("public.default", "C", "removed"),
            Hop("public.default", "C", "fallback"),
        ),
    )
    assert len(plan) == 1


def test_added_in_higher_priority_layer_overrides(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("Bold", "B", "added")])
    assert plan.affected["B"] == AffectedRecord(
        "B",
        "rebuild",
        (Hop("Bold", "B", "added"), Hop("Bold", "B", "override")),
    )
    # Bacute and Bacutemacron resolve B in the default layer: unchanged.
    assert len(plan) == 1


def test_added_in_shadowed_lower_layer_ignored(index: GlyphIndex) -> None:
    # D only exists in Bold; adding a shadowed default-layer version changes
    # nothing in the index.
    plan = index.plan_rebuild([GlyphChange("public.default", "D", "added")])
    assert len(plan) == 0


def test_added_new_glyph(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("Bold", "G", "added")])
    assert plan.affected["G"] == AffectedRecord(
        "G", "new", (Hop("Bold", "G", "added"),)
    )
    assert len(plan) == 1


def test_added_glyph_resolves_dangling_reference(index: GlyphIndex) -> None:
    # E references F in the default layer, where F does not exist yet.
    plan = index.plan_rebuild([GlyphChange("public.default", "F", "added")])
    assert plan.affected["F"].status == "new"
    assert plan.affected["E"] == AffectedRecord(
        "E",
        "rebuild",
        (
            Hop("public.default", "F", "added"),
            Hop("public.default", "E", "component"),
        ),
    )


def test_change_in_layer_outside_priority_ignored(index: GlyphIndex) -> None:
    plan = index.plan_rebuild([GlyphChange("Thin", "A", "modified")])
    assert len(plan) == 0


def test_plan_reports_processing_volume(index: GlyphIndex) -> None:
    plan = index.plan_rebuild(
        [
            GlyphChange("public.default", "A", "modified"),
            GlyphChange("public.default", "C", "removed"),
            GlyphChange("Bold", "G", "added"),
        ]
    )
    assert len(plan) == 5
    assert plan.names == ["B", "Bacute", "Bacutemacron", "C", "G"]
    assert [record.name for record in plan.by_status("removed")] == ["C"]
    assert [record.name for record in plan.by_status("new")] == ["G"]
    assert [record.name for record in plan.by_status("rebuild")] == [
        "B",
        "Bacute",
        "Bacutemacron",
    ]


def test_apply_rebuilds_records_from_updated_font(
    font: Font, index: GlyphIndex
) -> None:
    updated = copy.deepcopy(font)
    del updated.layers["Bold"]["A"]
    updated.layers["Bold"].addGlyph(plain_glyph("G"))

    plan = index.plan_rebuild(
        [GlyphChange("Bold", "A", "removed"), GlyphChange("Bold", "G", "added")]
    )
    index.apply(plan, updated)

    # A fell back to the default layer, G was added, Aacute was rebuilt.
    assert index.records["A"] == GlyphRecord("A", "public.default", ())
    assert index.records["G"] == GlyphRecord("G", "Bold", ())
    assert index.presence["Bold"] == {"Aacute", "D", "G"}

    # The updated index answers the next round consistently: Aacute in Bold
    # now resolves A against a glyph that only exists in the default layer.
    follow_up = index.plan_rebuild([GlyphChange("public.default", "A", "modified")])
    assert "Aacute" not in follow_up.affected
    assert "A" in follow_up.affected


def test_apply_removes_gone_records(font: Font, index: GlyphIndex) -> None:
    updated = copy.deepcopy(font)
    del updated.layers.defaultLayer["C"]

    plan = index.plan_rebuild([GlyphChange("public.default", "C", "removed")])
    index.apply(plan, updated)

    assert "C" not in index.records
    assert "C" not in index.presence["public.default"]
