from importlib.metadata import version as _dist_version

from .aggregate import (
    CaseKey,
    FailureCountChange,
    ReportConflict,
    ReportMode,
    ReportReceipt,
    ShardReportStore,
    ShardVersionError,
    merge_shard_reports,
)
from .junitparser import (
    Attr,
    Element,
    Error,
    Failure,
    FloatAttr,
    IntAttr,
    JUnitXml,
    JUnitXmlError,
    Properties,
    Property,
    Skipped,
    SystemOut,
    SystemErr,
    TestCase,
    TestSuite,
)

version = _dist_version("junitparser")

__all__ = [
    "Attr",
    "CaseKey",
    "Element",
    "Error",
    "Failure",
    "FailureCountChange",
    "FloatAttr",
    "IntAttr",
    "JUnitXml",
    "JUnitXmlError",
    "Properties",
    "Property",
    "ReportConflict",
    "ReportMode",
    "ReportReceipt",
    "ShardReportStore",
    "ShardVersionError",
    "Skipped",
    "SystemOut",
    "SystemErr",
    "TestCase",
    "TestSuite",
    "merge_shard_reports",
    "version",
]
