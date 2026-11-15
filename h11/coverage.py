# Reconstruction of a single archived object from a historical capture that
# mixes partial (206) responses with retries that returned the whole
# representation (200).
#
# The capture is treated as evidence, not as a body to concatenate. Each
# stored response contributes a byte interval, and intervals are only merged
# when a strong validator proves that they describe the same version of the
# object. A 200 response is a complete snapshot in its own right. Once one is
# present, every stored interval is checked byte-for-byte against it, which
# both validates the older shards and pins down exactly which bytes of a
# disagreement are wrong.
#
# Weak entity tags and Last-Modified are surfaced as evidence, but equality of
# a weak validator never proves that two ranges can be combined. Such stories
# are reported as unattributed instead of being silently stitched together.

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ._connection import Connection, NEED_DATA, PAUSED
from ._events import (
    Data,
    EndOfMessage,
    InformationalResponse,
    Request,
    Response,
)
from ._headers import HeaderTypes, Headers, normalize_and_validate
from ._state import CLIENT
from ._util import RemoteProtocolError

__all__ = [
    "ByteRange",
    "CaptureParseError",
    "CapturedResponse",
    "Conflict",
    "ContentRange",
    "CoverageError",
    "CoverageIssue",
    "CoverageReport",
    "CoverageShard",
    "FullCheck",
    "IncompleteCoverageError",
    "VersionCoverage",
    "analyze",
    "analyze_capture",
    "parse_content_range",
    "parse_response_capture",
]


