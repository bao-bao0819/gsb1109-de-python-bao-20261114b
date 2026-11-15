from __future__ import annotations

import pytest

import ufoLib2
from ufoLib2.kerning_compare import (
    GLYPH_GLYPH,
    GLYPH_GROUP,
    GROUP_GROUP,
    GROUP_GLYPH,
    LEFT_GROUP_PREFIX,
    RIGHT_GROUP_PREFIX,
    build_model,
    compare_fonts,
    compare_paths,
    effective_kerning,
    render_report,
)

GLYPHS = ["A", "V", "W", "T", "O"]

L_A = "public.kern1.L_A"
L_T = "public.kern1.L_T"
L_W = "public.kern1.L_W"
L_V = "public.kern1.L_V"
R_V = "public.kern2.R_V"


def _font(groups, kerning) -> ufoLib2.Font:
    font = ufoLib2.Font()
    for name in GLYPHS:
        font.newGlyph(name)
    font.groups.update(groups)
    font.kerning.update(kerning)
    return font


@pytest.fixture
def old_font() -> ufoLib2.Font:
    return _font(
        groups={
            L_A: ["A"],
            L_T: ["T"],
            R_V: ["V", "W"],
        },
        kerning={
            ("A", "V"): -80,
            ("A", "W"): -60,
            ("V", "O"): -10,
            ("A", R_V): -50,
            (L_A, R_V): -20,
            ("T", "O"): 0,
            ("T", R_V): -90,
            (L_T, "O"): -30,
        },
    )


@pytest.fixture
def new_font() -> ufoLib2.Font:
    return _font(
        groups={
            L_A: ["A"],
            L_T: ["T"],
            L_W: ["W"],
            L_V: ["V"],
            R_V: ["V", "W", "O"],
        },
        kerning={
            # designer moved the A,V / A,W singles into the A x R_V group
            ("A", R_V): -50,
            (L_A, R_V): -20,
            # pure declaration rewrite: single replaced by a left group of same value
            (L_V, "O"): -10,
            # explicit zero kept; must keep suppressing group values
            ("T", "O"): 0,
            ("T", R_V): -90,
            (L_T, "O"): -30,
            # new explicit zero suppressing a group value
            ("W", "O"): 0,
            (L_W, "O"): -25,
            # invalid side / unknown group declarations
            (R_V, "A"): -11,
            ("A", L_A): -12,
            ("A", "public.kern2.R_GHOST"): -13,
        },
    )


def test_priority_order_and_candidates(old_font: ufoLib2.Font) -> None:
    model = build_model(old_font)
    resolution = model.resolve_pair("A", "V")
    assert resolution is not None
    assert resolution.value == -80
    assert resolution.source.side == GLYPH_GLYPH
    sides = [source.side for source in resolution.candidates]
    assert sides == sorted(sides)
    assert sides == [GLYPH_GLYPH, GLYPH_GROUP, GROUP_GROUP]

    assert model.resolve_pair("A", "O") is None

    t_o = model.resolve_pair("T", "O")
    assert t_o is not None
    assert t_o.value == 0
    assert t_o.source.raw_left == "T" and t_o.source.raw_right == "O"
    assert [source.value for source in t_o.candidates] == [0, -30]


def test_explicit_zero_suppresses_group_in_new_font(new_font: ufoLib2.Font) -> None:
    effective = effective_kerning(new_font)
    assert effective[("T", "O")].value == 0
    suppressed = {source.value for source in effective[("T", "O")].candidates}
    assert suppressed == {0, -90, -30}

    w_o = effective[("W", "O")]
    assert w_o.value == 0
    assert w_o.source.side == GLYPH_GLYPH
    assert [source.value for source in w_o.candidates] == [0, -25]


def test_invalid_side_and_unknown_group_ignored(new_font: ufoLib2.Font) -> None:
    model = build_model(new_font)
    reasons = {(item.raw_left, item.raw_right): item.reason for item in model.ignored}
    assert (R_V, "A") in reasons
    assert ("A", L_A) in reasons
    assert ("A", "public.kern2.R_GHOST") in reasons
    # the wrongly-sided declaration must not create effective pairs
    assert model.resolve_pair("V", "A") is None
    assert model.resolve_pair("A", "A") is None


def test_group_expansion_uses_real_glyphs_only() -> None:
    font = _font(
        groups={R_V: ["V", "W", "DoesNotExist"]},
        kerning={("A", R_V): -40},
    )
    effective = effective_kerning(font)
    assert set(effective) == {("A", "V"), ("A", "W")}


