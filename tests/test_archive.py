from __future__ import annotations

import json
from pathlib import Path

import pytest

from ufoLib2 import Character, Font, GlyphName, GlyphSource
from ufoLib2.objects import Component, Glyph


def _add_glyph(
    font: Font,
    layer: str,
    name: str,
    components: tuple[str, ...] = (),
    unicodes: tuple[int, ...] = (),
) -> Glyph:
    target = font.layers[layer]
    glyph = target.newGlyph(name)
    glyph.unicodes = list(unicodes)
    glyph.components = [Component(base) for base in components]
    return glyph


@pytest.fixture
def layered_font() -> Font:
    font = Font()
    font.newLayer("public.special")
    _add_glyph(font, "public.default", "A", unicodes=(0x41,))
    _add_glyph(font, "public.default", "acute", unicodes=(0xB4,))
    _add_glyph(font, "public.default", "Aacute", ("A", "acute"), unicodes=(0xC1,))
    _add_glyph(font, "public.special", "A", unicodes=(0x41,))
    _add_glyph(font, "public.special", "Aacute", ("A", "acute"), unicodes=(0xC1,))
    return font


def test_packages_each_layer_independently(layered_font: Font) -> None:
    archive = layered_font.archive([Character("A"), Character("Á")])

    assert set(archive.layers) == {"public.default", "public.special"}
    default = archive["public.default"]
    special = archive["public.special"]

    # Both layers contain the requested composite and its local base.
    assert set(default.glyphs) == {"A", "acute", "Aacute"}
    # "acute" exists only in the default layer; the same component in
    # public.special must not borrow it.
    assert set(special.glyphs) == {"A", "Aacute"}
    assert special.missing == ["acute"]
    assert special["Aacute"].name == "Aacute"
    assert special["A"].name == "A"
    assert default.glyphs["A"] is not special.glyphs["A"]


def test_missing_component_reports_request_chain(layered_font: Font) -> None:
    archive = layered_font.archive([Character("Á")], layers="public.special")
    request = archive["public.special"].requests[0]

    assert request.status == "missing"
    assert not request.complete
    assert request.chains == [("Aacute",), ("Aacute", "A"), ("Aacute", "acute")]
    assert request.missing == ["acute"]
    assert any(
        issue.code == "missing-glyph" and issue.glyph == "acute"
        for issue in archive["public.special"].issues
    )


def test_explicit_substitute_fills_only_the_mapped_miss(layered_font: Font) -> None:
    archive = layered_font.archive(
        [Character("Á")],
        layers="public.special",
        substitutes={"acute": ("public.default", "acute")},
    )
    layer = archive["public.special"]

    assert layer.is_complete
    assert set(layer.glyphs) == {"A", "Aacute", "acute"}
    assert layer["acute"].components == []
    assert layer.substitutions[0].glyph == "acute"
    assert layer.substitutions[0].source_layer == "public.default"
    assert layer.substitutions[0].source_glyph == "acute"
    assert layer.substitutions[0].used_by == ["U+00C1"]

    # The substitute is a copy placed in the target layer; the source layer
    # and the target layer's original glyph set are left untouched.
    assert layer["acute"] is not layered_font["acute"]
    assert "acute" not in layered_font.layers["public.special"]


def test_layer_specific_substitute_wins(layered_font: Font) -> None:
    archive = layered_font.archive(
        [Character("Á")],
        layers="public.special",
        substitutes={
            "acute": ("public.default", "A"),
            ("public.special", "acute"): GlyphSource("public.default", "acute"),
        },
    )
    layer = archive["public.special"]

    assert layer.is_complete
    assert layer.substitutions[0].source_glyph == "acute"


def test_shared_base_is_stored_once_with_per_request_provenance() -> None:
    font = Font()
    _add_glyph(font, "public.default", "base")
    _add_glyph(font, "public.default", "left", ("base",))
    _add_glyph(font, "public.default", "right", ("base",))
    _add_glyph(font, "public.default", "both", ("left", "right"))

    archive = font.archive([GlyphName("left"), GlyphName("right"), GlyphName("both")])
    layer = archive["public.default"]

    assert set(layer.glyphs) == {"base", "left", "right", "both"}
    assert len([name for name in layer.glyphs if name == "base"]) == 1

    reaching = layer.requests_reaching("base")
    assert [request.label for request in reaching] == ["left", "right", "both"]
    chains = sorted(chain for _request, chain in layer.provenance("base"))
    assert chains == [
        ("both", "left", "base"),
        ("both", "right", "base"),
        ("left", "base"),
        ("right", "base"),
    ]