# ---------------------------------------------------------------------------
# Byte intervals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class ByteRange:
    """An inclusive interval of object bytes, as used by ``Content-Range``."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("byte range start must be non-negative")
        if self.end < self.start:
            raise ValueError("byte range end must not precede its start")

    def __len__(self) -> int:
        return self.end - self.start + 1

    def __str__(self) -> str:
        return "{}-{}".format(self.start, self.end)

    def intersects(self, other: "ByteRange") -> bool:
        return self.start <= other.end and other.start <= self.end

    def intersection(self, other: "ByteRange") -> Optional["ByteRange"]:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start > end:
            return None
        return ByteRange(start, end)

    def contains(self, other: "ByteRange") -> bool:
        return self.start <= other.start and other.end <= self.end


@dataclass(frozen=True)
class ContentRange:
    """A parsed ``Content-Range`` value; ``total`` is ``None`` for ``*``."""

    byte_range: ByteRange
    total: Optional[int]


_content_range_re = re.compile(rb"bytes[ \t]+([0-9]+)-([0-9]+)/([0-9]+|\*)")
_unsatisfied_content_range_re = re.compile(rb"bytes[ \t]+\*/([0-9]+|\*)")


def _as_header_bytes(value: Union[bytes, str]) -> bytes:
    if isinstance(value, bytes):
        return value.strip()
    return value.encode("ascii").strip()


def parse_content_range(value: Union[bytes, str]) -> ContentRange:
    """Parse a satisfied ``Content-Range: bytes first-last/complete`` value."""

    raw = _as_header_bytes(value)
    match = _content_range_re.fullmatch(raw)
    if match is None:
        raise ValueError("malformed Content-Range: {!r}".format(raw))
    start = int(match.group(1))
    end = int(match.group(2))
    if end < start:
        raise ValueError("Content-Range ends before it starts: {!r}".format(raw))
    if match.group(3) == b"*":
        total: Optional[int] = None
    else:
        total = int(match.group(3))
        if end >= total:
            raise ValueError(
                "Content-Range exceeds the stated complete length: {!r}".format(raw)
            )
    return ContentRange(ByteRange(start, end), total)


def _parse_unsatisfied_total(value: bytes) -> Optional[int]:
    match = _unsatisfied_content_range_re.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            "malformed unsatisfied Content-Range: {!r}".format(value)
        )
    if match.group(1) == b"*":
        return None
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Capture intake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedResponse:
    """One stored HTTP response from the capture.

    ``headers`` accepts the same forms as :class:`h11.Response` and is
    normalized with the same header rules, so malformed framing headers raise
    :exc:`h11.LocalProtocolError` rather than being interpreted. ``body`` is
    the message body with transfer coding already removed. After construction
    ``headers`` is always a :class:`h11.Headers`.
    """

    status_code: int
    headers: Union[HeaderTypes, Headers] = field(default_factory=list)
    body: bytes = b""

    def __post_init__(self) -> None:
        normalized = normalize_and_validate(self.headers)
        object.__setattr__(self, "headers", normalized)
        object.__setattr__(self, "body", bytes(self.body))

    def header_list(self) -> Headers:
        # ``headers`` is normalized in __post_init__; this accessor just
        # narrows the declared union for type checkers.
        assert isinstance(self.headers, Headers)
        return self.headers


class CaptureParseError(Exception):
    """A raw capture could not be split into complete HTTP responses."""

    def __init__(self, response_index: int, message: str) -> None:
        Exception.__init__(
            self, "response {}: {}".format(response_index, message)
        )
        self.response_index = response_index


def parse_response_capture(data: bytes) -> List[CapturedResponse]:
    """Split a raw capture holding consecutive HTTP/1.1 responses.

    The input may be one stored response or several responses recorded on one
    persistent connection. Informational responses are skipped and a final
    close-delimited body is accepted. Truncated or malformed input raises
    :exc:`CaptureParseError`; it is never guessed at.
    """

    remaining = bytes(data)
    responses: List[CapturedResponse] = []
    while remaining:
        index = len(responses)
        connection = Connection(CLIENT)
        # h11 only accepts an informational response while a request is
        # outstanding, and a capture of stored server responses has no request
        # to replay. A syntactically inert placeholder request supplies that
        # state without constraining what the server may answer.
        placeholder = Request(
            method="GET", target="/", headers=[("Host", "capture.local")]
        )
        connection.send(placeholder)
        connection.send(EndOfMessage())
        connection.receive_data(remaining)
        saw_eof = False
        head: Optional[Response] = None
        body = bytearray()
        while True:
            try:
                event = connection.next_event()
            except RemoteProtocolError as exc:
                raise CaptureParseError(index, str(exc)) from exc
            if event is NEED_DATA:
                if saw_eof:
                    raise CaptureParseError(index, "truncated response")
                saw_eof = True
                connection.receive_data(b"")
                continue
            if event is PAUSED:
                raise CaptureParseError(index, "connection paused mid-response")
            if isinstance(event, InformationalResponse):
                continue
            if isinstance(event, Response):
                if head is not None:
                    raise CaptureParseError(index, "more than one response head")
                head = event
                continue
            if isinstance(event, Data):
                body += event.data
                continue
            if isinstance(event, EndOfMessage):
                break
            raise CaptureParseError(index, "unexpected event {!r}".format(event))
        if head is None:
            raise CaptureParseError(index, "capture contains no response head")
        responses.append(
            CapturedResponse(
                status_code=head.status_code,
                headers=head.headers,
                body=bytes(body),
            )
        )
        remaining = connection.trailing_data[0]
    return responses


# ---------------------------------------------------------------------------
# Report objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageIssue:
    """A problem noticed while reading one shard or the capture as a whole."""

    code: str
    message: str
    shard_index: Optional[int] = None


@dataclass(frozen=True)
class FullCheck:
    """A stored range checked byte-for-byte against one whole-object response."""

    full_index: int
    matches: bool
    overlap: Optional[ByteRange]
    differences: Tuple[ByteRange, ...]
    outside: bool = False


@dataclass(frozen=True)
class Conflict:
    """Two overlapping ranges whose shared bytes disagree.

    The shards are tied to the same version by a strong validator they share,
    or by a complete snapshot that proves one of them. ``overlap`` is their
    whole intersection and ``differences`` is the exact set of sub-ranges
    whose bytes disagree. When a whole-object response settles which bytes
    are right, its index is recorded in ``resolved_by_full``; otherwise the
    conflict blocks publishing.
    """

    version_key: str
    shard_indices: Tuple[int, int]
    overlap: ByteRange
    differences: Tuple[ByteRange, ...]
    resolved_by_full: Optional[int] = None

    @property
    def blocking(self) -> bool:
        return self.resolved_by_full is None


@dataclass(frozen=True)
class CoverageShard:
    """One stored response as interpreted by the coverage audit.

    ``attribution`` names the evidence that was present on the response
    itself. ``proven_by`` names what actually tied the shard to a version:
    its own strong validator, a whole-object response whose bytes matched the
    shard, or nothing (``None``).
    """

    index: int
    status_code: int
    byte_range: Optional[ByteRange]
    body: bytes
    attribution: str
    version_key: Optional[str]
    etag: Optional[bytes]
    weak_etag: Optional[bytes]
    last_modified: Optional[bytes]
    declared_total: Optional[int]
    accepted: bool
    proven_by: Optional[str]
    issues: Tuple[CoverageIssue, ...]
    full_checks: Tuple[FullCheck, ...] = ()

    @property
    def length(self) -> int:
        if self.byte_range is None:
            return 0
        return len(self.byte_range)

    @property
    def unattributed(self) -> bool:
        return self.byte_range is not None and self.proven_by is None


@dataclass(frozen=True)
class VersionCoverage:
    """Coverage of one version of the archived object.

    ``proven`` is true when a strong validator (or byte-identical complete
    snapshots) ties the contributing responses to one version. A candidate
    entry with ``proven=False`` is only a weak-validator grouping: its gaps
    and conflicts are informative, but it can never be published by joining
    ranges. ``publishable`` implies ``proven``.
    """

    key: str
    etag: Optional[bytes]
    total_length: Optional[int]
    coverage: Tuple[ByteRange, ...]
    missing: Optional[Tuple[ByteRange, ...]]
    conflicts: Tuple[Conflict, ...]
    shard_indices: Tuple[int, ...]
    full_indices: Tuple[int, ...]
    complete: bool
    publishable: bool
    proven: bool = True

    @property
    def covered_length(self) -> int:
        return sum(len(found) for found in self.coverage)

    @property
    def blocking_conflicts(self) -> Tuple[Conflict, ...]:
        return tuple(conflict for conflict in self.conflicts if conflict.blocking)


class CoverageError(Exception):
    """Base class for coverage reconstruction failures."""


class IncompleteCoverageError(CoverageError):
    """The requested version still has gaps or unresolved conflicts."""


@dataclass(frozen=True)
class CoverageReport:
    """The reconstructed coverage table for one archived object."""

    shards: Tuple[CoverageShard, ...]
    versions: Tuple[VersionCoverage, ...]
    conflicts: Tuple[Conflict, ...]
    issues: Tuple[CoverageIssue, ...]
    expected_length: Optional[int]
    total_length: Optional[int]
    publishable: Optional[VersionCoverage]

    def assemble(
        self, version: Union[None, str, int, VersionCoverage] = None
    ) -> bytes:
        """Return the published bytes for a complete version.

        By default the newest publishable version is used, preferring a whole
        object when one exists. A version can also be selected by key, by
        position in :attr:`versions`, or by passing the
        :class:`VersionCoverage` itself. Gaps and unresolved conflicts raise
        :exc:`IncompleteCoverageError` rather than emitting a partial or
        silently patched object.
        """

        if isinstance(version, VersionCoverage):
            chosen = version
        elif version is None:
            if self.publishable is None:
                raise IncompleteCoverageError("no publishable version")
            chosen = self.publishable
        elif isinstance(version, int):
            try:
                chosen = self.versions[version]
            except IndexError:
                raise IncompleteCoverageError(
                    "no version at position {}".format(version)
                )
        else:
            matches = [found for found in self.versions if found.key == version]
            if not matches:
                raise IncompleteCoverageError(
                    "unknown version {!r}".format(version)
                )
            chosen = matches[0]
        if not chosen.publishable or chosen.total_length is None:
            raise IncompleteCoverageError(
                "version {} is not publishable".format(chosen.key)
            )
        if chosen.total_length == 0:
            return b""
        output = bytearray(chosen.total_length)
        selected = [
            self.shards[index]
            for index in chosen.shard_indices
            if self.shards[index].accepted
            and self.shards[index].byte_range is not None
        ]
        # Whole-object responses are written last so that a range can never
        # overwrite bytes that came from a complete, authoritative snapshot.
        selected.sort(key=lambda shard: (shard.status_code == 200, shard.index))
        for shard in selected:
            assert shard.byte_range is not None
            start = shard.byte_range.start
            output[start : start + len(shard.body)] = shard.body
        return bytes(output)

    def format_table(self) -> str:
        """Render a plain-text coverage table suitable for a terminal."""

        lines = ["SHARDS"]
        for shard in self.shards:
            where = "empty" if shard.byte_range is None else str(shard.byte_range)
            state = "accepted" if shard.accepted else "rejected"
            lines.append(
                "#{:<3d} {:3d}  {:>12s}  {:>7s}  {:<16s} {}".format(
                    shard.index,
                    shard.status_code,
                    where,
                    "{}B".format(shard.length),
                    shard.attribution,
                    state,
                )
            )
            for check in shard.full_checks:
                if check.matches:
                    lines.append(
                        "      verified against whole object #{}".format(
                            check.full_index
                        )
                    )
                elif check.overlap is not None or check.outside:
                    differences = (
                        ", ".join(str(found) for found in check.differences)
                        if check.differences
                        else "(no overlapping bytes)"
                    )
                    lines.append(
                        "      differs from whole object #{} in {}".format(
                            check.full_index, differences
                        )
                    )
        lines.append("")
        lines.append("VERSIONS")
        if not self.versions:
            lines.append(
                "(no version proven; weak validators alone cannot be joined)"
            )
        for version in self.versions:
            if version.total_length is None:
                total = "?"
            else:
                total = str(version.total_length)
            state = "publishable" if version.publishable else "incomplete"
            lines.append(
                "{}  {}/{} bytes  {}".format(
                    version.key, version.covered_length, total, state
                )
            )
            if not version.proven and version.key.startswith("candidate:"):
                lines.append(
                    "  not proven: equal weak validators do not justify joining ranges"
                )
            if version.missing is None:
                lines.append("  missing: unknown (complete length not stated)")
            elif version.missing:
                lines.append(
                    "  missing: {}".format(
                        ", ".join(str(found) for found in version.missing)
                    )
                )
            else:
                lines.append("  missing: none")
            for conflict in version.conflicts:
                if conflict.resolved_by_full is None:
                    suffix = "unresolved"
                else:
                    suffix = "resolved by #{}".format(conflict.resolved_by_full)
                lines.append(
                    "  conflict #{} vs #{} in {} at {} ({})".format(
                        conflict.shard_indices[0],
                        conflict.shard_indices[1],
                        conflict.overlap,
                        ", ".join(str(found) for found in conflict.differences),
                        suffix,
                    )
                )
        if self.issues:
            lines.append("")
            lines.append("ISSUES")
            for issue in self.issues:
                if issue.shard_index is None:
                    where = "capture"
                else:
                    where = "#{}".format(issue.shard_index)
                lines.append("{}  {}: {}".format(where, issue.code, issue.message))
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly view of the whole report."""

        def range_dict(found: Optional[ByteRange]) -> Optional[Dict[str, int]]:
            if found is None:
                return None
            return {"start": found.start, "end": found.end}

        def ranges_dict(
            found: Optional[Iterable[ByteRange]],
        ) -> Optional[List[Optional[Dict[str, int]]]]:
            if found is None:
                return None
            return [range_dict(item) for item in found]

        def header_text(value: Optional[bytes]) -> Optional[str]:
            if value is None:
                return None
            return value.decode("latin-1")

        return {
            "expected_length": self.expected_length,
            "total_length": self.total_length,
            "publishable": None if self.publishable is None else self.publishable.key,
            "versions": [
                {
                    "key": version.key,
                    "etag": header_text(version.etag),
                    "total_length": version.total_length,
                    "covered_length": version.covered_length,
                    "coverage": ranges_dict(version.coverage),
                    "missing": ranges_dict(version.missing),
                    "complete": version.complete,
                    "publishable": version.publishable,
                    "shard_indices": list(version.shard_indices),
                    "full_indices": list(version.full_indices),
                    "conflicts": [
                        {
                            "shard_indices": list(conflict.shard_indices),
                            "overlap": range_dict(conflict.overlap),
                            "differences": ranges_dict(conflict.differences),
                            "resolved_by_full": conflict.resolved_by_full,
                        }
                        for conflict in version.conflicts
                    ],
                }
                for version in self.versions
            ],
            "shards": [
                {
                    "index": shard.index,
                    "status_code": shard.status_code,
                    "range": range_dict(shard.byte_range),
                    "length": shard.length,
                    "attribution": shard.attribution,
                    "version_key": shard.version_key,
                    "etag": header_text(shard.etag),
                    "weak_etag": header_text(shard.weak_etag),
                    "last_modified": header_text(shard.last_modified),
                    "declared_total": shard.declared_total,
                    "accepted": shard.accepted,
                    "unattributed": shard.unattributed,
                    "proven_by": shard.proven_by,
                    "issues": [issue.code for issue in shard.issues],
                    "full_checks": [
                        {
                            "full_index": check.full_index,
                            "matches": check.matches,
                            "overlap": range_dict(check.overlap),
                            "differences": ranges_dict(check.differences),
                            "outside": check.outside,
                        }
                        for check in shard.full_checks
                    ],
                }
                for shard in self.shards
            ],
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "shard_index": issue.shard_index,
                }
                for issue in self.issues
            ],
        }


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------


