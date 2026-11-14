from __future__ import annotations

import json

import pytest
from fontTools.misc.transform import Transform

import ufoLib2
from ufoLib2.anchor_index import (
    AnchorEntry,
    OverrideRule,
    build_anchor_index,
    collect_glyph_anchors,
    compute_attachments,
    decide_attachment,
    export_index,
    main,
    replay_entry,
)
from ufoLib2.objects import Anchor, Component, Glyph


def make_glyph(
    name: str,
    anchors: list[tuple[float, float, str]] | None = None,
    components: list[tuple[str, tuple[float, ...]]] | None = None,
) -> Glyph:
    return Glyph(
        name=name,
        anchors=[Anchor(x=x, y=y, name=anchor_name) for x, y, anchor_name in anchors or []],
        components=[
            Component(baseGlyph=base, transformation=transform)
            for base, transform in components or []
        ],
    )


@pytest.fixture
def fonts() -> dict[str, ufoLib2.Font]:
    main_font = ufoLib2.Font()
    main_font.addGlyph(make_glyph("a", anchors=[(250, 500, "top")]))
    main_font.addGlyph(
        make_glyph(
            "aacute",
            anchors=[(260, 770, "top")],
            components=[
                ("a", (1, 0, 0, 1, 10, 20)),
                ("acute", (1, 0, 0, 1, 100, 50)),
            ],
        )
    )
    main_font.addGlyph(
        make_glyph(
            "adbl",
            components=[
                ("acute", (1, 0, 0, 1, 0, 0)),
                ("acute", (1, 0, 0, 1, 200, 30)),
            ],
        )
    )
    main_font.addGlyph(
        make_glyph("nested", components=[("aacute", (1, 0, 0, 1, 5, 7))])
    )
    marks_font = ufoLib2.Font()
    marks_font.addGlyph(make_glyph("acute", anchors=[(150, 700, "top")]))
    return {"main": main_font, "marks": marks_font}


@pytest.fixture
def suppliers(fonts: dict[str, ufoLib2.Font]) -> list[tuple[str, ufoLib2.Font]]:
    return [("main", fonts["main"]), ("marks", fonts["marks"])]


def by_path(entries: list[AnchorEntry]) -> dict[str, AnchorEntry]:
    return {entry.path: entry for entry in entries}


def test_composite_collects_own_and_component_anchors(suppliers):
    entries = by_path(collect_glyph_anchors(suppliers, "aacute"))
    assert set(entries) == {"", "a@0", "acute@1"}
    # The composite's own top anchor is exported under the empty path.
    assert (entries[""].x, entries[""].y) == (260, 770)
    # Component anchors are transformed into the composite's space.
    assert (entries["a@0"].x, entries["a@0"].y) == (260, 520)
    assert (entries["acute@1"].x, entries["acute@1"].y) == (250, 750)
    # The acute component is supplied by the second font.
    assert entries["acute@1"].source_font == "marks"
    assert entries["acute@1"].source_glyph == "acute"
    assert entries["acute@1"].local == (150, 700)


def test_same_component_twice_is_two_instances(suppliers):
    entries = by_path(collect_glyph_anchors(suppliers, "adbl"))
    assert set(entries) == {"acute@0", "acute@1"}
    assert (entries["acute@0"].x, entries["acute@0"].y) == (150, 700)
    assert (entries["acute@1"].x, entries["acute@1"].y) == (350, 730)
    assert entries["acute@0"].chain != entries["acute@1"].chain


def test_nested_components_build_multi_level_chain(suppliers):
    entries = by_path(collect_glyph_anchors(suppliers, "nested"))
    assert set(entries) == {
        "aacute@0",
        "aacute@0/a@0",
        "aacute@0/acute@1",
    }
    deep = entries["aacute@0/acute@1"]
    assert deep.source_font == "marks"
    assert len(deep.chain) == 2
    assert (deep.x, deep.y) == (255, 757)
    outer, inner = deep.chain
    assert (outer.font, outer.glyph, outer.base_glyph) == ("main", "nested", "aacute")
    assert (inner.font, inner.glyph, inner.base_glyph) == ("main", "aacute", "acute")


def test_replay_round_trips_every_exported_point(suppliers, fonts):
    entries = build_anchor_index(suppliers, ["aacute", "adbl", "nested"])
    assert entries
    for entry in entries:
        x, y = replay_entry(fonts, entry)
        assert (x, y) == pytest.approx((entry.x, entry.y))


def test_replay_uses_transforms_from_the_original_ufos(suppliers, fonts):
    (entry,) = [
        e
        for e in collect_glyph_anchors(suppliers, "nested")
        if e.path == "aacute@0/acute@1"
    ]
    # Independently of the recorded chain, walk the fonts level by level.
    point = (150.0, 700.0)  # acute's top anchor in the marks font
    point = fonts["main"]["aacute"].components[1].transformation.transformPoint(point)
    point = fonts["main"]["nested"].components[0].transformation.transformPoint(point)
    assert replay_entry(fonts, entry) == point == (entry.x, entry.y)


