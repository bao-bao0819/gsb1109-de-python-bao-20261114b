from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ufoLib2.catalog import (
    Catalog,
    CatalogError,
    GlyphRecord,
    SourceSpec,
    load_source,
    normalize_manual_mapping,
    normalize_stable_ids,
    resolve_catalog,
    summarize,
)


def _load_pair(left: Path, right: Path) -> tuple[list[Any], list[GlyphRecord]]:
    sources: list[Any] = []
    records: list[GlyphRecord] = []
    for spec in (
        SourceSpec("left", left, stable_id_key="com.example.stableId"),
        SourceSpec("right", right, stable_id_key="com.example.stableId"),
    ):
        info, source_records = load_source(spec)
        sources.append(info)
        records.extend(source_records)
    return sources, records


def test_unicode_named_and_internal_suppliers_make_provable_pairs(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    sources, records = _load_pair(*two_supplier_ufos)
    catalog = resolve_catalog(sources, records)

    pairs = {(match.left, match.right) for match in catalog.matches}
    assert ("left:uni0041", "right:A") in pairs
    assert ("left:uni0042", "right:B") in pairs
    assert ("left:u1F600", "right:smiley") in pairs

    # Records come from two naming conventions yet share explicit evidence.
    assert catalog.sources[0].naming == "unicode"
    assert catalog.sources[1].naming == "internal"

    record_ids = [record.id for record in catalog.records]
    assert record_ids == sorted(
        [
            "left:uni0041",
            "left:uni0042",
            "left:uni0043",
            "left:u1F600",
            "right:A",
            "right:A.ss01",
            "right:B",
            "right:C.alt1",
            "right:C.alt2",
            "right:smiley",
        ]
    )


def test_unencoded_alternate_remains_its_own_record(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    _, records = _load_pair(*two_supplier_ufos)
    catalog = resolve_catalog(*_load_pair(*two_supplier_ufos))

    alternate = catalog.record("right:A.ss01")
    assert alternate.unencoded
    assert alternate.stable_id is None
    assert alternate.unicodes == ()
    assert "right:A.ss01" in {item.record for item in catalog.unmatched}
    # It was not merged with right:A just because the name prefix matches.
    assert all(
        "right:A.ss01" not in (match.left, match.right) for match in catalog.matches
    )


def test_multiple_candidates_form_one_unsplittable_group(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    catalog = resolve_catalog(*_load_pair(*two_supplier_ufos))

    groups = [
        group
        for group in catalog.candidate_groups
        if "right:C.alt1" in group.records
    ]
    assert len(groups) == 1
    group = groups[0]
    assert set(group.records) == {"left:uni0043", "right:C.alt1", "right:C.alt2"}
    assert group.unicodes == (0x43,)
    assert group.status == "ambiguous"
    assert any(
        "left:uni0043" not in (match.left, match.right)
        for match in catalog.matches
    )


def test_name_versus_stable_id_conflict_preserves_both_claims(
    conflict_ufos: tuple[Path, Path],
) -> None:
    catalog = resolve_catalog(*_load_pair(*conflict_ufos))

    conflicts = [
        conflict for conflict in catalog.conflicts if conflict.record == "left:A"
    ]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    bases = {claim.basis: claim for claim in conflict.claims}

    # Name points at right:A; stable id points at right:B.
    assert bases["name"].targets == ("right:A",)
    assert bases["stable-id"].targets == ("right:B",)
    assert {tuple(pair) for pair in conflict.disagreements} == {
        ("stable-id", "unicode"),
        ("stable-id", "name"),
    }

    # Neither claim may silently win.
    assert {("left:A", "right:A"), ("left:A", "right:B")}.isdisjoint(
        {(match.left, match.right) for match in catalog.matches}
    )
    conflicted_group = next(
        group
        for group in catalog.candidate_groups
        if "left:A" in group.records
    )
    assert set(conflicted_group.records) == {
        "left:A",
        "left:B",
        "right:A",
        "right:B",
    }
    assert conflicted_group.status == "conflicted"

    # right:B sees the mirror-image disagreement: its stable id points at
    # left:A while its Unicode set points at left:B.
    mirror = next(
        conflict
        for conflict in catalog.conflicts
        if conflict.record == "right:B"
    )
    mirror_claims = {claim.basis: claim for claim in mirror.claims}
    assert mirror_claims["stable-id"].targets == ("left:A",)
    assert mirror_claims["unicode"].targets == ("left:B",)


def test_result_is_independent_of_record_order(
    conflict_ufos: tuple[Path, Path],
) -> None:
    sources, records = _load_pair(*conflict_ufos)
    first = resolve_catalog(sources, records)
    second = resolve_catalog(sources, list(reversed(records)))
    assert first.to_json() == second.to_json()


def test_swapping_supplier_sides_does_not_change_resolution(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    left, right = two_supplier_ufos
    specs = [
        SourceSpec("left", left, stable_id_key="com.example.stableId"),
        SourceSpec("right", right, stable_id_key="com.example.stableId"),
    ]
    sources, records = [], []
    for spec in specs:
        info, source_records = load_source(spec)
        sources.append(info)
        records.extend(source_records)
    first = resolve_catalog(sources, records)

    swapped_sources, swapped_records = [], []
    for spec in reversed(specs):
        info, source_records = load_source(spec)
        swapped_sources.append(info)
        swapped_records.extend(source_records)
    second = resolve_catalog(swapped_sources, swapped_records)

    def pair_sets(catalog: Catalog) -> set[tuple[str, str]]:
        return {
            tuple(sorted((match.left, match.right))) for match in catalog.matches
        }

    assert pair_sets(first) == pair_sets(second)
    assert {group.records for group in first.candidate_groups} == {
        group.records for group in second.candidate_groups
    }
    assert {tuple(sorted((c.record, c.id))) for c in first.conflicts} == {
        tuple(sorted((c.record, c.id))) for c in second.conflicts
    }


def test_manual_mapping_confirms_otherwise_ambiguous_pair(
    two_supplier_ufos: tuple[Path, Path],
    write_json: Any,
) -> None:
    sources, records = _load_pair(*two_supplier_ufos)
    manual = normalize_manual_mapping(
        {"pairs": [{"left": "uni0043", "right": "C.alt2", "note": "design review"}]},
        [
            SourceSpec("left", two_supplier_ufos[0]),
            SourceSpec("right", two_supplier_ufos[1]),
        ],
        records,
    )
    catalog = resolve_catalog(sources, records, manual)

    match = next(match for match in catalog.matches if match.left == "left:uni0043")
    assert match.right == "right:C.alt2"
    assert match.basis == ("manual", "unicode")
    assert "human-confirmed" in match.evidence[0].detail


def test_manual_mapping_rejects_one_to_many(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    specs = [
        SourceSpec("left", two_supplier_ufos[0]),
        SourceSpec("right", two_supplier_ufos[1]),
    ]
    _, records = _load_pair(*two_supplier_ufos)
    with pytest.raises(CatalogError, match="conflicting human-confirmed mappings"):
        normalize_manual_mapping(
            [
                {"left": "uni0043", "right": "C.alt1"},
                {"left": "uni0043", "right": "C.alt2"},
            ],
            specs,
            records,
        )


def test_stable_id_file_duplicate_keys_are_an_error(
    two_supplier_ufos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    path = tmp_path / "stable.json"
    path.write_text('{"left": {"uni0041": "x", "uni0041": "y"}}', encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate JSON key"):
        from ufoLib2.catalog import load_json_file

        load_json_file(path)


def test_duplicate_stable_id_within_source_is_a_candidate_group(
    two_supplier_ufos: tuple[Path, Path],
) -> None:
    sources, records = _load_pair(*two_supplier_ufos)
    repeated = [
        replace(record, stable_id="dup") if record.id == "right:C.alt1" else record
        for record in records
    ]
    repeated = [
        replace(record, stable_id="dup") if record.id == "right:C.alt2" else record
        for record in repeated
    ]
    catalog = resolve_catalog(sources, repeated)
    group = next(
        group for group in catalog.candidate_groups if "right:C.alt1" in group.records
    )
    assert {"right:C.alt1", "right:C.alt2"} <= set(group.records)
    assert any(
        evidence.basis == "stable-id" and "not unique" in evidence.detail
        for evidence in group.evidence
    )


def test_manual_override_keeps_disagreeing_claims_visible(
    conflict_ufos: tuple[Path, Path],
) -> None:
    sources, records = _load_pair(*conflict_ufos)
    manual = normalize_manual_mapping(
        {
            "pairs": [
                {
                    "left": "A",
                    "right": "B",
                    "note": "confirm the stable-id reading",
                }
            ]
        },
        [
            SourceSpec("left", conflict_ufos[0]),
            SourceSpec("right", conflict_ufos[1]),
        ],
        records,
    )
    catalog = resolve_catalog(sources, records, manual)

    match = next(match for match in catalog.matches if match.left == "left:A")
    assert match.right == "right:B"
    assert "manual" in match.basis

    override = next(
        conflict
        for conflict in catalog.conflicts
        if conflict.record == "left:A" and conflict.kind == "manual-override"
    )
    claims = {claim.basis: claim.targets for claim in override.claims}
    assert claims["manual"] == ("right:B",)
    assert claims["stable-id"] == ("right:B",)
    assert claims["name"] == ("right:A",)
    assert claims["unicode"] == ("right:A",)
    assert ("manual", "name") in override.disagreements
    assert ("manual", "unicode") in override.disagreements


def test_summary_is_plain_and_mentions_both_claim_targets(
    conflict_ufos: tuple[Path, Path],
) -> None:
    catalog = resolve_catalog(*_load_pair(*conflict_ufos))
    text = summarize(catalog)
    assert "Confirmed one-to-one correspondences" in text
    assert "right:A" in text
    assert "right:B" in text
    assert "stable-id" in text
    assert "name" in text
