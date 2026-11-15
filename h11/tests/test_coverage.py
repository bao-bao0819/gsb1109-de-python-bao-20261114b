from pathlib import Path
from typing import Iterable, List, Tuple

import pytest

from ..coverage import (
    analyze,
    analyze_capture,
    ByteRange,
    CaptureParseError,
    CapturedResponse,
    CoverageReport,
    IncompleteCoverageError,
    parse_content_range,
    parse_response_capture,
)

BODY = b"HELLOWORLD"


def response(
    status_code: int,
    headers: Iterable[Tuple[str, str]] = (),
    body: bytes = b"",
) -> CapturedResponse:
    return CapturedResponse(
        status_code=status_code,
        headers=[(name, value) for name, value in headers],
        body=body,
    )


def issues(report: CoverageReport) -> List[str]:
    return [issue.code for issue in report.issues]


def test_parse_content_range() -> None:
    parsed = parse_content_range(b"bytes 0-4/10")
    assert parsed.byte_range == ByteRange(0, 4)
    assert parsed.total == 10

    assert parse_content_range("bytes 2-2/*").byte_range == ByteRange(2, 2)
    assert parse_content_range("bytes 2-2/*").total is None

    for bad in [b"bytes 5-4/10", b"bytes 0-10/10", b"bytes 0-1", b"items 0-1/2"]:
        with pytest.raises(ValueError):
            parse_content_range(bad)


def test_byte_range_helpers() -> None:
    assert ByteRange(0, 4).intersection(ByteRange(3, 9)) == ByteRange(3, 4)
    assert ByteRange(0, 4).intersection(ByteRange(5, 9)) is None
    assert ByteRange(0, 4).contains(ByteRange(1, 3))
    assert not ByteRange(0, 4).contains(ByteRange(1, 5))
    assert len(ByteRange(2, 4)) == 3
    with pytest.raises(ValueError):
        ByteRange(3, 2)


def test_strong_etag_ranges_merge_into_a_publishable_version() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"v1"')],
                BODY[:5],
            ),
            response(
                206,
                [("Content-Range", "bytes 5-9/10"), ("ETag", '"v1"')],
                BODY[5:],
            ),
        ]
    )
    assert report.total_length == 10
    assert [str(found) for found in report.versions[0].coverage] == ["0-9"]
    assert report.versions[0].missing == ()
    assert report.versions[0].publishable
    assert report.assemble() == BODY


def test_gap_is_reported_against_the_declared_total() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-3/10"), ("ETag", '"v1"')],
                BODY[:4],
            ),
            response(
                206,
                [("Content-Range", "bytes 7-9/10"), ("ETag", '"v1"')],
                BODY[7:],
            ),
        ]
    )
    version = report.versions[0]
    assert not version.complete
    assert [str(found) for found in version.missing or ()] == ["4-6"]
    with pytest.raises(IncompleteCoverageError):
        report.assemble()


def test_conflict_blocks_publishing_until_a_whole_object_settles_it() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-5/10"), ("ETag", '"v1"')],
                b"HELLOX",
            ),
            response(
                206,
                [("Content-Range", "bytes 4-9/10"), ("ETag", '"v1"')],
                b"ZZORLD",
            ),
        ]
    )
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.shard_indices == (0, 1)
    assert conflict.overlap == ByteRange(4, 5)
    assert conflict.differences == (ByteRange(4, 5),)
    assert conflict.resolved_by_full is None
    assert report.publishable is None
    with pytest.raises(IncompleteCoverageError):
        report.assemble()


def test_conflict_keeps_only_the_bytes_that_actually_differ() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-9/10"), ("ETag", '"v1"')],
                b"HELLOWORLD",
            ),
            response(
                206,
                [("Content-Range", "bytes 3-7/10"), ("ETag", '"v1"')],
                b"LOWOX",
            ),
        ]
    )
    conflict = report.conflicts[0]
    assert conflict.overlap == ByteRange(3, 7)
    assert conflict.differences == (ByteRange(7, 7),)