def test_cycle_is_reported_as_a_whole_ring_and_does_not_block_others() -> None:
    font = Font()
    _add_glyph(font, "public.default", "A", ("B",))
    _add_glyph(font, "public.default", "B", ("C",))
    _add_glyph(font, "public.default", "C", ("A",))
    _add_glyph(font, "public.default", "D", ("base",))
    _add_glyph(font, "public.default", "base")

    archive = font.archive([GlyphName("A"), GlyphName("D")])
    layer = archive["public.default"]

    cycle = layer.cycles[0]
    assert cycle.glyphs == ("A", "B", "C", "A")
    assert cycle.members == ("A", "B", "C")
    assert cycle.requests == ["A"]

    by_label = {request.label: request for request in layer.requests}
    assert by_label["A"].status == "cycle"
    assert by_label["A"].cycles == [("A", "B", "C", "A")]
    assert not by_label["A"].complete

    # The unrelated glyph is packaged and keeps its own clean status.
    assert by_label["D"].status == "ok"
    assert set(layer.glyphs) == {"A", "B", "C", "D", "base"}
    assert "component-cycle" in by_label["A"].issue_codes
    assert "component-cycle" not in by_label["D"].issue_codes


def test_partial_cycle_pointer_is_reported_as_the_reached_segment() -> None:
    font = Font()
    _add_glyph(font, "public.default", "top", ("mid",))
    _add_glyph(font, "public.default", "mid", ("top", "leaf"))
    _add_glyph(font, "public.default", "leaf")

    archive = font.archive([GlyphName("top"), GlyphName("leaf")])
    layer = archive["public.default"]

    assert layer.cycles[0].glyphs == ("top", "mid", "top")
    by_label = {request.label: request for request in layer.requests}
    assert by_label["top"].status == "cycle"
    assert by_label["leaf"].status == "ok"


def test_missing_character_is_reported_without_aborting_others() -> None:
    font = Font()
    _add_glyph(font, "public.default", "a", unicodes=(0x61,))

    archive = font.archive([Character("é"), Character("a")])
    layer = archive["public.default"]

    assert layer.requests[0].status == "missing"
    assert layer.requests[1].status == "ok"
    assert set(layer.glyphs) == {"a"}


def test_bare_one_character_string_falls_back_to_glyph_name() -> None:
    font = Font()
    _add_glyph(font, "public.default", "x")

    archive = font.archive(["x"])
    request = archive["public.default"].requests[0]

    assert request.kind == "glyph"
    assert request.glyph == "x"
    assert request.status == "ok"


def test_archive_can_be_saved_and_manifest_is_json_serializable(
    layered_font: Font, tmp_path: Path
) -> None:
    archive = layered_font.archive(
        [Character("Á")],
        substitutes={"acute": ("public.default", "acute")},
    )
    path = tmp_path / "archive.ufo"
    archive.save(path)

    reopened = Font.open(path, lazy=False)
    assert set(reopened.layers.layerOrder) == {"public.default", "public.special"}
    assert set(reopened.layers["public.special"].keys()) == {
        "A",
        "Aacute",
        "acute",
    }

    manifest = archive.to_dict()
    json.dumps(manifest)
    assert manifest["complete"] is True


def test_unknown_layer_is_rejected(layered_font: Font) -> None:
    with pytest.raises(KeyError, match="unknown layer"):
        layered_font.archive([Character("A")], layers="missing")


def test_missing_substitute_source_is_reported(layered_font: Font) -> None:
    archive = layered_font.archive(
        [Character("Á")],
        layers="public.special",
        substitutes={"acute": ("public.default", "not-there")},
    )
    layer = archive["public.special"]
    request = layer.requests[0]

    assert request.status == "missing"
    assert request.missing == ["acute"]
    assert "missing-substitute" in request.issue_codes
    assert any(issue.code == "missing-substitute" for issue in layer.issues)


def test_global_substitute_applies_to_every_layer(layered_font: Font) -> None:
    archive = layered_font.archive(
        [Character("Á")],
        substitutes={"acute": ("public.default", "acute")},
    )

    default = archive["public.default"]
    special = archive["public.special"]

    # The local glyph wins in the layer that already has it ...
    assert default.is_complete
    assert default.substitutions == []
    # ... while the other layer receives the explicitly mapped copy.
    assert special.is_complete
    assert "acute" in special
    assert special.substitutions[0].source_layer == "public.default"
