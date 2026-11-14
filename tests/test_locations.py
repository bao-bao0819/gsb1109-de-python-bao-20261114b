# -*- coding: utf-8 -*-
"""
Tests for source-location tracking (``frontmatter.locations``).

These cover the awkward cases from real manuscripts: folded multi-line
scalars, anchors/aliases (declaration vs. reference), quoted keys containing
dots, and byte-accurate offsets under BOM and CRLF inputs.
"""

import frontmatter
from frontmatter.locations import (
    ALIAS,
    ANCHOR,
    KEY,
    VALUE,
    SourceMap,
    build_source_map,
)

# A document exercising every tricky construct. Offsets are asserted by
# slicing the original bytes rather than hard-coded numbers.
SAMPLE = """\
---
defaults: &d
  a: 1
  tags:
    - one
    - two
folded: >-
  hello
  world
literal: |
  line one
  line two
ref: *d
"a.b": dotted
nested:
  k: v
---
body text
"""


def _kinds(source_map, path):
    return sorted(location.kind for location in source_map.for_path(path))


def _value_slice(data, source_map, path):
    locations = [
        location
        for location in source_map.for_path(path)
        if location.kind in (VALUE, ALIAS)
    ]
    assert len(locations) == 1
    location = locations[0]
    return data[location.start : location.end], location


def test_post_carries_source_map():
    post = frontmatter.loads(SAMPLE)
    assert isinstance(post.source_map, SourceMap)


def test_scalar_value_and_key_byte_ranges():
    data = SAMPLE.encode("utf-8")
    source_map = frontmatter.loads(SAMPLE).source_map

    value_bytes, location = _value_slice(data, source_map, ("nested", "k"))
    assert value_bytes == b"v"
    assert location.kind == VALUE

    key_locations = [
        location
        for location in source_map.for_path(("nested", "k"))
        if location.kind == KEY
    ]
    assert len(key_locations) == 1
    assert data[key_locations[0].start : key_locations[0].end] == b"k"


def test_folded_scalar_spans_all_source_lines():
    data = SAMPLE.encode("utf-8")
    source_map = frontmatter.loads(SAMPLE).source_map

    folded, location = _value_slice(data, source_map, ("folded",))
    assert folded == b">-\n  hello\n  world\n"
    assert location.start_line != location.end_line

    literal, _ = _value_slice(data, source_map, ("literal",))
    assert literal == b"|\n  line one\n  line two\n"


def test_anchor_declaration_and_alias_reference_are_separate():
    data = SAMPLE.encode("utf-8")
    source_map = frontmatter.loads(SAMPLE).source_map

    anchor_locations = [
        location
        for location in source_map.for_path(("defaults",))
        if location.kind == ANCHOR
    ]
    assert len(anchor_locations) == 1
    anchor = anchor_locations[0]
    assert data[anchor.start : anchor.end] == b"&d"
    assert anchor.target is None

    alias_locations = [
        location
        for location in source_map.for_path(("ref",))
        if location.kind == ALIAS
    ]
    assert len(alias_locations) == 1
    alias = alias_locations[0]
    assert data[alias.start : alias.end] == b"*d"
    assert alias.is_reference
    assert alias.target == ("defaults",)
    assert source_map.anchor_path("d") == ("defaults",)
    assert source_map.references_to(("defaults",)) == [alias]


def test_quoted_dotted_key_is_a_single_field():
    source_map = frontmatter.loads(SAMPLE).source_map

    # A string path splits on dots and must not match the quoted key.
    assert source_map.for_path("a.b") == []
    # A tuple addresses the literal dotted key.
    assert _kinds(source_map, ("a.b",)) == [KEY, VALUE]


def test_sequence_index_paths():
    source_map = frontmatter.loads(SAMPLE).source_map
    assert _kinds(source_map, ("defaults", "tags", 0)) == [VALUE]
    assert _kinds(source_map, ("defaults", "tags", 1)) == [VALUE]


def test_reverse_lookup_by_line_point_and_byte():
    data = SAMPLE.encode("utf-8")
    source_map = frontmatter.loads(SAMPLE).source_map
    _, alias = _value_slice(data, source_map, ("ref",))

    # The alias sits on the line beginning "ref: *d".
    line_hits = source_map.at_line(alias.start_line)
    assert any(hit.path == ("ref",) and hit.kind == ALIAS for hit in line_hits)

    point_hits = source_map.at_point(alias.start_line, alias.start_column)
    assert (alias.kind, alias.path) in [(h.kind, h.path) for h in point_hits]
    # The most specific (shortest) range is reported first.
    assert point_hits[0].kind == ALIAS

    byte_hits = source_map.at_byte(alias.start)
    assert any(hit.path == ("ref",) and hit.kind == ALIAS for hit in byte_hits)


