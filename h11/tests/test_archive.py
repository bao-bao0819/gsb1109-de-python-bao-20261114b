import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from .._archive import (
    ArchiveFormatError,
    ArchivePage,
    ArchiveRecord,
    ManifestBuilder,
    PageCollector,
)
from .._connection import Connection
from .._state import CLIENT, SERVER
from .._events import (
    ConnectionClosed,
    Data,
    EndOfMessage,
    Event,
    Request,
    Response,
)
from .helpers import get_all_events


def make_page_events(
    batch: str,
    start: int,
    end: int,
    records: List[Dict[str, Any]],
    replica: Optional[str] = None,
) -> List[Event]:
    body = b"".join(
        json.dumps(record).encode("utf-8") + b"\n" for record in records
    )
    headers = [("Content-Length", str(len(body)))]
    if replica is not None:
        headers.append(("Archive-Replica", replica))
    events: List[Event] = [Response(status_code=200, headers=headers)]
    if body:
        events.append(Data(data=body))
    events.append(
        EndOfMessage(
            headers=[
                ("Archive-Batch", batch),
                ("Archive-Range", "{}-{}".format(start, end)),
            ]
        )
    )
    return events


def collect(events: List[Event]) -> ArchivePage:
    collector = PageCollector()
    collector.feed_all(events)
    return collector.finish()


def build(pages: List[ArchivePage]) -> Any:
    builder = ManifestBuilder()
    for page in pages:
        builder.add_page(page)
    return builder.build()


def test_single_page_complete() -> None:
    page = collect(
        make_page_events(
            "b7",
            0,
            2,
            [
                {"key": "a", "value": 1, "version": 3},
                {"key": "b", "value": "x"},
            ],
            replica="r1",
        )
    )
    assert page.complete
    assert page.batch == "b7"
    assert (page.start, page.end) == (0, 2)
    assert page.replica == "r1"
    manifest = build([page])
    assert manifest.complete
    assert manifest.batch == "b7"
    assert manifest.coverage == ((0, 2),)
    assert manifest.gaps == ()
    assert manifest.records == (
        ArchiveRecord("a", 1, 3),
        ArchiveRecord("b", "x", None),
    )
    assert manifest.quarantined == ()


def test_chained_pages_acknowledge_full_coverage() -> None:
    first = collect(make_page_events("b7", 0, 2, [{"key": "a", "value": 1}]))
    second = collect(make_page_events("b7", 2, 4, [{"key": "b", "value": 2}]))
    manifest = build([first, second])
    assert manifest.complete
    assert manifest.coverage == ((0, 4),)
    assert [record.key for record in manifest.records] == ["a", "b"]


def test_unchained_pages_leave_a_gap() -> None:
    first = collect(make_page_events("b7", 0, 2, [{"key": "a", "value": 1}]))
    second = collect(make_page_events("b7", 5, 7, [{"key": "b", "value": 2}]))
    manifest = build([first, second])
    assert not manifest.complete
    assert manifest.gaps == ((2, 5),)
    # The records that were confirmed are still publishable.
    assert [record.key for record in manifest.records] == ["a", "b"]


def test_confirmed_empty_page_closes_the_gap() -> None:
    first = collect(make_page_events("b7", 0, 2, [{"key": "a", "value": 1}]))
    empty = collect(make_page_events("b7", 2, 5, []))
    last = collect(make_page_events("b7", 5, 7, [{"key": "b", "value": 2}]))
    manifest = build([first, empty, last])
    assert manifest.complete
    assert manifest.gaps == ()
    assert manifest.coverage == ((0, 7),)


def test_truncated_response_cannot_testify() -> None:
    first = collect(make_page_events("b7", 0, 2, [{"key": "a", "value": 1}]))
    last = collect(make_page_events("b7", 5, 7, [{"key": "c", "value": 3}]))
    # This response is cut off before EndOfMessage, so its trailers --
    # and therefore its declared range -- never arrive. It cannot close
    # the (2, 5) hole, and its partial body is withheld.
    truncated = collect(
        [
            Response(status_code=200, headers=[("Transfer-Encoding", "chunked")]),
            Data(data=json.dumps({"key": "b", "value": 2}).encode("utf-8")),
            ConnectionClosed(),
        ]
    )
    assert not truncated.complete
    assert truncated.records == ()
    manifest = build([first, truncated, last])
    assert not manifest.complete
    assert manifest.pending == (truncated,)
    assert manifest.coverage == ((0, 2), (5, 7))
    assert manifest.gaps == ((2, 5),)
    assert [record.key for record in manifest.records] == ["a", "c"]


