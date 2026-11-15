# Assembling a publishable record manifest from archive pages received
# over h11.
#
# The archive interface serves a large result set as a sequence of
# "pages", one per HTTP response, and pages may be fetched from
# different replicas. A page only reveals its delivery metadata at the
# very end of the response, as trailers attached to EndOfMessage:
#
#   archive-batch: <token>        batch this delivery belongs to
#   archive-range: <start>-<end>  half-open range this page covers
#
# and the response status line headers may carry:
#
#   archive-replica: <token>      identity of the serving replica
#
# The body is newline-delimited JSON, one record per line:
#
#   {"key": "some-key", "value": <any JSON>, "version": <int, optional>}
#
# A manifest is only acknowledged as complete coverage when the batch
# numbers agree, the declared ranges chain up without holes, and every
# admitted page reached EndOfMessage. A truncated response cannot
# testify for missing pages, while a confirmed empty page closes the
# gap it declares. When overlapping pages disagree on a key and the
# versions cannot decide the winner, only that key is quarantined.

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ._events import (
    ConnectionClosed,
    Data,
    EndOfMessage,
    Event,
    InformationalResponse,
    Response,
)
from ._headers import Headers

__all__ = [
    "ArchiveFormatError",
    "ArchiveRecord",
    "ArchivePage",
    "ArchiveManifest",
    "PageCollector",
    "ManifestBuilder",
]

BATCH_TRAILER = b"archive-batch"
RANGE_TRAILER = b"archive-range"
REPLICA_HEADER = b"archive-replica"

_range_re = re.compile(rb"([0-9]+)-([0-9]+)")


class ArchiveFormatError(ValueError):
    """An archive page was malformed: bad trailers or a bad body."""


@dataclass(frozen=True)
class ArchiveRecord:
    """A single record inside an archive page.

    ``key`` is the stable key identifying the record across pages and
    replicas. ``version`` is optional; when absent the record cannot be
    ordered against conflicting values for the same key.
    """

    key: str
    value: Any
    version: Optional[int] = None


@dataclass(frozen=True)
class ArchivePage:
    """One collected response from the archive interface.

    ``complete`` is True only if the response reached EndOfMessage.
    Only complete pages carry a ``batch`` and a declared ``start``/``end``
    range, because those arrive as trailers; an incomplete page cannot
    testify for anything.
    """

    complete: bool
    batch: Optional[str]
    start: Optional[int]
    end: Optional[int]
    records: Tuple[ArchiveRecord, ...]
    replica: Optional[str] = None


@dataclass(frozen=True)
class ArchiveManifest:
    """The publishable result of assembling a set of archive pages.

    ``complete`` is True only when every confirmed page agrees on the
    batch, no confirmed page was rejected, and the declared ranges chain
    into a single contiguous interval with no gaps. ``quarantined``
    holds the stable keys whose values conflicted across overlapping
    pages without a comparable version; those keys are withheld from
    ``records`` while every uncontested key is still published.
    """

    batch: Optional[str]
    records: Tuple[ArchiveRecord, ...]
    quarantined: Tuple[str, ...]
    coverage: Tuple[Tuple[int, int], ...]
    gaps: Tuple[Tuple[int, int], ...]
    complete: bool
    rejected: Tuple[ArchivePage, ...]
    pending: Tuple[ArchivePage, ...]


def _single_field(headers: Headers, name: bytes) -> Optional[bytes]:
    values = [value for field_name, value in headers if field_name == name]
    if len(values) > 1:
        raise ArchiveFormatError("duplicate {} field".format(name.decode("ascii")))
    if values:
        return values[0]
    return None


def _parse_range(raw: bytes) -> Tuple[int, int]:
    match = _range_re.fullmatch(raw)
    if match is None:
        raise ArchiveFormatError(
            "malformed {} field: {!r}".format(RANGE_TRAILER.decode("ascii"), raw)
        )
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise ArchiveFormatError("declared range {}-{} is inverted".format(start, end))
    return start, end


def _parse_records(body: bytes) -> Tuple[ArchiveRecord, ...]:
    records = []
    for raw_line in body.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArchiveFormatError(
                "malformed record line: {!r}".format(line)
            ) from exc
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ArchiveFormatError(
                "record line lacks a string 'key': {!r}".format(line)
            )
        if "value" not in item:
            raise ArchiveFormatError("record line lacks a 'value': {!r}".format(line))
        version = item.get("version")
        if version is not None and (
            not isinstance(version, int) or isinstance(version, bool)
        ):
            raise ArchiveFormatError(
                "record 'version' must be an integer: {!r}".format(line)
            )
        records.append(
            ArchiveRecord(key=item["key"], value=item["value"], version=version)
        )
    return tuple(records)


