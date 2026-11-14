"""Layout-based compatibility between two preprocessed modules.

No type spelling is consulted: descriptors produced by
:mod:`structmap.collector` already carry fully expanded, scope-resolved
layouts, so comparison is purely structural. Cyclic record graphs are
handled with a stack of paired record ids.
"""

from dataclasses import dataclass, field

from .collector import (
    Array,
    FuncPtr,
    ModuleSchema,
    Prim,
    Ptr,
    RecordRef,
    TypeDescriptor,
    Unknown,
)

Pair = tuple[str, str]


@dataclass
class FieldRow:
    index: int
    name_a: str | None
    name_b: str | None
    status: str
    reason: str | None = None
    layout_a: dict | None = None
    layout_b: dict | None = None


@dataclass
class RecordMatch:
    rid_a: str | None
    rid_b: str | None
    verdict: str
    reason: str | None = None
    fields: list[FieldRow] = field(default_factory=list)


@dataclass
class ComparisonReport:
    source_a: str
    source_b: str
    record_matches: list[RecordMatch]
    alias_rows: list[dict]

    def to_dict(self) -> dict:
        return {
            "source_a": self.source_a,
            "source_b": self.source_b,
            "record_matches": [
                {
                    "rid_a": match.rid_a,
                    "rid_b": match.rid_b,
                    "verdict": match.verdict,
                    "reason": match.reason,
                    "fields": [
                        {
                            "index": row.index,
                            "name_a": row.name_a,
                            "name_b": row.name_b,
                            "status": row.status,
                            "reason": row.reason,
                            "layout_a": row.layout_a,
                            "layout_b": row.layout_b,
                        }
                        for row in match.fields
                    ],
                }
                for match in self.record_matches
            ],
            "alias_rows": self.alias_rows,
        }


def records_equivalent(
    schema_a: ModuleSchema,
    rid_a: str,
    schema_b: ModuleSchema,
    rid_b: str,
    _active: tuple[Pair, ...] = (),
) -> bool:
    pair = (rid_a, rid_b)
    if pair in _active:
        return True
    record_a = schema_a.records.get(rid_a)
    record_b = schema_b.records.get(rid_b)
    if record_a is None or record_b is None:
        return False
    if record_a.kind != record_b.kind:
        return False
    if len(record_a.fields) != len(record_b.fields):
        return False
    active = _active + (pair,)
    for field_a, field_b in zip(record_a.fields, record_b.fields):
        if not _descriptors_equal(
            field_a.descriptor, schema_a, field_b.descriptor, schema_b, active
        ):
            return False
    return True


def _descriptors_equal(
    desc_a: TypeDescriptor,
    schema_a: ModuleSchema,
    desc_b: TypeDescriptor,
    schema_b: ModuleSchema,
    active: tuple[Pair, ...],
) -> bool:
    return _compare_descriptors(desc_a, schema_a, desc_b, schema_b, active) is None


def _compare_descriptors(
    desc_a: TypeDescriptor,
    schema_a: ModuleSchema,
    desc_b: TypeDescriptor,
    schema_b: ModuleSchema,
    active: tuple[Pair, ...],
) -> str | None:
    if type(desc_a) is not type(desc_b):
        return f"kind {type(desc_a).__name__} != {type(desc_b).__name__}"
    if isinstance(desc_a, Prim):
        assert isinstance(desc_b, Prim)
        if desc_a.names != desc_b.names:
            return f"primitive {list(desc_a.names)} != {list(desc_b.names)}"
        return None
    if isinstance(desc_a, FuncPtr):
        return None
    if isinstance(desc_a, Unknown):
        assert isinstance(desc_b, Unknown)
        if desc_a.spelling != desc_b.spelling:
            return f"unresolved {desc_a.spelling} != {desc_b.spelling}"
        return None
    if isinstance(desc_a, Array):
        assert isinstance(desc_b, Array)
        if desc_a.dim != desc_b.dim:
            return f"array dim {desc_a.dim} != {desc_b.dim}"
        return _compare_descriptors(
            desc_a.element, schema_a, desc_b.element, schema_b, active
        )
    if isinstance(desc_a, Ptr):
        assert isinstance(desc_b, Ptr)
        return _compare_descriptors(
            desc_a.pointee, schema_a, desc_b.pointee, schema_b, active
        )
    if isinstance(desc_a, RecordRef):
        assert isinstance(desc_b, RecordRef)
        if desc_a.kind != desc_b.kind:
            return f"record kind {desc_a.kind} != {desc_b.kind}"
        if (desc_a.rid, desc_b.rid) in active:
            return None
        if not records_equivalent(schema_a, desc_a.rid, schema_b, desc_b.rid, active):
            return f"record {desc_a.rid} layout != {desc_b.rid}"
        return None
    return f"unsupported descriptor {type(desc_a).__name__}"


