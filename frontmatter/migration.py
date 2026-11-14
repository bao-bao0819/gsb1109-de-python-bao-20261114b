# -*- coding: utf-8 -*-
"""
Lossless migration of YAML and TOML front matter into JSON.

The JSON index this library feeds only understands JSON scalars. That is not
enough to reconstruct the original metadata faithfully: YAML can express
dates, sets and byte strings, and both YAML and TOML distinguish integers from
floats. This module therefore produces a *migration artifact*: ordinary JSON
metadata plus a small ``types`` table that names only the values whose type
would otherwise be lost.

An artifact looks like this::

    {
      "version": 1,
      "format": "yaml",
      "encoding": "utf-8",
      "delimiters": {"start": "---", "end": "---", "newline": "\\n"},
      "metadata": {"published": "2024-05-01", "views": 1200},
      "types": {"/published": "date"},
      "content": "\\nThe body, exactly as it appeared after the closing fence.",
      "prefix": ""
    }

``metadata`` is plain JSON. ``types`` is a mapping of JSON Pointer to a type
tag; it is empty when every value is already unambiguous in JSON. Because the
annotations live beside the metadata rather than inside it, an article whose
front matter already contains a field called ``metadata``, ``types``,
``content``, ``format`` or any other reserved name round-trips without a
collision.

Body content is stored as the exact text following the closing delimiter
line, including its line ending and any trailing bytes. It is never stripped,
so Markdown horizontal rules and byte-for-byte content survive.

The command line interface converts and restores files::

    python -m frontmatter.migration convert old.md -o old.json
    python -m frontmatter.migration restore old.json -o old.md
    python -m frontmatter.migration verify old.json --against old.md
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .default_handlers import (
    JSONHandler,
    TOMLHandler,
    YAMLHandler,
    BaseHandler,
)
from .serialization import (
    SerializationError,
    UnsupportedTypeError,
    decode_metadata,
    encode_metadata,
)

__all__ = [
    "ARTIFACT_VERSION",
    "MigrationArtifact",
    "MigrationError",
    "VerificationError",
    "migrate",
    "migrate_file",
    "restore",
    "restore_file",
    "verify",
    "verify_file",
    "artifact_dumps",
    "artifact_loads",
]

ARTIFACT_VERSION = 1

FORMATS = ("yaml", "toml", "json", "none")


class MigrationError(ValueError):
    """Raised when a document cannot be converted to an artifact."""


class VerificationError(AssertionError):
    """Raised when a restored document differs from its source."""


@dataclass
class MigrationArtifact:
    """
    Plain JSON metadata and the type notes needed to restore the source format.

    ``metadata`` and ``types`` are exactly what is written to the JSON artifact.
    Use :py:meth:`decoded_metadata` to get Python values back.
    """

    metadata: dict[str, object]
    types: dict[str, str] = field(default_factory=dict)
    content: str = ""
    format: str = "yaml"
    encoding: str = "utf-8"
    start_delimiter: str = "---"
    end_delimiter: str = "---"
    newline: str = "\n"
    prefix: str = ""
    version: int = ARTIFACT_VERSION

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise MigrationError(f"unknown source format: {self.format!r}")
        if self.version != ARTIFACT_VERSION:
            raise MigrationError(f"unsupported artifact version: {self.version!r}")
        if not isinstance(self.metadata, dict):
            raise MigrationError("metadata must be a JSON object")
        if not isinstance(self.types, dict):
            raise MigrationError("types must be a JSON object")
        if not isinstance(self.content, str):
            raise MigrationError("content must be a string")
        if not isinstance(self.prefix, str):
            raise MigrationError("prefix must be a string")

    @property
    def decoded_metadata(self) -> dict[str, object]:
        "Return metadata with the original Python types restored."
        return decode_metadata(self.metadata, self.types)

    @property
    def reserved_fields(self) -> tuple[str, ...]:
        "Top-level artifact keys that are never mixed into metadata."
        return (
            "version",
            "format",
            "encoding",
            "delimiters",
            "metadata",
            "types",
            "content",
            "prefix",
        )

    def to_dict(self) -> dict[str, object]:
        "Return the artifact as a JSON-serializable dictionary."
        data: dict[str, object] = {
            "version": self.version,
            "format": self.format,
            "encoding": self.encoding,
            "delimiters": {
                "start": self.start_delimiter,
                "end": self.end_delimiter,
                "newline": self.newline,
            },
            "metadata": self.metadata,
            "types": self.types,
            "content": self.content,
            "prefix": self.prefix,
        }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MigrationArtifact":
        "Build an artifact from a parsed JSON object, validating its shape."
        if not isinstance(data, Mapping):
            raise MigrationError("artifact must be a JSON object")

        version = data.get("version", ARTIFACT_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise MigrationError("artifact version must be an integer")

        format = data.get("format")
        if not isinstance(format, str):
            raise MigrationError("artifact format must be a string")

        encoding = data.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise MigrationError("artifact encoding must be a string")

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            raise MigrationError("artifact metadata must be a JSON object")

        types = data.get("types", {})
        if not isinstance(types, dict):
            raise MigrationError("artifact types must be a JSON object")
        for pointer, tag in types.items():
            if not isinstance(pointer, str) or not isinstance(tag, str):
                raise MigrationError("artifact types must map JSON Pointers to tags")

        content = data.get("content")
        if not isinstance(content, str):
            raise MigrationError("artifact content must be a string")

        prefix = data.get("prefix", "")
        if not isinstance(prefix, str):
            raise MigrationError("artifact prefix must be a string")

        delimiters = data.get("delimiters", {})
        if not isinstance(delimiters, dict):
            raise MigrationError("artifact delimiters must be a JSON object")
        start = delimiters.get("start", "---")
        end = delimiters.get("end", "---")
        newline = delimiters.get("newline", "\n")
        if not all(isinstance(part, str) for part in (start, end, newline)):
            raise MigrationError("artifact delimiters must be strings")

        return cls(
            metadata=metadata,
            types=types,
            content=content,
            format=format,
            encoding=encoding,
            start_delimiter=start,
            end_delimiter=end,
            newline=newline,
            prefix=prefix,
            version=version,
        )


@dataclass(frozen=True)
class _Line:
    start: int
    text: str
    ending: str


def _lines(text: str) -> Iterator[_Line]:
    "Yield exact line text, its line ending and its offset in ``text``."
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        yield _Line(offset, body, line[len(body) :])
        offset += len(line)


BOM = "﻿"


def _probe(text: str) -> tuple[str, int]:
    """
    Skip a BOM then any leading blank lines, which are kept as a prefix.

    Only whitespace *before* the opening fence is treated as a prefix. Leading
    spaces inside a delimiter line are part of the delimiter itself.
    """
    index = 0
    if text.startswith(BOM):
        index = len(BOM)
    while True:
        line_end = text.find("\n", index)
        if line_end == -1:
            break
        if text[index:line_end].strip("\r"):
            break
        index = line_end + 1
    return text[index:], index


def _handler_for(format: str) -> BaseHandler:
    if format == "yaml":
        return YAMLHandler()
    if format == "toml":
        if TOMLHandler is None:
            raise MigrationError("TOML support requires the toml package")
        return TOMLHandler()
    if format == "json":
        return JSONHandler()
    raise MigrationError(f"no handler for format {format!r}")


def _detect_format(text: str) -> str:
    probe, _ = _probe(text)
    if YAMLHandler().detect(probe):
        return "yaml"
    if TOMLHandler is not None and TOMLHandler().detect(probe):
        return "toml"
    if JSONHandler().detect(probe) and probe.startswith("{"):
        return "json"
    return "none"


def _json_span(text: str) -> tuple[int, int] | None:
    "Find the exact extent of a leading JSON object, braces included."
    probe, prefix = _probe(text)
    if not probe.startswith("{"):
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(probe)
    except json.JSONDecodeError:
        return None
    return prefix, prefix + end


def _line_matches(
    format: str, line: _Line, handler: BaseHandler | None = None
) -> bool:
    if format == "json" and handler is None:
        return line.text in ("{", "}")
    pattern = (handler or _handler_for(format)).FM_BOUNDARY
    if isinstance(pattern, str):
        return line.text == pattern
    assert pattern is not None
    return pattern.match(line.text) is not None


def _newline_at(text: str, offset: int) -> str:
    "Return the line ending used at ``offset``, defaulting to a newline."
    newline_at = text.find("\n", offset)
    if newline_at == -1:
        return "\n"
    if newline_at > offset and text[newline_at - 1] == "\r":
        return "\r\n"
    return "\n"


def _delimiter_lines(
    text: str, format: str, handler: BaseHandler | None = None
) -> tuple[int, list[_Line]]:
    """
    Return the prefix length and matched delimiter lines.

    Matching starts after the prefix (BOM and leading blank lines), so offsets
    are shifted back into ``text`` coordinates before returning. A supplied
    handler may use literal delimiters the default boundary does not know
    about, so its declared start and end strings also count as fences.
    """
    _, prefix_length = _probe(text)
    tail = text[prefix_length:]

    def matches(line: _Line) -> bool:
        if _line_matches(format, line, handler):
            return True
        if handler is None:
            return False
        return line.text in (
            handler.START_DELIMITER,
            handler.END_DELIMITER,
        )

    return prefix_length, [
        _Line(prefix_length + line.start, line.text, line.ending)
        for line in _lines(tail)
        if matches(line)
    ]


def _split_document(
    text: str,
    format: str | None = None,
    handler: BaseHandler | None = None,
) -> tuple[str, str, str, str, str, str]:
    """
    Split raw text without normalizing anything.

    Returns ``(format, prefix, start_line, end_line, newline, metadata_text)``.
    The body begins immediately after the closing delimiter; callers use
    :func:`_body_offset` to find it in the original text.
    """
    format = format or _detect_format(text)
    if format == "none":
        return format, "", "", "", "\n", ""

    if format == "json":
        span = _json_span(text)
        if span is None:
            return "none", "", "", "", "\n", ""
        brace_start, brace_end = span
        return (
            "json",
            text[:brace_start],
            "{",
            "}",
            _newline_at(text, brace_start),
            text[brace_start + 1 : brace_end - 1],
        )

    prefix_length, matches = _delimiter_lines(text, format, handler)
    if len(matches) < 2:
        return "none", "", "", "", "\n", ""

    start = matches[0]
    end = matches[1]

    metadata_text = text[start.start + len(start.text) + len(start.ending) : end.start]
    return (
        format,
        text[:prefix_length],
        start.text,
        end.text,
        start.ending or "\n",
        metadata_text,
    )


def _load_metadata(
    format: str, metadata_text: str, handler: BaseHandler | None = None
) -> dict[str, object]:
    handler = handler or _handler_for(format)
    fm = "{" + metadata_text + "}" if format == "json" else metadata_text
    try:
        loaded = handler.load(fm)
    except Exception as exc:
        raise MigrationError(f"could not parse {format} front matter: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise MigrationError(f"{format} front matter must be a mapping")
    return loaded


def _handler_format(handler: BaseHandler) -> str:
    "Name the format of an explicitly supplied handler."
    if isinstance(handler, YAMLHandler):
        return "yaml"
    if isinstance(handler, JSONHandler):
        return "json"
    if TOMLHandler is not None and isinstance(handler, TOMLHandler):
        return "toml"
    format = getattr(handler, "FORMAT", None)
    if isinstance(format, str) and format in FORMATS:
        return format
    raise MigrationError(
        "cannot determine artifact format for this handler; "
        "set a FORMAT attribute on the handler"
    )


def _artifact_from_text(
    text: str,
    encoding: str,
    format: str,
    handler: BaseHandler | None = None,
) -> MigrationArtifact:
    split_format, prefix, start, end, newline, metadata_text = _split_document(
        text, format if format != "none" else None, handler
    )
    if split_format == "none":
        return MigrationArtifact(
            metadata={}, types={}, content=text, format="none", encoding=encoding
        )

    loaded = _load_metadata(format, metadata_text, handler)
    metadata, types = encode_metadata(loaded)
    return MigrationArtifact(
        metadata=metadata,
        types=types,
        content=text[_body_offset(text, format, handler) :],
        format=format,
        encoding=encoding,
        start_delimiter=start,
        end_delimiter=end,
        newline=newline,
        prefix=prefix,
    )


def _validate_handler_layout(
    text: str,
    format: str,
    handler: BaseHandler,
    metadata: dict[str, object],
) -> None:
    """
    Reject handlers whose own split disagrees with the top-of-file layout.

    Handlers like the examples' reversed-front-matter handler put metadata at
    the bottom. An artifact records one opening and one closing fence, so it
    cannot represent that layout; failing here is better than emitting an
    artifact that quietly drops the body or the metadata.
    """
    try:
        fm, content = handler.split(text)
        loaded = handler.load(fm)
    except Exception as exc:
        raise MigrationError(
            "handler layout could not be reproduced by a migration artifact: "
            f"{exc}"
        ) from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict) or loaded != metadata:
        raise MigrationError(
            "handler layout could not be reproduced by a migration artifact; "
            "only layouts with one opening and one closing fence are supported"
        )

    if content != text[_body_offset(text, format, handler) :]:
        raise MigrationError(
            "handler layout could not be reproduced by a migration artifact; "
            "the body must follow the closing fence"
        )


def migrate(
    text: str | bytes,
    encoding: str = "utf-8",
    handler: BaseHandler | None = None,
) -> MigrationArtifact:
    """
    Convert a YAML, TOML or JSON document into a migration artifact.

    The document may be ``str`` or bytes. Bytes are decoded with ``encoding``
    and that encoding is recorded so :py:func:`restore` can write the same
    bytes back. Passing ``handler`` forces a format instead of detecting it.

    >>> artifact = migrate('---\\npublished: 2024-05-01\\n---\\n\\nBody')
    >>> artifact.metadata
    {'published': '2024-05-01'}
    >>> artifact.types
    {'/published': 'date'}
    >>> artifact.content
    '\\n\\nBody'
    """
    if isinstance(text, bytes):
        text = text.decode(encoding)

    if handler is not None:
        format = _handler_format(handler)
        artifact = _artifact_from_text(text, encoding, format, handler)
        if artifact.format != "none":
            _validate_handler_layout(text, format, handler, artifact.metadata)
        return artifact

    format, _, _, _, _, _ = _split_document(text)
    return _artifact_from_text(text, encoding, format)


def _body_offset(text: str, format: str, handler: BaseHandler | None = None) -> int:
    "Offset just after the closing delimiter, excluding its line ending."
    if format == "json":
        span = _json_span(text)
        return span[1] if span is not None else len(text)

    _, matches = _delimiter_lines(text, format, handler)
    if len(matches) < 2:
        return len(text)
    end = matches[1]
    return end.start + len(end.text)


def migrate_file(
    filename: str | pathlib.Path,
    encoding: str = "utf-8",
    handler: BaseHandler | None = None,
) -> MigrationArtifact:
    """
    Read a document from disk as bytes and return its migration artifact.
    """
    data = pathlib.Path(filename).read_bytes()
    return migrate(data, encoding, handler)


def _has_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return any(_has_null(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_has_null(item) for item in value)
    return False


def _render_metadata(
    artifact: MigrationArtifact, metadata: dict[str, object]
) -> str:
    if artifact.format == "none":
        return ""
    if artifact.format == "json":
        try:
            return json.dumps(metadata, ensure_ascii=False, indent=4)
        except (TypeError, ValueError) as exc:
            raise UnsupportedTypeError(
                f"metadata cannot be written as JSON: {exc}"
            ) from exc

    if artifact.format == "toml" and _has_null(metadata):
        raise UnsupportedTypeError(
            "TOML has no null value, so an explicit null cannot be restored"
        )

    handler = _handler_for(artifact.format)
    if not metadata:
        return ""
    try:
        # Keep the metadata key order recorded in the artifact, and remove
        # only the serializer's trailing newline so delimiters stay adjacent.
        return handler.export(metadata, sort_keys=False).rstrip("\n")
    except (TypeError, ValueError, SerializationError) as exc:
        raise UnsupportedTypeError(
            f"metadata cannot be written as {artifact.format}: {exc}"
        ) from exc


def restore(artifact: MigrationArtifact | Mapping[str, Any]) -> str:
    """
    Turn an artifact back into text in its original format.

    Metadata is decoded using the artifact's type table. Body text, including
    every line ending, is appended unchanged.

    >>> artifact = migrate('---\\npublished: 2024-05-01\\n---\\n\\nBody')
    >>> restore(artifact)
    '---\\npublished: 2024-05-01\\n---\\n\\nBody'
    """
    if not isinstance(artifact, MigrationArtifact):
        artifact = MigrationArtifact.from_dict(artifact)

    metadata = artifact.decoded_metadata

    if artifact.format == "none":
        return artifact.content

    if artifact.format == "json":
        body = _render_metadata(artifact, metadata)
        return artifact.prefix + body + artifact.content

    metadata_text = _render_metadata(artifact, metadata)
    if metadata_text:
        document = (
            artifact.start_delimiter
            + artifact.newline
            + metadata_text
            + artifact.newline
            + artifact.end_delimiter
        )
    else:
        document = (
            artifact.start_delimiter + artifact.newline + artifact.end_delimiter
        )

    return artifact.prefix + document + artifact.content


def restore_file(
    artifact: MigrationArtifact | Mapping[str, Any] | str | pathlib.Path,
    filename: str | pathlib.Path,
    encoding: str | None = None,
) -> None:
    """
    Restore an artifact to ``filename``, writing the artifact's original bytes.
    """
    if not isinstance(artifact, MigrationArtifact):
        if isinstance(artifact, (str, pathlib.Path)):
            artifact = artifact_loads(pathlib.Path(artifact).read_text("utf-8"))
        else:
            artifact = MigrationArtifact.from_dict(artifact)

    text = restore(artifact)
    pathlib.Path(filename).write_bytes(text.encode(encoding or artifact.encoding))


def artifact_dumps(artifact: MigrationArtifact, indent: int = 2) -> str:
    "Serialize an artifact to JSON text."
    return json.dumps(artifact.to_dict(), ensure_ascii=False, indent=indent)


def artifact_loads(text: str | bytes) -> MigrationArtifact:
    "Parse an artifact from JSON text or bytes."
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"artifact is not valid JSON: {exc}") from exc
    return MigrationArtifact.from_dict(data)


def _compare_values(expected: object, actual: object, path: str) -> None:
    location = path or "/"

    if type(expected) is not type(actual):
        raise VerificationError(
            f"{location}: expected type {type(expected).__name__}, "
            f"got {type(actual).__name__}"
        )

    if isinstance(expected, float):
        assert isinstance(actual, float)
        if math.isnan(expected) and math.isnan(actual):
            return
        if expected != actual:
            raise VerificationError(f"{location}: {expected!r} != {actual!r}")
        return

    if isinstance(expected, Mapping):
        assert isinstance(actual, Mapping)
        expected_keys = list(expected.keys())
        actual_keys = list(actual.keys())
        # Key order is not a value; the artifact records order, but the
        # comparison here is about presence, values and types.
        if set(expected_keys) != set(actual_keys):
            missing = [key for key in expected_keys if key not in actual_keys]
            extra = [key for key in actual_keys if key not in expected_keys]
            raise VerificationError(
                f"{location}: keys differ: missing={missing!r}, extra={extra!r}"
            )
        for key in expected_keys:
            token = str(key).replace("~", "~0").replace("/", "~1")
            _compare_values(expected[key], actual[key], f"{path}/{token}")
        return

    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            raise VerificationError(f"{location}: expected a sequence")
        if len(expected) != len(actual):
            raise VerificationError(
                f"{location}: expected {len(expected)} items, got {len(actual)}"
            )
        for index, (left, right) in enumerate(zip(expected, actual)):
            _compare_values(left, right, f"{path}/{index}")
        return

    if expected != actual:
        raise VerificationError(f"{location}: {expected!r} != {actual!r}")


def verify(
    artifact: MigrationArtifact | Mapping[str, Any] | str,
    text: str | bytes,
    encoding: str = "utf-8",
    handler: BaseHandler | None = None,
) -> None:
    """
    Check that an artifact restores ``text`` with the same values and types.

    Raises :class:`VerificationError` with the first difference, including the
    path to the offending value. Body content is compared exactly. Pass
    ``handler`` when the source used delimiters other than the defaults.
    """
    if isinstance(artifact, str):
        artifact = artifact_loads(artifact)
    elif not isinstance(artifact, MigrationArtifact):
        artifact = MigrationArtifact.from_dict(artifact)

    if isinstance(text, bytes):
        text = text.decode(encoding)

    original_format, _, _, _, _, original_metadata_text = _split_document(
        text, None if handler else artifact.format, handler
    )
    if original_format == "none":
        original_metadata: dict[str, object] = {}
    else:
        original_metadata = _load_metadata(
            artifact.format, original_metadata_text, handler
        )

    _compare_values(original_metadata, artifact.decoded_metadata, "")

    restored_text = restore(artifact)
    restored_format, _, _, _, _, restored_metadata_text = _split_document(
        restored_text, None if handler else artifact.format, handler
    )
    if restored_format == "none":
        restored_metadata: dict[str, object] = {}
    else:
        restored_metadata = _load_metadata(
            artifact.format, restored_metadata_text, handler
        )

    _compare_values(original_metadata, restored_metadata, "")

    original_body = (
        text[_body_offset(text, artifact.format, handler)]
        if original_format != "none"
        else text
    )
    restored_body = (
        restored_text[
            _body_offset(restored_text, artifact.format, handler)
        ]
        if restored_format != "none"
        else restored_text
    )
    if original_body != restored_body:
        raise VerificationError(
            "body content differs: expected "
            f"{original_body!r}, got {restored_body!r}"
        )


def verify_file(
    artifact: MigrationArtifact | Mapping[str, Any] | str | pathlib.Path,
    filename: str | pathlib.Path,
    encoding: str = "utf-8",
    handler: BaseHandler | None = None,
) -> None:
    "Verify a restored artifact against the original file on disk."
    if not isinstance(artifact, MigrationArtifact):
        if isinstance(artifact, (str, pathlib.Path)):
            artifact = artifact_loads(pathlib.Path(artifact).read_text("utf-8"))
        else:
            artifact = MigrationArtifact.from_dict(artifact)

    verify(artifact, pathlib.Path(filename).read_bytes(), encoding, handler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m frontmatter.migration",
        description="Convert YAML/TOML front matter to lossless JSON artifacts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="write a JSON artifact")
    convert.add_argument("source")
    convert.add_argument("-o", "--output")
    convert.add_argument("--encoding", default="utf-8")
    convert.add_argument("--indent", type=int, default=2)

    restore_parser = subparsers.add_parser(
        "restore", help="write the document back in its original format"
    )
    restore_parser.add_argument("artifact")
    restore_parser.add_argument("-o", "--output")
    restore_parser.add_argument("--encoding")

    verify_parser = subparsers.add_parser(
        "verify", help="compare a restored artifact with its source"
    )
    verify_parser.add_argument("artifact")
    verify_parser.add_argument("--against", required=True)
    verify_parser.add_argument("--encoding", default="utf-8")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "convert":
            artifact = migrate_file(args.source, args.encoding)
            output = artifact_dumps(artifact, args.indent) + "\n"
            if args.output:
                pathlib.Path(args.output).write_text(output, "utf-8")
            else:
                sys.stdout.write(output)
            return 0

        if args.command == "restore":
            artifact = artifact_loads(
                pathlib.Path(args.artifact).read_text("utf-8")
            )
            if args.output:
                restore_file(artifact, args.output, args.encoding)
            else:
                sys.stdout.buffer.write(
                    restore(artifact).encode(args.encoding or artifact.encoding)
                )
            return 0

        verify_file(args.artifact, args.against, args.encoding)
        print("ok: values, types and body content match")
        return 0
    except (MigrationError, VerificationError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