@dataclass
class _Shard:
    index: int
    status_code: int
    byte_range: Optional[ByteRange]
    body: bytes
    etag: Optional[bytes]
    weak_etag: Optional[bytes]
    last_modified: Optional[bytes]
    declared_total: Optional[int]
    valid: bool
    attribution: str
    version_key: Optional[str]
    issues: List[CoverageIssue] = field(default_factory=list)


def _values(headers: Headers, name: bytes) -> List[bytes]:
    return [value for found_name, value in headers if found_name == name]


def _shard_issue(code: str, message: str, index: int) -> CoverageIssue:
    return CoverageIssue(code, message, index)


def _read_validators(
    headers: Headers, index: int, issues: List[CoverageIssue]
) -> Tuple[Optional[bytes], Optional[bytes], Optional[bytes]]:
    etags = _values(headers, b"etag")
    if len(etags) > 1:
        issues.append(_shard_issue("multiple_etags", "multiple ETag headers", index))
    etag: Optional[bytes] = None
    weak_etag: Optional[bytes] = None
    if etags:
        value = etags[0].strip()
        if value.startswith(b"W/"):
            weak_etag = value
        elif value:
            etag = value
        else:
            issues.append(_shard_issue("empty_etag", "empty ETag header", index))
    modified = _values(headers, b"last-modified")
    if len(modified) > 1:
        issues.append(
            _shard_issue(
                "multiple_last_modified", "multiple Last-Modified headers", index
            )
        )
    last_modified: Optional[bytes] = None
    if modified and modified[0].strip():
        last_modified = modified[0].strip()
    return etag, weak_etag, last_modified


