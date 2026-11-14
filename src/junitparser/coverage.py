"""Cross-platform test coverage computation from JUnit reports.

Given a plan matrix (which cases exist and under which capability
conditions they apply) and a platform capability table (which
capabilities each device/platform has), this module computes a
per-platform coverage figure from JUnit XML reports that is *clearly
comparable* across platforms:

* Only cases that are applicable to a platform and produced a firm
  verdict (passed / failed / errored) enter the denominator.
* Cases with unknown outcome (explicitly skipped, absent from the
  report, or the whole report never submitted) are excluded from the
  firm denominator but reported, and best/worst bounds are computed
  for the two extreme assumptions (all unknowns pass / all fail).
* Cells whose applicability conditions contradict each other
  (a capability is both required and forbidden) are listed separately
  and never enter any denominator.
* A platform/suite-level skip never erases case-level evidence that
  was actually received.
* Every platform's denominator can be traced back to the concrete
  plan items it consists of.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .junitparser import Error, Failure, JUnitXml, Skipped, TestCase, TestSuite


class Verdict(Enum):
    """Per-cell outcome of a planned case on one platform."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"  # explicitly skipped in the report
    MISSING = "missing"  # report received, but the case is absent
    NO_REPORT = "no_report"  # the platform never submitted a report


FIRM_VERDICTS = frozenset({Verdict.PASSED, Verdict.FAILED, Verdict.ERROR})
UNKNOWN_VERDICTS = frozenset({Verdict.SKIPPED, Verdict.MISSING, Verdict.NO_REPORT})


@dataclass(frozen=True)
class PlanItem:
    """One row of the plan matrix.

    ``requires``/``forbids`` are capability conditions: the item applies
    to a platform iff the platform has every required capability and none
    of the forbidden ones.
    """

    id: str
    requires: frozenset = frozenset()
    forbids: frozenset = frozenset()

    @property
    def is_contradictory(self) -> bool:
        """Whether the applicability conditions contradict each other."""
        return bool(self.requires & self.forbids)

    def applies_to(self, capabilities: frozenset) -> bool:
        return self.requires <= capabilities and not (self.forbids & capabilities)


@dataclass
class PlatformCoverage:
    """Coverage result for a single platform, fully traceable."""

    platform: str
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    errored: list = field(default_factory=list)
    unknown: dict = field(default_factory=dict)  # item id -> Verdict value
    not_applicable: list = field(default_factory=list)
    contradictory: list = field(default_factory=list)

    @property
    def denominator(self) -> list:
        """The firm denominator, as the concrete plan item ids."""
        return self.passed + self.failed + self.errored

    @property
    def applicable_total(self) -> int:
        """All applicable cells, known and unknown alike."""
        return len(self.denominator) + len(self.unknown)

    @property
    def coverage(self) -> Optional[float]:
        """Firm coverage: passed / (firm verdicts). None if undefined."""
        total = len(self.denominator)
        return len(self.passed) / total if total else None

    @property
    def coverage_best(self) -> Optional[float]:
        """Coverage if every unknown cell were a pass."""
        total = self.applicable_total
        return (len(self.passed) + len(self.unknown)) / total if total else None

    @property
    def coverage_worst(self) -> Optional[float]:
        """Coverage if every unknown cell were a failure."""
        total = self.applicable_total
        return len(self.passed) / total if total else None

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "passed": sorted(self.passed),
            "failed": sorted(self.failed),
            "errored": sorted(self.errored),
            "unknown": dict(sorted(self.unknown.items())),
            "not_applicable": sorted(self.not_applicable),
            "contradictory": sorted(self.contradictory),
            "denominator": sorted(self.denominator),
            "coverage": self.coverage,
            "coverage_best": self.coverage_best,
            "coverage_worst": self.coverage_worst,
        }


