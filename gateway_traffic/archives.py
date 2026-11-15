"""Load captured HTTP transactions from gateway archive files or directories.

An archive is one of:

* a JSON array, or JSON-lines/NDJSON file, of transaction records;
* a directory containing one JSON record per transaction;
* a directory of paired wire files, e.g. ``abc.request`` and ``abc.response``
  (also ``.req``/``.res`` or ``.http``/``.resp``).

A record normally has a transaction id and the raw request and response
bytes.  Raw bytes may be JSON strings (interpreted as UTF-8, falling back to
latin-1), ``{"base64": ...}`` or ``{"hex": ...}`` objects, or strings with a
``base64:`` or ``hex:`` prefix.  Records may alternatively provide
``request_file`` and ``response_file`` paths relative to the record.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .sampling import ObservedTransaction, SamplingError
from .wire import ParsedTransaction, parse_transaction

_ID_FIELDS = ("transaction_id", "id", "txn_id", "transaction", "capture_id", "uuid")
_REQUEST_FIELDS = ("request", "request_wire", "request_bytes", "req", "raw_request")
_RESPONSE_FIELDS = ("response", "response_wire", "response_bytes", "resp", "raw_response")
_PATH_FIELDS = ("path", "request_path", "target", "uri")
_STATUS_FIELDS = ("final_status", "status", "status_code", "response_status")
_BODY_FIELDS = ("body_bytes", "response_body_bytes", "response_bytes_received")

_JSON_SUFFIXES = {".json", ".jsonl", ".ndjson"}
_REQUEST_SUFFIXES = {".request", ".req", ".request.http"}
_RESPONSE_SUFFIXES = {".response", ".res", ".resp", ".response.http"}


@dataclass
class CapturedRecord:
    transaction_id: str
    request_wire: Optional[bytes] = None
    response_wire: Optional[bytes] = None
    path: Optional[str] = None
    final_status: Optional[int] = None
    body_bytes: Optional[int] = None
    method: Optional[str] = None
    source: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def parsed(self) -> ParsedTransaction:
        if self.request_wire is None or self.response_wire is None:
            raise SamplingError(
                f"transaction {self.transaction_id!r} lacks raw request/response bytes; "
                "h11 cannot derive status and received body bytes"
            )
        return parse_transaction(self.request_wire, self.response_wire)


def _first(record: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def decode_wire(value: Any) -> bytes:
    """Decode a wire value from its JSON representation."""

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Mapping):
        if "base64" in value:
            try:
                return base64.b64decode(str(value["base64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SamplingError("invalid base64 wire bytes") from exc
        if "hex" in value:
            try:
                return bytes.fromhex(str(value["hex"]))
            except ValueError as exc:
                raise SamplingError("invalid hex wire bytes") from exc
        if "text" in value:
            return str(value["text"]).encode("utf-8")
        raise SamplingError(f"unrecognized wire encoding object: {sorted(value)}")
    if isinstance(value, str):
        if value.startswith("base64:"):
            try:
                return base64.b64decode(value[len("base64:") :], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SamplingError("invalid base64 wire bytes") from exc
        if value.startswith("hex:"):
            try:
                return bytes.fromhex(value[len("hex:") :])
            except ValueError as exc:
                raise SamplingError("invalid hex wire bytes") from exc
        try:
            return value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("latin-1")
    raise SamplingError(f"unrecognized wire value: {type(value).__name__}")


def _read_side(
    record: Mapping[str, Any],
    fields: Sequence[str],
    file_key: str,
    record_dir: Path,
) -> Optional[bytes]:
    value = _first(record, fields)
    if value is not None:
        return decode_wire(value)
    file_value = record.get(file_key)
    if file_value:
        path = Path(str(file_value))
        if not path.is_absolute():
            path = record_dir / path
        return path.read_bytes()
    return None


def record_from_mapping(record: Mapping[str, Any], base_dir: Path) -> CapturedRecord:
    request_wire = _read_side(record, _REQUEST_FIELDS, "request_file", base_dir)
    response_wire = _read_side(record, _RESPONSE_FIELDS, "response_file", base_dir)

    transaction_id = _first(record, _ID_FIELDS)
    if transaction_id is None:
        if request_wire is None:
            raise SamplingError("archive record has neither transaction id nor request bytes")
        transaction_id = hashlib.sha256(request_wire).hexdigest()
    transaction_id = str(transaction_id)

    path = _first(record, _PATH_FIELDS)
    status = _first(record, _STATUS_FIELDS)
    body_bytes = _first(record, _BODY_FIELDS)
    method = record.get("method")

    return CapturedRecord(
        transaction_id=transaction_id,
        request_wire=request_wire,
        response_wire=response_wire,
        path=None if path is None else str(path),
        final_status=None if status is None else int(status),
        body_bytes=None if body_bytes is None else int(body_bytes),
        method=None if method is None else str(method),
        metadata=dict(record),
    )


def _load_json(path: Path) -> List[Mapping[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    if path.suffix == ".json":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if value is not None:
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                for key in ("transactions", "records", "entries", "items"):
                    if isinstance(value.get(key), list):
                        return [item for item in value[key] if isinstance(item, Mapping)]
                return [value]
            raise SamplingError(f"{path}: expected a JSON object or array")
    records: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SamplingError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(item, Mapping):
            raise SamplingError(f"{path}:{line_number}: record must be a JSON object")
        records.append(item)
    return records


def _pair_suffix(name: str) -> Tuple[Optional[str], str]:
    """Split a paired-wire filename into (suffix, stem)."""

    lowered = name.lower()
    for suffix in sorted(
        _REQUEST_SUFFIXES | _RESPONSE_SUFFIXES, key=len, reverse=True
    ):
        if lowered.endswith(suffix):
            return suffix, name[: -len(suffix)]
    return None, name


def _load_paired_directory(path: Path) -> List[Mapping[str, Any]]:
    requests: Dict[str, Path] = {}
    responses: Dict[str, Path] = {}
    for child in sorted(path.iterdir()):
        if not child.is_file():
            continue
        suffix, stem = _pair_suffix(child.name)
        if suffix in _REQUEST_SUFFIXES:
            requests[stem] = child
        elif suffix in _RESPONSE_SUFFIXES:
            responses[stem] = child
    records: List[Mapping[str, Any]] = []
    for stem in sorted(set(requests) | set(responses)):
        record: Dict[str, Any] = {"transaction_id": stem}
        if stem in requests:
            record["request_file"] = str(requests[stem])
        if stem in responses:
            record["response_file"] = str(responses[stem])
        records.append(record)
    return records


def load_records(path: Path) -> List[CapturedRecord]:
    path = Path(path)
    if not path.exists():
        raise SamplingError(f"archive does not exist: {path}")
    if path.is_dir():
        json_files = sorted(
            child for child in path.iterdir() if child.is_file() and child.suffix.lower() in _JSON_SUFFIXES
        )
        if json_files:
            records: List[CapturedRecord] = []
            for json_file in json_files:
                for item in _load_json(json_file):
                    records.append(record_from_mapping(item, json_file.parent))
            return records
        raw_records = _load_paired_directory(path)
        return [record_from_mapping(item, path) for item in raw_records]
    if path.suffix.lower() not in _JSON_SUFFIXES:
        # A single file: look for a neighbouring pair with the same stem.
        requests = [
            path.with_suffix(suffix) for suffix in _REQUEST_SUFFIXES if path.with_suffix(suffix).exists()
        ]
        responses = [
            path.with_suffix(suffix) for suffix in _RESPONSE_SUFFIXES if path.with_suffix(suffix).exists()
        ]
        if requests and responses:
            return [
                record_from_mapping(
                    {
                        "transaction_id": path.stem,
                        "request_file": str(requests[0]),
                        "response_file": str(responses[0]),
                    },
                    path.parent,
                )
            ]
        raise SamplingError(f"unsupported archive file type: {path}")
    return [record_from_mapping(item, path.parent) for item in _load_json(path)]


def _validated_observation(
    record: CapturedRecord, *, path_archive: bool, status_archive: bool
) -> ObservedTransaction:
    parsed = record.parsed()
    if parsed.request is None:
        raise SamplingError(f"transaction {record.transaction_id!r}: no h11 Request event")
    if parsed.final_status is None:
        raise SamplingError(
            f"transaction {record.transaction_id!r}: no final Response event"
        )
    path = parsed.path if parsed.path is not None else record.path
    if path is None:
        raise SamplingError(f"transaction {record.transaction_id!r}: no request path")
    if record.path is not None and parsed.path is not None and record.path != parsed.path:
        raise SamplingError(
            f"transaction {record.transaction_id!r}: archive path {record.path!r} "
            f"does not match h11 request target {parsed.path!r}"
        )
    if record.final_status is not None and record.final_status != parsed.final_status:
        raise SamplingError(
            f"transaction {record.transaction_id!r}: archive status "
            f"{record.final_status} does not match h11 final status {parsed.final_status}"
        )
    if record.body_bytes is not None and record.body_bytes != parsed.body_bytes:
        raise SamplingError(
            f"transaction {record.transaction_id!r}: archive body_bytes "
            f"{record.body_bytes} does not match h11 received body bytes "
            f"{parsed.body_bytes}"
        )
    return ObservedTransaction(
        transaction_id=record.transaction_id,
        path=path,
        final_status=parsed.final_status,
        body_bytes=parsed.body_bytes,
        in_path_archive=path_archive,
        in_status_archive=status_archive,
        method=(
            parsed.method.decode("latin-1")
            if parsed.method is not None
            else record.method
        ),
        response_complete=parsed.response_complete,
        errors=tuple(parsed.errors),
        metadata={
            "response_wire_bytes": parsed.response_wire_bytes,
            "informational_statuses": list(parsed.informational_statuses),
            "source": record.source,
        },
    )


@dataclass
class MergedArchives:
    transactions: List[ObservedTransaction]
    path_only: int = 0
    status_only: int = 0
    both: int = 0
    conflicts: List[str] = field(default_factory=list)


def merge_archives(
    path_records: Iterable[CapturedRecord],
    status_records: Iterable[CapturedRecord],
) -> MergedArchives:
    """Deduplicate two archives by transaction identity.

    A transaction found in both files is one unit with both membership flags
    set.  Repeating an identity within one archive is an error.
    """

    path_map: Dict[str, CapturedRecord] = {}
    for record in path_records:
        if record.transaction_id in path_map:
            raise SamplingError(
                f"duplicate transaction {record.transaction_id!r} in path archive"
            )
        path_map[record.transaction_id] = record
    status_map: Dict[str, CapturedRecord] = {}
    for record in status_records:
        if record.transaction_id in status_map:
            raise SamplingError(
                f"duplicate transaction {record.transaction_id!r} in status archive"
            )
        status_map[record.transaction_id] = record

    ids = sorted(set(path_map) | set(status_map))
    transactions: List[ObservedTransaction] = []
    both = path_only = status_only = 0
    conflicts: List[str] = []
    for transaction_id in ids:
        in_path = transaction_id in path_map
        in_status = transaction_id in status_map
        chosen = path_map.get(transaction_id) or status_map[transaction_id]
        if in_path and in_status:
            both += 1
            other = status_map[transaction_id]
            # Both archives describe the same transaction.  Parse each
            # independently and reject a disagreement rather than guessing.
            first = chosen.parsed()
            second = other.parsed()
            raw_disagrees = (
                chosen.request_wire is not None
                and other.request_wire is not None
                and chosen.request_wire != other.request_wire
            ) or (
                chosen.response_wire is not None
                and other.response_wire is not None
                and chosen.response_wire != other.response_wire
            )
            if (
                raw_disagrees
                or first.path != second.path
                or first.final_status != second.final_status
                or first.body_bytes != second.body_bytes
            ):
                conflicts.append(
                    f"{transaction_id}: path/status/body disagreement between archives"
                )
                raise SamplingError(conflicts[-1])
        elif in_path:
            path_only += 1
        else:
            status_only += 1
        transaction = _validated_observation(
            chosen, path_archive=in_path, status_archive=in_status
        )
        transactions.append(transaction)
    return MergedArchives(
        transactions=transactions,
        path_only=path_only,
        status_only=status_only,
        both=both,
        conflicts=conflicts,
    )


def load_and_merge(path_archive: Path, status_archive: Path) -> MergedArchives:
    return merge_archives(load_records(path_archive), load_records(status_archive))
