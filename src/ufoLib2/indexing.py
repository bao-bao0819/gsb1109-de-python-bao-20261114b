"""Layer-priority glyph index with incremental rebuild planning.

This module implements a retrieval index over the layers of a
:class:`ufoLib2.objects.Font`. Given a business-defined layer priority (an
ordering of layer names, highest priority first), the index keeps one record
per glyph name: the version found in the first priority layer that contains
it, together with a summary of the components that version holds. Component
references always resolve in the layer the referencing glyph lives in, as
prescribed by the UFO data model.

When the source package is updated, the update is described as a list of
:class:`GlyphChange` objects (glyphs added, modified or removed per layer) and
:meth:`GlyphIndex.plan_rebuild` computes which index records must be rebuilt:

- A glyph modified in the layer its record currently resolves to invalidates
  that record.
- A glyph removed from its record's layer makes the record fall back to a
  lower-priority layer (or disappear); both count as changes.
- A glyph added in a higher-priority layer shadows the previous version and
  invalidates the record; a glyph changed in a layer that stays shadowed by a
  higher-priority layer does not.
- Records depending on a changed glyph through component references (resolved
  in the record's own layer) are invalidated as well, transitively across
  multiple glyphs.

Every affected record carries an explanation path leading from the patch
change to the record, so the processing volume of an update round can be
audited.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from typing import Literal

from attrs import define, field
from fontTools.misc.transform import Transform

from ufoLib2.objects.font import Font

__all__ = [
    "AffectedRecord",
    "ComponentSummary",
    "GlyphChange",
    "GlyphIndex",
    "GlyphRecord",
    "Hop",
    "RebuildPlan",
]

ChangeKind = Literal["added", "modified", "removed"]
"""The kind of a single glyph-level change in a source package update."""

RecordStatus = Literal["new", "rebuild", "removed"]
"""What an update round does to an index record."""

HopVia = Literal["added", "modified", "removed", "component", "fallback", "override"]
"""How one hop of an explanation path was reached."""


@define(frozen=True)
class GlyphChange:
    """A single glyph-level change in a source package update."""

    layer: str
    """The name of the layer the change happened in."""

    glyph: str
    """The name of the glyph that changed."""

    kind: ChangeKind
    """Whether the glyph was added, modified or removed."""


@define(frozen=True)
class ComponentSummary:
    """A summary of one component of the indexed glyph version."""

    baseGlyph: str
    """The name of the referenced glyph, resolved in the same layer."""

    transformation: Transform
    """The affine transformation of the component."""


@define(frozen=True)
class GlyphRecord:
    """One index record: the effective version of a glyph name."""

    name: str
    """The glyph name the record is indexed under."""

    layer: str
    """The first priority layer that contains this glyph name."""

    components: tuple[ComponentSummary, ...]
    """The summary of the components of this version, resolved in its layer."""


@define(frozen=True)
class Hop:
    """One step of an explanation path from a patch change to a record."""

    layer: str
    """The layer of the glyph this step is about."""

    glyph: str
    """The glyph this step is about."""

    via: HopVia
    """How this step was reached.

    The first hop of a path uses the patch change kind (``added``,
    ``modified`` or ``removed``). Subsequent hops use ``component`` (the
    glyph of this hop references the glyph of the previous hop in this hop's
    layer), ``fallback`` (the previous version disappeared and the record
    resolves to this hop's layer now) or ``override`` (this hop's layer now
    shadows the layer the record resolved to before).
    """


@define(frozen=True)
class AffectedRecord:
    """An index record that must be rebuilt, with its explanation path."""

    name: str
    """The glyph name of the record."""

    status: RecordStatus
    """Whether the record is new, must be rebuilt or must be removed."""

    path: tuple[Hop, ...]
    """A shortest explanation path from the patch to this record."""


@define
class RebuildPlan:
    """The set of index records an update round must rebuild."""

    affected: dict[str, AffectedRecord] = field(factory=dict)
    """The affected records, keyed by glyph name."""

    def __len__(self) -> int:
        """The number of records to process in this round."""
        return len(self.affected)

    def __iter__(self) -> Iterator[AffectedRecord]:
        return iter(self.affected.values())

    @property
    def names(self) -> list[str]:
        """The sorted glyph names of all affected records."""
        return sorted(self.affected)

    def by_status(self, status: RecordStatus) -> list[AffectedRecord]:
        """The affected records with the given status, sorted by name."""
        return sorted(
            (record for record in self.affected.values() if record.status == status),
            key=lambda record: record.name,
        )


@define
class GlyphIndex:
    """A retrieval index over the layers of a font, by layer priority.

    For each glyph name present in any of the priority layers, the index
    holds the :class:`GlyphRecord` of the first priority layer containing it.
    """

    layer_priority: tuple[str, ...]
    """The layer names to consider, highest priority first."""

    records: dict[str, GlyphRecord]
    """The index records, keyed by glyph name."""

    presence: dict[str, set[str]]
    """The glyph names present in each priority layer."""

    _referrers: dict[tuple[str, str], set[str]]
    """Maps (layer, glyph name) to the records referencing it as a component."""

    @classmethod
    def build(cls, font: Font, layer_priority: Iterable[str]) -> GlyphIndex:
        """Build an index over ``font`` for the given layer priority.

        Args:
            font: The font to index.
            layer_priority: Layer names, highest priority first. Each must
                exist in the font and occur at most once.
        """
        priority = tuple(layer_priority)
        if not priority:
            raise ValueError("layer_priority must not be empty")
        if len(set(priority)) != len(priority):
            raise ValueError("layer_priority must not contain duplicates")
        for layer_name in priority:
            if layer_name not in font.layers:
                raise KeyError(f"layer {layer_name!r} not found in font")

        presence = {
            layer_name: set(font.layers[layer_name].keys()) for layer_name in priority
        }
        records: dict[str, GlyphRecord] = {}
        for layer_name in priority:
            for glyph_name in sorted(presence[layer_name]):
                if glyph_name in records:
                    continue  # shadowed by a higher-priority layer
                record = _read_record(font, layer_name, glyph_name)
                records[glyph_name] = record
        return cls(priority, records, presence, _collect_referrers(records.values()))

    def plan_rebuild(self, changes: Iterable[GlyphChange]) -> RebuildPlan:
        """Compute which records a source package update must rebuild.

        Args:
            changes: The glyph-level changes of the update, described against
                the state this index was built from. Changes in layers outside
                the layer priority are ignored.

        Returns:
            A :class:`RebuildPlan` mapping each affected glyph name to its
            status and a shortest explanation path from the patch to it.
        """
        rank = {layer_name: i for i, layer_name in enumerate(self.layer_priority)}
        presence = {name: set(glyphs) for name, glyphs in self.presence.items()}
        pair_paths: dict[tuple[str, str], tuple[Hop, ...]] = {}
        affected: dict[str, AffectedRecord] = {}
        queue: deque[tuple[str, str]] = deque()

        def seed_pair(layer: str, glyph: str, path: tuple[Hop, ...]) -> None:
            pair = (layer, glyph)
            if pair not in pair_paths:
                pair_paths[pair] = path
                queue.append(pair)

        def mark(
            name: str,
            status: RecordStatus,
            path: tuple[Hop, ...],
            propagate: bool = False,
        ) -> None:
            if name in affected:
                return
            affected[name] = AffectedRecord(name, status, path)
            record = self.records.get(name)
            if propagate and record is not None:
                # The resolved content of this glyph in the record's layer
                # changed, so dependents resolving it there must be rebuilt
                # as well. (Fallback and override move the record to another
                # layer instead; the patch change itself already seeds the
                # pairs those cases affect.)
                seed_pair(record.layer, name, path)

        for change in changes:
            if change.layer not in rank:
                continue
            hop = Hop(change.layer, change.glyph, change.kind)
            seed_pair(change.layer, change.glyph, (hop,))
            record = self.records.get(change.glyph)
            if change.kind == "modified":
                if record is not None and record.layer == change.layer:
                    mark(change.glyph, "rebuild", (hop,))
            elif change.kind == "removed":
                presence[change.layer].discard(change.glyph)
                if record is not None and record.layer == change.layer:
                    new_layer = next(
                        (
                            layer_name
                            for layer_name in self.layer_priority
                            if change.glyph in presence[layer_name]
                        ),
                        None,
                    )
                    fallback_layer = (
                        new_layer if new_layer is not None else record.layer
                    )
                    path = (hop, Hop(fallback_layer, change.glyph, "fallback"))
                    mark(
                        change.glyph,
                        "rebuild" if new_layer is not None else "removed",
                        path,
                    )
            else:  # added
                presence[change.layer].add(change.glyph)
                if record is None:
                    mark(change.glyph, "new", (hop,))
                elif rank[change.layer] <= rank[record.layer]:
                    path = (hop, Hop(change.layer, change.glyph, "override"))
                    mark(change.glyph, "rebuild", path)

        while queue:
            pair = queue.popleft()
            path = pair_paths[pair]
            for name in self._referrers.get(pair, ()):
                if name not in affected:
                    record = self.records[name]
                    mark(
                        name,
                        "rebuild",
                        path + (Hop(record.layer, name, "component"),),
                        propagate=True,
                    )

        return RebuildPlan(affected)

    def apply(self, plan: RebuildPlan, font: Font) -> None:
        """Apply a rebuild plan, re-reading affected records from ``font``.

        Args:
            plan: The plan returned by :meth:`plan_rebuild` for the update.
            font: The font in its updated state.
        """
        for layer_name in self.layer_priority:
            self.presence[layer_name] = set(font.layers[layer_name].keys())
        for name, affected_record in plan.affected.items():
            if affected_record.status == "removed":
                self.records.pop(name, None)
                continue
            for layer_name in self.layer_priority:
                if name in self.presence[layer_name]:
                    self.records[name] = _read_record(font, layer_name, name)
                    break
            else:
                self.records.pop(name, None)
        self._referrers = _collect_referrers(self.records.values())


def _read_record(font: Font, layer_name: str, glyph_name: str) -> GlyphRecord:
    glyph = font.layers[layer_name][glyph_name]
    components = tuple(
        ComponentSummary(component.baseGlyph, component.transformation)
        for component in glyph.components
    )
    return GlyphRecord(glyph_name, layer_name, components)


def _collect_referrers(
    records: Iterable[GlyphRecord],
) -> dict[tuple[str, str], set[str]]:
    referrers: dict[tuple[str, str], set[str]] = {}
    for record in records:
        for component in record.components:
            pair = (record.layer, component.baseGlyph)
            referrers.setdefault(pair, set()).add(record.name)
    return referrers
