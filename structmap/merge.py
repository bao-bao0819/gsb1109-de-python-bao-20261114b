"""Business-level merge table built from layout-equivalent record pairs."""

from dataclasses import dataclass

from .collector import ModuleSchema
from .compare import ComparisonReport, FieldRow, compare_modules


@dataclass
class BusinessField:
    index: int
    canonical_name: str
    name_in_a: str
    name_in_b: str
    merged_descriptor: dict
    note: str | None


@dataclass
class BusinessType:
    business_name: str
    rid_a: str
    rid_b: str
    fields: list[BusinessField]


def business_merge_table(
    schema_a: ModuleSchema,
    schema_b: ModuleSchema,
    report: ComparisonReport | None = None,
) -> list[BusinessType]:
    """One row group per mergeable record pair.

    Only layout-equivalent records enter the table; incompatible and
    one-sided records stay out so the business schema never silently drops a
    mismatch.
    """
    report = report or compare_modules(schema_a, schema_b)
    table: list[BusinessType] = []
    for match in report.record_matches:
        if match.verdict != "mergeable":
            continue
        assert match.rid_a is not None and match.rid_b is not None
        fields = [
            _business_field(row)
            for row in match.fields
            if row.status in ("same", "renamed_layout_compatible")
        ]
        table.append(
            BusinessType(
                business_name=match.rid_a,
                rid_a=match.rid_a,
                rid_b=match.rid_b,
                fields=fields,
            )
        )
    return table


def _business_field(row: FieldRow) -> BusinessField:
    note = None
    if row.status == "renamed_layout_compatible":
        note = f"field renamed: {row.name_a} -> {row.name_b}"
    return BusinessField(
        index=row.index,
        canonical_name=row.name_a or row.name_b or "",
        name_in_a=row.name_a or "",
        name_in_b=row.name_b or "",
        merged_descriptor=row.layout_a if row.layout_a is not None else {},
        note=note,
    )


def table_to_dict(table: list[BusinessType]) -> list[dict]:
    return [
        {
            "business_name": item.business_name,
            "rid_a": item.rid_a,
            "rid_b": item.rid_b,
            "fields": [
                {
                    "index": field.index,
                    "canonical_name": field.canonical_name,
                    "name_in_a": field.name_in_a,
                    "name_in_b": field.name_in_b,
                    "merged_descriptor": field.merged_descriptor,
                    "note": field.note,
                }
                for field in item.fields
            ],
        }
        for item in table
    ]
