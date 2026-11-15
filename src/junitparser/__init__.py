from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

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
from .triage import (
    CONTINUE,
    END,
    SPLIT,
    AlertDecision,
    Edge,
    FailureSample,
    TriageReport,
    triage,
)

try:
    version = _dist_version("junitparser")
except PackageNotFoundError:  # running straight from a source checkout
    version = "0.0.0"

__all__ = [
    "Attr",
    "Element",
    "Error",
    "Failure",
    "FloatAttr",
    "IntAttr",
    "JUnitXml",
    "JUnitXmlError",
    "Properties",
    "Property",
    "Skipped",
    "SystemOut",
    "SystemErr",
    "TestCase",
    "TestSuite",
    "CONTINUE",
    "END",
    "SPLIT",
    "AlertDecision",
    "Edge",
    "FailureSample",
    "TriageReport",
    "triage",
    "version",
]
