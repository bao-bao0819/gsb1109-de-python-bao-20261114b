from __future__ import annotations

import json
from pathlib import Path

import pytest

from ufoLib2.packing import (
    DEFAULT_LAYER,
    GlyphRef,
    GlyphSpec,
    PackageManifest,
    PackingPlan,
    main,
    plan_packages,
    specs_from_dict,
)


def spec(
    name: str,
    size: int,
    components: tuple[str, ...] = (),
    layer: str = DEFAULT_LAYER,
) -> GlyphSpec:
    return GlyphSpec(name=name, size=size, components=components, layer=layer)


def keys(package: PackageManifest) -> set[tuple[str, str]]:
    return {ref.key for ref in package.glyphs}


def closure_keys(
    specs: list[GlyphSpec], target: tuple[str, str]
) -> set[tuple[str, str]]:
    by_key = {glyph.key: glyph for glyph in specs}
    seen: set[tuple[str, str]] = set()
    stack = [target]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for component in by_key[current].components:
            stack.append((current[0], component))
    return seen


def assert_readable(
    plan: PackingPlan, specs: list[GlyphSpec], required: list[tuple[str, str]]
) -> None:
    """Every required, non-overflow glyph is delivered with its full closure."""
    for target in required:
        if any(
            item.layer == target[0] and item.name == target[1]
            for item in plan.overflow
        ):
            continue
        package_index = plan.assignments[target]
        members = keys(plan.packages[package_index])
        assert closure_keys(specs, target) <= members


def test_single_glyph_fits() -> None:
    plan = plan_packages([spec("a", 100)], capacity=100)
    assert len(plan.packages) == 1
    assert plan.packages[0].size == 100
    assert plan.packages[0].glyphs == (GlyphRef(DEFAULT_LAYER, "a", 100),)
    assert plan.overflow == []
    assert plan.assignments == {(DEFAULT_LAYER, "a"): 0}
    assert plan.shipped_bytes == 100
    assert plan.unique_bytes == 100
    assert plan.duplicated_bytes == 0


def test_components_counted_once_per_package() -> None:
    # a and c both use b; b also uses d: the shared dependency bytes are
    # counted only once inside the same package.
    specs = [
        spec("a", 100, ("b",)),
        spec("c", 80, ("b",)),
        spec("b", 40, ("d",)),
        spec("d", 10),
    ]
    plan = plan_packages(specs, capacity=1000)
    assert len(plan.packages) == 1
    package = plan.packages[0]
    assert package.size == 230
    assert keys(package) == {
        (DEFAULT_LAYER, "a"),
        (DEFAULT_LAYER, "b"),
        (DEFAULT_LAYER, "c"),
        (DEFAULT_LAYER, "d"),
    }
    assert plan.duplicated_bytes == 0


def test_nested_components_expanded_transitively() -> None:
    specs = [
        spec("root", 10, ("mid",)),
        spec("mid", 20, ("leaf",)),
        spec("leaf", 30),
    ]
    plan = plan_packages(specs, capacity=1000)
    assert plan.packages[0].size == 60
    assert plan.assignments[(DEFAULT_LAYER, "root")] == 0


def test_splitting_duplicates_shared_components() -> None:
    # a alone already fills 140 of 150; c (plus shared b) needs a new package,
    # so b's 40 bytes travel twice.
    specs = [
        spec("a", 100, ("b",)),
        spec("b", 40),
        spec("c", 30, ("b",)),
    ]
    plan = plan_packages(specs, capacity=150)
    assert len(plan.packages) == 2
    assert all(package.size <= 150 for package in plan.packages)
    assert plan.shipped_bytes == 140 + 70
    assert plan.unique_bytes == 170
    assert plan.duplicated_bytes == 40
    assert_readable(plan, specs, [(DEFAULT_LAYER, name) for name in ("a", "b", "c")])


def test_greedy_reuses_package_with_most_overlap() -> None:
    # Two existing packages both have room; the one already carrying the
    # dependency must be preferred to avoid re-shipping component bytes.
    specs = [
        spec("a", 50, ("shared",)),
        spec("z", 50),
        spec("shared", 40),
        spec("b", 10, ("shared",)),
    ]
    plan = plan_packages(specs, capacity=100)
    # a + shared fill one package (90); z opens another; b joins a's package
    # because shared is already there, adding only 10 bytes.
    b_package = plan.assignments[(DEFAULT_LAYER, "b")]
    assert (DEFAULT_LAYER, "shared") in keys(plan.packages[b_package])
    assert plan.packages[b_package].size == 100
    assert plan.duplicated_bytes == 0


def test_layers_with_same_names_are_not_shared() -> None:
    specs = [
        spec("a", 60, ("b",), layer="L1"),
        spec("b", 40, layer="L1"),
        spec("a", 60, ("b",), layer="L2"),
        spec("b", 40, layer="L2"),
    ]
    plan = plan_packages(specs, capacity=100)
    assert len(plan.packages) == 2
    for package in plan.packages:
        layer_names = {ref.layer for ref in package.glyphs}
        assert len(layer_names) == 1
        assert package.size == 100
    assert plan.duplicated_bytes == 0


def test_overflow_reports_minimal_complete_closure() -> None:
    specs = [
        spec("big", 200, ("mid",)),
        spec("mid", 40, ("leaf",)),
        spec("leaf", 10),
    ]
    plan = plan_packages(specs, capacity=100, required=[("public.default", "big")])
    assert plan.packages == []
    assert plan.assignments == {}
    assert len(plan.overflow) == 1
    overflow = plan.overflow[0]
    assert overflow.name == "big"
    assert overflow.size == 250
    assert [(ref.layer, ref.name) for ref in overflow.closure] == [
        (DEFAULT_LAYER, "big"),
        (DEFAULT_LAYER, "leaf"),
        (DEFAULT_LAYER, "mid"),
    ]