def compare_modules(schema_a: ModuleSchema, schema_b: ModuleSchema) -> ComparisonReport:
    matched_b: set[str] = set()
    matches: list[RecordMatch] = []
    for rid_a, record_a in schema_a.records.items():
        rid_b = _find_partner(schema_a, rid_a, schema_b, matched_b)
        if rid_b is None:
            matches.append(RecordMatch(rid_a, None, "only_in_a"))
            continue
        matched_b.add(rid_b)
        matches.append(_diff_records(schema_a, rid_a, schema_b, rid_b))

    unmatched_a = [m.rid_a for m in matches if m.verdict == "only_in_a"]
    fallback: dict[str, str] = {}
    for rid_a in unmatched_a:
        rid_b = _find_similar(schema_a, rid_a, schema_b, matched_b)
        if rid_b is not None:
            fallback[rid_a] = rid_b
            matched_b.add(rid_b)
    if fallback:
        matches = [m for m in matches if m.rid_a not in fallback]
        for rid_a, rid_b in fallback.items():
            matches.append(_diff_records(schema_a, rid_a, schema_b, rid_b))

    for rid_b in schema_b.records:
        if rid_b not in matched_b:
            matches.append(RecordMatch(None, rid_b, "only_in_b"))
    return ComparisonReport(
        source_a=schema_a.source,
        source_b=schema_b.source,
        record_matches=matches,
        alias_rows=_alias_rows(schema_a, schema_b),
    )


def _find_similar(
    schema_a: ModuleSchema,
    rid_a: str,
    schema_b: ModuleSchema,
    unavailable: set[str],
) -> str | None:
    """Best-effort, spelling-free pairing for an otherwise unmatched record.

    Candidates must share the record kind and field count (same shape); the
    one with the most layout-equal fields wins. Paired records are still
    reported as incompatible, so this never admits a mismatch into the merge
    table.
    """
    record_a = schema_a.records[rid_a]
    best_rid: str | None = None
    best_score = -1
    for rid_b, record_b in schema_b.records.items():
        if rid_b in unavailable or record_b.kind != record_a.kind:
            continue
        if len(record_a.fields) != len(record_b.fields):
            continue
        score = sum(
            1
            for field_a, field_b in zip(record_a.fields, record_b.fields)
            if _descriptors_equal(
                field_a.descriptor,
                schema_a,
                field_b.descriptor,
                schema_b,
                ((rid_a, rid_b),),
            )
        )
        if score > best_score:
            best_score = score
            best_rid = rid_b
    return best_rid


def _find_partner(
    schema_a: ModuleSchema,
    rid_a: str,
    schema_b: ModuleSchema,
    already_matched: set[str],
) -> str | None:
    for rid_b in schema_b.records:
        if rid_b in already_matched:
            continue
        if records_equivalent(schema_a, rid_a, schema_b, rid_b):
            return rid_b
    return None


def _diff_records(
    schema_a: ModuleSchema,
    rid_a: str,
    schema_b: ModuleSchema,
    rid_b: str,
) -> RecordMatch:
    record_a = schema_a.records[rid_a]
    record_b = schema_b.records[rid_b]
    rows: list[FieldRow] = []
    total = max(len(record_a.fields), len(record_b.fields))
    incompatible = False
    for index in range(total):
        field_a = record_a.fields[index] if index < len(record_a.fields) else None
        field_b = record_b.fields[index] if index < len(record_b.fields) else None
        if field_a is None:
            rows.append(
                FieldRow(
                    index,
                    None,
                    field_b.name,
                    "only_in_b",
                    layout_b=field_b.descriptor.to_dict(),
                )
            )
            incompatible = True
            continue
        if field_b is None:
            rows.append(
                FieldRow(
                    index,
                    field_a.name,
                    None,
                    "only_in_a",
                    layout_a=field_a.descriptor.to_dict(),
                )
            )
            incompatible = True
            continue
        reason = _compare_descriptors(
            field_a.descriptor,
            schema_a,
            field_b.descriptor,
            schema_b,
            ((rid_a, rid_b),),
        )
        if reason is None and field_a.name == field_b.name:
            status = "same"
        elif reason is None:
            status = "renamed_layout_compatible"
        else:
            status = "type_mismatch"
            incompatible = True
        rows.append(
            FieldRow(
                index,
                field_a.name,
                field_b.name,
                status,
                reason,
                field_a.descriptor.to_dict(),
                field_b.descriptor.to_dict(),
            )
        )
    verdict = "incompatible" if incompatible else "mergeable"
    reason = "field layout differs" if incompatible else None
    return RecordMatch(rid_a, rid_b, verdict, reason, rows)


def _alias_rows(schema_a: ModuleSchema, schema_b: ModuleSchema) -> list[dict]:
    rows: list[dict] = []
    names = sorted(set(schema_a.aliases) | set(schema_b.aliases))
    for name in names:
        alias_a = schema_a.aliases.get(name)
        alias_b = schema_b.aliases.get(name)
        if alias_a is None or alias_b is None:
            status = "only_in_one"
            equivalent = False
        else:
            equivalent = _descriptors_equal(
                alias_a.descriptor, schema_a, alias_b.descriptor, schema_b, ()
            )
            status = "layout_equal" if equivalent else "layout_differs"
        rows.append(
            {
                "alias": name,
                "status": status,
                "layout_equal": equivalent,
                "layout_a": alias_a.descriptor.to_dict() if alias_a else None,
                "layout_b": alias_b.descriptor.to_dict() if alias_b else None,
            }
        )
    return rows
