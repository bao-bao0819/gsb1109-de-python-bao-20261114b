"""Audit JUnit timing data for discrepancies beyond rounding noise.

Reporters write ``time`` attributes with different precision (one source may
round to milliseconds, another to microseconds).  A plain float comparison of
a suite's ``time`` against the sum of its children therefore flags harmless
rounding as "double counting".  This module reads the *raw* ``time`` strings,
turns each into an interval of ``value ± half a rounding unit`` (using exact
fraction arithmetic), and checks whether the difference between a suite and
its sequentially executed children can still be explained by rounding plus a
known preparation-time range.

A missing ``time`` attribute is *unknown* — it is never treated as a real
``0.000``.  When sibling suites each look fine but their shared parent cannot
contain them, the audit reports the subset of children causing the
contradiction together with the minimal exceeded time.
"""

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterator, List, Optional, Tuple

from .junitparser import JUnitXml, JUnitXmlError, TestCase, TestSuite

#: Audit verdicts.
OK = "ok"
EXCEEDED = "exceeded"
SHORTFALL = "shortfall"
UNKNOWN = "unknown"

_TIME_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<int>\d+)(?:\.(?P<frac>\d+))?(?:[eE](?P<exp>[+-]?\d+))?$"
)


@dataclass(frozen=True)
class TimeInterval:
    """An exact interval ``[low, high]`` a raw ``time`` string may represent."""

    low: Fraction
    high: Fraction
    raw: str

    @property
    def value(self) -> Fraction:
        """The midpoint (nominal) value of the interval."""
        return (self.low + self.high) / 2


@dataclass
class ChildTiming:
    """Timing of a direct child (testcase or testsuite) of a suite."""

    name: str
    kind: str  # "testcase" or "testsuite"
    interval: Optional[TimeInterval]  # None means the time is unknown


@dataclass
class SuiteFinding:
    """Result of auditing one suite (or the ``<testsuites>`` root)."""

    path: Tuple[str, ...]
    status: str
    suite_interval: Optional[TimeInterval]
    children: List[ChildTiming] = field(default_factory=list)
    unknown_children: List[ChildTiming] = field(default_factory=list)
    sum_low: Optional[Fraction] = None
    sum_high: Optional[Fraction] = None
    prep_min: Fraction = Fraction(0)
    prep_max: Fraction = Fraction(0)
    min_excess: Optional[Fraction] = None
    min_shortfall: Optional[Fraction] = None
    culprit: Tuple[ChildTiming, ...] = ()
    culprit_min_excess: Optional[Fraction] = None

    @property
    def name(self) -> str:
        return self.path[-1] if self.path else ""


def parse_time_interval(raw: str) -> TimeInterval:
    """Parse a raw ``time`` string into its rounding interval.

    The number of decimals (adjusted for any exponent) determines the
    rounding unit; the true value lies within half a unit of the written
    value.  The lower bound is clamped at zero since durations cannot be
    negative.  Raises :class:`JUnitXmlError` for unparseable values.
    """
    text = raw.replace(",", "").strip()
    match = _TIME_RE.match(text)
    if match is None:
        raise JUnitXmlError(f"Unparseable time value: {raw!r}")
    frac_digits = len(match.group("frac") or "")
    exponent = int(match.group("exp") or 0)
    value = Fraction(text)
    scale = exponent - frac_digits
    if scale >= 0:
        half_unit = Fraction(10**scale, 2)
    else:
        half_unit = Fraction(1, 2 * 10 ** (-scale))
    return TimeInterval(
        low=max(Fraction(0), value - half_unit), high=value + half_unit, raw=raw
    )


def read_time_interval(element) -> Optional[TimeInterval]:
    """Read the raw ``time`` attribute of an element as an interval.

    Returns ``None`` when the attribute is missing — an unknown duration,
    which is deliberately distinct from a genuine ``0.000``.  Unlike
    :attr:`TestSuite.time`, this never recomputes statistics.
    """
    raw = element._elem.attrib.get("time")
    if raw is None:
        return None
    return parse_time_interval(raw)


def _minimal_culprit(children, prep_min, suite_high):
    """Find a smallest subset of children that alone overflows the suite.

    Children are considered by their most favourable (lowest) possible
    durations; the subset is minimal in cardinality because the largest
    contributors are taken first.
    """
    ordered = sorted(children, key=lambda child: child.interval.low, reverse=True)
    culprit = []
    total = prep_min
    for child in ordered:
        culprit.append(child)
        total += child.interval.low
        if total > suite_high:
            return tuple(culprit), total - suite_high
    return (), None  # pragma: no cover - only called when a culprit exists


