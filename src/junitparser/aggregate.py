"""Versioned aggregation of per-shard JUnit reports.

A day's results are written one shard (device) at a time, and the same shard
may hand in several reports for the same day: an incomplete one first, a
completed one later, sometimes one that withdraws cases that were run by
mistake. This module gives the aggregation side a store that accepts JUnit
reports keyed by *shard* and *version* instead of blindly merging files.

Two ingestion modes are supported:

* :attr:`ReportMode.REPLACE` -- the report is the full truth for the shard.
  Cases missing from the report are withdrawn, so a late report can never
  resurrect a case that a newer version already withdrew.
* :attr:`ReportMode.APPEND` -- the report only carries additional cases.
  Existing cases are kept unless the incoming report explicitly replaces them.

The effective state of a shard is obtained by folding its accepted reports in
version order (REPLACE resets the case set, APPEND updates it). Because the
fold follows version numbers rather than arrival order, replaying the same
group of reports in reverse order reconciles to the same per-shard counts. A
report that arrives late with an older version is retained as evidence and
takes its place in the version-ordered fold, but any case it reintroduces is
wiped again by the newer REPLACE report that withdrew it -- so an outdated
report can never resurrect a withdrawn case.

Reports with the same shard and version but conflicting contents do not
overwrite each other: both copies are retained as evidence and the conflict is
recorded on the receipt. When a version has conflicting copies, the one with
the smallest content hash feeds the fold, which keeps reconciliation
independent of arrival order.
"""

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import IO, Iterable

from .junitparser import JUnitXml, TestCase, write_xml

try:
    from lxml import etree
except ImportError:
    from xml.etree import ElementTree as etree


class ShardVersionError(Exception):
    """Raised when a shard version cannot be compared or diffed."""


class ReportMode(str, Enum):
    """How a report relates to the shard's previous versions."""

    REPLACE = "replace"
    APPEND = "append"


class ReportConflict(str, Enum):
    """Outcome of ingesting a report whose (shard, version) already exists."""

    NONE = "none"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CaseKey:
    """Identity of a testcase within a shard."""

    classname: str | None
    name: str | None

    @property
    def case_id(self) -> str:
        return f"{self.classname or ''}.{self.name or ''}"


@dataclass(frozen=True)
class ReportReceipt:
    """Record of one ingested report.

    Attributes:
        shard: Shard/device the report belongs to.
        version: Version of the shard report.
        mode: Ingestion mode used for the report.
        sequence: Global ingestion order, assigned by the store.
        conflict: Whether the report duplicated or conflicted with an earlier
            report at the same version.
        evidence: The parsed JUnit report, retained as evidence.
        content_hash: SHA-256 of the report's serialized XML content.

    Whether a receipt currently feeds the shard's effective state depends on
    conflict resolution; ask the store via
    :meth:`ShardReportStore.applied_receipts`. Every distinct version takes
    part in the version-ordered fold, including reports that arrive late.
    """

    shard: str
    version: str
    mode: ReportMode
    sequence: int
    conflict: ReportConflict
    evidence: JUnitXml
    content_hash: str

    @property
    def stats(self) -> dict[str, int]:
        """Counts computed from the report's testcases (not its attributes)."""
        return _count_cases(self.evidence)

    @property
    def failures(self) -> int:
        """Number of failed or errored testcases in the report."""
        stats = self.stats
        return stats["failures"] + stats["errors"]


@dataclass(frozen=True)
class FailureCountChange:
    """Why the failure count moved between two adjacent shard versions."""

    shard: str
    from_version: str | None
    to_version: str
    before: int
    after: int
    added: tuple[CaseKey, ...]
    regressed: tuple[CaseKey, ...]
    resolved: tuple[CaseKey, ...]
    withdrawn: tuple[CaseKey, ...]
    status_changed: tuple[tuple[CaseKey, str, str], ...]

    @property
    def delta(self) -> int:
        return self.after - self.before

    def reasons(self) -> list[str]:
        """Human-readable lines explaining the count change."""
        lines = [
            f"{self.shard}: failure count {self.before} -> {self.after} "
            f"({self.delta:+d}) between "
            f"{self.from_version or '<none>'} and {self.to_version}"
        ]
        for key in self.added:
            lines.append(f"  + new failing case: {key.case_id}")
        for key in self.regressed:
            lines.append(f"  ^ previously passing, now failing: {key.case_id}")
        for key in self.resolved:
            lines.append(f"  - no longer failing: {key.case_id}")
        for key in self.withdrawn:
            lines.append(f"  ! withdrawn from the shard report: {key.case_id}")
        for key, old, new in self.status_changed:
            lines.append(f"  ~ {key.case_id}: {old} -> {new}")
        if len(lines) == 1:
            lines.append("  no failing case changed")
        return lines