def _attribution(
    status_code: int,
    etag: Optional[bytes],
    weak_etag: Optional[bytes],
    last_modified: Optional[bytes],
) -> str:
    if status_code == 200:
        return "full+strong-etag" if etag is not None else "full"
    if etag is not None:
        return "strong-etag"
    if weak_etag is not None:
        return "weak-etag-only"
    if last_modified is not None:
        return "last-modified-only"
    return "no-validator"


def _version_key(
    status_code: int, etag: Optional[bytes], index: int
) -> Optional[str]:
    if etag is not None:
        return "etag:" + etag.decode("latin-1")
    if status_code == 200:
        return "full:#{}".format(index)
    return None


def _content_length(
    headers: Headers, index: int, issues: List[CoverageIssue]
) -> Optional[int]:
    values = _values(headers, b"content-length")
    if not values:
        return None
    if len(values) > 1:
        issues.append(
            _shard_issue(
                "multiple_content_lengths", "multiple Content-Length headers", index
            )
        )
    try:
        return int(values[0])
    except ValueError:
        issues.append(
            _shard_issue("invalid_content_length", "invalid Content-Length", index)
        )
        return None


def _interpret(
    response: CapturedResponse, index: int, expected_length: Optional[int]
) -> _Shard:
    headers = response.header_list()
    issues: List[CoverageIssue] = []
    etag, weak_etag, last_modified = _read_validators(headers, index, issues)
    attribution = _attribution(
        response.status_code, etag, weak_etag, last_modified
    )
    key = _version_key(response.status_code, etag, index)
    body = response.body
    status = response.status_code

    def shard(
        byte_range: Optional[ByteRange],
        total: Optional[int],
        valid: bool,
        extra_issues: Optional[List[CoverageIssue]] = None,
    ) -> _Shard:
        return _Shard(
            index=index,
            status_code=status,
            byte_range=byte_range,
            body=body,
            etag=etag,
            weak_etag=weak_etag,
            last_modified=last_modified,
            declared_total=total,
            valid=valid,
            attribution=attribution,
            version_key=key,
            issues=list(issues) + (extra_issues or []),
        )

    if status == 200:
        declared = _content_length(headers, index, issues)
        if declared is not None and declared != len(body):
            return shard(
                None,
                declared,
                False,
                [
                    _shard_issue(
                        "content_length_mismatch",
                        "whole object has {} bytes but Content-Length says {}".format(
                            len(body), declared
                        ),
                        index,
                    )
                ],
            )
        if expected_length is not None and len(body) != expected_length:
            return shard(
                None,
                len(body),
                False,
                [
                    _shard_issue(
                        "total_length_mismatch",
                        "whole object has {} bytes but {} were expected".format(
                            len(body), expected_length
                        ),
                        index,
                    )
                ],
            )
        total = len(body) if declared is None else declared
        if total == 0:
            return shard(None, 0, True)
        return shard(ByteRange(0, len(body) - 1), total, True)

    if status == 206:
        content_types = _values(headers, b"content-type")
        if content_types and content_types[0].lower().startswith(
            b"multipart/byteranges"
        ):
            return shard(
                None,
                None,
                False,
                [
                    _shard_issue(
                        "multipart_byteranges_unsupported",
                        "a multipart/byteranges body cannot be attributed to "
                        "a single interval",
                        index,
                    )
                ],
            )
        values = _values(headers, b"content-range")
        if len(values) != 1:
            code = "missing_content_range" if not values else "multiple_content_ranges"
            return shard(
                None, None, False,
                [_shard_issue(code, "206 needs exactly one Content-Range", index)],
            )
        try:
            content_range = parse_content_range(values[0])
        except ValueError as exc:
            return shard(
                None, None, False,
                [_shard_issue("invalid_content_range", str(exc), index)],
            )
        declared_range = content_range.byte_range
        if len(body) != len(declared_range):
            return shard(
                declared_range,
                content_range.total,
                False,
                [
                    _shard_issue(
                        "range_body_length_mismatch",
                        "Content-Range {} needs {} bytes but body has {}".format(
                            declared_range, len(declared_range), len(body)
                        ),
                        index,
                    )
                ],
            )
        if (
            expected_length is not None
            and content_range.total is not None
            and content_range.total != expected_length
        ):
            return shard(
                declared_range,
                content_range.total,
                False,
                [
                    _shard_issue(
                        "total_length_mismatch",
                        "Content-Range total {} but {} was expected".format(
                            content_range.total, expected_length
                        ),
                        index,
                    )
                ],
            )
        effective_total = (
            content_range.total
            if content_range.total is not None
            else expected_length
        )
        if effective_total is not None and declared_range.end >= effective_total:
            return shard(
                declared_range,
                effective_total,
                False,
                [
                    _shard_issue(
                        "range_outside_total",
                        "Content-Range {} exceeds complete length {}".format(
                            declared_range, effective_total
                        ),
                        index,
                    )
                ],
            )
        return shard(declared_range, content_range.total, True)

    if status == 416:
        values = _values(headers, b"content-range")
        if len(values) != 1:
            return shard(
                None, None, False,
                [
                    _shard_issue(
                        "invalid_content_range",
                        "416 needs Content-Range: bytes */complete",
                        index,
                    )
                ],
            )
        try:
            stated_total = _parse_unsatisfied_total(values[0])
        except ValueError as exc:
            return shard(
                None, None, False,
                [_shard_issue("invalid_content_range", str(exc), index)],
            )
        unsatisfied_total = (
            stated_total if stated_total is not None else expected_length
        )
        if unsatisfied_total is None:
            return shard(
                None, None, False,
                [
                    _shard_issue(
                        "unknown_total", "416 does not state a complete length", index
                    )
                ],
            )
        if (
            expected_length is not None
            and unsatisfied_total != expected_length
        ):
            return shard(
                None,
                unsatisfied_total,
                False,
                [
                    _shard_issue(
                        "total_length_mismatch",
                        "416 total {} but {} was expected".format(
                            unsatisfied_total, expected_length
                        ),
                        index,
                    )
                ],
            )
        return shard(
            None,
            unsatisfied_total,
            True,
            [_shard_issue("range_not_satisfiable", "server rejected the range", index)],
        )

    return shard(
        None,
        None,
        False,
        [
            _shard_issue(
                "unsupported_status",
                "a {} response cannot contribute bytes".format(status),
                index,
            )
        ],
    )