def test_compare_clusters_and_classifies(
    old_font: ufoLib2.Font, new_font: ufoLib2.Font
) -> None:
    diff = compare_fonts(old_font, new_font)

    # declaration layer: singles removed even though the table got shorter
    removed_pairs = {(left, right) for left, right, _ in diff.raw_kerning_removed}
    assert {("A", "V"), ("A", "W"), ("V", "O")} <= removed_pairs
    assert diff.group_membership_changes[R_V] == (["O"], [])
    assert diff.groups_added == [L_V, L_W]

    # the moved singles share one cause even though their old values differed
    moved = next(
        group
        for group in diff.changes
        if group.cause.kind == "winner_changed"
        and group.cause.anchor is not None
        and group.cause.anchor.raw_left == "A"
        and group.cause.anchor.raw_right == R_V
    )
    assert {change.pair for change in moved.pairs} == {("A", "V"), ("A", "W")}
    assert moved.user_visible_count == 2
    assert all(change.user_visible for change in moved.pairs)

    # group membership extension makes the same declaration cover new pairs
    added = next(
        group
        for group in diff.changes
        if group.cause.kind == "added"
        and group.cause.anchor is not None
        and group.cause.anchor.raw_right == R_V
    )
    assert {change.pair for change in added.pairs} == {("A", "O")}
    assert added.pairs[0].old_value is None and added.pairs[0].new_value == -50

    # same-value move is declaration-only: user still sees -10
    rewrite = next(
        group
        for group in diff.changes
        if {change.pair for change in group.pairs} == {("V", "O")}
    )
    assert rewrite.cause.kind == "winner_changed"
    assert rewrite.user_visible_count == 0
    assert rewrite.declaration_only_count == 1
    change = rewrite.pairs[0]
    assert change.old_value == change.new_value == -10
    assert change.old_source.raw_left == "V"
    assert change.new_source.raw_left == L_V
    assert change.new_source.raw_right == "O"

    # new explicit zero vs group: user-visible value stays zero -> declaration only
    w_o = diff.pair_index[("W", "O")]
    assert w_o.user_visible is False
    assert (w_o.old_value or 0) == (w_o.new_value or 0) == 0
    assert w_o.new_source.value == 0

    # explicit zero kept suppressing groups: no change recorded for T,O
    assert ("T", "O") not in diff.pair_index

    assert (
        diff.user_visible_count + diff.declaration_only_count
        == diff.pair_change_count
    )


def test_reconstruct_restores_full_provenance(
    old_font: ufoLib2.Font, new_font: ufoLib2.Font
) -> None:
    diff = compare_fonts(old_font, new_font)
    change = diff.reconstruct(("A", "V"))
    assert change.old_value == -80 and change.new_value == -50
    assert (change.old_source.raw_left, change.old_source.raw_right) == ("A", "V")
    assert (change.new_source.raw_left, change.new_source.raw_right) == ("A", R_V)
    old_identities = [
        (source.raw_left, source.raw_right) for source in change.old_candidates
    ]
    assert old_identities == [("A", "V"), ("A", R_V), (L_A, R_V)]
    new_identities = [
        (source.raw_left, source.raw_right) for source in change.new_candidates
    ]
    assert new_identities == [("A", R_V), (L_A, R_V)]

    with pytest.raises(KeyError):
        diff.reconstruct(("T", "O"))


def test_report_mentions_both_layers(
    old_font: ufoLib2.Font, new_font: ufoLib2.Font
) -> None:
    report = render_report(compare_fonts(old_font, new_font))
    assert "声明层变化" in report
    assert "用户实际看到的有效字对变化" in report
    assert "形式改写" in report
    assert "public.kern2.R_GHOST" in report


def test_compare_paths_roundtrip(
    tmp_path, old_font: ufoLib2.Font, new_font: ufoLib2.Font
) -> None:
    old_path = tmp_path / "Old.ufo"
    new_path = tmp_path / "New.ufo"
    old_font.save(old_path)
    new_font.save(new_path)
    diff = compare_paths(old_path, new_path)
    assert diff.pair_change_count == compare_fonts(old_font, new_font).pair_change_count


def test_mutator_sans_smoke() -> None:
    font = ufoLib2.Font.open("tests/data/MutatorSansBoldCondensed.ufo")
    model = build_model(font)
    effective = list(model.effective_pairs())
    assert len(effective) == len(font.kerning)
    assert all(item.source.side == GLYPH_GLYPH for item in effective)
    # non-kerning groups must not leak into the kerning group maps
    assert "testGroup" not in model.left_members
    assert "testGroup" not in model.right_members


def test_side_constants_cover_all_forms() -> None:
    assert GLYPH_GLYPH < GLYPH_GROUP < GROUP_GLYPH < GROUP_GROUP
    assert LEFT_GROUP_PREFIX == "public.kern1."
    assert RIGHT_GROUP_PREFIX == "public.kern2."
