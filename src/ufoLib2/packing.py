"""Offline glyph package splitting.

The offline annotation station can only load one glyph package of a limited
byte capacity at a time. Every package must be independently readable, so a
glyph has to travel together with the transitive closure of all of its
components. Components that are themselves composite glyphs are expanded
recursively; within one package every glyph is counted only once, and
same-named glyphs living in different layers are never shared.

Given the measured byte size of every glyph, the component relationships and
the package capacity, :func:`plan_packages` produces a deterministic packing
manifest that tries to keep the number of component bytes duplicated across
packages low. A required glyph whose minimal complete closure does not fit a
single package is reported separately (with its full composition) instead of
silently spilling its dependencies into another package.

Example:
    >>> specs = [
    ...     GlyphSpec("a", 100, ("b",)),
    ...     GlyphSpec("b", 40),
    ...     GlyphSpec("c", 30, ("b",)),
    ... ]
    >>> plan = plan_packages(specs, capacity=150)
    >>> len(plan.packages)
    2
    >>> plan.duplicated_bytes
    40

A glyph that cannot fit together with its dependencies is listed as overflow:

    >>> plan = plan_packages(
    ...     [GlyphSpec("x", 200, ("y",)), GlyphSpec("y", 50)],
    ...     capacity=100,
    ...     required=["x"],
    ... )
    >>> plan.packages
    []
    >>> plan.overflow[0].size
    250
    >>> sorted(ref.name for ref in plan.overflow[0].closure)
    ['x', 'y']

Same-named glyphs in different layers are packed independently:

    >>> specs = [
    ...     GlyphSpec("a", 60, ("b",), layer="L1"),
    ...     GlyphSpec("b", 40, layer="L1"),
    ...     GlyphSpec("a", 60, ("b",), layer="L2"),
    ...     GlyphSpec("b", 40, layer="L2"),
    ... ]
    >>> plan = plan_packages(specs, capacity=100)
    >>> len(plan.packages)
    2
    >>> plan.duplicated_bytes
    0
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from attrs import define, field, frozen

DEFAULT_LAYER = "public.default"

GlyphKey = tuple[str, str]
"""A glyph is uniquely identified by its ``(layer name, glyph name)`` pair."""


@frozen
class GlyphSpec:
    """Measured size and direct component relationships of one glyph.

    Components always refer to glyphs in the same layer.

    Args:
        name: The glyph name inside its layer.
        size: The measured byte size of the glyph itself.
        components: Names of the glyphs directly referenced as components.
        layer: The layer the glyph belongs to.
    """

    name: str
    size: int
    components: tuple[str, ...] = ()
    layer: str = DEFAULT_LAYER

    @property
    def key(self) -> GlyphKey:
        return (self.layer, self.name)


@frozen
class GlyphRef:
    """A glyph reference inside a generated manifest."""

    layer: str
    name: str
    size: int

    @property
    def key(self) -> GlyphKey:
        return (self.layer, self.name)


@frozen
class PackageManifest:
    """One independently readable package."""

    index: int
    size: int
    glyphs: tuple[GlyphRef, ...]


@frozen
class OverflowManifest:
    """A required glyph whose complete closure exceeds the package capacity.

    Attributes:
        layer: Layer of the required glyph.
        name: Name of the required glyph.
        size: Minimal complete byte size, counting every dependency once.
        closure: The full composition (the glyph itself and all transitive
            components), sorted by ``(layer, name)``.
    """

    layer: str
    name: str
    size: int
    closure: tuple[GlyphRef, ...]


@define
class PackingPlan:
    """The resulting split manifest."""

    capacity: int
    packages: list[PackageManifest] = field(factory=list)
    overflow: list[OverflowManifest] = field(factory=list)
    assignments: dict[GlyphKey, int] = field(factory=dict)

    @property
    def shipped_bytes(self) -> int:
        """Total bytes physically shipped across all packages."""
        return sum(package.size for package in self.packages)

    @property
    def unique_bytes(self) -> int:
        """Bytes of the distinct glyphs shipped at least once."""
        return self._union_size()

    def _union_size(self) -> int:
        seen: set[GlyphKey] = set()
        total = 0
        for package in self.packages:
            for ref in package.glyphs:
                if ref.key not in seen:
                    seen.add(ref.key)
                    total += ref.size
        return total

    @property
    def duplicated_bytes(self) -> int:
        """Component bytes shipped more than once across packages."""
        return self.shipped_bytes - self._union_size()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the manifest."""
        return {
            "capacity": self.capacity,
            "packages": [
                {
                    "index": package.index,
                    "size": package.size,
                    "glyphs": [
                        {"layer": ref.layer, "name": ref.name, "size": ref.size}
                        for ref in package.glyphs
                    ],
                }
                for package in self.packages
            ],
            "overflow": [
                {
                    "layer": item.layer,
                    "name": item.name,
                    "size": item.size,
                    "closure": [
                        {"layer": ref.layer, "name": ref.name, "size": ref.size}
                        for ref in item.closure
                    ],
                }
                for item in self.overflow
            ],
            "assignments": [
                {"layer": layer, "name": name, "package": index}
                for (layer, name), index in sorted(self.assignments.items())
            ],
            "totals": {
                "shipped_bytes": self.shipped_bytes,
                "unique_bytes": self._union_size(),
                "duplicated_bytes": self.duplicated_bytes,
            },
        }