def test_weak_etag_tiling_is_never_publishable() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", 'W/"v1"')],
                BODY[:5],
            ),
            response(
                206,
                [("Content-Range", "bytes 5-9/10"), ("ETag", 'W/"v1"')],
                BODY[5:],
            ),
        ]
    )
    assert report.publishable is None
    assert report.versions[0].key.startswith("candidate:weak-etag:")
    assert report.versions[0].covered_length == 10
    assert not report.versions[0].proven
    with pytest.raises(IncompleteCoverageError):
        report.assemble()


def test_weak_last_modified_alone_is_never_publishable() -> None:
    report = analyze(
        [
            response(
                206,
                [
                    ("Content-Range", "bytes 0-4/10"),
                    ("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT"),
                ],
                BODY[:5],
            ),
            response(
                206,
                [
                    ("Content-Range", "bytes 5-9/10"),
                    ("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT"),
                ],
                BODY[5:],
            ),
        ]
    )
    assert report.publishable is None
    assert report.versions[0].key.startswith("candidate:last-modified:")


def test_whole_object_validates_older_shards_and_fills_the_version() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", 'W/"v1"')],
                BODY[:5],
            ),
            response(
                206,
                [("Content-Range", "bytes 2-9/10"), ("ETag", 'W/"v1"')],
                BODY[2:],
            ),
            response(200, [("Content-Length", "10")], BODY),
        ]
    )
    assert [shard.accepted for shard in report.shards] == [True, True, True]
    assert [shard.proven_by for shard in report.shards] == [
        "whole-object",
        "whole-object",
        "whole-object",
    ]
    assert report.publishable is not None
    assert report.assemble() == BODY


def test_whole_object_rejects_a_shard_whose_bytes_do_not_match() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"v1"')],
                b"HELLX",
            ),
            response(200, [("Content-Length", "10"), ("ETag", '"v1"')], BODY),
        ]
    )
    assert report.shards[0].accepted is False
    assert report.shards[0].full_checks[0].differences == (ByteRange(4, 4),)
    assert "bytes_differ_from_full" in issues(report)
    # The whole object itself is enough to publish.
    assert report.publishable is not None
    assert report.assemble() == BODY


def test_whole_object_exposes_a_shard_that_lies_outside_it() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-8/10"), ("ETag", '"v1"')],
                b"HELLOWORL",
            ),
            response(200, [("Content-Length", "5"), ("ETag", '"v1"')], b"HELLO"),
        ]
    )
    assert report.shards[0].full_checks[0].outside
    assert report.shards[0].full_checks[0].overlap == ByteRange(0, 4)
    assert "range_outside_full" in issues(report)
    assert report.shards[0].accepted is False
    # The newer whole object must not be polluted by the longer old shard.
    assert report.assemble('etag:"v1"') == b"HELLO"


def test_two_versions_are_reported_separately() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"old"')],
                b"HELLO",
            ),
            response(
                200,
                [("Content-Length", "10"), ("ETag", '"new"')],
                b"HELLOWORLX",
            ),
        ]
    )
    keys = [version.key for version in report.versions]
    assert keys == ['etag:"old"', 'etag:"new"']
    assert report.versions[0].missing == (ByteRange(5, 9),)
    assert report.versions[1].publishable
    assert report.assemble('etag:"new"') == b"HELLOWORLX"
    with pytest.raises(IncompleteCoverageError):
        report.assemble('etag:"old"')


def test_expected_length_is_enforced() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"v1"')],
                BODY[:5],
            )
        ],
        expected_length=11,
    )
    assert "total_length_mismatch" in issues(report)
    assert report.shards[0].accepted is False
    assert report.publishable is None


def test_full_body_shorter_than_expected_is_rejected() -> None:
    report = analyze(
        [response(200, [("ETag", '"v1"')], b"SHORT")],
        expected_length=10,
    )
    assert "total_length_mismatch" in issues(report)
    assert report.publishable is None


