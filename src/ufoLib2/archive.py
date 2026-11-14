"""Layer-local glyph collection for archiving a set of characters.

The UFO data model resolves components within the layer that contains the
composite glyph.  This module follows that rule strictly: a glyph with the
same name in another layer is never used as an implicit fallback.  Missing
components are only filled from an explicit substitute mapping.

The collector stores one glyph entity per layer and glyph name, regardless of
how many requests or component chains reach it.  Each request keeps its own
chains, so shared entities remain traceable.  Component rings are reported as
whole rings, and only requests that actually reach a ring are marked by it;
unrelated requests in the same layer are still packaged normally.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from attrs import define, field

from ufoLib2.objects.glyph import Glyph

if TYPE_CHECKING:
    from ufoLib2.objects.font import Font
    from ufoLib2.objects.layer import Layer

__all__ = [
    "Archive",
    "ArchiveIssue",
    "ArchiveRequest",
    "Character",
    "Cycle",
    "GlyphName",
    "GlyphSource",
    "LayerArchive",
    "Substitution",
    "build_archive",
]

RequestValue: TypeAlias = str | int
RequestItem: TypeAlias = "str | int | Character | GlyphName"
SubstituteKey: TypeAlias = "str | tuple[str, str]"
SubstituteTarget: TypeAlias = "GlyphSource | tuple[str, str] | Glyph"

MAX_CHAINS_PER_REQUEST = 10_000
"""Safety limit for simple component paths recorded for one request."""


@define(frozen=True)
class Character:
    """An explicitly typed character request.

    ``value`` is either a Unicode code point (``int``) or a string containing
    exactly one character.  The wrapper removes the ambiguity between a
    one-character request and a one-letter glyph name.
    """

    value: str | int

    def __attrs_post_init__(self) -> None:
        if isinstance(self.value, str):
            if len(self.value) != 1:
                raise ValueError(
                    "Character.value must contain exactly one character, "
                    f"found {self.value!r}"
                )
        elif isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError(
                "Character.value must be a single character or a code point, "
                f"found {type(self.value).__name__}"
            )
        elif not 0 <= self.value <= 0x10FFFF:
            raise ValueError(f"invalid Unicode code point: {self.value!r}")

    @property
    def codepoint(self) -> int:
        """The Unicode code point represented by this request."""
        return self.value if isinstance(self.value, int) else ord(self.value)


@define(frozen=True)
class GlyphName:
    """An explicitly typed glyph-name request."""

    name: str

    def __attrs_post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("GlyphName.name must be a non-empty string")


@define(frozen=True)
class GlyphSource:
    """The layer and glyph name to use for an explicit substitution."""

    layer: str
    glyph: str

    def __attrs_post_init__(self) -> None:
        if not isinstance(self.layer, str) or not self.layer:
            raise ValueError("GlyphSource.layer must be a non-empty string")
        if not isinstance(self.glyph, str) or not self.glyph:
            raise ValueError("GlyphSource.glyph must be a non-empty string")


@define
class ArchiveIssue:
    """A non-fatal problem found while collecting a layer."""

    layer: str
    """The layer the issue was found in."""

    code: str
    """A stable machine-readable issue code."""

    message: str
    """A human-readable description."""

    glyph: str | None = None
    """The glyph that could not be resolved, when applicable."""

    requests: list[str] = field(factory=list)
    """Labels of the requests affected by this issue."""

    def _add_request(self, label: str) -> None:
        if label not in self.requests:
            self.requests.append(label)


@define
class Substitution:
    """Records that a missing glyph was filled from an explicit source."""

    layer: str
    """The target layer."""

    glyph: str
    """The glyph name in the target layer."""

    source_layer: str | None = None
    """The source layer, or ``None`` for an inline glyph entity."""

    source_glyph: str | None = None
    """The source glyph name, or ``None`` for an inline glyph entity."""

    used_by: list[str] = field(factory=list)
    """Labels of requests that needed the substituted entity."""

    def _add_request(self, label: str) -> None:
        if label not in self.used_by:
            self.used_by.append(label)


@define
class Cycle:
    """A component ring found during traversal.

    ``glyphs`` starts and ends with the same glyph name, for example
    ``("B", "C", "B")``.  The full ring is kept so the report identifies the
    whole cycle rather than only the component edge that closed it.
    """

    layer: str
    """The layer containing the ring."""

    glyphs: tuple[str, ...]
    """The closed ring; the first and last entries are identical."""

    requests: list[str] = field(factory=list)
    """Labels of requests whose closure reaches this ring."""

    def __attrs_post_init__(self) -> None:
        if len(self.glyphs) < 2 or self.glyphs[0] != self.glyphs[-1]:
            raise ValueError("Cycle.glyphs must start and end with the same glyph")

    @property
    def members(self) -> tuple[str, ...]:
        """The unique glyph names in the ring, in traversal order."""
        return self.glyphs[:-1]

    def _add_request(self, label: str) -> None:
        if label not in self.requests:
            self.requests.append(label)


@define
class ArchiveRequest:
    """The result of resolving one requested character or glyph in a layer."""

    layer: str
    value: RequestValue
    label: str
    kind: Literal["character", "glyph"]
    glyph: str | None = None
    chains: list[tuple[str, ...]] = field(factory=list)
    missing: list[str] = field(factory=list)
    cycles: list[tuple[str, ...]] = field(factory=list)
    substitutions: list[Substitution] = field(factory=list)
    issue_codes: list[str] = field(factory=list)

    @property
    def complete(self) -> bool:
        """Whether the request and its full component closure were packaged."""
        return self.glyph is not None and not self.missing and not self.cycles

    @property
    def status(self) -> Literal["ok", "missing", "cycle"]:
        """A compact status for manifests."""
        if self.glyph is None or self.missing:
            return "missing"
        if self.cycles:
            return "cycle"
        return "ok"

    def _add_chain(self, chain: Sequence[str]) -> bool:
        """Record a chain; return False once the per-request limit is hit."""
        path = tuple(chain)
        if path in self.chains:
            return True
        if len(self.chains) >= MAX_CHAINS_PER_REQUEST:
            return False
        self.chains.append(path)
        return True

    def _add_missing(self, name: str) -> None:
        if name not in self.missing:
            self.missing.append(name)

    def _add_cycle(self, cycle: Cycle) -> None:
        if cycle.glyphs not in self.cycles:
            self.cycles.append(cycle.glyphs)

    def _add_substitution(self, substitution: Substitution) -> None:
        if substitution not in self.substitutions:
            self.substitutions.append(substitution)

    def _add_issue_code(self, code: str) -> None:
        if code not in self.issue_codes:
            self.issue_codes.append(code)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest entry for this request."""
        return {
            "value": self.value,
            "label": self.label,
            "kind": self.kind,
            "glyph": self.glyph,
            "status": self.status,
            "complete": self.complete,
            "chains": [list(chain) for chain in self.chains],
            "missing": list(self.missing),
            "cycles": [list(cycle) for cycle in self.cycles],
            "substitutions": [
                {
                    "glyph": substitution.glyph,
                    "sourceLayer": substitution.source_layer,
                    "sourceGlyph": substitution.source_glyph,
                }
                for substitution in self.substitutions
            ],
            "issues": list(self.issue_codes),
        }


