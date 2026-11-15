import base64
import json
from pathlib import Path

import pytest

from gateway_traffic.archives import (
    decode_wire,
    load_and_merge,
    load_records,
    merge_archives,
    record_from_mapping,
)
from gateway_traffic.sampling import SamplingError


def record(transaction_id="t1", path="/x", status=200, body=b"hello"):
    request = f"GET {path} HTTP/1.1\r\nHost: e\r\n\r\n".encode()
    response = (
        f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    return {
        "transaction_id": transaction_id,
        "request": request.decode("latin-1"),
        "response": response.decode("latin-1"),
    }


def test_load_json_and_jsonl(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text(
        json.dumps(record("a")) + "\n" + json.dumps(record("b")) + "\n",
        encoding="utf-8",
    )
    records = load_records(path)
    assert [item.transaction_id for item in records] == ["a", "b"]
    parsed = records[0].parsed()
    assert parsed.final_status == 200
    assert parsed.body_bytes == 5


def test_load_json_array_with_nested_transactions(tmp_path):
    path = tmp_path / "archive.json"
    path.write_text(
        json.dumps({"transactions": [record("a"), record("b")]}), encoding="utf-8"
    )
    assert len(load_records(path)) == 2


def test_base64_and_hex_wire_encodings(tmp_path):
    request = b"GET /enc HTTP/1.1\r\nHost: e\r\n\r\n"
    response = b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\nok"
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps(
            [
                {
                    "transaction_id": "base64",
                    "request": {"base64": base64.b64encode(request).decode()},
                    "response": {"hex": response.hex()},
                },
                {
                    "transaction_id": "prefix",
                    "request": "base64:" + base64.b64encode(request).decode(),
                    "response": "hex:" + response.hex(),
                },
            ]
        ),
        encoding="utf-8",
    )
    records = load_records(path)
    for item in records:
        parsed = item.parsed()
        assert parsed.path == "/enc"
        assert parsed.final_status == 201
        assert parsed.body_bytes == 2
    assert decode_wire({"text": "abc"}) == b"abc"


def test_paired_raw_files_directory(tmp_path):
    request = b"GET /pair HTTP/1.1\r\nHost: e\r\n\r\n"
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\npair"
    (tmp_path / "abc.request").write_bytes(request)
    (tmp_path / "abc.response").write_bytes(response)
    records = load_records(tmp_path)
    assert len(records) == 1
    assert records[0].transaction_id == "abc"
    parsed = records[0].parsed()
    assert parsed.path == "/pair"
    assert parsed.body_bytes == 4


def test_paired_http_suffix_files(tmp_path):
    request = b"GET /http-pair HTTP/1.1\r\nHost: e\r\n\r\n"
    response = b"HTTP/1.1 201 Created\r\nContent-Length: 3\r\n\r\nnew"
    (tmp_path / "abc.request.http").write_bytes(request)
    (tmp_path / "abc.response.http").write_bytes(response)
    records = load_records(tmp_path)
    assert len(records) == 1
    assert records[0].transaction_id == "abc"
    assert records[0].parsed().path == "/http-pair"
    assert records[0].parsed().body_bytes == 3


def test_merge_deduplicates_shared_transactions(tmp_path):
    path_archive = tmp_path / "path.json"
    status_archive = tmp_path / "status.json"
    shared = record("shared", "/s", 200, b"12345")
    path_only = record("path-only", "/p", 200, b"123")
    status_only = record("status-only", "/q", 404, b"1234567")
    path_archive.write_text(json.dumps([shared, path_only]), encoding="utf-8")
    status_archive.write_text(json.dumps([shared, status_only]), encoding="utf-8")

    merged = load_and_merge(path_archive, status_archive)
    assert len(merged.transactions) == 3
    assert (merged.path_only, merged.status_only, merged.both) == (1, 1, 1)
    shared_tx = next(item for item in merged.transactions if item.transaction_id == "shared")
    assert shared_tx.in_path_archive and shared_tx.in_status_archive
    assert shared_tx.body_bytes == 5


def test_repeated_identity_within_one_archive_is_rejected(tmp_path):
    archive = tmp_path / "archive.json"
    archive.write_text(json.dumps([record("same"), record("same")]), encoding="utf-8")
    with pytest.raises(SamplingError, match="duplicate transaction"):
        merge_archives(load_records(archive), [])


def test_disagreeing_copies_of_a_shared_transaction_are_rejected(tmp_path):
    path_archive = tmp_path / "path.json"
    status_archive = tmp_path / "status.json"
    path_archive.write_text(
        json.dumps([record("shared", "/x", 200, b"aaaa")]), encoding="utf-8"
    )
    status_archive.write_text(
        json.dumps([record("shared", "/x", 200, b"bbbb")]), encoding="utf-8"
    )
    with pytest.raises(SamplingError, match="disagreement"):
        load_and_merge(path_archive, status_archive)


def test_archive_metadata_must_match_h11_result(tmp_path):
    item = record("t1", "/x", 200, b"hello")
    item["body_bytes"] = 99
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([item]), encoding="utf-8")
    with pytest.raises(SamplingError, match="body_bytes"):
        merge_archives(load_records(path), [])


def test_record_without_wire_bytes_cannot_be_estimated(tmp_path):
    path = tmp_path / "nomb.json"
    path.write_text(
        json.dumps([{"transaction_id": "t", "path": "/x", "final_status": 200}]),
        encoding="utf-8",
    )
    entry = record_from_mapping(
        {"transaction_id": "t", "path": "/x", "final_status": 200}, tmp_path
    )
    with pytest.raises(SamplingError, match="lacks raw request/response"):
        entry.parsed()
