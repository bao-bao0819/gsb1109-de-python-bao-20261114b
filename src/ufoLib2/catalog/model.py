from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ufoLib2.errors import Error

FORMAT_NAME = "ufoLib2-glyph-catalog"
FORMAT_VERSION = 1

BASIS_ORDER = (
    "manual",
    "stable-id",
    "unicode",
    "name",
    "name-unicode",
    "manual-rejection",
)


class CatalogError(Error):
    """Raised when catalog input or output is inconsistent."""


def unicode_label(codepoint: int) -> str:
    """Return the canonical ``U+XXXX`` label for a Unicode scalar value."""
    return f"U+{codepoint:04X}"


def unicode_labels(codepoints: Iterable[int]) -> list[str]:
    return [unicode_label(cp) for cp in codepoints]


def _validate_unicodes(values: Sequence[int], what: str) -> tuple[int, ...]:
    result: list[int] = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool):
            raise CatalogError(f"{what} must contain integers, found {value!r}")
        if value < 0 or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            raise CatalogError(f"{what} contains invalid Unicode scalar U+{value:04X}")
        result.append(value)
    return tuple(sorted(set(result)))


def stable_key(prefix: str, parts: Iterable[str]) -> str:
    """Build a deterministic identifier from a sorted collection of strings."""
    material = "\0".join(sorted(parts))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


