# -*- coding: utf-8 -*-
"""
Find every handler that can explain the beginning of a document.

``frontmatter.load`` stops at the first handler whose ``detect`` method
matches.  That is convenient, but it hides other readings of the same bytes.
This module keeps all of them and records the byte interval each reading
consumes and the exact body slice it leaves behind.

All offsets are byte offsets into the original input.  Body content is never
stripped or otherwise normalized.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Sequence, cast

if TYPE_CHECKING:
    from . import Post
    from .default_handlers import BaseHandler


__all__ = [
    "AmbiguousFormatError",
    "Interpretation",
    "Interpretations",
    "NoInterpretationError",
    "interpret",
    "probe",
]


class NoInterpretationError(ValueError):
    """Raised when no handler can fully explain the beginning of a text."""


class AmbiguousFormatError(ValueError):
    """
    Raised when handlers explain the beginning in different ways.

    ``interpretations`` holds every candidate, so a caller can ask the user
    which format to use.  Candidates that differ only in metadata key order are
    equivalent and do not raise this error.
    """

    def __init__(self, interpretations: Sequence[Interpretation]):
        self.interpretations = list(interpretations)
        details = ", ".join(
            "{0}(body_start={1})".format(
                type(candidate.handler).__name__, candidate.body_start
            )
            for candidate in self.interpretations
        )
        super().__init__("Ambiguous frontmatter format; choose one of: " + details)


@dataclass(frozen=True, eq=False)
class Interpretation:
    """
    One handler's complete reading of the beginning of a document.

    ``consumed`` is the byte interval ``(0, body_start)`` explained as
    metadata, including delimiters.  ``content`` is the exact decoded slice
    from ``body_start`` to the end; ``content_bytes`` is the same slice as
    bytes.  The first blank line after the metadata is preserved.
    """

    handler: BaseHandler
    metadata: dict[str, object]
    content: str
    body_start: int
    char_start: int
    source: bytes = field(repr=False, compare=False)

    @property
    def consumed(self) -> tuple[int, int]:
        "The original byte interval consumed as metadata"
        return (0, self.body_start)

    @property
    def consumed_bytes(self) -> bytes:
        "The original metadata bytes, including delimiters"
        return self.source[: self.body_start]

    @property
    def content_bytes(self) -> bytes:
        "The original body bytes"
        return self.source[self.body_start :]

    def to_dict(self) -> dict[str, object]:
        "This interpretation as a plain dictionary"
        return {
            "handler": type(self.handler).__name__,
            "metadata": self.metadata,
            "consumed": self.consumed,
            "body_start": self.body_start,
            "content": self.content,
        }

    def to_post(self) -> Post:
        "Turn this interpretation into a :py:class:`frontmatter.Post`"
        from . import Post

        return Post(self.content, self.handler, **self.metadata)


class Interpretations(list[Interpretation]):
    """
    All complete readings of a document's beginning.

    This is a list of :py:class:`Interpretation` objects with convenience
    properties and a :py:meth:`choose` method.
    """

    @property
    def candidates(self) -> list[Interpretation]:
        "The same candidates, as a plain list"
        return list(self)

    @property
    def requires_selection(self) -> bool:
        """
        True when candidates cut the document at different places, or disagree
        about metadata contents.  Metadata key order alone does not count.
        """
        if len(self) < 2:
            return False

        first = _equivalence_key(self[0])
        return any(_equivalence_key(other) != first for other in self[1:])

    @property
    def ambiguous(self) -> bool:
        "Alias for :py:attr:`requires_selection`"
        return self.requires_selection

    def to_dicts(self) -> list[dict[str, object]]:
        "All candidates as plain dictionaries, suitable for JSON output"
        return [candidate.to_dict() for candidate in self]

    def choose(
        self,
        handler: BaseHandler | type[BaseHandler] | None = None,
        index: int | None = None,
    ) -> Interpretation:
        """
        Return one candidate, or raise :py:class:`AmbiguousFormatError`.

        Pass ``handler`` (an instance or class) or ``index`` to choose
        explicitly.  Otherwise a single equivalent result is returned
        automatically.
        """
        if index is not None:
            return self[index]

        if handler is not None:
            for candidate in self:
                if _matches(candidate.handler, handler):
                    return candidate
            raise LookupError(
                "No interpretation used handler {0}".format(
                    getattr(handler, "__name__", type(handler).__name__)
                )
            )

        if not self:
            raise NoInterpretationError("No interpretations available")

        if self.requires_selection:
            raise AmbiguousFormatError(self)

        return self[0]


def _matches(candidate: object, selected: object) -> bool:
    if candidate is selected:
        return True
    if isinstance(selected, type):
        return isinstance(candidate, selected)
    return type(candidate) is type(selected) and _handler_config(
        candidate
    ) == _handler_config(selected)


def _handler_config(handler: object) -> tuple[object, object, object, object]:
    boundary = getattr(handler, "FM_BOUNDARY", None)
    return (
        getattr(boundary, "pattern", boundary),
        getattr(boundary, "flags", None),
        getattr(handler, "START_DELIMITER", None),
        getattr(handler, "END_DELIMITER", None),
    )


def _freeze(value: object) -> object:
    "Turn nested metadata into an order-insensitive comparable value."
    if isinstance(value, dict):
        return tuple(sorted((repr(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(repr(_freeze(item)) for item in value))
    return value


def _equivalence_key(candidate: Interpretation) -> tuple[int, object]:
    "Two candidates are equivalent when body start and metadata agree."
    return (candidate.body_start, _freeze(candidate.metadata))


def _normalize_with_boundaries(raw: str) -> tuple[str, list[int]]:
    """
    Mirror ``util.u`` newline normalization while remembering character
    boundaries in the original text.
    """
    normalized: list[str] = []
    boundaries = [0]
    index = 0

    while index < len(raw):
        if raw[index] == "\r" and index + 1 < len(raw) and raw[index + 1] == "\n":
            index += 2
            normalized.append("\n")
        else:
            normalized.append(raw[index])
            index += 1
        boundaries.append(index)

    return "".join(normalized), boundaries


def _prepare(text: str | bytes, encoding: str) -> tuple[str, bytes]:
    if isinstance(text, bytes):
        raw_text = text.decode(encoding)
        source = bytes(text)
    else:
        raw_text = str(text)
        source = raw_text.encode(encoding)
    return raw_text, source


def _uses_stock_split(handler: BaseHandler) -> bool:
    "True for handlers that split at their FM_BOUNDARY without extra rules."
    from .default_handlers import BaseHandler, JSONHandler

    return type(handler).split in (BaseHandler.split, JSONHandler.split)


def _boundary_matches(handler: BaseHandler, text: str, count: int) -> list[object]:
    boundary = getattr(handler, "FM_BOUNDARY", None)
    finditer = getattr(boundary, "finditer", None)
    if not callable(finditer):
        return []

    matches: list[object] = []
    try:
        for match in finditer(text):
            matches.append(match)
            if len(matches) == count:
                break
    except Exception:
        return []
    return matches


def _line_end_after(text: str, position: int) -> int:
    "Position just after the next line break at or after ``position``."
    newline = text.find("\n", position)
    return len(text) if newline < 0 else newline + 1


def _body_start(handler: BaseHandler, text: str, content_start: int) -> int:
    """
    Keep the first blank line in the body.

    Stock split implementations cut where the closing delimiter's pattern
    ends.  Patterns such as ``^-{3,}\\s*$`` swallow the line break and any
    blank lines after the delimiter, so move the cut back to the end of the
    delimiter's own line.  Custom split methods are left alone.
    """
    if not _uses_stock_split(handler):
        return content_start

    matches = _boundary_matches(handler, text, 2)
    if len(matches) < 2:
        return content_start

    first, closing = matches
    if getattr(first, "start")() != 0:
        return content_start

    closing_start = getattr(closing, "start")()
    target = _line_end_after(text, closing_start)

    low, high = sorted((target, content_start))
    if high > len(text) or text[low:high].strip():
        return content_start

    return target


def _interpret_with(
    handler: BaseHandler,
    normalized: str,
    boundaries: list[int],
    raw_text: str,
    source: bytes,
    encoding: str,
    *,
    require_detect: bool = True,
) -> Interpretation | None:
    stock = _uses_stock_split(handler)

    # ``parse`` strips leading whitespace before detection.  Do the same for
    # the stock handlers, but keep the original text for custom ones.
    split_input = normalized.lstrip() if stock else normalized
    strip_offset = len(normalized) - len(split_input)

    if require_detect:
        detect = getattr(handler, "detect", None)
        if callable(detect):
            try:
                if not detect(split_input):
                    return None
            except Exception:
                return None

    try:
        fm, content = handler.split(split_input)
    except Exception:
        return None

    if not isinstance(fm, str) or not isinstance(content, str):
        return None

    # A handler explains the beginning only if it leaves a literal suffix of
    # the text as body.  An empty metadata prefix is allowed: it means the
    # handler reads the whole document as body.
    if len(content) > len(split_input) or not split_input.endswith(content):
        return None

    normalized_start = strip_offset + _body_start(
        handler, split_input, len(split_input) - len(content)
    )
    char_start = boundaries[normalized_start]

    try:
        loaded = handler.load(fm)
    except Exception:
        return None

    metadata: dict[str, object] = (
        cast(dict[str, object], dict(loaded)) if isinstance(loaded, Mapping) else {}
    )

    return Interpretation(
        handler=handler,
        metadata=metadata,
        content=raw_text[char_start:],
        body_start=len(raw_text[:char_start].encode(encoding)),
        char_start=char_start,
        source=source,
    )


def probe(
    text: str | bytes,
    encoding: str = "utf-8",
    handlers: Iterable[BaseHandler] | None = None,
) -> Interpretations:
    """
    List every handler that can fully explain the beginning of ``text``.

    Each candidate records the handler, the consumed byte interval, the byte
    offset where that handler says the body starts, and the exact body slice.
    Handlers are tried independently; this does not stop after the first
    match.

    .. doctest::

        >>> import frontmatter
        >>> with open('tests/json/hello-json.md', 'rb') as f:
        ...     candidates = frontmatter.probe(f.read())
        >>> type(candidates[0].handler).__name__
        'JSONHandler'
        >>> candidates[0].consumed
        (0, 71)
        >>> candidates[0].content.startswith('\\nTitle')
        True

    """
    if handlers is None:
        from . import handlers as default_handlers

        handlers = default_handlers

    raw_text, source = _prepare(text, encoding)
    normalized, boundaries = _normalize_with_boundaries(raw_text)

    candidates: list[Interpretation] = []
    for handler in handlers:
        candidate = _interpret_with(
            handler, normalized, boundaries, raw_text, source, encoding
        )
        if candidate is not None:
            candidates.append(candidate)

    return Interpretations(candidates)


def interpret(
    text: str | bytes,
    encoding: str = "utf-8",
    handler: BaseHandler | None = None,
    handlers: Iterable[BaseHandler] | None = None,
    index: int | None = None,
) -> Interpretation:
    """
    Return the single unambiguous reading of ``text``.

    If readings cut the body at different byte offsets, or disagree about
    metadata, :py:class:`AmbiguousFormatError` is raised.  Pass ``handler``
    (or ``index``) to choose one.  Differences in metadata key order alone are
    not considered ambiguous.

    The returned :py:attr:`Interpretation.content` is an exact slice of the
    original input; unlike :py:func:`frontmatter.parse`, no whitespace is
    stripped from it.
    """
    report = probe(text, encoding, handlers)

    if handler is not None:
        for candidate in report:
            if _matches(candidate.handler, handler):
                return candidate

        # Match the long-standing behavior of parse/loads: an explicitly
        # supplied handler does not need to pass detect().
        raw_text, source = _prepare(text, encoding)
        normalized, boundaries = _normalize_with_boundaries(raw_text)
        selected = _interpret_with(
            handler,
            normalized,
            boundaries,
            raw_text,
            source,
            encoding,
            require_detect=False,
        )
        if selected is None:
            raise NoInterpretationError(
                "{0} cannot explain the beginning of this text".format(
                    type(handler).__name__
                )
            )
        return selected

    return report.choose(index=index)