class PageCollector:
    """Collects the h11 events of one archive response into an ArchivePage.

    Feed every event emitted by :meth:`Connection.next_event` for a
    single response, then call :meth:`finish`. The page only becomes
    admissible evidence once EndOfMessage arrives, because the batch and
    the declared range travel as trailers.
    """

    def __init__(self) -> None:
        self._body = bytearray()
        self._replica: Optional[str] = None
        self._batch: Optional[str] = None
        self._range: Optional[Tuple[int, int]] = None
        self._saw_end_of_message = False

    def feed(self, event: Event) -> None:
        if isinstance(event, InformationalResponse):
            return
        if self._saw_end_of_message and not isinstance(event, ConnectionClosed):
            raise ArchiveFormatError(
                "unexpected event after EndOfMessage: {!r}".format(event)
            )
        if isinstance(event, Response):
            replica = _single_field(event.headers, REPLICA_HEADER)
            self._replica = (
                replica.decode("ascii", "replace") if replica is not None else None
            )
        elif isinstance(event, Data):
            self._body += event.data
        elif isinstance(event, EndOfMessage):
            self._saw_end_of_message = True
            batch = _single_field(event.headers, BATCH_TRAILER)
            if batch is None:
                raise ArchiveFormatError(
                    "completed page is missing the {} trailer".format(
                        BATCH_TRAILER.decode("ascii")
                    )
                )
            self._batch = batch.decode("ascii", "replace")
            raw_range = _single_field(event.headers, RANGE_TRAILER)
            if raw_range is None:
                raise ArchiveFormatError(
                    "completed page is missing the {} trailer".format(
                        RANGE_TRAILER.decode("ascii")
                    )
                )
            self._range = _parse_range(raw_range)
        elif isinstance(event, ConnectionClosed):
            pass
        else:
            raise TypeError("cannot collect {!r} into an archive page".format(event))

    def feed_all(self, events: Iterable[Event]) -> None:
        for event in events:
            self.feed(event)

    @property
    def complete(self) -> bool:
        return self._saw_end_of_message

    def finish(self) -> ArchivePage:
        if not self._saw_end_of_message:
            # A truncated response carries no trailers, hence no batch
            # and no declared range: it cannot testify for anything.
            return ArchivePage(
                complete=False,
                batch=None,
                start=None,
                end=None,
                records=(),
                replica=self._replica,
            )
        assert self._batch is not None and self._range is not None
        return ArchivePage(
            complete=True,
            batch=self._batch,
            start=self._range[0],
            end=self._range[1],
            records=_parse_records(bytes(self._body)),
            replica=self._replica,
        )


def _sweep(
    spans: Iterable[Tuple[int, int]],
) -> Tuple[Tuple[Tuple[int, int], ...], Tuple[Tuple[int, int], ...]]:
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if start == end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    gaps = []
    for previous, following in zip(merged, merged[1:]):
        gaps.append((previous[1], following[0]))
    return tuple(merged), tuple(gaps)


def _same_value(left: Any, right: Any) -> bool:
    return bool(left == right)


def _merge_records(
    pages: List[ArchivePage],
) -> Tuple[Tuple[ArchiveRecord, ...], Tuple[str, ...]]:
    observations: Dict[str, List[ArchiveRecord]] = {}
    for page in sorted(pages, key=lambda p: (p.start, p.end)):
        for record in page.records:
            observations.setdefault(record.key, []).append(record)
    records = []
    quarantined = []
    for key in sorted(observations):
        observed = observations[key]
        distinct: List[Any] = []
        for record in observed:
            if not any(_same_value(record.value, seen) for seen in distinct):
                distinct.append(record.value)
        if len(distinct) == 1:
            versions = [r.version for r in observed if r.version is not None]
            records.append(
                ArchiveRecord(
                    key=key,
                    value=distinct[0],
                    version=max(versions) if versions else None,
                )
            )
            continue
        # Overlapping pages disagree on this key. Only a comparable
        # version on every observation can pick a winner; otherwise the
        # key -- and only this key -- is quarantined.
        if any(record.version is None for record in observed):
            quarantined.append(key)
            continue
        best = max(
            record.version for record in observed if record.version is not None
        )
        winners: List[Any] = []
        for record in observed:
            if record.version == best and not any(
                _same_value(record.value, seen) for seen in winners
            ):
                winners.append(record.value)
        if len(winners) == 1:
            records.append(ArchiveRecord(key=key, value=winners[0], version=best))
        else:
            quarantined.append(key)
    return tuple(records), tuple(quarantined)


class ManifestBuilder:
    """Assembles archive pages into a publishable ArchiveManifest."""

    def __init__(self) -> None:
        self._pages: List[ArchivePage] = []

    def add_page(self, page: ArchivePage) -> None:
        if page.complete:
            if page.batch is None or page.start is None or page.end is None:
                raise ArchiveFormatError(
                    "a complete page must carry a batch and a declared range"
                )
            if page.start > page.end:
                raise ArchiveFormatError(
                    "declared range {}-{} is inverted".format(page.start, page.end)
                )
        self._pages.append(page)

    def add_events(self, events: Iterable[Event]) -> ArchivePage:
        collector = PageCollector()
        collector.feed_all(events)
        page = collector.finish()
        self.add_page(page)
        return page

    def build(self) -> ArchiveManifest:
        confirmed = [page for page in self._pages if page.complete]
        pending = tuple(page for page in self._pages if not page.complete)
        if not confirmed:
            return ArchiveManifest(
                batch=None,
                records=(),
                quarantined=(),
                coverage=(),
                gaps=(),
                complete=False,
                rejected=(),
                pending=pending,
            )
        batch = confirmed[0].batch
        admitted = [page for page in confirmed if page.batch == batch]
        rejected = tuple(page for page in confirmed if page.batch != batch)
        spans = []
        for page in admitted:
            assert page.start is not None and page.end is not None
            spans.append((page.start, page.end))
        coverage, gaps = _sweep(spans)
        records, quarantined = _merge_records(admitted)
        complete = bool(coverage) and not gaps and not rejected
        return ArchiveManifest(
            batch=batch,
            records=records,
            quarantined=quarantined,
            coverage=coverage,
            gaps=gaps,
            complete=complete,
            rejected=rejected,
            pending=pending,
        )