def test_empty_object_is_publishable() -> None:
    report = analyze([response(200, [("Content-Length", "0")], b"")])
    assert report.total_length == 0
    assert report.publishable is not None
    assert report.assemble() == b""


def test_416_states_the_total_without_contributing_bytes() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-9/10"), ("ETag", '"v1"')],
                BODY,
            ),
            response(
                416,
                [("Content-Range", "bytes */10"), ("ETag", '"v1"')],
            ),
        ]
    )
    assert report.total_length == 10
    assert report.publishable is not None
    assert report.assemble() == BODY


def test_416_with_unknown_total_and_ranges_without_validators() -> None:
    report = analyze([response(416, [("Content-Range", "bytes */*")])])
    assert "unknown_total" in issues(report)

    unvalidated = analyze(
        [response(206, [("Content-Range", "bytes 0-4/10")], BODY[:5])]
    )
    assert unvalidated.publishable is None
    assert unvalidated.versions[0].key == "candidate:no-validator"


def test_range_body_length_mismatch_is_rejected() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"v1"')],
                b"TOOLONG",
            )
        ]
    )
    assert "range_body_length_mismatch" in issues(report)
    assert report.shards[0].accepted is False


def test_missing_and_multiple_content_range() -> None:
    missing = analyze([response(206, [("ETag", '"v1"')], b"HELLO")])
    assert "missing_content_range" in issues(missing)

    duplicated = analyze(
        [
            response(
                206,
                [
                    ("Content-Range", "bytes 0-4/10"),
                    ("Content-Range", "bytes 0-4/10"),
                    ("ETag", '"v1"'),
                ],
                b"HELLO",
            )
        ]
    )
    assert "multiple_content_ranges" in issues(duplicated)


def test_multipart_byteranges_is_reported_as_unsupported() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Type", "multipart/byteranges; boundary=abc")],
                b"--abc--",
            )
        ]
    )
    assert "multipart_byteranges_unsupported" in issues(report)
    assert report.publishable is None


def test_unsupported_status_is_ignored() -> None:
    report = analyze([response(304, [("ETag", '"v1"')])])
    assert "unsupported_status" in issues(report)
    assert report.publishable is None


def test_conflicting_whole_snapshots_with_the_same_etag_block_publishing() -> None:
    report = analyze(
        [
            response(200, [("ETag", '"v1"'), ("Content-Length", "10")], BODY),
            response(200, [("ETag", '"v1"'), ("Content-Length", "10")], b"HELLOWORLX"),
        ]
    )
    assert "conflicting_whole_objects" in issues(report)
    assert report.publishable is None
    assert len(report.conflicts) == 1
    assert report.conflicts[0].differences == (ByteRange(9, 9),)


def test_duplicate_whole_snapshots_are_fine() -> None:
    report = analyze(
        [
            response(200, [("ETag", '"v1"'), ("Content-Length", "10")], BODY),
            response(200, [("ETag", '"v1"'), ("Content-Length", "10")], BODY),
        ]
    )
    assert report.publishable is not None
    assert report.assemble() == BODY


def test_assemble_accepts_a_version_object_and_rejects_bad_indexes() -> None:
    report = analyze([response(200, [("ETag", '"v1"')], BODY)])
    version = report.versions[0]
    assert report.assemble(version) == BODY
    assert report.assemble(0) == BODY
    with pytest.raises(IncompleteCoverageError):
        report.assemble(4)
    with pytest.raises(IncompleteCoverageError):
        report.assemble("etag:nope")


def test_report_is_serializable() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"v1"')],
                b"HELLX",
            ),
            response(200, [("ETag", '"v1"'), ("Content-Length", "10")], BODY),
        ]
    )
    data = report.to_dict()
    assert data["publishable"] == 'etag:"v1"'
    assert data["shards"][0]["proven_by"] is None
    assert data["shards"][0]["full_checks"][0]["differences"] == [
        {"start": 4, "end": 4}
    ]
    assert '"v1"' in data["versions"][0]["key"]