@dataclass
class _ShardState:
    receipts: list[ReportReceipt] = field(default_factory=list)
    current: dict[CaseKey, TestCase] = field(default_factory=dict)


def _version_key(version: str) -> tuple:
    """Split a version string into comparable numeric/text chunks.

    ``"v2" < "v10"`` because the numeric chunks are compared as integers.
    """
    key = []
    for part in re.split(r"(\d+)", version):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _case_key(case: TestCase) -> CaseKey:
    return CaseKey(case.classname, case.name)


def _case_category(case: TestCase) -> str:
    if case.is_failure:
        return "failure"
    if case.is_error:
        return "error"
    if case.is_skipped:
        return "skipped"
    return "passed"


def _iter_cases(xml: JUnitXml) -> Iterable[TestCase]:
    for suite in xml:
        for case in suite:
            yield case


def _count_cases(xml: JUnitXml) -> dict[str, int]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for case in _iter_cases(xml):
        counts["tests"] += 1
        if case.is_failure:
            counts["failures"] += 1
        elif case.is_error:
            counts["errors"] += 1
        elif case.is_skipped:
            counts["skipped"] += 1
    return counts


def _content_hash(xml: JUnitXml) -> str:
    return hashlib.sha256(etree.tostring(xml._elem)).hexdigest()


def _is_failing(case: TestCase) -> bool:
    return case.is_failure or case.is_error


