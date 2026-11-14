# -*- coding: utf-8 -*-
"""
Source-location tracking for parsed front matter.

When an indexer reports a metadata field path, an editor needs to highlight
the *raw* bytes in the original document where that value was actually
written -- not the re-serialized output. This module maps field paths to
original byte ranges (and back again) by walking PyYAML's event stream.

It handles the awkward cases found in real manuscripts:

- folded/literal multi-line scalars (``>`` / ``|``), whose value spans
  several source lines;
- YAML anchors (``&name``) and aliases (``*name``), keeping the declaration
  site and every reference site as separately labelled locations;
- quoted keys that literally contain dots (``"a.b"``), which a naive
  ``path.split(".")`` would misread as nested fields;
- documents with a leading BOM or CRLF line endings, so the returned byte
  offsets select exactly the original bytes.

All offsets, lines and columns are zero-based. Byte offsets use Python slice
semantics (the end offset is exclusive); line/column pairs point at characters
within the (newline-normalized) original text.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Sequence

import yaml

from .default_handlers import SafeLoader
from .util import BOM, u

# A field path element is either a mapping key (str) or a sequence index (int).
PathElement = str | int
FieldPath = tuple[PathElement, ...]

# Location kinds.
VALUE = "value"
KEY = "key"
ANCHOR = "anchor"
ALIAS = "alias"


@dataclass(frozen=True)
class Location:
    """
    A raw byte range in the original document.

    ``start`` and ``end`` are zero-based byte offsets into the original
    document, with ``end`` exclusive. ``start_line``/``start_column`` and
    ``end_line``/``end_column`` are zero-based character positions spanning
    the same range.

    ``kind`` labels what the range covers:

    - ``"value"``: a scalar or collection where a real value is written;
    - ``"key"``: a mapping key token;
    - ``"anchor"``: an ``&name`` declaration (where an anchored value lives);
    - ``"alias"``: an ``*name`` reference; ``target`` then points at the
      path of the anchor it resolves to.
    """

    start: int
    end: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    kind: str
    path: FieldPath
    anchor: str | None = None
    target: FieldPath | None = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(
                f"Location start ({self.start}) must not exceed end ({self.end})"
            )

    @property
    def is_reference(self) -> bool:
        "True for alias references, False for real value/anchor declarations."
        return self.kind == ALIAS

    def contains_point(self, line: int, column: int) -> bool:
        "Whether a zero-based (line, column) character position is inside."
        if not self.contains_line(line):
            return False
        if self.start_line == self.end_line:
            return self.start_column <= column <= self.end_column
        if line == self.start_line:
            return column >= self.start_column
        if line == self.end_line:
            return column <= self.end_column
        return True

    def contains_line(self, line: int) -> bool:
        "Whether the range spans the given zero-based line."
        return self.start_line <= line <= self.end_line

    def as_dict(self) -> dict[str, Any]:
        "Dump as a plain dictionary, useful for JSON serialization."
        return {
            "start": self.start,
            "end": self.end,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "kind": self.kind,
            "path": list(self.path),
            "anchor": self.anchor,
            "target": list(self.target) if self.target is not None else None,
        }


@dataclass
class SourceMap:
    """
    Bidirectional index between metadata field paths and source locations.

    Returned on parsed :class:`~frontmatter.Post` objects as
    ``post.source_map`` when the handler can provide one.
    """

    encoding: str = "utf-8"
    _locations: list[Location] = field(default_factory=list)
    _by_path: dict[FieldPath, list[Location]] = field(default_factory=dict)
    _anchors: dict[str, FieldPath] = field(default_factory=dict)

    def _add(self, location: Location) -> None:
        self._locations.append(location)
        self._by_path.setdefault(location.path, []).append(location)

    def _register_anchor(self, name: str, path: FieldPath) -> None:
        self._anchors.setdefault(name, path)

    @property
    def locations(self) -> list[Location]:
        "Every recorded location, in source order."
        return list(self._locations)

    @property
    def paths(self) -> list[FieldPath]:
        "All known field paths, in first-seen order."
        return list(self._by_path.keys())

    def for_path(self, path: str | Sequence[PathElement]) -> list[Location]:
        """
        Return every location for a field path.

        A string is split on ``"."``; pass a tuple/list to address keys that
        themselves contain dots or to use integer sequence indices, e.g.
        ``("a.b", 0, "c")``. Alias references and the anchor declaration are
        returned together, each labelled by its ``kind``.
        """
        key = _normalize_path(path)
        return list(self._by_path.get(key, ()))

    def anchor_path(self, name: str) -> FieldPath | None:
        "Return the field path where anchor ``name`` is declared, if known."
        return self._anchors.get(name)

    def references_to(self, path: str | Sequence[PathElement]) -> list[Location]:
        "Return alias-reference locations resolving to the given field path."
        target = _normalize_path(path)
        return [
            location
            for location in self._locations
            if location.kind == ALIAS and location.target == target
        ]

    def at_point(self, line: int, column: int) -> list[Location]:
        """
        Reverse-lookup the fields touched by a zero-based line/column.

        Nested containers overlap their children, so several locations may
        match; the most specific (shortest) range comes first.
        """
        matches = [
            location
            for location in self._locations
            if location.contains_point(line, column)
        ]
        matches.sort(
            key=lambda location: (location.end - location.start, location.start)
        )
        return matches

    def at_line(self, line: int) -> list[Location]:
        "Return every location spanning the given zero-based line."
        return [
            location for location in self._locations if location.contains_line(line)
        ]

    def at_byte(self, offset: int) -> list[Location]:
        "Reverse-lookup locations containing a zero-based byte offset."
        matches = [
            location
            for location in self._locations
            if location.start <= offset < location.end
        ]
        matches.sort(
            key=lambda location: (location.end - location.start, location.start)
        )
        return matches


def _normalize_path(path: str | Sequence[PathElement]) -> FieldPath:
    if isinstance(path, str):
        return tuple(path.split("."))
    return tuple(path)


@dataclass
class _Frame:
    kind: str
    path: FieldPath
    next_is_key: bool = True
    pending_key: str | None = None
    index: int = 0
    start_index: int = 0


def build_source_map(
    text: str | bytes,
    encoding: str = "utf-8",
    boundary: re.Pattern[str] | None = None,
    loader: Any = SafeLoader,
) -> SourceMap | None:
    """
    Build a :class:`SourceMap` for the YAML front matter in ``text``.

    ``text`` is the original document (``str`` or raw bytes); it is decoded
    but otherwise left untouched, so BOM and CRLF are accounted for when
    computing byte offsets. Returns ``None`` when no front matter is present.
    """
    if boundary is None:
        boundary = re.compile(r"^-{3,}\s*$", re.MULTILINE)

    original = text.decode(encoding) if isinstance(text, bytes) else str(text)
    normalized = u(original, encoding)

    # A leading BOM would stop the boundary's line-anchored pattern from
    # matching. Skip it for detection but keep it in every offset below.
    bom_offset = 1 if normalized.startswith(BOM) else 0
    body = normalized[bom_offset:]

    first = boundary.search(body)
    if first is None:
        return None
    second = boundary.search(body, first.end())
    if second is None:
        return None

    fm_start = first.end()
    fm_end = second.start()
    fm_text = body[fm_start:fm_end]

    norm_to_orig, line_starts = _build_index_maps(original, normalized)
    char_bytes = _build_byte_table(original, encoding)

    source_map = SourceMap(encoding=encoding)
    stack: list[_Frame] = []

    def resolve(fm_relative_index: int) -> int:
        # Event marks are relative to ``fm_text``; translate back through the
        # skipped front-matter prefix and any BOM to a normalized index.
        normalized_index = bom_offset + fm_start + fm_relative_index
        if normalized_index >= len(norm_to_orig):
            return len(original)
        return norm_to_orig[normalized_index]

    def make_location(
        norm_start: int,
        norm_end: int,
        kind: str,
        path: FieldPath,
        anchor: str | None = None,
        target: FieldPath | None = None,
    ) -> Location:
        char_start = resolve(norm_start)
        char_end = resolve(norm_end)
        start_line = bisect_right(line_starts, char_start) - 1
        end_line = bisect_right(line_starts, char_end) - 1
        return Location(
            start=char_bytes[char_start],
            end=char_bytes[char_end],
            start_line=start_line,
            start_column=char_start - line_starts[start_line],
            end_line=end_line,
            end_column=char_end - line_starts[end_line],
            kind=kind,
            path=path,
            anchor=anchor,
            target=target,
        )

    def value_path(frame: _Frame | None) -> FieldPath:
        if frame is None:
            return ()
        if frame.kind == "seq":
            path = frame.path + (frame.index,)
            frame.index += 1
            return path
        # mapping value: the key scalar was recorded just before this node
        key = frame.pending_key if frame.pending_key is not None else ""
        frame.pending_key = None
        frame.next_is_key = True
        return frame.path + (key,)

    try:
        events = list(yaml.parse(fm_text, Loader=loader))
    except yaml.YAMLError:
        return None

    for event in events:
        if isinstance(
            event,
            (
                yaml.StreamStartEvent,
                yaml.StreamEndEvent,
                yaml.DocumentStartEvent,
                yaml.DocumentEndEvent,
            ),
        ):
            continue

        if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            frame = stack.pop()
            # Skip the outermost document mapping: its span is the whole
            # front matter and would match every reverse lookup.
            if frame.path:
                source_map._add(
                    make_location(
                        frame.start_index,
                        event.end_mark.index,
                        VALUE,
                        frame.path,
                    )
                )
            continue

        frame = stack[-1] if stack else None
        is_map_key = (
            frame is not None
            and frame.kind == "map"
            and frame.next_is_key
            and isinstance(event, yaml.ScalarEvent)
        )

        if is_map_key:
            # A mapping key: its path is already known, record it and wait
            # for the value node that follows.
            assert isinstance(event, yaml.ScalarEvent)
            assert frame is not None
            key_path = frame.path + (event.value,)
            frame.pending_key = event.value
            frame.next_is_key = False
            source_map._add(
                make_location(
                    event.start_mark.index, event.end_mark.index, KEY, key_path
                )
            )
            continue

        node_path = value_path(frame)

        if isinstance(event, yaml.AliasEvent):
            target = source_map.anchor_path(event.anchor)
            source_map._add(
                make_location(
                    event.start_mark.index,
                    event.end_mark.index,
                    ALIAS,
                    node_path,
                    anchor=event.anchor,
                    target=target,
                )
            )
            continue

        # Scalar or collection start. Register an anchor declaration first.
        anchor_name = getattr(event, "anchor", None)
        if anchor_name:
            source_map._register_anchor(anchor_name, node_path)
            token = "&" + anchor_name
            # Anchor names are unique per document, so the first occurrence
            # at or after the node's start mark is this node's anchor.
            token_start = fm_text.find(token, event.start_mark.index)
            if token_start == -1:
                token_start = event.start_mark.index
            source_map._add(
                make_location(
                    token_start,
                    token_start + len(token),
                    ANCHOR,
                    node_path,
                    anchor=anchor_name,
                )
            )

        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            kind = "map" if isinstance(event, yaml.MappingStartEvent) else "seq"
            stack.append(
                _Frame(
                    kind=kind,
                    path=node_path,
                    start_index=event.start_mark.index,
                )
            )
        elif isinstance(event, yaml.ScalarEvent):
            source_map._add(
                make_location(
                    event.start_mark.index, event.end_mark.index, VALUE, node_path
                )
            )

    return source_map


def _build_index_maps(
    original: str, normalized: str
) -> tuple[list[int], list[int]]:
    """
    Map normalized character indices back to original ones, and record where
    each line starts in the original string.

    The only normalization applied is CRLF -> LF, so each normalized index
    points at the original character it came from (the ``\\r`` for a newline).
    """
    line_starts = [0]
    for index, char in enumerate(original):
        if char == "\n":
            line_starts.append(index + 1)

    mapping: list[int] = []
    original_index = 0
    while original_index < len(original):
        char = original[original_index]
        if (
            char == "\r"
            and original_index + 1 < len(original)
            and original[original_index + 1] == "\n"
        ):
            mapping.append(original_index)
            original_index += 2
        else:
            mapping.append(original_index)
            original_index += 1

    if len(mapping) != len(normalized):
        # Defensive: u() only collapses CRLF, so lengths must agree.
        raise RuntimeError("normalized text does not match original document")
    return mapping, line_starts


def _build_byte_table(original: str, encoding: str) -> list[int]:
    """Cumulative byte offset of each character index in the original text."""
    offsets = [0]
    for char in original:
        offsets.append(offsets[-1] + len(char.encode(encoding)))
    return offsets