def test_crlf_offsets_select_original_bytes():
    data = (
        b"---\r\n"
        b"base: &d\r\n"
        b"  x: 1\r\n"
        b"folded: >-\r\n"
        b"  hello\r\n"
        b"  world\r\n"
        b"ref: *d\r\n"
        b"---\r\nbody"
    )
    source_map = build_source_map(data)
    assert source_map is not None

    for location in source_map.locations:
        assert 0 <= location.start <= location.end <= len(data)

    folded, _ = _value_slice(data, source_map, ("folded",))
    assert folded == b">-\r\n  hello\r\n  world\r\n"

    alias = [
        location
        for location in source_map.for_path(("ref",))
        if location.kind == ALIAS
    ][0]
    assert data[alias.start : alias.end] == b"*d"
    # Line/column on a CRLF document still resolve on the physical line.
    assert alias.start_line == 6


def test_bom_and_crlf_offsets_select_original_bytes():
    data = (
        "\ufeff---\r\n"
        'base: &d\r\n'
        'title: "my title"\r\n'
        "ref: *d\r\n"
        "---\r\nbody"
    ).encode("utf-8")

    # BOM used to prevent detection; it should now parse and map.
    post = frontmatter.loads(data)
    assert post.metadata["title"] == "my title"
    source_map = post.source_map
    assert source_map is not None

    title, location = _value_slice(data, source_map, ("title",))
    assert title == b'"my title"'
    # The three-byte BOM is accounted for, so the offset points into the
    # original bytes exactly (no off-by-three).
    assert data[location.start : location.end] == b'"my title"'

    alias = [
        item
        for item in source_map.for_path(("ref",))
        if item.kind == ALIAS
    ][0]
    assert data[alias.start : alias.end] == b"*d"


def test_multibyte_value_byte_offsets():
    data = "\ufeff---\n\"weird.key\": 中文\n---\n".encode("utf-8")
    source_map = build_source_map(data)
    assert source_map is not None
    value, location = _value_slice(data, source_map, ("weird.key",))
    assert value == "中文".encode("utf-8")
    assert location.end - location.start == len("中文".encode("utf-8"))


def test_str_input_with_bom_indexes_its_own_encoded_bytes():
    # Text-mode ``open`` translates CRLF -> LF but leaves the BOM, so this is
    # the shape seen when loading by path. Offsets must index that decoded
    # text's encoding, not a different on-disk buffer.
    text = "\ufeff---\nbase: &d\nref: *d\n---\nbody"
    encoded = text.encode("utf-8")
    source_map = build_source_map(text)
    assert source_map is not None

    alias = [
        location
        for location in source_map.for_path(("ref",))
        if location.kind == ALIAS
    ][0]
    assert encoded[alias.start : alias.end] == b"*d"
    anchor = [
        location
        for location in source_map.for_path(("base",))
        if location.kind == ANCHOR
    ][0]
    assert encoded[anchor.start : anchor.end] == b"&d"


def test_load_by_path_carries_consistent_source_map(tmp_path):
    data = (
        "\ufeff---\n"
        "base: &d\n"
        "ref: *d\n"
        '"a.b": v\n'
        "---\nbody"
    ).encode("utf-8")
    path = tmp_path / "post.md"
    path.write_bytes(data)

    post = frontmatter.load(path)
    source_map = post.source_map
    assert source_map is not None
    encoded = path.read_text(encoding="utf-8").encode("utf-8")

    alias = [
        location
        for location in source_map.for_path(("ref",))
        if location.kind == ALIAS
    ][0]
    assert encoded[alias.start : alias.end] == b"*d"
    assert alias.target == ("base",)
    assert [location.kind for location in source_map.for_path(("a.b",))] == [
        KEY,
        VALUE,
    ]


def test_accepts_str_and_bytes_equivalently():
    text_map = build_source_map(SAMPLE)
    bytes_map = build_source_map(SAMPLE.encode("utf-8"))
    assert text_map is not None and bytes_map is not None
    assert [location.as_dict() for location in text_map.locations] == [
        location.as_dict() for location in bytes_map.locations
    ]


def test_no_frontmatter_returns_none_and_empty_map():
    assert build_source_map("just some text\nno front matter\n") is None

    empty = build_source_map("---\n---\nbody")
    assert empty is not None
    assert empty.locations == []
    assert empty.paths == []


def test_non_yaml_handlers_have_no_source_map():
    json_post = frontmatter.loads(
        '{\n"title": "hi"\n}\n\nbody', handler=frontmatter.JSONHandler()
    )
    assert json_post.source_map is None


def test_dump_round_trip_is_unchanged():
    post = frontmatter.loads(SAMPLE)
    reloaded = frontmatter.loads(frontmatter.dumps(post))
    assert reloaded.metadata == post.metadata
    assert reloaded.content == post.content


def test_location_as_dict_is_json_serializable():
    import json

    source_map = frontmatter.loads(SAMPLE).source_map
    encoded = json.dumps([location.as_dict() for location in source_map.locations])
    assert isinstance(encoded, str)