def _merge_ranges(ranges: Iterable[ByteRange]) -> Tuple[ByteRange, ...]:
    merged: List[ByteRange] = []
    for found in sorted(ranges):
        if merged and found.start <= merged[-1].end + 1:
            previous = merged[-1]
            merged[-1] = ByteRange(previous.start, max(previous.end, found.end))
        else:
            merged.append(found)
    return tuple(merged)


def _missing_ranges(
    coverage: Sequence[ByteRange], total: Optional[int]
) -> Optional[Tuple[ByteRange, ...]]:
    if total is None:
        return None
    missing: List[ByteRange] = []
    cursor = 0
    for found in coverage:
        if found.start > cursor:
            missing.append(ByteRange(cursor, found.start - 1))
        cursor = max(cursor, found.end + 1)
    if cursor < total:
        missing.append(ByteRange(cursor, total - 1))
    return tuple(missing)


def _differing_ranges(
    first: bytes, second: bytes, base: int
) -> Tuple[ByteRange, ...]:
    differences: List[ByteRange] = []
    start: Optional[int] = None
    for offset, (left, right) in enumerate(zip(first, second)):
        if left != right:
            if start is None:
                start = offset
        elif start is not None:
            differences.append(ByteRange(base + start, base + offset - 1))
            start = None
    if start is not None:
        differences.append(ByteRange(base + start, base + len(first) - 1))
    return tuple(differences)