@define
class _Bin:
    """Mutable package used while the greedy packing is built."""

    index: int
    members: dict[GlyphKey, int] = field(factory=dict)

    @property
    def size(self) -> int:
        return sum(self.members.values())

    def added_cost(
        self, closure: frozenset[GlyphKey], sizes: Mapping[GlyphKey, int]
    ) -> int:
        return sum(sizes[key] for key in closure if key not in self.members)

    def add(self, closure: Iterable[GlyphKey], sizes: Mapping[GlyphKey, int]) -> None:
        for key in closure:
            self.members.setdefault(key, sizes[key])


def _normalize_key(
    value: str | GlyphKey, default_layer: str = DEFAULT_LAYER
) -> GlyphKey:
    if isinstance(value, str):
        return (default_layer, value)
    layer, name = value
    return (layer, name)


def _build_spec_map(specs: Iterable[GlyphSpec]) -> dict[GlyphKey, GlyphSpec]:
    spec_map: dict[GlyphKey, GlyphSpec] = {}
    for spec in specs:
        if spec.size < 0:
            raise ValueError(f"glyph {spec.key!r} has a negative size: {spec.size}")
        if spec.key in spec_map:
            raise ValueError(f"duplicate glyph specification for {spec.key!r}")
        spec_map[spec.key] = spec
    return spec_map


def _closure(
    root: GlyphKey,
    spec_map: Mapping[GlyphKey, GlyphSpec],
    memo: dict[GlyphKey, frozenset[GlyphKey]],
) -> frozenset[GlyphKey]:
    """Return the transitive component closure of ``root`` (itself included).

    Component cycles are tolerated. Missing component references raise
    :class:`KeyError`, mirroring the behavior of looking up an absent glyph.
    """
    if root in memo:
        return memo[root]
    seen: set[GlyphKey] = set()
    stack: list[GlyphKey] = [root]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in memo:
            seen.update(memo[current])
            continue
        try:
            spec = spec_map[current]
        except KeyError:
            raise KeyError(f"unknown glyph referenced as a component: {current!r}")
        for component in spec.components:
            child = (spec.layer, component)
            if child not in seen:
                stack.append(child)
    result = frozenset(seen)
    memo[root] = result
    return result