def test_rule_selects_component_position(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    rules = [OverrideRule("aacute", "top", level=1, path="acute@1")]
    decision = decide_attachment(entries, rules, "aacute", "top")
    assert decision.status == "resolved"
    assert decision.position == (250, 750)
    assert decision.level == 1


def test_higher_level_rule_wins(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    rules = [
        OverrideRule("aacute", "top", level=1, path="a@0"),
        OverrideRule("aacute", "top", level=2, path="acute@1"),
    ]
    decision = decide_attachment(entries, rules, "aacute", "top")
    assert decision.status == "resolved"
    assert decision.position == (250, 750)
    assert decision.level == 2


def test_same_level_rules_with_different_positions_keep_conflict(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    rules = [
        OverrideRule("aacute", "top", level=1, path="a@0"),
        OverrideRule("aacute", "top", level=1, position=(999, 999)),
    ]
    decision = decide_attachment(entries, rules, "aacute", "top")
    assert decision.status == "conflict"
    assert decision.position is None
    assert set(decision.candidates) == {(260, 520), (999, 999)}


def test_same_level_rules_agreeing_resolve(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    rules = [
        OverrideRule("aacute", "top", level=1, path="acute@1"),
        OverrideRule("aacute", "top", level=1, position=(250, 750)),
    ]
    decision = decide_attachment(entries, rules, "aacute", "top")
    assert decision.status == "resolved"
    assert decision.position == (250, 750)


def test_default_falls_back_to_own_anchor(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    decision = decide_attachment(entries, [], "aacute", "top")
    assert decision.status == "resolved"
    assert decision.position == (260, 770)
    assert decision.level is None


def test_rule_with_unknown_path_raises(suppliers):
    entries = collect_glyph_anchors(suppliers, "aacute")
    rules = [OverrideRule("aacute", "top", level=1, path="grave@9")]
    with pytest.raises(KeyError):
        decide_attachment(entries, rules, "aacute", "top")


def test_rule_requires_exactly_one_selector():
    with pytest.raises(ValueError):
        OverrideRule("aacute", "top", level=1)
    with pytest.raises(ValueError):
        OverrideRule("aacute", "top", level=1, path="a@0", position=(1, 2))


def test_component_cycle_raises(suppliers, fonts):
    fonts["main"].addGlyph(
        make_glyph("loopy", components=[("loopy", (1, 0, 0, 1, 0, 0))])
    )
    with pytest.raises(ValueError, match="cycle"):
        collect_glyph_anchors(suppliers, "loopy")


def test_export_index_is_json_serializable(suppliers):
    entries = build_anchor_index(suppliers, ["aacute", "adbl"])
    rules = [
        OverrideRule("aacute", "top", level=1, path="a@0"),
        OverrideRule("aacute", "top", level=1, position=(999, 999)),
    ]
    decisions = compute_attachments(entries, rules)
    data = json.loads(json.dumps(export_index(entries, decisions)))
    aacute = data["glyphs"]["aacute"]
    assert {a["path"] for a in aacute["anchors"]} == {"", "a@0", "acute@1"}
    assert aacute["attachments"]["top"]["status"] == "conflict"
    assert len(aacute["attachments"]["top"]["candidates"]) == 2
    adbl_paths = {a["path"] for a in data["glyphs"]["adbl"]["anchors"]}
    assert adbl_paths == {"acute@0", "acute@1"}


def test_cli_round_trip(tmp_path, fonts):
    for font_id, font in fonts.items():
        font.save(tmp_path / f"{font_id}.ufo")
    (tmp_path / "rules.json").write_text(
        json.dumps([{"glyph": "aacute", "anchor": "top", "level": 1, "path": "acute@1"}])
    )
    out = tmp_path / "index.json"
    rc = main(
        [
            str(tmp_path / "main.ufo"),
            str(tmp_path / "marks.ufo"),
            "--glyph",
            "aacute",
            "--rules",
            str(tmp_path / "rules.json"),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    attachment = data["glyphs"]["aacute"]["attachments"]["top"]
    assert attachment["status"] == "resolved"
    assert attachment["position"] == [250, 750]
    # The exported points replay against the saved UFOs.
    reopened = {
        font_id: ufoLib2.Font.open(tmp_path / f"{font_id}.ufo")
        for font_id in ("main", "marks")
    }
    entries = build_anchor_index(
        [("main", reopened["main"]), ("marks", reopened["marks"])], ["aacute"]
    )
    exported = {a["path"]: a for a in data["glyphs"]["aacute"]["anchors"]}
    for entry in entries:
        x, y = replay_entry(reopened, entry)
        assert [x, y] == [exported[entry.path]["x"], exported[entry.path]["y"]]