def test_mismatched_batch_is_rejected() -> None:
    first = collect(make_page_events("b7", 0, 2, [{"key": "a", "value": 1}]))
    # Same delivery, but a stale replica answers from an older batch.
    stale = collect(make_page_events("b6", 2, 4, [{"key": "b", "value": 2}]))
    manifest = build([first, stale])
    assert not manifest.complete
    assert manifest.batch == "b7"
    assert manifest.rejected == (stale,)
    assert [record.key for record in manifest.records] == ["a"]


def test_overlapping_replicas_agreeing_merge_cleanly() -> None:
    first = collect(
        make_page_events(
            "b7",
            0,
            3,
            [
                {"key": "a", "value": 1, "version": 1},
                {"key": "b", "value": 2, "version": 1},
            ],
            replica="r1",
        )
    )
    second = collect(
        make_page_events(
            "b7",
            2,
            5,
            [
                {"key": "b", "value": 2, "version": 1},
                {"key": "c", "value": 3, "version": 1},
            ],
            replica="r2",
        )
    )
    manifest = build([first, second])
    assert manifest.complete
    assert manifest.coverage == ((0, 5),)
    assert [record.key for record in manifest.records] == ["a", "b", "c"]
    assert manifest.quarantined == ()


def test_comparable_versions_pick_the_newer_value() -> None:
    old = collect(
        make_page_events("b7", 0, 3, [{"key": "a", "value": "old", "version": 1}])
    )
    new = collect(
        make_page_events("b7", 1, 4, [{"key": "a", "value": "new", "version": 2}])
    )
    manifest = build([old, new])
    assert manifest.complete
    assert manifest.records == (ArchiveRecord("a", "new", 2),)
    assert manifest.quarantined == ()


def test_incomparable_conflict_quarantines_only_that_key() -> None:
    first = collect(
        make_page_events(
            "b7",
            0,
            3,
            [
                {"key": "a", "value": "old"},
                {"key": "b", "value": 2},
            ],
            replica="r1",
        )
    )
    second = collect(
        make_page_events(
            "b7",
            1,
            4,
            [
                {"key": "a", "value": "new"},
                {"key": "b", "value": 2},
            ],
            replica="r2",
        )
    )
    manifest = build([first, second])
    assert manifest.complete
    # "a" has two live values and no version to compare: only it is
    # quarantined, while the uncontested "b" is still published.
    assert manifest.quarantined == ("a",)
    assert manifest.records == (ArchiveRecord("b", 2, None),)


def test_tied_versions_still_quarantine() -> None:
    first = collect(
        make_page_events("b7", 0, 2, [{"key": "a", "value": 1, "version": 5}])
    )
    second = collect(
        make_page_events("b7", 0, 2, [{"key": "a", "value": 2, "version": 5}])
    )
    manifest = build([first, second])
    assert manifest.records == ()
    assert manifest.quarantined == ("a",)


def test_missing_trailers_are_a_format_error() -> None:
    collector = PageCollector()
    with pytest.raises(ArchiveFormatError):
        collector.feed_all(
            [
                Response(status_code=200, headers=[("Content-Length", "0")]),
                EndOfMessage(),
            ]
        )


def test_malformed_record_is_a_format_error() -> None:
    collector = PageCollector()
    collector.feed(Response(status_code=200, headers=[("Content-Length", "5")]))
    collector.feed(Data(data=b"not json"))
    collector.feed(
        EndOfMessage(headers=[("Archive-Batch", "b7"), ("Archive-Range", "0-1")])
    )
    with pytest.raises(ArchiveFormatError):
        collector.finish()


def test_inverted_range_is_a_format_error() -> None:
    with pytest.raises(ArchiveFormatError):
        collect(make_page_events("b7", 4, 2, []))


def test_wire_roundtrip() -> None:
    server = Connection(SERVER)
    client = Connection(CLIENT)
    request = client.send(
        Request(
            method="GET", target="/archive", headers=[("Host", "example.org")]
        )
    )
    assert request is not None
    server.receive_data(request)
    get_all_events(server)
    body = b'{"key": "a", "value": 1, "version": 1}\n'
    outgoing = b""
    for event in [
        Response(status_code=200, headers=[("Transfer-Encoding", "chunked")]),
        Data(data=body),
        EndOfMessage(headers=[("Archive-Batch", "b7"), ("Archive-Range", "0-1")]),
    ]:
        data = server.send(event)
        assert data is not None
        outgoing += data
    client.receive_data(outgoing)
    builder = ManifestBuilder()
    page = builder.add_events(get_all_events(client))
    assert page.complete
    manifest = builder.build()
    assert manifest.complete
    assert manifest.records == (ArchiveRecord("a", 1, 1),)
