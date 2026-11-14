from importlib.metadata import version as _dist_version

from .idle import (
    IdleBounds,
    InfeasibleScheduleError,
    MachineIdleBounds,
    idle_bounds,
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
    "Element",
    "Error",
    "Failure",
    "FloatAttr",
    "IdleBounds",
    "InfeasibleScheduleError",
    "IntAttr",
    "JUnitXml",
    "JUnitXmlError",
    "MachineIdleBounds",
    "Properties",
    "Property",
    "Skipped",
    "SystemOut",
    "SystemErr",
    "TestCase",
    "TestSuite",
    "idle_bounds",
    "version",
]