def test_parse_capture_reads_consecutive_responses() -> None:
    raw = (
        b"HTTP/1.1 206 Partial Content\r\n"
        b"Content-Range: bytes 0-4/10\r\nContent-Length: 5\r\nETag: \"v1\"\r\n\r\n"
        b"HELLO"
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nETag: \"v1\"\r\n\r\n"
        b"HELLOWORLD"
    )
    parsed = parse_response_capture(raw)
    assert [item.status_code for item in parsed] == [206, 200]
    assert parsed[0].body == b"HELLO"

    report = analyze_capture(raw)
    assert report.publishable is not None
    assert report.assemble() == BODY


def test_parse_capture_handles_informational_response_and_close_delimitation() -> None:
    raw = (
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 200 OK\r\n\r\nHELLOWORLD"
    )
    parsed = parse_response_capture(raw)
    assert [(item.status_code, item.body) for item in parsed] == [(200, BODY)]


def test_parse_capture_handles_chunked_body() -> None:
    raw = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nHELLO\r\n5\r\nWORLD\r\n0\r\n\r\n"
    )
    assert parse_response_capture(raw)[0].body == BODY


def test_parse_capture_rejects_truncation() -> None:
    with pytest.raises(CaptureParseError) as excinfo:
        parse_response_capture(
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nHELLO"
        )
    assert excinfo.value.response_index == 0


def test_parse_capture_rejects_garbage() -> None:
    with pytest.raises(CaptureParseError):
        parse_response_capture(b"not http at all\r\n\r\n")


def test_default_publish_prefers_the_whole_object_snapshot() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-4/10"), ("ETag", '"old"')],
                b"HELLO",
            ),
            response(
                206,
                [("Content-Range", "bytes 5-9/10"), ("ETag", '"old"')],
                b"WORLD",
            ),
            response(
                200,
                [("Content-Length", "10"), ("ETag", '"new"')],
                b"HELLOWORLX",
            ),
        ]
    )
    keys = [version.key for version in report.versions]
    assert keys == ['etag:"old"', 'etag:"new"']
    assert report.publishable is not None
    assert report.publishable.key == 'etag:"new"'
    assert report.assemble() == b"HELLOWORLX"


def test_cli_writes_only_a_publishable_object(tmp_path: "Path") -> None:
    from ..coverage_cli import main

    capture = tmp_path / "capture.bin"
    output = tmp_path / "object.bin"
    capture.write_bytes(
        b"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-4/10\r\n"
        b'Content-Length: 5\r\nETag: "v1"\r\n\r\nHELLO'
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n"
        b'ETag: "v1"\r\n\r\nHELLOWORLD'
    )
    assert main([str(capture), "--output", str(output)]) == 0
    assert output.read_bytes() == BODY

    gapped = tmp_path / "gapped.bin"
    gapped.write_bytes(
        b"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-4/10\r\n"
        b'Content-Length: 5\r\nETag: "v1"\r\n\r\nHELLO'
    )
    failed = tmp_path / "failed.bin"
    assert main([str(gapped), "--output", str(failed)]) == 1
    assert not failed.exists()


def test_format_table_names_the_missing_part_and_conflict() -> None:
    report = analyze(
        [
            response(
                206,
                [("Content-Range", "bytes 0-5/10"), ("ETag", '"v1"')],
                b"HELLOX",
            ),
            response(
                206,
                [("Content-Range", "bytes 4-7/10"), ("ETag", '"v1"')],
                b"ZZOR",
            ),
        ]
    )
    table = report.format_table()
    assert "missing: 8-9" in table
    assert "conflict #0 vs #1 in 4-5 at 4-7" not in table
    assert "at 4-5" in table