@define
class LayerArchive:
    """The packaged glyphs and diagnostics for one UFO layer."""

    name: str
    glyphs: dict[str, Glyph] = field(factory=dict)
    requests: list[ArchiveRequest] = field(factory=list)
    cycles: list[Cycle] = field(factory=list)
    substitutions: list[Substitution] = field(factory=list)
    issues: list[ArchiveIssue] = field(factory=list)
    edges: dict[str, tuple[str, ...]] = field(factory=dict)
    """Component edges for every packaged entity, after substitution."""

    def __contains__(self, name: object) -> bool:
        return name in self.glyphs

    def __getitem__(self, name: str) -> Glyph:
        return self.glyphs[name]

    def __iter__(self) -> "Iterator[Glyph]":
        return iter(self.glyphs.values())

    def __len__(self) -> int:
        return len(self.glyphs)

    @property
    def is_complete(self) -> bool:
        """True if every request in this layer was packaged without problems."""
        return all(request.complete for request in self.requests)

    @property
    def missing(self) -> list[str]:
        """All glyph names that could not be resolved in this layer."""
        result: list[str] = []
        for request in self.requests:
            for name in request.missing:
                if name not in result:
                    result.append(name)
        return result

    def provenance(self, glyph: str) -> list[tuple[ArchiveRequest, tuple[str, ...]]]:
        """Return every request chain that reached ``glyph``.

        Each item is a ``(request, chain)`` pair.  ``chain`` starts at the
        request's root glyph and ends at ``glyph``.  A shared base glyph thus
        has one entry per request and per distinct path, while
        :attr:`glyphs` still stores it only once.
        """
        result: list[tuple[ArchiveRequest, tuple[str, ...]]] = []
        for request in self.requests:
            for chain in request.chains:
                if chain and chain[-1] == glyph:
                    result.append((request, chain))
        return result

    def requests_reaching(self, glyph: str) -> list[ArchiveRequest]:
        """Return the distinct requests that reach ``glyph``."""
        result: list[ArchiveRequest] = []
        for request, _chain in self.provenance(glyph):
            if request not in result:
                result.append(request)
        return result

    def to_layer(self) -> "Layer":
        """Create a new :class:`~ufoLib2.objects.Layer` with copied glyphs."""
        from ufoLib2.objects.layer import Layer

        return Layer(
            name=self.name,
            glyphs={name: glyph.copy() for name, glyph in self.glyphs.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest for this layer."""
        return {
            "name": self.name,
            "glyphs": sorted(self.glyphs),
            "edges": {name: list(targets) for name, targets in self.edges.items()},
            "requests": [request.to_dict() for request in self.requests],
            "cycles": [
                {"glyphs": list(cycle.glyphs), "requests": list(cycle.requests)}
                for cycle in self.cycles
            ],
            "substitutions": [
                {
                    "glyph": substitution.glyph,
                    "sourceLayer": substitution.source_layer,
                    "sourceGlyph": substitution.source_glyph,
                    "usedBy": list(substitution.used_by),
                }
                for substitution in self.substitutions
            ],
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "glyph": issue.glyph,
                    "requests": list(issue.requests),
                }
                for issue in self.issues
            ],
        }


@define
class Archive:
    """A per-layer package of glyphs collected for a character list.

    Use :func:`build_archive` or :meth:`ufoLib2.objects.Font.archive` to
    create one.  Each selected layer gets its own :class:`LayerArchive`;
    same-named glyphs in different layers remain independent entities.
    """

    layers: dict[str, LayerArchive]
    source_default_layer: str | None = None

    def __contains__(self, layer_name: object) -> bool:
        return layer_name in self.layers

    def __getitem__(self, layer_name: str) -> LayerArchive:
        return self.layers[layer_name]

    def __iter__(self) -> "Iterator[LayerArchive]":
        return iter(self.layers.values())

    def __len__(self) -> int:
        return len(self.layers)

    @property
    def is_complete(self) -> bool:
        """True if all requests in all selected layers were packaged."""
        return all(layer.is_complete for layer in self.layers.values())

    def to_font(self, default_layer: str | None = None) -> "Font":
        """Build an in-memory :class:`~ufoLib2.objects.Font` from the package.

        Args:
            default_layer: Name of the layer to mark as default.  If omitted,
                the source font's default layer is used when it was selected;
                otherwise the first selected layer is used.
        """
        from ufoLib2.objects.font import Font
        from ufoLib2.objects.layer import Layer
        from ufoLib2.objects.layerSet import LayerSet

        if default_layer is None:
            if self.source_default_layer in self.layers:
                default_layer = self.source_default_layer
            elif self.layers:
                default_layer = next(iter(self.layers))
        if default_layer not in self.layers:
            raise ValueError(f"default layer {default_layer!r} is not in the archive")

        layers = [
            Layer(
                name=name,
                glyphs={
                    glyph_name: glyph.copy()
                    for glyph_name, glyph in layer.glyphs.items()
                },
                default=name == default_layer,
            )
            for name, layer in self.layers.items()
        ]
        return Font(layers=LayerSet.from_iterable(layers))

    def save(self, path: Any, **kwargs: Any) -> None:
        """Write the packaged glyphs as a UFO with :meth:`Font.save`."""
        self.to_font().save(path, **kwargs)

    write = save

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest for the whole archive."""
        return {
            "sourceDefaultLayer": self.source_default_layer,
            "complete": self.is_complete,
            "layers": [layer.to_dict() for layer in self.layers.values()],
        }


@define
class _Collector:
    """Per-layer traversal state."""

    layer: Layer
    resolve_source: Callable[[str, str], Glyph | None]
    substitutes: dict[tuple[str | None, str], GlyphSource | Glyph]
    glyphs: dict[str, Glyph] = field(factory=dict)
    edges: dict[str, tuple[str, ...]] = field(factory=dict)
    substitutions: dict[str, Substitution] = field(factory=dict)
    cycles: dict[tuple[str, ...], Cycle] = field(factory=dict)
    issues: dict[tuple[str, str | None, str], ArchiveIssue] = field(factory=dict)

    def find_substitute(self, glyph: str) -> GlyphSource | Glyph | None:
        layer_specific = self.substitutes.get((self.layer.name, glyph))
        if layer_specific is not None:
            return layer_specific
        return self.substitutes.get((None, glyph))

    def entity(self, name: str, request: ArchiveRequest | None = None) -> Glyph | None:
        """Return the local entity for ``name``, applying explicit substitutes."""
        if name in self.glyphs:
            return self.glyphs[name]

        if name in self.layer:
            glyph = self.layer[name].copy()
        else:
            substitute = self.find_substitute(name)
            if substitute is None:
                return None
            source = substitute
            if isinstance(source, Glyph):
                glyph = source.copy(name=name)
                substitution = Substitution(layer=self.layer.name, glyph=name)
            else:
                source_glyph = self.resolve_source(source.layer, source.glyph)
                if source_glyph is None:
                    self.issue(
                        "missing-substitute",
                        f"substitute {source.glyph!r} not found in layer "
                        f"{source.layer!r} for missing glyph {name!r}",
                        glyph=name,
                        request=request,
                    )
                    return None
                glyph = source_glyph.copy(name=name)
                substitution = Substitution(
                    layer=self.layer.name,
                    glyph=name,
                    source_layer=source.layer,
                    source_glyph=source.glyph,
                )
            self.substitutions[name] = substitution

        self.glyphs[name] = glyph
        self.edges[name] = tuple(
            dict.fromkeys(component.baseGlyph for component in glyph.components)
        )
        return glyph

    def issue(
        self,
        code: str,
        message: str,
        *,
        glyph: str | None = None,
        request: ArchiveRequest | None = None,
    ) -> ArchiveIssue:
        key = (code, glyph, message)
        issue = self.issues.get(key)
        if issue is None:
            issue = ArchiveIssue(
                layer=self.layer.name, code=code, message=message, glyph=glyph
            )
            self.issues[key] = issue
        if request is not None:
            issue._add_request(request.label)
            request._add_issue_code(code)
        return issue

    def canonical_cycle(self, members: Sequence[str]) -> tuple[str, ...]:
        values = tuple(members)
        rotations = [values[index:] + values[:index] for index in range(len(values))]
        ring = min(rotations)
        return ring + (ring[0],)

    def record_cycle(self, request: ArchiveRequest, members: Sequence[str]) -> None:
        """Record a ring in traversal order, keyed by a stable rotation."""
        traversal = tuple(members)
        display = traversal + (traversal[0],)
        key = self.canonical_cycle(traversal)
        cycle = self.cycles.get(key)
        if cycle is None:
            cycle = Cycle(layer=self.layer.name, glyphs=display)
            self.cycles[key] = cycle
        cycle._add_request(request.label)
        request._add_cycle(cycle)
        request._add_issue_code("component-cycle")

    def collect_closure(self, request: ArchiveRequest, root: str) -> bool:
        """Load every entity reachable from ``root`` and detect rings.

        This pass is independent of chain recording, so packaging and ring
        detection stay complete even when the provenance chain limit
        truncates a request's chain report.
        """
        complete = True
        gray: set[str] = set()
        black: set[str] = set()
        path: list[str] = []
        stack: list[tuple[str, Iterator[str]]] = []

        def enter(name: str) -> Iterator[str] | None:
            nonlocal complete
            glyph = self.entity(name, request)
            if glyph is None:
                complete = False
                request._add_missing(name)
                substitute = self.find_substitute(name)
                if substitute is None:
                    self.issue(
                        "missing-glyph",
                        f"missing component glyph {name!r} in layer "
                        f"{self.layer.name!r}",
                        glyph=name,
                        request=request,
                    )
                return None
            gray.add(name)
            path.append(name)
            substitution = self.substitutions.get(name)
            if substitution is not None:
                substitution._add_request(request.label)
                request._add_substitution(substitution)
            return iter(self.edges.get(name, ()))

        root_children = enter(root)
        if root_children is None:
            return False
        stack.append((root, root_children))

        while stack:
            name, children = stack[-1]
            for child in children:
                if child in gray:
                    start = path.index(child)
                    self.record_cycle(request, path[start:])
                    continue
                if child in black:
                    continue
                child_children = enter(child)
                if child_children is None:
                    continue
                stack.append((child, child_children))
                break
            else:
                stack.pop()
                gray.discard(name)
                black.add(name)
                path.pop()

        return complete

    def walk(self, request: ArchiveRequest, root: str) -> None:
        """Record all simple component paths starting at ``root``."""
        issue_limit: set[tuple[str, str | None, str]] = set()
        path: list[str] = [root]
        if not request._add_chain(path):
            self._chain_limit(request, root, issue_limit)
            return
        stack: list[tuple[str, Iterator[str]]] = [
            (root, iter(self.edges.get(root, ())))
        ]

        while stack:
            name, children = stack[-1]
            for child in children:
                child_path = tuple(path) + (child,)
                if not request._add_chain(child_path):
                    self._chain_limit(request, child, issue_limit)
                    continue
                if child in path:
                    self.record_cycle(request, path[path.index(child) :])
                    continue

                # The closure pass has already loaded (or reported) every
                # reachable entity and built the component edges.
                if child not in self.edges:
                    continue

                substitution = self.substitutions.get(child)
                if substitution is not None:
                    substitution._add_request(request.label)
                    request._add_substitution(substitution)

                path.append(child)
                stack.append((child, iter(self.edges.get(child, ()))))
                break
            else:
                stack.pop()
                path.pop()

    def _chain_limit(
        self,
        request: ArchiveRequest,
        glyph: str,
        issue_limit: set[tuple[str, str | None, str]],
    ) -> None:
        key = ("chain-limit", glyph, str(MAX_CHAINS_PER_REQUEST))
        if key in issue_limit:
            return
        issue_limit.add(key)
        self.issue(
            "chain-limit",
            f"stopped after {MAX_CHAINS_PER_REQUEST} component chains "
            f"for request {request.label!r}",
            glyph=glyph,
            request=request,
        )


def _codepoint_label(codepoint: int) -> str:
    return f"U+{codepoint:04X}"


def _parse_request(
    value: Any,
) -> tuple[Literal["character", "glyph"], int | None, str | None, str]:
    """Return ``(kind, codepoint, glyph_name, label)`` for a request value."""
    if isinstance(value, Character):
        return "character", value.codepoint, None, _codepoint_label(value.codepoint)
    if isinstance(value, GlyphName):
        return "glyph", None, value.name, value.name
    if isinstance(value, bool):
        raise TypeError("request values must not be booleans")
    if isinstance(value, int):
        if not 0 <= value <= 0x10FFFF:
            raise ValueError(f"invalid Unicode code point: {value!r}")
        return "character", value, None, _codepoint_label(value)
    if isinstance(value, str):
        if not value:
            raise ValueError("request values must not be empty strings")
        if value[:2].upper() == "U+" and len(value) > 2:
            try:
                codepoint = int(value[2:], 16)
            except ValueError:
                pass
            else:
                if 0 <= codepoint <= 0x10FFFF:
                    return (
                        "character",
                        codepoint,
                        None,
                        _codepoint_label(codepoint),
                    )
        if len(value) == 1:
            return "character", ord(value), None, _codepoint_label(ord(value))
        return "glyph", None, value, value
    raise TypeError(
        "request values must be str, int, Character, or GlyphName; "
        f"found {type(value).__name__}"
    )


def _request_value(value: Any) -> RequestValue:
    if isinstance(value, Character):
        return value.value
    if isinstance(value, GlyphName):
        return value.name
    assert isinstance(value, (str, int))
    return value


def _normalize_substitutes(
    substitutes: Mapping[SubstituteKey, SubstituteTarget] | None,
    font: Font | None,
) -> dict[tuple[str | None, str], GlyphSource | Glyph]:
    if not substitutes:
        return {}
    result: dict[tuple[str | None, str], GlyphSource | Glyph] = {}
    for key, value in substitutes.items():
        if isinstance(key, str):
            if not key:
                raise ValueError("substitute keys must not be empty")
            layer_name: str | None = None
            glyph_name = key
        elif isinstance(key, tuple) and len(key) == 2:
            layer_name, glyph_name = key
            if not isinstance(layer_name, str) or not layer_name:
                raise TypeError("substitute key layer must be a non-empty string")
            if font is not None and layer_name not in font.layers:
                raise KeyError(f"unknown layer in substitute key: {layer_name!r}")
        else:
            raise TypeError(
                "substitute keys must be glyph names or (layer, glyph) pairs"
            )
        if not isinstance(glyph_name, str) or not glyph_name:
            raise TypeError("substitute key glyph must be a non-empty string")

        if isinstance(value, GlyphSource):
            source: GlyphSource | Glyph = value
        elif isinstance(value, Glyph):
            source = value
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            source = GlyphSource(value[0], value[1])
        else:
            raise TypeError(
                "substitute values must be GlyphSource, Glyph, or (layer, glyph) pairs"
            )
        if (
            font is not None
            and isinstance(source, GlyphSource)
            and source.layer not in font.layers
        ):
            raise KeyError(f"unknown substitute source layer: {source.layer!r}")
        result[(layer_name, glyph_name)] = source
    return result


def _build_cmap(collector: _Collector) -> dict[int, str]:
    cmap: dict[int, str] = {}
    for glyph in collector.layer:
        assert glyph.name is not None
        for codepoint in glyph.unicodes:
            existing = cmap.get(codepoint)
            if existing is not None and existing != glyph.name:
                collector.issue(
                    "ambiguous-unicode",
                    f"code point {_codepoint_label(codepoint)} maps to both "
                    f"{existing!r} and {glyph.name!r}; using {existing!r}",
                    glyph=existing,
                )
                continue
            cmap[codepoint] = glyph.name
    return cmap


def _collect_layer(
    layer: Layer,
    requests: Sequence[Any],
    substitutes: Mapping[tuple[str | None, str], GlyphSource | Glyph],
    resolve_source: Callable[[str, str], Glyph | None],
) -> LayerArchive:
    collector = _Collector(
        layer=layer,
        resolve_source=resolve_source,
        substitutes=dict(substitutes),
    )
    layer_archive = LayerArchive(name=layer.name)
    cmap = _build_cmap(collector)

    for value in requests:
        kind, codepoint, glyph_name, label = _parse_request(value)
        request = ArchiveRequest(
            layer=layer.name,
            value=_request_value(value),
            label=label,
            kind=kind,
        )
        layer_archive.requests.append(request)

        if kind == "character":
            assert codepoint is not None
            resolved = cmap.get(codepoint)
            if resolved is None:
                if isinstance(value, str) and len(value) == 1 and value in layer:
                    # A bare one-character string doubles as a glyph name
                    # when the layer has no Unicode mapping for it.
                    request.kind = "glyph"
                    glyph_name = value
                else:
                    collector.issue(
                        "missing-character",
                        f"code point {_codepoint_label(codepoint)} has no "
                        f"glyph in layer {layer.name!r}",
                        glyph=_codepoint_label(codepoint),
                        request=request,
                    )
                    request._add_missing(_codepoint_label(codepoint))
                    continue
            else:
                glyph_name = resolved
        assert glyph_name is not None
        request.glyph = glyph_name

        entity = collector.entity(glyph_name, request)
        if entity is None:
            request._add_missing(glyph_name)
            substitute = collector.find_substitute(glyph_name)
            if substitute is None:
                collector.issue(
                    "missing-glyph",
                    f"requested glyph {glyph_name!r} is missing from layer "
                    f"{layer.name!r}",
                    glyph=glyph_name,
                    request=request,
                )
            continue

        collector.collect_closure(request, glyph_name)
        collector.walk(request, glyph_name)

    layer_archive.glyphs = collector.glyphs
    layer_archive.edges = collector.edges
    layer_archive.cycles = sorted(
        collector.cycles.values(), key=lambda cycle: cycle.glyphs
    )
    layer_archive.substitutions = sorted(
        collector.substitutions.values(),
        key=lambda substitution: substitution.glyph,
    )
    layer_archive.issues = list(collector.issues.values())
    return layer_archive


def build_archive(
    font: Font,
    characters: Iterable[RequestItem],
    *,
    layers: Iterable[str] | str | None = None,
    substitutes: Mapping[SubstituteKey, SubstituteTarget] | None = None,
) -> Archive:
    """Collect glyphs and component closures for a list of characters.

    Args:
        font: The source :class:`~ufoLib2.objects.Font`.
        characters: The requested characters or glyphs.  ``int`` values are
            Unicode code points; :class:`Character` values are explicit
            characters; :class:`GlyphName` values are explicit glyph names.
            A bare string with more than one character is treated as a glyph
            name.  A bare one-character string is treated as a character
            first and falls back to a glyph name only when the layer has no
            glyph for that code point.  ``"U+XXXX"`` strings are accepted as
            code points.
        layers: Layer names to package separately.  A single layer name may
            be passed directly.  If omitted, every layer in the font is
            packaged.
        substitutes: Optional mapping of missing glyphs to explicit sources.
            Keys are either a glyph name (applies to every layer) or a
            ``(layer, glyph)`` pair.  Values are :class:`GlyphSource`
            objects, :class:`Glyph` objects, or ``(source_layer,
            source_glyph)`` pairs.  A layer-specific key takes precedence
            over a glyph-name key.  A substituted glyph is copied into the
            target layer, and its own components are resolved in that target
            layer; any further missing pieces need their own explicit entry.

    Returns:
        An :class:`Archive` with one :class:`LayerArchive` per selected layer.

    Raises:
        KeyError: A selected layer or a substitute source layer does not exist.
        TypeError: A request or substitute value has an invalid type.
        ValueError: A request or substitute value is malformed.
    """
    if layers is None:
        layer_names = list(font.layers.layerOrder)
    elif isinstance(layers, str):
        layer_names = [layers]
    else:
        layer_names = list(layers)
        if len(set(layer_names)) != len(layer_names):
            raise ValueError("layers must not contain duplicates")

    selected: list[Layer] = []
    for name in layer_names:
        if name not in font.layers:
            raise KeyError(f"unknown layer: {name!r}")
        selected.append(font.layers[name])

    request_values: list[Any] = []
    for value in characters:
        if not isinstance(value, (str, int, Character, GlyphName)):
            raise TypeError(
                "characters must contain str, int, Character, or GlyphName; "
                f"found {type(value).__name__}"
            )
        _parse_request(value)  # validate before any traversal
        request_values.append(value)

    normalized_substitutes = _normalize_substitutes(substitutes, font)

    def resolve_source(layer_name: str, glyph_name: str) -> Glyph | None:
        layer = font.layers.get(layer_name)
        if layer is None or glyph_name not in layer:
            return None
        return layer[glyph_name]

    archives: dict[str, LayerArchive] = {}
    for layer in selected:
        archives[layer.name] = _collect_layer(
            layer,
            request_values,
            normalized_substitutes,
            resolve_source,
        )

    default_name = font.layers.defaultLayer.name
    return Archive(
        layers=archives,
        source_default_layer=default_name if default_name in archives else None,
    )
