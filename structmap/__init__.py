"""structmap: scoped struct-layout extraction and cross-module merge tables."""

from .collector import (
    AliasInfo,
    Array,
    FieldInfo,
    FuncPtr,
    ModuleSchema,
    Prim,
    Ptr,
    Record,
    RecordRef,
    TypeDescriptor,
    Unknown,
    extract_module,
)
from .compare import (
    ComparisonReport,
    FieldRow,
    RecordMatch,
    compare_modules,
    records_equivalent,
)
from .layout import PointerClassifier, expand_record, render_schema
from .merge import business_merge_table

__all__ = [
    "AliasInfo",
    "Array",
    "ComparisonReport",
    "FieldInfo",
    "FieldRow",
    "FuncPtr",
    "ModuleSchema",
    "PointerClassifier",
    "Prim",
    "Ptr",
    "Record",
    "RecordMatch",
    "RecordRef",
    "TypeDescriptor",
    "Unknown",
    "business_merge_table",
    "compare_modules",
    "expand_record",
    "extract_module",
    "records_equivalent",
    "render_schema",
]
