# -*- coding: utf-8 -*-
"""
Layered front-matter metadata inheritance.

Textbook-style content is organized in three layers -- a *site* layer,
one or more *section* layers and the *article* layer -- and each layer
carries its own front matter. This module merges the layers into the
effective metadata of a single article while remembering where every
value came from.

Three kinds of fields are supported:

* **Plain fields** -- a child inherits the parent value whenever the key
  is absent from its own metadata. Writing the key overrides the parent.
* **Clearable fields** -- declared in the policy as fields where an
  explicit ``null`` (a key that *is present* with the value ``None``)
  clears an inherited value instead of overriding it with ``None``.
* **Keyed list fields** -- lists whose entries carry an identifier
  (``id`` by default). Entries are merged per identifier across layers;
  an entry marked with the remove marker (``_remove: true``) deletes the
  inherited entry, and a later layer may re-add an entry with the same
  identifier. The two operations are kept apart in the entry history.

Presence matters: a missing key and a key explicitly written as ``null``
are never treated as the same thing.

.. testsetup:: *

    >>> import frontmatter

A small example, merging a site, a section and an article:

.. doctest::

    >>> from frontmatter import Layer, InheritancePolicy, merge_layers
    >>> policy = InheritancePolicy(
    ...     clearable_fields=["summary"],
    ...     keyed_lists={"references": "id"},
    ... )
    >>> site = Layer("site", {
    ...     "layout": "book",
    ...     "summary": "site summary",
    ...     "references": [{"id": "r1", "title": "First"}],
    ... })
    >>> section = Layer("section", {
    ...     "summary": None,
    ...     "references": [{"id": "r1", "_remove": True}],
    ... })
    >>> article = Layer("article", {
    ...     "title": "Chapter 1",
    ...     "references": [{"id": "r1", "title": "Re-added"}],
    ... })
    >>> resolution = merge_layers([site, section, article], policy)
    >>> resolution.metadata["layout"]
    'book'
    >>> "summary" in resolution.metadata
    False
    >>> resolution.sources["summary"]
    FieldSource(layer='section', action='cleared')
    >>> resolution.metadata["references"]
    [{'id': 'r1', 'title': 'Re-added'}]
    >>> [s.action for s in resolution.list_history["references"]["r1"]]
    ['set', 'cleared', 'set']
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "DELETE_KEY",
    "FieldSource",
    "InheritancePolicy",
    "Layer",
    "MetadataIndex",
    "Resolution",
    "merge_layers",
]


SET = "set"
CLEARED = "cleared"


class _DeleteKey:
    """Sentinel for removing a key outright, as opposed to writing null."""

    _instance: "_DeleteKey | None" = None

    def __new__(cls) -> "_DeleteKey":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DELETE_KEY"

    def __bool__(self) -> bool:
        return False


DELETE_KEY = _DeleteKey()


@dataclass(frozen=True)
class FieldSource:
    """Where a value (or a list entry) came from.

    ``action`` is :data:`SET` when the layer wrote a value and
    :data:`CLEARED` when an explicit null (or a remove marker) deleted an
    inherited value.
    """

    layer: str
    action: str = SET


@dataclass(frozen=True, init=False)
class InheritancePolicy:
    """Declares how fields behave while layers are merged.

    ``clearable_fields`` names fields on which an explicit ``null``
    clears the inherited value instead of storing ``None``.

    ``keyed_lists`` maps a field name to the identifier key used by its
    entries. A plain sequence of field names may be passed as well, in
    which case entries are identified by ``"id"``.

    ``remove_key`` is the marker key inside a keyed-list entry that
    turns the entry into a deletion.
    """

    clearable_fields: frozenset[str] = frozenset()
    keyed_lists: Mapping[str, str] = field(default_factory=dict)
    remove_key: str = "_remove"

    def __init__(
        self,
        clearable_fields: Sequence[str] = (),
        keyed_lists: Mapping[str, str] | Sequence[str] = (),
        remove_key: str = "_remove",
    ) -> None:
        object.__setattr__(self, "clearable_fields", frozenset(clearable_fields))
        if isinstance(keyed_lists, Mapping):
            normalized = dict(keyed_lists)
        else:
            normalized = {name: "id" for name in keyed_lists}
        object.__setattr__(self, "keyed_lists", normalized)
        object.__setattr__(self, "remove_key", remove_key)


@dataclass(init=False)
class Layer:
    """One named metadata layer, e.g. a site, a section or an article.

    ``metadata`` may be a plain mapping or a :class:`frontmatter.Post`;
    only the post's metadata mapping is used.
    """

    name: str
    metadata: Mapping[str, Any]

    def __init__(self, name: str, metadata: Any = None) -> None:
        self.name = name
        self.metadata = as_mapping(metadata)


@dataclass
class Resolution:
    """The outcome of merging a chain of layers for one article."""

    metadata: dict[str, Any]
    sources: dict[str, FieldSource]
    list_sources: dict[str, dict[str, FieldSource]]
    list_history: dict[str, dict[str, list[FieldSource]]]
    layers: tuple[Layer, ...]
    touched: dict[str, frozenset[str]]

    def source_of(
        self, field: str, entry_id: str | None = None
    ) -> FieldSource | None:
        """Return the source of a field, or of one keyed-list entry."""
        if entry_id is None:
            return self.sources.get(field)
        return self.list_sources.get(field, {}).get(entry_id)

    def inherits(self, field: str, layer_name: str) -> bool:
        """True if ``field`` is inherited from above ``layer_name``.

        A field counts as inherited by the named layer when the layer
        neither writes nor clears it itself, even when another layer
        above or below happens to carry the same key.
        """
        if field not in self.sources:
            return False
        return layer_name not in self.touched.get(field, frozenset())


def as_mapping(node: Any) -> Mapping[str, Any]:
    """Coerce a post, mapping or ``None`` into a metadata mapping."""
    if node is None:
        return {}
    if isinstance(node, Mapping):
        return node
    metadata = getattr(node, "metadata", None)
    if isinstance(metadata, Mapping):
        return metadata
    raise TypeError(f"Cannot use {type(node)!r} as a metadata layer")


def _normalize_entry(
    entry: Any, id_key: str, remove_key: str
) -> tuple[Any, Any, bool]:
    """Return ``(identifier, payload, is_removal)`` for one list entry."""
    if isinstance(entry, Mapping):
        if id_key not in entry:
            raise ValueError(
                f"Keyed list entry is missing identifier key {id_key!r}: {entry!r}"
            )
        identifier = entry[id_key]
        is_removal = bool(entry.get(remove_key, False))
        payload = {k: v for k, v in entry.items() if k != remove_key}
        return identifier, payload, is_removal
    return entry, entry, False


def merge_layers(
    layers: Sequence[Layer | tuple[str, Any]],
    policy: InheritancePolicy | None = None,
) -> Resolution:
    """Merge ordered metadata layers (root first, leaf last) for an article.

    Each item is a :class:`Layer` or a ``(name, metadata)`` pair, where
    metadata may again be a :class:`frontmatter.Post`. Returns a
    :class:`Resolution` holding both the effective metadata and the
    provenance of every value.
    """
    policy = policy or InheritancePolicy()
    normalized_layers = tuple(
        item if isinstance(item, Layer) else Layer(item[0], item[1])
        for item in layers
    )

    effective: dict[str, Any] = {}
    sources: dict[str, FieldSource] = {}
    list_sources: dict[str, dict[str, FieldSource]] = {}
    list_history: dict[str, dict[str, list[FieldSource]]] = {}
    list_state: dict[str, dict[Any, Any]] = {}
    touched: dict[str, set[str]] = {}

    for layer in normalized_layers:
        metadata = layer.metadata
        for key in metadata:  # iterate present keys only: null stays distinct
            touched.setdefault(key, set()).add(layer.name)
            value = metadata[key]
            if key in policy.keyed_lists:
                _merge_keyed_list(
                    key=key,
                    value=value,
                    layer=layer,
                    policy=policy,
                    effective=effective,
                    sources=sources,
                    list_sources=list_sources,
                    list_history=list_history,
                    list_state=list_state,
                )
            else:
                _merge_scalar(
                    key=key,
                    value=value,
                    layer=layer,
                    policy=policy,
                    effective=effective,
                    sources=sources,
                )

    for key, entries in list_state.items():
        effective[key] = list(entries.values())

    return Resolution(
        metadata=effective,
        sources=sources,
        list_sources=list_sources,
        list_history=list_history,
        layers=normalized_layers,
        touched={key: frozenset(names) for key, names in touched.items()},
    )


def _merge_scalar(
    key: str,
    value: Any,
    layer: Layer,
    policy: InheritancePolicy,
    effective: dict[str, Any],
    sources: dict[str, FieldSource],
) -> None:
    if value is None and key in policy.clearable_fields:
        effective.pop(key, None)
        sources[key] = FieldSource(layer.name, CLEARED)
        return

    # An explicit null on a non-clearable field is still a written value,
    # so it must never be confused with an absent key.
    effective[key] = value
    sources[key] = FieldSource(layer.name, SET)


def _merge_keyed_list(
    key: str,
    value: Any,
    layer: Layer,
    policy: InheritancePolicy,
    effective: dict[str, Any],
    sources: dict[str, FieldSource],
    list_sources: dict[str, dict[str, FieldSource]],
    list_history: dict[str, dict[str, list[FieldSource]]],
    list_state: dict[str, dict[Any, Any]],
) -> None:
    id_key = policy.keyed_lists[key]

    if value is None:
        if key in policy.clearable_fields:
            list_state.pop(key, None)
            list_sources.pop(key, None)
            effective.pop(key, None)
            sources[key] = FieldSource(layer.name, CLEARED)
        else:
            list_state.pop(key, None)
            list_sources.pop(key, None)
            effective[key] = None
            sources[key] = FieldSource(layer.name, SET)
        return

    if not isinstance(value, list):
        raise TypeError(
            f"Field {key!r} is declared as a keyed list but got {type(value)!r}"
        )

    entries = list_state.setdefault(key, {})
    entry_sources = list_sources.setdefault(key, {})
    history = list_history.setdefault(key, {})

    actions: list[str] = []
    for raw_entry in value:
        identifier, payload, is_removal = _normalize_entry(
            raw_entry, id_key, policy.remove_key
        )
        record = history.setdefault(identifier, [])
        if is_removal:
            entries.pop(identifier, None)
            entry_sources.pop(identifier, None)
            source = FieldSource(layer.name, CLEARED)
        else:
            entries[identifier] = payload
            source = FieldSource(layer.name, SET)
            entry_sources[identifier] = source
        record.append(source)
        actions.append(source.action)

    if actions:
        action = SET if SET in actions else CLEARED
        sources[key] = FieldSource(layer.name, action)


class MetadataIndex:
    """Holds a site layer, named sections and the articles under them.

    The index resolves effective article metadata via
    :meth:`resolve` and can revise section defaults in place, reporting
    exactly which articles inherited the revised value.
    """

    def __init__(
        self,
        site: Any = None,
        *,
        policy: InheritancePolicy | None = None,
        site_name: str = "site",
    ) -> None:
        self.policy = policy or InheritancePolicy()
        self.site_name = site_name
        self._site: dict[str, Any] = dict(as_mapping(site))
        self._sections: dict[str, dict[str, Any]] = {}
        self._articles: dict[str, dict[str, dict[str, Any]]] = {}

    def set_site(self, metadata: Any) -> "MetadataIndex":
        """Replace the site layer metadata."""
        self._site = dict(as_mapping(metadata))
        return self

    def add_section(self, name: str, metadata: Any) -> "MetadataIndex":
        """Add or replace a section layer."""
        self._sections[name] = dict(as_mapping(metadata))
        self._articles.setdefault(name, {})
        return self

    def add_article(
        self, section: str, name: str, metadata: Any
    ) -> "MetadataIndex":
        """Add or replace an article under ``section``."""
        if section not in self._sections:
            raise KeyError(f"Unknown section {section!r}")
        self._articles[section][name] = dict(as_mapping(metadata))
        return self

    def section_names(self) -> tuple[str, ...]:
        return tuple(self._sections)

    def article_names(self, section: str) -> tuple[str, ...]:
        return tuple(self._articles[section])

    def _layers(self, section: str, article: str | None) -> list[Layer]:
        layers = [
            Layer(self.site_name, self._site),
            Layer(section, self._sections[section]),
        ]
        if article is not None:
            layers.append(Layer(article, self._articles[section][article]))
        return layers

    def resolve_section(self, section: str) -> Resolution:
        """Resolve the site + section defaults, without an article."""
        return merge_layers(self._layers(section, None), self.policy)

    def resolve(self, section: str, article: str) -> Resolution:
        """Resolve the effective metadata of one article."""
        return merge_layers(self._layers(section, article), self.policy)

    def articles_inheriting(self, section: str, field: str) -> tuple[str, ...]:
        """Names of articles under ``section`` that inherit ``field``.

        An article inherits when it neither writes nor clears the field
        itself, so a section default revision reaches exactly these
        articles.
        """
        return tuple(
            name
            for name in self.article_names(section)
            if self.resolve(section, name).inherits(field, name)
        )

    def revise_section(
        self, section: str, changes: Mapping[str, Any]
    ) -> dict[str, dict[str, tuple[Any, Any]]]:
        """Apply a section default revision and report affected articles.

        ``changes`` maps field names to new raw values. Passing
        :data:`DELETE_KEY` removes the key from the section altogether
        (distinct from writing ``None``). Only articles whose effective
        value actually changes are returned, mapped to the old and new
        values of each changed field.
        """
        if section not in self._sections:
            raise KeyError(f"Unknown section {section!r}")

        before = {
            name: self.resolve(section, name).metadata
            for name in self.article_names(section)
        }

        section_metadata = self._sections[section]
        for key, value in changes.items():
            if isinstance(value, _DeleteKey):
                section_metadata.pop(key, None)
            else:
                section_metadata[key] = value

        affected: dict[str, dict[str, tuple[Any, Any]]] = {}
        for name, old_metadata in before.items():
            new_metadata = self.resolve(section, name).metadata
            changed: dict[str, tuple[Any, Any]] = {}
            keys = set(old_metadata) | set(new_metadata)
            for key in keys:
                old_value = old_metadata.get(key)
                new_value = new_metadata.get(key)
                old_present = key in old_metadata
                new_present = key in new_metadata
                if (old_present, old_value) != (new_present, new_value):
                    changed[key] = (
                        old_value if old_present else DELETE_KEY,
                        new_value if new_present else DELETE_KEY,
                    )
            if changed:
                affected[name] = changed
        return affected