def test_overflow_does_not_spill_dependencies_into_other_packages() -> None:
    # Only "big" is required: its unshippable dependencies must not appear in
    # any regular package behind the caller's back.
    specs = [
        spec("big", 200, ("dep",)),
        spec("dep", 50),
    ]
    plan = plan_packages(specs, capacity=100, required=["big"])
    assert plan.packages == []
    assert len(plan.overflow) == 1
    shipped = {ref.key for package in plan.packages for ref in package.glyphs}
    assert shipped == set()


def test_overflow_mixes_with_feasible_packages() -> None:
    specs = [
        spec("big", 200, ("dep",)),
        spec("dep", 50),
        spec("ok", 30),
    ]
    plan = plan_packages(specs, capacity=100, required=["big", "ok"])
    assert len(plan.overflow) == 1
    assert len(plan.packages) == 1
    assert plan.packages[0].glyphs == (GlyphRef(DEFAULT_LAYER, "ok", 30),)
    assert (DEFAULT_LAYER, "dep") not in keys(plan.packages[0])


def test_component_cycles_are_supported() -> None:
    specs = [
        spec("a", 10, ("b",)),
        spec("b", 20, ("a",)),
    ]
    plan = plan_packages(specs, capacity=1000)
    assert len(plan.packages) == 1
    assert plan.packages[0].size == 30


def test_missing_component_raises() -> None:
    with pytest.raises(KeyError, match="unknown glyph"):
        plan_packages([spec("a", 10, ("ghost",))], capacity=100)


def test_unknown_required_glyph_raises() -> None:
    with pytest.raises(KeyError, match="unknown required glyph"):
        plan_packages([spec("a", 10)], capacity=100, required=["ghost"])


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="capacity"):
        plan_packages([spec("a", 10)], capacity=-1)
    with pytest.raises(ValueError, match="negative size"):
        plan_packages([spec("a", -1)], capacity=100)
    with pytest.raises(ValueError, match="duplicate glyph"):
        plan_packages([spec("a", 10), spec("a", 20)], capacity=100)


def test_zero_capacity() -> None:
    plan = plan_packages([spec("a", 0), spec("b", 1)], capacity=0)
    assert len(plan.packages) == 1
    assert plan.packages[0].size == 0
    assert len(plan.overflow) == 1
    assert plan.overflow[0].name == "b"


def test_explicit_required_subset() -> None:
    specs = [spec("a", 10, ("b",)), spec("b", 20), spec("c", 30)]
    plan = plan_packages(specs, capacity=1000, required=["a", ("public.default", "c")])
    shipped = {ref.key for package in plan.packages for ref in package.glyphs}
    assert (DEFAULT_LAYER, "a") in shipped
    assert (DEFAULT_LAYER, "b") in shipped
    assert (DEFAULT_LAYER, "c") in shipped
    assert set(plan.assignments) == {
        (DEFAULT_LAYER, "a"),
        (DEFAULT_LAYER, "c"),
    }


def test_duplicate_required_entries_are_deduplicated() -> None:
    specs = [spec("big", 200), spec("ok", 10)]
    plan = plan_packages(specs, capacity=100, required=["big", "big", "ok"])
    assert len(plan.overflow) == 1
    assert plan.overflow[0].name == "big"
    assert len(plan.packages) == 1
    assert set(plan.assignments) == {(DEFAULT_LAYER, "ok")}


def test_manifest_is_deterministic() -> None:
    specs = [
        spec("a", 100, ("b",)),
        spec("b", 40),
        spec("c", 30, ("b",)),
    ]
    first = plan_packages(specs, capacity=150).to_dict()
    second = plan_packages(list(reversed(specs)), capacity=150).to_dict()
    assert first == second
    json.dumps(first)  # the manifest is JSON serializable


def test_to_dict_structure() -> None:
    plan = plan_packages([spec("a", 10, ("b",)), spec("b", 5)], capacity=100)
    data = plan.to_dict()
    assert data["capacity"] == 100
    assert data["packages"][0]["index"] == 0
    assert data["totals"] == {
        "shipped_bytes": 15,
        "unique_bytes": 15,
        "duplicated_bytes": 0,
    }
    entry = data["assignments"][0]
    assert set(entry) == {"layer", "name", "package"}


def test_specs_from_dict_roundtrip() -> None:
    data = {
        "capacity": 100,
        "glyphs": [
            {"name": "a", "size": 60, "components": ["b"], "layer": "L1"},
            {"name": "b", "size": 40, "layer": "L1"},
        ],
        "required": [["L1", "a"]],
    }
    specs, required, capacity = specs_from_dict(data)
    assert capacity == 100
    assert specs == [
        GlyphSpec("a", 60, ("b",), "L1"),
        GlyphSpec("b", 40, (), "L1"),
    ]
    assert required == [("L1", "a")]
    plan = plan_packages(specs, capacity, required)
    assert len(plan.packages) == 1


def test_cli_writes_manifest(tmp_path: Path) -> None:
    input_path = tmp_path / "glyphs.json"
    output_path = tmp_path / "manifest.json"
    input_path.write_text(
        json.dumps(
            {
                "capacity": 150,
                "glyphs": [
                    {"name": "a", "size": 100, "components": ["b"]},
                    {"name": "b", "size": 40},
                    {"name": "c", "size": 30, "components": ["b"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main([str(input_path), "-o", str(output_path)]) == 0
    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(manifest["packages"]) == 2
    assert manifest["totals"]["duplicated_bytes"] == 40
    assert manifest["overflow"] == []