@dataclass
class CoverageReport:
    """Coverage over all platforms plus the contradictory cells."""

    platforms: dict = field(default_factory=dict)  # name -> PlatformCoverage

    @property
    def contradictory(self) -> list:
        """Contradictory cells, listed separately (same for all platforms)."""
        seen = []
        for cov in self.platforms.values():
            for item_id in cov.contradictory:
                if item_id not in seen:
                    seen.append(item_id)
        return sorted(seen)

    def to_dict(self) -> dict:
        return {
            "platforms": {
                name: cov.to_dict() for name, cov in sorted(self.platforms.items())
            },
            "contradictory": self.contradictory,
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def _case_keys(case: TestCase) -> list:
    keys = []
    if case.classname:
        keys.append(f"{case.classname}.{case.name}")
    keys.append(case.name)
    return keys


def _suite_fully_skipped(suite: TestSuite) -> bool:
    """Whether the suite carries a platform/suite-level skip marker."""
    tests, skipped = suite.tests, suite.skipped
    if tests is not None and skipped is not None:
        return tests > 0 and skipped >= tests
    return False


def _case_verdict(case: TestCase, suite_skipped: bool) -> Verdict:
    """Verdict of a single case.

    Case-level evidence always wins; a suite-level skip only applies to
    cases that carry no evidence of their own.
    """
    if case.is_failure:
        return Verdict.FAILED
    if case.is_error:
        return Verdict.ERROR
    if case.is_skipped:
        return Verdict.SKIPPED
    if suite_skipped:
        return Verdict.SKIPPED
    return Verdict.PASSED


# Evidence precedence when a case id appears more than once in a report:
# a firm verdict beats a skip, and a failure beats a pass.
_PRECEDENCE = {
    Verdict.FAILED: 3,
    Verdict.ERROR: 3,
    Verdict.PASSED: 2,
    Verdict.SKIPPED: 1,
}


def _merge_verdict(old: Optional[Verdict], new: Verdict) -> Verdict:
    if old is None:
        return new
    return new if _PRECEDENCE[new] > _PRECEDENCE[old] else old


def read_report_verdicts(path) -> dict:
    """Read a JUnit XML report into a {case key: Verdict} mapping."""
    xml = JUnitXml.fromfile(str(path))
    suites = [xml] if isinstance(xml, TestSuite) else list(xml)
    verdicts = {}
    for suite in suites:
        suite_skipped = _suite_fully_skipped(suite)
        for case in suite:
            verdict = _case_verdict(case, suite_skipped)
            for key in _case_keys(case):
                verdicts[key] = _merge_verdict(verdicts.get(key), verdict)
    return verdicts


def _lookup(verdicts: Mapping, item_id: str) -> Optional[Verdict]:
    if item_id in verdicts:
        return verdicts[item_id]
    # Fall back to matching by the bare test name.
    bare = item_id.rsplit(".", 1)[-1]
    return verdicts.get(bare)


def compute_coverage(
    plan: Sequence[PlanItem],
    capabilities: Mapping[str, Sequence[str]],
    reports: Mapping[str, Optional[object]],
) -> CoverageReport:
    """Compute per-platform coverage.

    ``plan`` is the plan matrix, ``capabilities`` maps platform name to
    its capability list, and ``reports`` maps platform name to a JUnit
    XML path (or ``None``/absent when the device never submitted one).
    """
    verdicts_by_platform = {
        platform: (read_report_verdicts(path) if path is not None else None)
        for platform, path in reports.items()
    }
    report = CoverageReport()
    for platform, caps in capabilities.items():
        caps = frozenset(caps)
        verdicts = verdicts_by_platform.get(platform)
        cov = PlatformCoverage(platform=platform)
        for item in plan:
            if item.is_contradictory:
                cov.contradictory.append(item.id)
                continue
            if not item.applies_to(caps):
                cov.not_applicable.append(item.id)
                continue
            if verdicts is None:
                cov.unknown[item.id] = Verdict.NO_REPORT.value
                continue
            verdict = _lookup(verdicts, item.id)
            if verdict is None:
                cov.unknown[item.id] = Verdict.MISSING.value
            elif verdict is Verdict.PASSED:
                cov.passed.append(item.id)
            elif verdict is Verdict.FAILED:
                cov.failed.append(item.id)
            elif verdict is Verdict.ERROR:
                cov.errored.append(item.id)
            else:  # SKIPPED
                cov.unknown[item.id] = Verdict.SKIPPED.value
        report.platforms[platform] = cov
    return report


def load_plan(path) -> list:
    """Load a plan matrix from JSON.

    Expected shape: ``[{"id": ..., "requires": [...], "forbids": [...]}]``.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        PlanItem(
            id=entry["id"],
            requires=frozenset(entry.get("requires", [])),
            forbids=frozenset(entry.get("forbids", [])),
        )
        for entry in raw
    ]


def load_capabilities(path) -> dict:
    """Load a capability table from JSON: ``{"platform": ["cap", ...]}``."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="junitparser-coverage",
        description="Compute comparable per-platform coverage from JUnit reports.",
    )
    parser.add_argument("--plan", required=True, help="Plan matrix JSON file")
    parser.add_argument(
        "--capabilities", required=True, help="Platform capability table JSON file"
    )
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="PLATFORM=PATH",
        help="JUnit report for a platform; repeatable. Platforms in the "
        "capability table without a report are treated as not submitted.",
    )
    args = parser.parse_args(argv)

    reports = {}
    for spec in args.report:
        platform, sep, path = spec.partition("=")
        if not sep:
            parser.error(f"--report must be PLATFORM=PATH, got: {spec!r}")
        reports[platform] = path

    plan = load_plan(args.plan)
    capabilities = load_capabilities(args.capabilities)
    report = compute_coverage(plan, capabilities, reports)
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