def _audit_node(name, path, interval, children, prep_min, prep_max):
    unknown = [child for child in children if child.interval is None]
    known = [child for child in children if child.interval is not None]
    sum_low = sum((child.interval.low for child in known), Fraction(0))
    sum_high = sum((child.interval.high for child in known), Fraction(0))
    finding = SuiteFinding(
        path=path,
        status=UNKNOWN,
        suite_interval=interval,
        children=children,
        unknown_children=unknown,
        sum_low=sum_low,
        sum_high=sum_high,
        prep_min=prep_min,
        prep_max=prep_max,
    )
    if interval is None:
        return finding
    required_low = sum_low + prep_min
    if required_low > interval.high:
        # Certain even with unknown children: known ones already overflow.
        finding.status = EXCEEDED
        finding.min_excess = required_low - interval.high
        finding.culprit, finding.culprit_min_excess = _minimal_culprit(
            known, prep_min, interval.high
        )
    elif unknown:
        finding.status = UNKNOWN
    elif sum_high + prep_max < interval.low:
        finding.status = SHORTFALL
        finding.min_shortfall = interval.low - (sum_high + prep_max)
    else:
        finding.status = OK
    return finding


def _child_timings(suite) -> List[ChildTiming]:
    children = []
    for case in suite.iterchildren(TestCase):
        children.append(
            ChildTiming(
                name=case.name or "<unnamed>",
                kind="testcase",
                interval=read_time_interval(case),
            )
        )
    for child_suite in suite.testsuites():
        children.append(
            ChildTiming(
                name=child_suite.name or "<unnamed>",
                kind="testsuite",
                interval=read_time_interval(child_suite),
            )
        )
    return children


def _audit_suite(suite, prep_min, prep_max, path) -> Iterator[SuiteFinding]:
    children = _child_timings(suite)
    yield _audit_node(
        suite.name or "<unnamed>",
        path,
        read_time_interval(suite),
        children,
        prep_min,
        prep_max,
    )
    for child_suite in suite.testsuites():
        child_path = path + (child_suite.name or "<unnamed>",)
        yield from _audit_suite(child_suite, prep_min, prep_max, child_path)


def audit_timing(obj, prep_range=(Fraction(0), Fraction(0))) -> List[SuiteFinding]:
    """Audit the timing consistency of a :class:`JUnitXml` or :class:`TestSuite`.

    ``prep_range`` is a ``(min, max)` pair of seconds allowed for preparation
    work on top of the children's durations (e.g. fixture setup measured by
    the suite but not by any case).  Returns one :class:`SuiteFinding` per
    suite, parents before children.
    """
    prep_min, prep_max = (Fraction(p) for p in prep_range)
    if prep_min > prep_max:
        raise ValueError("prep_range min must not exceed max.")
    if isinstance(obj, TestSuite):
        name = obj.name or "<unnamed>"
        return list(_audit_suite(obj, prep_min, prep_max, (name,)))
    if isinstance(obj, JUnitXml):
        children = []
        findings = []
        for suite in obj:
            name = suite.name or "<unnamed>"
            findings.extend(_audit_suite(suite, prep_min, prep_max, (name,)))
            children.append(
                ChildTiming(
                    name=name,
                    kind="testsuite",
                    interval=read_time_interval(suite),
                )
            )
        root_name = obj._elem.attrib.get("name") or "<testsuites>"
        findings.insert(
            0,
            _audit_node(
                root_name,
                (root_name,),
                read_time_interval(obj),
                children,
                prep_min,
                prep_max,
            ),
        )
        return findings
    raise TypeError(f"Cannot audit object of type {type(obj)!r}.")


def format_seconds(value: Fraction) -> str:
    """Format a fractional number of seconds for human-readable reports."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_report(findings: List[SuiteFinding]) -> str:
    """Render audit findings as a human-readable report."""
    lines = []
    for finding in findings:
        where = " / ".join(finding.path)
        if finding.status == OK:
            lines.append(f"OK        {where}")
        elif finding.status == UNKNOWN:
            if finding.suite_interval is None:
                reason = "suite time is unknown"
            else:
                names = ", ".join(c.name for c in finding.unknown_children)
                reason = f"unknown child time(s): {names}"
            lines.append(f"UNKNOWN   {where} ({reason})")
        elif finding.status == EXCEEDED:
            culprit = ", ".join(c.name for c in finding.culprit)
            lines.append(
                f"EXCEEDED  {where}: children need at least "
                f"{format_seconds(finding.sum_low + finding.prep_min)}s "
                f"(incl. prep) but suite allows at most "
                f"{format_seconds(finding.suite_interval.high)}s; "
                f"min excess {format_seconds(finding.min_excess)}s; "
                f"contradicting subset: {culprit}"
            )
        elif finding.status == SHORTFALL:
            lines.append(
                f"SHORTFALL {where}: children plus prep cover at most "
                f"{format_seconds(finding.sum_high + finding.prep_max)}s "
                f"but suite took at least "
                f"{format_seconds(finding.suite_interval.low)}s; "
                f"min unaccounted {format_seconds(finding.min_shortfall)}s"
            )
    return "\n".join(lines)
