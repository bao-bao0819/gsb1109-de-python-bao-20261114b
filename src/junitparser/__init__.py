from importlib.metadata import version as _dist_version

from .inheritance import (
    CaseDimensions,
    InheritanceConfig,
    InheritanceMode,
    InheritanceResult,
    PropertyConflict,
    PropertySource,
    ResolvedDimension,
    resolve_properties,
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
    "CaseDimensions",
    "Element",
    "Error",
    "Failure",
    "FloatAttr",
    "InheritanceConfig",
    "InheritanceMode",
    "InheritanceResult",
    "IntAttr",
    "JUnitXml",
    "JUnitXmlError",
    "Properties",
    "Property",
    "PropertyConflict",
    "PropertySource",
    "ResolvedDimension",
    "Skipped",
    "SystemOut",
    "SystemErr",
    "TestCase",
    "TestSuite",
    "resolve_properties",
    "version",
]