def _check_whole_object(shard: _Shard, whole: _Shard) -> FullCheck:
    whole_range = whole.byte_range
    shard_range = shard.byte_range
    if whole_range is None:
        return FullCheck(
            full_index=whole.index,
            matches=shard_range is None and shard.body == whole.body,
            overlap=None,
            differences=(),
            outside=shard_range is not None,
        )
    if shard_range is None:
        return FullCheck(
            full_index=whole.index,
            matches=False,
            overlap=None,
            differences=(),
            outside=True,
        )
    overlap = shard_range.intersection(whole_range)
    outside = not whole_range.contains(shard_range)
    differences: Tuple[ByteRange, ...] = ()
    if overlap is not None:
        shard_offset = overlap.start - shard_range.start
        whole_offset = overlap.start - whole_range.start
        differences = _differing_ranges(
            shard.body[shard_offset : shard_offset + len(overlap)],
            whole.body[whole_offset : whole_offset + len(overlap)],
            overlap.start,
        )
    return FullCheck(
        full_index=whole.index,
        matches=not outside and not differences and overlap is not None,
        overlap=overlap,
        differences=differences,
        outside=outside,
    )


def _unique_total(
    shards: Iterable[_Shard], expected_length: Optional[int]
) -> Optional[int]:
    # Different versions of one object may legitimately have different
    # lengths, so a disagreement here is not by itself an error.
    if expected_length is not None:
        return expected_length
    totals = {
        shard.declared_total for shard in shards if shard.declared_total is not None
    }
    if len(totals) == 1:
        return totals.pop()
    return None


def _overlaps(
    left: _Shard, right: _Shard
) -> Optional[Tuple[ByteRange, Tuple[ByteRange, ...]]]:
    if left.byte_range is None or right.byte_range is None:
        return None
    overlap = left.byte_range.intersection(right.byte_range)
    if overlap is None:
        return None
    left_offset = overlap.start - left.byte_range.start
    right_offset = overlap.start - right.byte_range.start
    differences = _differing_ranges(
        left.body[left_offset : left_offset + len(overlap)],
        right.body[right_offset : right_offset + len(overlap)],
        overlap.start,
    )
    return overlap, differences


def _version_conflicts(
    key: str, group: Sequence[_Shard], ground_truth: Optional[_Shard]
) -> Tuple[Conflict, ...]:
    found: List[Conflict] = []
    for position, left in enumerate(group):
        for right in group[position + 1 :]:
            overlap, differences = (
                _overlaps(left, right) or (None, ())
            )
            if overlap is None or not differences:
                continue
            found.append(
                Conflict(
                    version_key=key,
                    shard_indices=(left.index, right.index),
                    overlap=overlap,
                    differences=differences,
                    resolved_by_full=(
                        None if ground_truth is None else ground_truth.index
                    ),
                )
            )
    return tuple(found)