class ShardReportStore:
    """Collects versioned JUnit reports, one shard at a time.

    The store keeps every accepted report as evidence, tracks the effective
    state of each shard, and can reconcile the shards back into a single
    :class:`JUnitXml` report.
    """

    def __init__(self):
        self._shards: dict[str, _ShardState] = {}
        self._sequence = 0

    def _state(self, shard: str) -> _ShardState:
        if shard not in self._shards:
            self._shards[shard] = _ShardState()
        return self._shards[shard]

    def ingest(
        self,
        shard: str,
        version: str,
        report: JUnitXml | str | bytes | IO,
        mode: ReportMode | str = ReportMode.REPLACE,
    ) -> ReportReceipt:
        """Accept a JUnit report for ``(shard, version)``.

        ``report`` may be a parsed :class:`JUnitXml`, an XML string/bytes, or a
        file-like object. Files on disk should be parsed with
        :meth:`ingest_file`.

        Same-version reports never overwrite each other. Identical content is
        acknowledged as a duplicate; conflicting content is retained as a
        second piece of evidence and flagged on the returned receipt.

        A report whose version is older than the shard's newest version is
        accepted normally and slotted into the version-ordered fold. It cannot
        resurrect a withdrawn case, because the newer REPLACE report is folded
        after it and removes anything the older report brought back.
        """
        mode = ReportMode(mode)
        xml = self._coerce_report(report)
        state = self._state(shard)
        digest = _content_hash(xml)

        conflict = ReportConflict.NONE
        for existing in state.receipts:
            if existing.version != version:
                continue
            conflict = (
                ReportConflict.DUPLICATE
                if existing.content_hash == digest
                else ReportConflict.CONFLICT
            )
            break

        receipt = ReportReceipt(
            shard=shard,
            version=version,
            mode=mode,
            sequence=self._sequence,
            conflict=conflict,
            evidence=xml,
            content_hash=digest,
        )
        self._sequence += 1
        state.receipts.append(receipt)
        state.current = self._fold(state)
        return receipt

    def ingest_file(
        self,
        shard: str,
        version: str,
        file: str | Path | IO,
        mode: ReportMode | str = ReportMode.REPLACE,
    ) -> ReportReceipt:
        """Ingest a report read from a file path or file object."""
        return self.ingest(shard, version, JUnitXml.fromfile(file), mode)

    @staticmethod
    def _coerce_report(report: JUnitXml | str | bytes | IO) -> JUnitXml:
        if isinstance(report, JUnitXml):
            return report
        if isinstance(report, (str, bytes)):
            return JUnitXml.fromstring(report)
        return JUnitXml.fromfile(report)

    def _newest_version(self, state: _ShardState) -> str | None:
        applied = self._applied_receipts(state)
        if not applied:
            return None
        return applied[-1].version

    def applied_receipts(self, shard: str) -> list[ReportReceipt]:
        """Receipts that feed the shard's fold, ordered by version.

        One winner per version (see :meth:`conflicts`); losing conflict copies
        are retained via :meth:`receipts` but absent here. Reports with
        distinct older versions are included -- their effects are simply
        superseded by later REPLACE reports during the fold.
        """
        return self._applied_receipts(self._state(shard))

    def _applied_receipts(self, state: _ShardState) -> list[ReportReceipt]:
        """One winning receipt per version, ordered by version.

        Same-version conflicts keep every copy as evidence, but only the copy
        with the smallest content hash feeds the fold. Picking the winner by
        content rather than arrival order makes reconciliation deterministic.
        """
        winners: dict[str, ReportReceipt] = {}
        for receipt in state.receipts:
            current = winners.get(receipt.version)
            if current is None or receipt.content_hash < current.content_hash:
                winners[receipt.version] = receipt
        return sorted(
            winners.values(), key=lambda receipt: _version_key(receipt.version)
        )

    def _fold(self, state: _ShardState) -> dict[CaseKey, TestCase]:
        current: dict[CaseKey, TestCase] = {}
        for receipt in self._applied_receipts(state):
            incoming = {
                _case_key(case): deepcopy(case)
                for case in _iter_cases(receipt.evidence)
            }
            if receipt.mode is ReportMode.REPLACE:
                current = incoming
            else:
                current.update(incoming)
        return current

    def receipts(self, shard: str | None = None) -> list[ReportReceipt]:
        """All retained receipts, in ingestion order (newest last)."""
        if shard is not None:
            return list(self._state(shard).receipts)
        ordered: list[ReportReceipt] = []
        for state in self._shards.values():
            ordered.extend(state.receipts)
        return sorted(ordered, key=lambda receipt: receipt.sequence)

    def conflicts(self, shard: str | None = None) -> list[ReportReceipt]:
        """Receipts whose same-version content conflicted with an earlier one."""
        return [
            receipt
            for receipt in self.receipts(shard)
            if receipt.conflict is ReportConflict.CONFLICT
        ]

    def shards(self) -> list[str]:
        """All known shard names."""
        return list(self._shards)

    def current_version(self, shard: str) -> str | None:
        """Newest applied version the shard's effective state reflects."""
        return self._newest_version(self._state(shard))

    def effective_cases(self, shard: str) -> dict[CaseKey, TestCase]:
        """Live case set of the shard after folding all accepted versions."""
        return dict(self._state(shard).current)

    def failure_count(self, shard: str) -> int:
        """Failing (failure or error) cases in the shard's current state."""
        return sum(
            1 for case in self._state(shard).current.values() if _is_failing(case)
        )

    def _version_snapshots(
        self, shard: str
    ) -> list[tuple[ReportReceipt, dict[CaseKey, TestCase]]]:
        state = self._state(shard)
        snapshots: list[tuple[ReportReceipt, dict[CaseKey, TestCase]]] = []
        current: dict[CaseKey, TestCase] = {}
        for receipt in self._applied_receipts(state):
            incoming = {
                _case_key(case): deepcopy(case)
                for case in _iter_cases(receipt.evidence)
            }
            if receipt.mode is ReportMode.REPLACE:
                current = incoming
            else:
                current.update(incoming)
            snapshots.append((receipt, dict(current)))
        return snapshots

    def failure_diff(
        self, shard: str, from_version: str | None = None
    ) -> FailureCountChange:
        """Explain the failure-count change between two adjacent versions.

        By default the two newest applied versions of the shard are compared.
        Pass ``from_version`` to select the earlier side explicitly; the later
        side is the next applied version.
        """
        snapshots = self._version_snapshots(shard)
        if not snapshots:
            raise ShardVersionError(f"No reports ingested for shard {shard!r}")

        if from_version is None:
            if len(snapshots) < 2:
                raise ShardVersionError(
                    f"Shard {shard!r} needs at least two versions to diff"
                )
            old_index = len(snapshots) - 2
        else:
            old_index = next(
                (
                    index
                    for index, (receipt, _) in enumerate(snapshots)
                    if receipt.version == from_version
                ),
                None,
            )
            if old_index is None:
                raise ShardVersionError(
                    f"Unknown applied version {from_version!r} for shard {shard!r}"
                )
            if old_index + 1 >= len(snapshots):
                raise ShardVersionError(
                    f"No version newer than {from_version!r} for shard {shard!r}"
                )

        old_receipt, old_cases = snapshots[old_index]
        new_receipt, new_cases = snapshots[old_index + 1]
        return self._diff_snapshots(
            shard, old_receipt.version, old_cases, new_receipt.version, new_cases
        )

    @staticmethod
    def _diff_snapshots(
        shard: str,
        old_version: str,
        old_cases: dict[CaseKey, TestCase],
        new_version: str,
        new_cases: dict[CaseKey, TestCase],
    ) -> FailureCountChange:
        old_failing = {key for key, case in old_cases.items() if _is_failing(case)}
        new_failing = {key for key, case in new_cases.items() if _is_failing(case)}

        added = tuple(
            sorted(new_failing - old_cases.keys(), key=lambda key: key.case_id)
        )
        regressed = tuple(
            sorted(
                (key for key in new_failing - old_failing if key in old_cases),
                key=lambda key: key.case_id,
            )
        )
        resolved = tuple(
            sorted(
                (
                    key
                    for key in old_failing - new_failing
                    if key in new_cases and not _is_failing(new_cases[key])
                ),
                key=lambda key: key.case_id,
            )
        )
        withdrawn = tuple(
            sorted(old_failing - new_cases.keys(), key=lambda key: key.case_id)
        )

        changed: list[tuple[CaseKey, str, str]] = []
        for key in sorted(old_failing & new_failing, key=lambda item: item.case_id):
            old_category = _case_category(old_cases[key])
            new_category = _case_category(new_cases[key])
            if old_category != new_category:
                changed.append((key, old_category, new_category))

        return FailureCountChange(
            shard=shard,
            from_version=old_version,
            to_version=new_version,
            before=len(old_failing),
            after=len(new_failing),
            added=added,
            regressed=regressed,
            resolved=resolved,
            withdrawn=withdrawn,
            status_changed=tuple(changed),
        )

    def reconcile_stats(self) -> dict[str, dict[str, int]]:
        """Per-shard counts of the effective state, used for reconciliation."""
        stats: dict[str, dict[str, int]] = {}
        for shard in sorted(self._shards):
            counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            for case in self._shards[shard].current.values():
                counts["tests"] += 1
                if case.is_failure:
                    counts["failures"] += 1
                elif case.is_error:
                    counts["errors"] += 1
                elif case.is_skipped:
                    counts["skipped"] += 1
            stats[shard] = counts
        return stats

    def reconcile_shard(self, shard: str) -> JUnitXml:
        """Build a single-suite :class:`JUnitXml` from one shard's state."""
        state = self._state(shard)
        xml = JUnitXml()
        suite = xml.testsuite(shard)
        for key in sorted(state.current, key=lambda item: item.case_id):
            suite._add_testcase_no_update_stats(deepcopy(state.current[key]))
        xml.add_testsuite(suite)
        xml.update_statistics()
        return xml

    def reconcile(self) -> JUnitXml:
        """Build one :class:`JUnitXml` from every shard's effective state.

        Each shard becomes one testsuite named after the shard. The result only
        depends on the folded effective state, so ingesting the same set of
        reports in reverse order reconciles to the same per-shard counts.
        """
        merged = JUnitXml()
        for shard in sorted(self._shards):
            merged += self.reconcile_shard(shard)
        merged.update_statistics()
        return merged

    def write_reconciled(
        self,
        file_or_filename: str | IO | Path | None = None,
        *,
        pretty: bool = False,
    ) -> None:
        """Reconcile the shards and write the result as a JUnit XML file."""
        write_xml(self.reconcile(), file_or_filename, pretty=pretty)


def merge_shard_reports(
    entries: Iterable[tuple[str, str, JUnitXml | str | bytes]],
    mode: ReportMode | str = ReportMode.REPLACE,
) -> ShardReportStore:
    """Ingest a group of ``(shard, version, report)`` entries in order.

    Useful for replaying an ordered (or reverse-ordered) batch of reports.
    """
    store = ShardReportStore()
    for shard, version, report in entries:
        store.ingest(shard, version, report, mode)
    return store