def plan_packages(
    specs: Iterable[GlyphSpec],
    capacity: int,
    required: Iterable[str | GlyphKey] | None = None,
) -> PackingPlan:
    """Split required glyphs into independently readable packages.

    Args:
        specs: All known glyphs with their measured byte sizes and direct
            component relationships.
        capacity: Maximum total byte size of a single package.
        required: The glyphs that must be delivered, given as bare names (in
            the default layer) or ``(layer, name)`` pairs. When omitted, every
            glyph in ``specs`` is required.

    Returns:
        A :class:`PackingPlan` listing the generated packages, the required
        glyphs whose complete closure does not fit a single package, and the
        package assignment of every deliverable required glyph.

    Raises:
        ValueError: If the capacity is negative, a glyph size is negative or
            a glyph is specified more than once.
        KeyError: If a required glyph or a referenced component is unknown.
    """
    if capacity < 0:
        raise ValueError(f"capacity must be non-negative, got {capacity}")

    spec_map = _build_spec_map(specs)
    sizes = {key: spec.size for key, spec in spec_map.items()}

    if required is None:
        targets = sorted(spec_map)
    else:
        targets = list(dict.fromkeys(_normalize_key(value) for value in required))

    memo: dict[GlyphKey, frozenset[GlyphKey]] = {}
    closures: dict[GlyphKey, frozenset[GlyphKey]] = {}
    closure_sizes: dict[GlyphKey, int] = {}
    for target in targets:
        if target not in spec_map:
            raise KeyError(f"unknown required glyph: {target!r}")
        closure = _closure(target, spec_map, memo)
        closures[target] = closure
        closure_sizes[target] = sum(sizes[key] for key in closure)

    plan = PackingPlan(capacity=capacity)

    overflow_targets = [
        target for target in targets if closure_sizes[target] > capacity
    ]
    for target in sorted(overflow_targets):
        closure = closures[target]
        plan.overflow.append(
            OverflowManifest(
                layer=target[0],
                name=target[1],
                size=closure_sizes[target],
                closure=tuple(
                    GlyphRef(layer=key[0], name=key[1], size=sizes[key])
                    for key in sorted(closure)
                ),
            )
        )

    feasible = [target for target in targets if closure_sizes[target] <= capacity]
    feasible.sort(key=lambda key: (-closure_sizes[key], key))

    bins: list[_Bin] = []
    for target in feasible:
        closure = closures[target]
        best: _Bin | None = None
        best_cost = 0
        for candidate in bins:
            cost = candidate.added_cost(closure, sizes)
            if candidate.size + cost > capacity:
                continue
            if best is None or cost < best_cost or (
                cost == best_cost and candidate.size > best.size
            ):
                best = candidate
                best_cost = cost
        if best is None:
            best = _Bin(index=len(bins))
            best.add(closure, sizes)
            bins.append(best)
        else:
            best.add(closure, sizes)
        plan.assignments[target] = best.index

    for package_bin in bins:
        plan.packages.append(
            PackageManifest(
                index=package_bin.index,
                size=package_bin.size,
                glyphs=tuple(
                    GlyphRef(layer=key[0], name=key[1], size=size)
                    for key, size in sorted(package_bin.members.items())
                ),
            )
        )

    return plan


def specs_from_dict(
    data: Mapping[str, Any],
) -> tuple[list[GlyphSpec], list[GlyphKey] | None, int]:
    """Parse the JSON input format used by the command line interface.

    Expected schema::

        {
          "capacity": 1000,
          "glyphs": [
            {"layer": "default", "name": "a", "size": 120, "components": ["b"]},
            {"layer": "default", "name": "b", "size": 50, "components": []}
          ],
          "required": [["default", "a"], "b"]
        }

    The ``layer`` and ``components`` fields are optional; entries in
    ``required`` may be bare glyph names (default layer) or
    ``[layer, name]`` pairs.
    """
    capacity = int(data["capacity"])
    specs: list[GlyphSpec] = []
    for item in data.get("glyphs", ()):
        specs.append(
            GlyphSpec(
                name=str(item["name"]),
                size=int(item["size"]),
                components=tuple(item.get("components", ())),
                layer=str(item.get("layer", DEFAULT_LAYER)),
            )
        )
    if "required" not in data:
        required = None
    else:
        required = [
            _normalize_key(value)
            for value in data["required"]  # type: ignore[arg-type]
        ]
    return specs, required, capacity


def main(argv: Sequence[str] | None = None) -> int:
    """Read a JSON glyph description and write a packing manifest."""
    parser = argparse.ArgumentParser(
        description="Split glyphs into independently readable offline packages."
    )
    parser.add_argument("input", type=Path, help="JSON file describing the glyphs")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Destination manifest JSON (defaults to standard output)",
    )
    args = parser.parse_args(argv)

    with args.input.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    specs, required, capacity = specs_from_dict(data)
    plan = plan_packages(specs, capacity, required)
    manifest = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)

    if args.output is None:
        print(manifest)
    else:
        args.output.write_text(manifest + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