def analyze(
    responses: Iterable[CapturedResponse],
    expected_length: Optional[int] = None,
) -> CoverageReport:
    """Build the coverage table for one archived object.

    Responses are tied to a version by strong ``ETag`` equality, or by being
    byte-for-byte confirmed against a complete (200) snapshot. Weak
    ``ETag``/``Last-Modified`` values are reported, but equality of a weak
    validator never proves that two ranges may be joined; shards whose only
    evidence is weak stay unattributed and are not merged. A 200 response is
    a complete object in its own right and also serves as ground truth: it
    can show that an older shard really corresponds to it, and a disagreement
    is reported at the exact differing intervals.

    ``expected_length`` pins the complete object length. Without it the total
    comes from ``Content-Range`` or from a whole body; contradictory totals
    leave the version incomplete rather than picking one arbitrarily.
    """

    shards = [
        _interpret(response, index, expected_length)
        for index, response in enumerate(responses)
    ]
    issues: List[CoverageIssue] = []
    valid_shards = [shard for shard in shards if shard.valid]
    total_length = _unique_total(valid_shards, expected_length)

    whole_shards = [shard for shard in valid_shards if shard.status_code == 200]
    consistent_wholes = [
        whole
        for whole in whole_shards
        if all(
            other.body == whole.body
            for other in whole_shards
            if other.index != whole.index and other.etag == whole.etag
        )
    ]
    checks: Dict[int, List[FullCheck]] = {shard.index: [] for shard in shards}
    for shard in valid_shards:
        for whole in consistent_wholes:
            if whole.index != shard.index:
                checks[shard.index].append(_check_whole_object(shard, whole))

    groups: Dict[str, List[_Shard]] = {}
    for shard in valid_shards:
        if shard.version_key is not None:
            groups.setdefault(shard.version_key, []).append(shard)

    extra_issues: Dict[int, List[CoverageIssue]] = {
        shard.index: [] for shard in shards
    }
    accepted: Dict[int, bool] = {shard.index: False for shard in shards}
    proven_by: Dict[int, Optional[str]] = {shard.index: None for shard in shards}

    # Whole snapshots are always complete and accepted. Range shards that
    # carry no strong validator but match a snapshot byte-for-byte are
    # validated by that snapshot and join its version.
    snapshot_keys: Dict[int, str] = {}
    for shard in valid_shards:
        if shard.status_code != 200:
            continue
        accepted[shard.index] = True
        proven_by[shard.index] = (
            "strong-etag" if shard.etag is not None else "whole-object"
        )
        key = shard.version_key
        if key is None:
            key = "full:#{}".format(shard.index)
            groups.setdefault(key, []).append(shard)
        snapshot_keys[shard.index] = key

    for shard in valid_shards:
        if shard.status_code == 200 or shard.version_key is not None:
            continue
        for outcome in checks[shard.index]:
            if not outcome.matches:
                continue
            key = snapshot_keys[outcome.full_index]
            shard.version_key = key
            shard.attribution = "whole-object-match"
            groups.setdefault(key, []).append(shard)
            accepted[shard.index] = True
            proven_by[shard.index] = "whole-object"
            break

    # Two complete snapshots that claim the same strong validator but carry
    # different bytes cannot both be right. Neither one is trustworthy as
    # ground truth, so both are kept for the audit but rejected.
    for key, group in groups.items():
        snapshot_group = [
            shard for shard in group if shard.status_code == 200
        ]
        if len(snapshot_group) > 1 and any(
            full.body != snapshot_group[0].body for full in snapshot_group[1:]
        ):
            for shard in snapshot_group:
                accepted[shard.index] = False
                proven_by[shard.index] = None
                extra_issues[shard.index].append(
                    _shard_issue(
                        "conflicting_whole_objects",
                        "complete responses sharing {} have different bytes".format(
                            key
                        ),
                        shard.index,
                    )
                )

    versions: List[VersionCoverage] = []
    conflicts: List[Conflict] = []
    for key, group in groups.items():
        group = sorted(group, key=lambda shard: shard.index)
        full_group = [shard for shard in group if shard.status_code == 200]
        ground_truth: Optional[_Shard] = None
        snapshots_agree = True
        version_total: Optional[int]
        if full_group:
            snapshots_agree = all(
                full.body == full_group[0].body for full in full_group[1:]
            )
            if snapshots_agree:
                ground_truth = full_group[0]
        # A complete snapshot is authoritative about its own length. A range
        # that claims a different total for the same version is stale; keep
        # the snapshot's length and let the mismatch surface as an issue.
        if ground_truth is not None and expected_length is None:
            version_total = len(ground_truth.body)
            for shard in group:
                if (
                    shard.status_code == 206
                    and shard.declared_total is not None
                    and shard.declared_total != version_total
                ):
                    extra_issues[shard.index].append(
                        _shard_issue(
                            "total_length_mismatch",
                            "range total {} disagrees with whole-object length "
                            "{}".format(shard.declared_total, version_total),
                            shard.index,
                        )
                    )
        else:
            version_total = _unique_total(group, expected_length)

        for shard in group:
            if shard.status_code == 200:
                continue
            if proven_by[shard.index] == "whole-object":
                continue
            if ground_truth is None:
                if not snapshots_agree:
                    accepted[shard.index] = False
                    proven_by[shard.index] = None
                    continue
                accepted[shard.index] = True
                proven_by[shard.index] = (
                    "strong-etag" if shard.etag is not None else None
                )
                continue
            outcome = _check_whole_object(shard, ground_truth)
            if outcome.matches:
                accepted[shard.index] = True
                if shard.etag is not None:
                    # Its own strong validator remains the primary proof;
                    # the snapshot is corroboration.
                    proven_by[shard.index] = "strong-etag"
                else:
                    proven_by[shard.index] = "whole-object"
            else:
                accepted[shard.index] = False
                proven_by[shard.index] = None
                extra_issues[shard.index].append(
                    _shard_issue(
                        "range_outside_full"
                        if outcome.outside
                        else "bytes_differ_from_full",
                        "stored range disagrees with whole-object response #{}".format(
                            ground_truth.index
                        ),
                        shard.index,
                    )
                )

        version_conflicts = _version_conflicts(key, group, ground_truth)
        conflicts.extend(version_conflicts)
        coverage = _merge_ranges(
            shard.byte_range
            for shard in group
            if accepted[shard.index] and shard.byte_range is not None
        )
        missing = _missing_ranges(coverage, version_total)
        complete = (
            snapshots_agree and version_total is not None and not missing
        )
        publishable = complete and not any(
            conflict.blocking for conflict in version_conflicts
        )
        versions.append(
            VersionCoverage(
                key=key,
                etag=group[0].etag,
                total_length=version_total,
                coverage=coverage,
                missing=missing,
                conflicts=version_conflicts,
                shard_indices=tuple(shard.index for shard in group),
                full_indices=tuple(shard.index for shard in full_group),
                complete=complete,
                publishable=publishable,
                proven=snapshots_agree
                and any(
                    proven_by[shard.index] is not None for shard in group
                ),
            )
        )

    # Weak-validator-only ranges are grouped for reporting. They are never
    # merged into a publishable version, but their gaps and disagreements are
    # still surfaced so a capture does not look healthier than it is.
    weak_groups: Dict[Tuple[str, bytes], List[_Shard]] = {}
    for shard in valid_shards:
        if shard.status_code != 206 or proven_by[shard.index] is not None:
            continue
        if shard.version_key is not None:
            continue
        if shard.weak_etag is not None:
            weak_key = ("weak-etag", shard.weak_etag)
        elif shard.last_modified is not None:
            weak_key = ("last-modified", shard.last_modified)
        else:
            continue
        weak_groups.setdefault(weak_key, []).append(shard)

    for (kind, value), group in weak_groups.items():
        group = sorted(group, key=lambda shard: shard.index)
        label = "candidate:{}:{}".format(kind, value.decode("latin-1"))
        weak_total: Optional[int] = _unique_total(group, expected_length)
        weak_conflicts = _version_conflicts(label, group, None)
        conflicts.extend(weak_conflicts)
        coverage = _merge_ranges(
            shard.byte_range for shard in group if shard.byte_range is not None
        )
        missing = _missing_ranges(coverage, weak_total)
        versions.append(
            VersionCoverage(
                key=label,
                etag=None,
                total_length=weak_total,
                coverage=coverage,
                missing=missing,
                conflicts=weak_conflicts,
                shard_indices=tuple(shard.index for shard in group),
                full_indices=(),
                complete=False,
                publishable=False,
                proven=False,
            )
        )

    # Ranges with no strong validator at all are collected into one
    # informational group per capture, so their intervals and disagreements
    # are still visible even though nothing proves they belong together.
    ungrouped = [
        shard
        for shard in valid_shards
        if shard.status_code == 206
        and shard.version_key is None
        and proven_by[shard.index] is None
        and shard.weak_etag is None
        and shard.last_modified is None
    ]
    if ungrouped:
        label = "candidate:no-validator"
        ungrouped = sorted(ungrouped, key=lambda shard: shard.index)
        ungrouped_total = _unique_total(ungrouped, expected_length)
        ungrouped_conflicts = _version_conflicts(label, ungrouped, None)
        conflicts.extend(ungrouped_conflicts)
        ungrouped_coverage = _merge_ranges(
            shard.byte_range
            for shard in ungrouped
            if shard.byte_range is not None
        )
        versions.append(
            VersionCoverage(
                key=label,
                etag=None,
                total_length=ungrouped_total,
                coverage=ungrouped_coverage,
                missing=_missing_ranges(ungrouped_coverage, ungrouped_total),
                conflicts=ungrouped_conflicts,
                shard_indices=tuple(shard.index for shard in ungrouped),
                full_indices=(),
                complete=False,
                publishable=False,
                proven=False,
            )
        )

    for shard in shards:
        issues.extend(shard.issues + extra_issues[shard.index])

    # Several versions can be complete at once (an old range that was fully
    # covered and the retry's whole object). The whole object is the latest
    # authoritative snapshot, so prefer a publishable version backed by one,
    # choosing the most recently captured snapshot when there are several.
    publishable_versions = [
        version for version in versions if version.publishable
    ]
    with_snapshot = [
        version for version in publishable_versions if version.full_indices
    ]
    if with_snapshot:
        published = max(
            with_snapshot, key=lambda version: max(version.full_indices)
        )
    elif publishable_versions:
        published = publishable_versions[0]
    else:
        published = None
    return CoverageReport(
        shards=tuple(
            CoverageShard(
                index=shard.index,
                status_code=shard.status_code,
                byte_range=shard.byte_range,
                body=shard.body,
                attribution=shard.attribution,
                version_key=shard.version_key,
                etag=shard.etag,
                weak_etag=shard.weak_etag,
                last_modified=shard.last_modified,
                declared_total=shard.declared_total,
                accepted=accepted[shard.index],
                proven_by=proven_by[shard.index],
                issues=tuple(shard.issues + extra_issues[shard.index]),
                full_checks=tuple(checks[shard.index]),
            )
            for shard in shards
        ),
        versions=tuple(versions),
        conflicts=tuple(conflicts),
        issues=tuple(issues),
        expected_length=expected_length,
        total_length=total_length,
        publishable=published,
    )


def analyze_capture(
    data: bytes, expected_length: Optional[int] = None
) -> CoverageReport:
    """Parse a raw response capture and build its coverage table."""

    return analyze(parse_response_capture(data), expected_length=expected_length)