@dataclass(frozen=True)
class GlyphRecord:
    """One glyph from one supplier, kept as an independent catalog entry.

    ``unicodes`` is the identity-bearing Unicode set.  It comes from the
    ``<unicode>`` elements in the glif, or, for a supplier that names glyphs
    canonically with ``uniXXXX``/``uXXXXX``, from the name.  ``name_unicodes``
    is deliberately weaker evidence: it is never used to make a one-to-one
    match, and its presence does not stop an unencoded alternate from being an
    independent record.
    """

    id: str
    source: str
    glyph_name: str
    stable_id: str | None = None
    unicodes: tuple[int, ...] = ()
    unicode_source: str = "none"
    name_unicodes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise CatalogError("glyph record id must not be empty")
        if not self.source:
            raise CatalogError("glyph record source must not be empty")
        if not self.glyph_name:
            raise CatalogError(f"glyph record {self.id!r} has an empty glyph name")
        if self.stable_id is not None and not self.stable_id:
            raise CatalogError(f"glyph record {self.id!r} has an empty stable id")
        object.__setattr__(
            self, "unicodes", _validate_unicodes(self.unicodes, "unicodes")
        )
        object.__setattr__(
            self,
            "name_unicodes",
            _validate_unicodes(self.name_unicodes, "name_unicodes"),
        )
        if self.unicode_source not in {"glif", "name", "none"}:
            raise CatalogError(
                f"glyph record {self.id!r} has invalid unicode_source "
                f"{self.unicode_source!r}"
            )

    @property
    def unencoded(self) -> bool:
        return not self.unicodes

    @property
    def unicode_labels(self) -> list[str]:
        return unicode_labels(self.unicodes)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "glyphName": self.glyph_name,
            "stableId": self.stable_id,
            "unicodes": self.unicode_labels,
            "unicodeSource": self.unicode_source,
            "nameUnicodes": unicode_labels(self.name_unicodes),
            "unencoded": self.unencoded,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GlyphRecord:
        try:
            return cls(
                id=str(data["id"]),
                source=str(data["source"]),
                glyph_name=str(data["glyphName"]),
                stable_id=(
                    None if data.get("stableId") is None else str(data["stableId"])
                ),
                unicodes=tuple(_parse_unicode_labels(data.get("unicodes", ()))),
                unicode_source=str(data.get("unicodeSource", "none")),
                name_unicodes=tuple(
                    _parse_unicode_labels(data.get("nameUnicodes", ()))
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid glyph record: {exc}") from exc


def _parse_unicode_labels(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)):
        raise CatalogError(f"expected a list of Unicode labels, found {values!r}")
    result: list[int] = []
    for value in values:
        if not isinstance(value, str) or not value.startswith("U+"):
            raise CatalogError(f"invalid Unicode label {value!r}")
        try:
            result.append(int(value[2:], 16))
        except ValueError as exc:
            raise CatalogError(f"invalid Unicode label {value!r}") from exc
    return result


@dataclass(frozen=True)
class SourceInfo:
    name: str
    path: str
    naming: str
    glyph_count: int
    stable_id_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "naming": self.naming,
            "glyphCount": self.glyph_count,
            "stableIdKey": self.stable_id_key,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SourceInfo:
        try:
            return cls(
                name=str(data["name"]),
                path=str(data["path"]),
                naming=str(data["naming"]),
                glyph_count=int(data["glyphCount"]),
                stable_id_key=(
                    None
                    if data.get("stableIdKey") is None
                    else str(data["stableIdKey"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid source info: {exc}") from exc


@dataclass(frozen=True)
class Evidence:
    basis: str
    left: str
    right: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "left": self.left,
            "right": self.right,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Evidence:
        try:
            return cls(
                basis=str(data["basis"]),
                left=str(data["left"]),
                right=str(data["right"]),
                detail=str(data["detail"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid evidence: {exc}") from exc


@dataclass(frozen=True)
class Match:
    id: str
    left: str
    right: str
    basis: tuple[str, ...]
    status: str
    evidence: tuple[Evidence, ...]

    @property
    def conflicted(self) -> bool:
        return self.status != "confirmed"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "left": self.left,
            "right": self.right,
            "basis": list(self.basis),
            "status": self.status,
            "conflicted": self.conflicted,
            "evidence": [item.to_json() for item in self.evidence],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Match:
        try:
            return cls(
                id=str(data["id"]),
                left=str(data["left"]),
                right=str(data["right"]),
                basis=tuple(str(item) for item in data["basis"]),
                status=str(data["status"]),
                evidence=tuple(
                    Evidence.from_json(item) for item in data.get("evidence", ())
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid match: {exc}") from exc


@dataclass(frozen=True)
class CandidateGroup:
    id: str
    records: tuple[str, ...]
    unicodes: tuple[int, ...]
    status: str
    reason: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(sorted(self.records)))
        object.__setattr__(self, "unicodes", tuple(sorted(set(self.unicodes))))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "records": list(self.records),
            "unicodes": unicode_labels(self.unicodes),
            "status": self.status,
            "reason": self.reason,
            "evidence": [item.to_json() for item in self.evidence],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CandidateGroup:
        try:
            return cls(
                id=str(data["id"]),
                records=tuple(str(item) for item in data["records"]),
                unicodes=tuple(_parse_unicode_labels(data.get("unicodes", ()))),
                status=str(data["status"]),
                reason=str(data["reason"]),
                evidence=tuple(
                    Evidence.from_json(item) for item in data.get("evidence", ())
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid candidate group: {exc}") from exc


@dataclass(frozen=True)
class ConflictClaim:
    basis: str
    targets: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(sorted(self.targets)))

    def to_json(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "targets": list(self.targets),
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ConflictClaim:
        try:
            return cls(
                basis=str(data["basis"]),
                targets=tuple(str(item) for item in data["targets"]),
                detail=str(data["detail"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid conflict claim: {exc}") from exc


def _basis_order(basis: str) -> int:
    try:
        return BASIS_ORDER.index(basis)
    except ValueError:
        return len(BASIS_ORDER)


@dataclass(frozen=True)
class Conflict:
    id: str
    kind: str
    record: str
    claims: tuple[ConflictClaim, ...]
    disagreements: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disagreements",
            tuple(
                sorted(
                    (
                        tuple(sorted(pair, key=_basis_order))
                        for pair in self.disagreements
                    ),
                    key=lambda pair: (_basis_order(pair[0]), _basis_order(pair[1])),
                )
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "record": self.record,
            "claims": [item.to_json() for item in self.claims],
            "disagreements": [list(item) for item in self.disagreements],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Conflict:
        try:
            return cls(
                id=str(data["id"]),
                kind=str(data["kind"]),
                record=str(data["record"]),
                claims=tuple(
                    ConflictClaim.from_json(item) for item in data.get("claims", ())
                ),
                disagreements=tuple(
                    (str(item[0]), str(item[1]))
                    for item in data.get("disagreements", ())
                ),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CatalogError(f"invalid conflict: {exc}") from exc


@dataclass(frozen=True)
class Unmatched:
    record: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"record": self.record, "reason": self.reason}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Unmatched:
        try:
            return cls(record=str(data["record"]), reason=str(data["reason"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid unmatched record: {exc}") from exc


@dataclass(frozen=True)
class Catalog:
    sources: tuple[SourceInfo, ...]
    records: tuple[GlyphRecord, ...]
    matches: tuple[Match, ...]
    candidate_groups: tuple[CandidateGroup, ...]
    conflicts: tuple[Conflict, ...]
    unmatched: tuple[Unmatched, ...]

    def __post_init__(self) -> None:
        ids = [record.id for record in self.records]
        if len(ids) != len(set(ids)):
            raise CatalogError("catalog contains duplicate glyph record ids")
        known = set(ids)
        for match in self.matches:
            for record_id in (match.left, match.right):
                if record_id not in known:
                    raise CatalogError(
                        f"match {match.id!r} references unknown record {record_id!r}"
                    )
        for group in self.candidate_groups:
            for record_id in group.records:
                if record_id not in known:
                    raise CatalogError(
                        f"candidate group {group.id!r} references unknown record "
                        f"{record_id!r}"
                    )
        for conflict in self.conflicts:
            if conflict.record not in known:
                raise CatalogError(
                    f"conflict {conflict.id!r} references unknown record "
                    f"{conflict.record!r}"
                )
        for item in self.unmatched:
            if item.record not in known:
                raise CatalogError(
                    f"unmatched item references unknown record {item.record!r}"
                )

    def record(self, record_id: str) -> GlyphRecord:
        for record in self.records:
            if record.id == record_id:
                return record
        raise KeyError(record_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "sources": [source.to_json() for source in self.sources],
            "records": [record.to_json() for record in self.records],
            "matches": [match.to_json() for match in self.matches],
            "candidateGroups": [
                group.to_json() for group in self.candidate_groups
            ],
            "conflicts": [conflict.to_json() for conflict in self.conflicts],
            "unmatched": [item.to_json() for item in self.unmatched],
        }

    def to_json_string(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_json(), indent=indent, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Catalog:
        if data.get("format") != FORMAT_NAME:
            raise CatalogError(f"unsupported catalog format {data.get('format')!r}")
        version = data.get("version")
        if version != FORMAT_VERSION:
            raise CatalogError(f"unsupported catalog version {version!r}")
        try:
            return cls(
                sources=tuple(
                    SourceInfo.from_json(item) for item in data["sources"]
                ),
                records=tuple(
                    GlyphRecord.from_json(item) for item in data["records"]
                ),
                matches=tuple(Match.from_json(item) for item in data["matches"]),
                candidate_groups=tuple(
                    CandidateGroup.from_json(item)
                    for item in data["candidateGroups"]
                ),
                conflicts=tuple(
                    Conflict.from_json(item) for item in data["conflicts"]
                ),
                unmatched=tuple(
                    Unmatched.from_json(item) for item in data["unmatched"]
                ),
            )
        except (KeyError, TypeError) as exc:
            raise CatalogError(f"invalid catalog: {exc}") from exc


def load_catalog(path: str | Path) -> Catalog:
    """Load a catalog JSON file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read catalog {path!s}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise CatalogError("catalog JSON must be an object")
    return Catalog.from_json(data)


def save_catalog(catalog: Catalog, path: str | Path) -> None:
    """Write a catalog JSON file in its canonical order."""
    Path(path).write_text(catalog.to_json_string(), encoding="utf-8")
