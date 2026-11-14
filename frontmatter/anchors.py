# -*- coding: utf-8 -*-
"""
Anchor and alias bookkeeping for YAML front matter.

PyYAML resolves YAML aliases into shared Python objects, which loses the
information that two values were connected by an anchor. This module keeps
that relationship explicit so a revision can target a single document or
propagate through every reference on purpose.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Iterable

Path = tuple[Any, ...]


def is_container(node: Any) -> bool:
    "Only containers can participate in reference cycles or identity sharing."
    return isinstance(node, (dict, list, tuple))


class RevisionScope(enum.Enum):
    """
    How a revision should treat values joined by a YAML anchor.

    ``LOCAL`` detaches the named location from its anchor group before
    editing, so only that document changes. ``SHARED`` edits the shared
    object once and reports every reference that changes with it.
    """

    LOCAL = "local"
    SHARED = "shared"


@dataclass(frozen=True)
class Source:
    """
    A finite explanation of where one value came from.

    ``location`` is the metadata path of an alias, ``anchor`` is the anchor
    name it points at, and ``definition`` is the path where that anchor is
    defined. ``cyclic`` marks the point where a nested reference looped back
    to an already visited object, so traversal stopped instead of recursing
    forever.
    """

    location: Path
    anchor: str
    definition: Path | None
    cyclic: bool = False

    def describe(self) -> str:
        text = f"{render_path(self.location)} -> alias *{self.anchor}"
        if self.definition is not None:
            text += f" (defined at {render_path(self.definition)})"
        if self.cyclic:
            text += "; cyclic reference, chain stopped"
        return text


@dataclass
class AnchorMap:
    """
    Anchor definitions and alias locations extracted from YAML events.

    Paths are tuples of mapping keys (``str``) and sequence indices
    (``int``), rooted at the front matter document.
    """

    definitions: dict[str, Path] = field(default_factory=dict)
    aliases: dict[Path, str] = field(default_factory=dict)

    def anchor_at(self, path: Path) -> str | None:
        "Return the alias anchor used at exactly this path, if any."
        return self.aliases.get(path)

    def anchor_for_path(self, path: Path) -> tuple[str, Path] | None:
        """
        Find the anchor group governing a nested path.

        Returns the anchor name and its definition path when the path is an
        alias itself or sits inside an anchored value, matching the longest
        path prefix first.
        """
        for length in range(len(path), 0, -1):
            prefix = path[:length]
            anchor = self.aliases.get(prefix)
            if anchor is not None:
                return anchor, self.definitions.get(anchor, prefix)
        for length in range(len(path), 0, -1):
            prefix = path[:length]
            for anchor, definition in self.definitions.items():
                if definition == prefix:
                    return anchor, definition
        return None

    def reference_paths(self, anchor: str) -> list[Path]:
        """
        Every reference location for an anchor, in document order.

        The anchor definition comes first, then its aliases in the order they
        were encountered while parsing.
        """
        aliases = [path for path, name in self.aliases.items() if name == anchor]
        definition = self.definitions.get(anchor)
        if definition is not None and definition not in aliases:
            return [definition, *aliases]
        return aliases

    def governing_group(self, path: Path) -> tuple[str, list[Path], Path] | None:
        """
        Return the anchor group governing a path as ``(anchor, members,
        prefix)``, where ``members`` are every reference location and
        ``prefix`` is the member that contains (or equals) the path.
        """
        match = self.anchor_for_path(path)
        if match is None:
            return None
        anchor, _definition = match
        members = self.reference_paths(anchor)
        prefix = next(
            (member for member in members if path[: len(member)] == member),
            members[0],
        )
        return anchor, members, prefix

    def detach(self, member: Path) -> None:
        """
        Remove one location from its anchor group, moving the anchor
        definition to a remaining alias when the definition itself leaves.
        """
        self.aliases.pop(member, None)
        for anchor, definition in list(self.definitions.items()):
            if definition != member:
                continue
            remaining = [
                path for path, name in self.aliases.items() if name == anchor
            ]
            if remaining:
                self.definitions[anchor] = remaining[0]
            else:
                del self.definitions[anchor]

    def source_for(self, path: Path) -> Source | None:
        "Build a finite source description for an aliased path."
        anchor = self.anchor_at(path)
        if anchor is None:
            match = self.anchor_for_path(path)
            if match is None:
                return None
            anchor, _ = match
        return Source(
            location=path,
            anchor=anchor,
            definition=self.definitions.get(anchor),
        )


@dataclass
class RevisionResult:
    """
    Outcome of a :py:meth:`Post.revise <frontmatter.Post.revise>` call.

    ``changed`` lists only the metadata paths whose stored value actually
    differs from before the revision. ``sources`` carries finite provenance
    for every alias or cycle encountered, so nested references can be
    explained without infinite recursion.
    """

    path: Path
    value: Any
    old_value: Any
    scope: RevisionScope
    changed: list[Path] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    detached: bool = False

    def describe(self) -> str:
        lines = [
            f"revised {render_path(self.path)} ({self.scope.value})",
            "changed:",
        ]
        if self.changed:
            lines.extend(f"  - {render_path(path)}" for path in self.changed)
        else:
            lines.append("  (none)")
        if self.sources:
            lines.append("sources:")
            lines.extend(f"  - {source.describe()}" for source in self.sources)
        return "\n".join(lines)


def normalize_path(path: str | Iterable[Any]) -> Path:
    """
    Turn a dotted string or iterable into a metadata path tuple.

    Integer segments are treated as sequence indices.
    """
    if isinstance(path, str):
        segments: list[Any] = []
        for segment in path.split("."):
            if segment.isdigit():
                segments.append(int(segment))
            else:
                segments.append(segment)
        return tuple(segments)
    return tuple(path)


def render_path(path: Path) -> str:
    "Render a metadata path as a dotted, human-readable string."
    parts: list[str] = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        elif parts:
            parts.append(f".{segment}")
        else:
            parts.append(str(segment))
    return "".join(parts)


def resolve(metadata: dict[str, Any], path: Path) -> Any:
    "Walk a metadata path, raising KeyError or IndexError on a broken path."
    node: Any = metadata
    for segment in path:
        node = node[segment]
    return node


def assign(metadata: dict[str, Any], path: Path, value: Any) -> None:
    "Set a value at a metadata path, creating no intermediate containers."
    if not path:
        raise ValueError("Cannot replace the metadata root")
    parent = resolve(metadata, path[:-1])
    parent[path[-1]] = value


def trace_sources(
    root: Any,
    root_path: Path = (),
    anchor_map: AnchorMap | None = None,
) -> list[Source]:
    """
    Describe alias and cyclic sources reachable from a value.

    Object identity makes the walk finite: a container entered a second time
    is reported once as a cyclic source and not descended into again.
    """
    sources: list[Source] = []
    first_seen: dict[int, Path] = {}

    def emit_alias(node: Any, full_path: Path, path: Path) -> None:
        if anchor_map is None:
            return
        is_alias = anchor_map.anchor_at(full_path) is not None
        if path and not is_alias:
            return
        source = anchor_map.source_for(full_path)
        if source is not None:
            sources.append(source)

    def walk(node: Any, path: Path) -> None:
        full_path = root_path + path

        if is_container(node):
            identity = id(node)
            if identity in first_seen:
                mapped = (
                    anchor_map.source_for(full_path)
                    if anchor_map is not None
                    else None
                )
                sources.append(
                    Source(
                        location=full_path,
                        anchor=mapped.anchor if mapped else "<cycle>",
                        definition=(
                            mapped.definition
                            if mapped and mapped.definition is not None
                            else first_seen[identity]
                        ),
                        cyclic=True,
                    )
                )
                return
            first_seen[identity] = full_path
            emit_alias(node, full_path, path)

        else:
            emit_alias(node, full_path, path)

        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, path + (index,))

    walk(root, ())
    return sources


def find_occurrences(root: Any, target: Any) -> list[Path]:
    """
    Find every path where ``target`` appears by object identity.

    Only containers are compared by identity: scalars may be interned by
    Python, so identical ints or strings are not treated as shared. Containers
    already visited are not entered again, so cyclic or diamond shaped
    references terminate and each physical occurrence is reported at most
    once. A re-entry through a cycle is still recorded as a path.
    """
    if not is_container(target):
        return []

    occurrences: list[Path] = []
    seen: set[int] = set()

    def walk(node: Any, path: Path) -> None:
        if is_container(node):
            if id(node) in seen:
                if node is target:
                    occurrences.append(path)
                return
            seen.add(id(node))
            if node is target:
                occurrences.append(path)
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, path + (index,))

    walk(root, ())
    return occurrences


def shared_group(
    metadata: dict[str, Any], path: Path
) -> tuple[Any, list[Path], Path] | None:
    """
    Find the deepest value on ``path`` that appears more than once by
    identity, returning that value, every occurrence path, and the shared
    prefix.

    This is the fallback for posts without anchor bookkeeping, such as JSON
    or TOML front matter.
    """
    for length in range(len(path), 0, -1):
        prefix = path[:length]
        value = resolve(metadata, prefix)
        occurrences = find_occurrences(metadata, value)
        if len(occurrences) > 1:
            return value, occurrences, prefix
    return None


def values_differ(left: Any, right: Any) -> bool:
    "Compare two values without blowing up on cyclic containers."
    try:
        return bool(left != right)
    except RecursionError:
        return left is not right
