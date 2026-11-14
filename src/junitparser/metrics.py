"""Aggregate rerun/flaky metrics across heterogeneous JUnit/xUnit reports.

Reports may come from two kinds of testers:

* xunit2 style (e.g. Jenkins), where failed reruns of a testcase are recorded
  as interim results (``<rerunFailure>``, ``<rerunError>``,
  ``<flakyFailure>`` and ``<flakyError>``) next to the final result.
* Plain JUnit style, where every execution of a test is emitted as a separate
  ``<testcase>`` carrying its own rerun sequence number.

The two flavors are reconciled through an execution identifier and a rerun
sequence number that accompany each report. The same attempt reported by both
testers is therefore recognized and counted only once.

Legacy reports that only contain a final result, without any rerun sequence
information, cannot be treated as first-try passes. They are tracked
separately so that the daily report only receives values (or value ranges)
that can actually be determined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .junitparser import (
    Error as JUnitError,
    Failure as JUnitFailure,
    JUnitXml,
    JUnitXmlError,
    Properties,
    Skipped,
    TestCase as JUnitTestCase,
)
from .xunit2 import (
    FlakyError,
    FlakyFailure,
    InterimResult,
    JUnitXml as XUnit2Xml,
    RerunError,
    RerunFailure,
    TestCase as XUnit2TestCase,
)

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_ERROR = "error"
OUTCOME_SKIPPED = "skipped"
_OUTCOMES = frozenset({OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_ERROR, OUTCOME_SKIPPED})

#: Attribute/property names checked (in order) when looking up the execution id.
DEFAULT_EXECUTION_ID_NAMES: tuple[str, ...] = (
    "execution_id",
    "execution-id",
    "executionId",
)

#: Attribute/property names checked (in order) when looking up the rerun index.
DEFAULT_RUN_INDEX_NAMES: tuple[str, ...] = (
    "run_index",
    "run-index",
    "runIndex",
    "rerun_index",
)


def default_test_id(case: JUnitTestCase) -> str:
    """Identify a testcase by ``classname.name`` when a classname exists."""
    if case.classname:
        return f"{case.classname}.{case.name}"
    return case.name


@dataclass(frozen=True)
class TestAttempt:
    """A single execution attempt of a test.

    Attributes:
        test_id: Stable identity of the test across reports and attempts.
        execution_id: Identifier of the pipeline execution the report belongs to.
        run_index: Zero-based position of this attempt in the rerun sequence.
        outcome: One of :data:`OUTCOME_PASSED`, :data:`OUTCOME_FAILED`,
            :data:`OUTCOME_ERROR` or :data:`OUTCOME_SKIPPED`.
        source: Where the attempt was observed (file path, tester name, ...);
            used for diagnostics only.
    """

    test_id: str
    execution_id: str
    run_index: int
    outcome: str
    source: str | None = None
    observers: tuple[str | None, ...] = ()

    __test__ = False

    def __post_init__(self):
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"Unknown attempt outcome: {self.outcome!r}")
        if not isinstance(self.run_index, int) or self.run_index < 0:
            raise ValueError(
                f"run_index must be a non-negative integer, got {self.run_index!r}"
            )

    @property
    def passed(self) -> bool:
        """Whether this attempt was a pass."""
        return self.outcome == OUTCOME_PASSED


@dataclass
class TestMetrics:
    """Reconciled execution history of one test within one pipeline execution.

    Attributes:
        test_id: Stable identity of the test.
        execution_id: Pipeline execution the attempts belong to.
        attempts: Deduplicated attempts (use :attr:`ordered_attempts` for
            chronological order).
        run_index_known: Whether the attempts carry rerun sequence numbers.
            ``False`` marks legacy reports whose prior attempts cannot be
            reconstructed.
    """

    test_id: str
    execution_id: str
    attempts: list[TestAttempt] = field(default_factory=list)
    run_index_known: bool = True

    __test__ = False

    @property
    def attempt_count(self) -> int:
        """Number of distinct attempts recorded for the test."""
        return len(self.attempts)

    @property
    def ordered_attempts(self) -> list[TestAttempt]:
        """Attempts in rerun order; insertion order when indices are unknown."""
        if self.run_index_known:
            return sorted(self.attempts, key=lambda attempt: attempt.run_index)
        return list(self.attempts)

    @property
    def sources(self) -> tuple[str | None, ...]:
        """Distinct report sources that observed any attempt."""
        sources: list[str | None] = []
        for attempt in self.attempts:
            for observer in attempt.observers or (
                (attempt.source,) if attempt.source is not None else ()
            ):
                if observer not in sources:
                    sources.append(observer)
        return tuple(sources)

    @property
    def has_first_attempt(self) -> bool:
        """Whether the attempt at index 0 (the first try) was observed."""
        return self.run_index_known and any(
            attempt.run_index == 0 for attempt in self.attempts
        )

    @property
    def is_history_complete(self) -> bool:
        """Whether every attempt from the first try onward was observed.

        ``False`` for legacy reports and for partial reports with gaps in the
        rerun sequence.
        """
        if not self.run_index_known or not self.attempts:
            return False
        indices = sorted(attempt.run_index for attempt in self.attempts)
        return indices == list(range(len(indices)))

    @property
    def first_attempt_passed(self) -> bool | None:
        """Whether the test passed on its first attempt.

        ``None`` when the first try was not observed, which includes legacy
        reports: such a test must never be counted as a first-try pass.
        """
        if not self.has_first_attempt:
            return None
        return next(
            attempt.passed for attempt in self.attempts if attempt.run_index == 0
        )

    @property
    def final_passed(self) -> bool:
        """Whether the last known attempt of the test passed."""
        if not self.attempts:
            return False
        return self.ordered_attempts[-1].passed

    @property
    def extra_attempts(self) -> int | None:
        """Number of attempts beyond the first one.

        ``None`` when the rerun history is unknown or incomplete, since the
        number of prior attempts cannot be reconstructed.
        """
        if not self.is_history_complete:
            return None
        return self.attempt_count - 1


@dataclass
class _Observation:
    """Outcome of one attempt together with every source that reported it."""

    outcome: str
    observers: list[str | None] = field(default_factory=list)

    def add_observer(self, source: str | None) -> None:
        if source not in self.observers:
            self.observers.append(source)


class TestMetricsBuilder:
    """Collect attempts from one or more reports and reconcile duplicates.

    Args:
        execution_id: Default execution id used when a report/case does not
            carry one itself.
        execution_id_names: Attribute/property names holding the execution id.
        run_index_names: Attribute/property names holding the rerun index.
        test_id_strategy: Callable mapping a testcase to its stable test id;
            defaults to :func:`default_test_id`.
    """

    __test__ = False

    def __init__(
        self,
        *,
        execution_id: str | None = None,
        execution_id_names: Sequence[str] = DEFAULT_EXECUTION_ID_NAMES,
        run_index_names: Sequence[str] = DEFAULT_RUN_INDEX_NAMES,
        test_id_strategy: Callable[[JUnitTestCase], str] = default_test_id,
    ):
        self._default_execution_id = execution_id
        self._execution_id_names = tuple(execution_id_names)
        self._run_index_names = tuple(run_index_names)
        self._test_id_strategy = test_id_strategy
        # (test_id, execution_id) -> {run_index: observation}
        self._known: dict[tuple[str, str], dict[int, "_Observation"]] = {}
        # (test_id, execution_id) -> legacy final-only observation
        self._legacy: dict[tuple[str, str], "_Observation"] = {}

    def __iter__(self) -> Iterator[TestMetrics]:
        return iter(self.build().tests)

    def __len__(self) -> int:
        return len(self._known.keys() | self._legacy.keys())

    def get(self, test_id: str, execution_id: str) -> TestMetrics | None:
        """Return the metrics for one test/execution pair, if present."""
        key = (test_id, execution_id)
        if key not in self._known and key not in self._legacy:
            return None
        return self._build_metrics(key)

    def add_report(
        self,
        report: str | Path | JUnitXml,
        *,
        source: str | None = None,
        execution_id: str | None = None,
    ) -> "TestMetricsBuilder":
        """Merge one report (a file path or a parsed :class:`JUnitXml`)."""
        if isinstance(report, (str, Path)):
            xml = XUnit2Xml.fromfile(str(report))
            source = source or str(report)
        else:
            xml = report
        default_execution_id = execution_id or self._default_execution_id
        for suite in xml:
            suite_properties = self._element_properties(suite)
            for case in suite:
                self._add_case(
                    case,
                    default_execution_id=default_execution_id,
                    source=source,
                    suite_properties=suite_properties,
                )
        return self

    def build(self) -> "MetricsReport":
        """Freeze the collected data into a :class:`MetricsReport`."""
        tests = tuple(
            self._build_metrics(key)
            for key in sorted(self._known.keys() | self._legacy.keys())
        )
        return MetricsReport(tests)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_metrics(self, key: tuple[str, str]) -> TestMetrics:
        test_id, execution_id = key
        if key in self._known:
            # Detailed reports supersede any legacy final-only observation.
            attempts = [
                TestAttempt(
                    test_id,
                    execution_id,
                    run_index,
                    observation.outcome,
                    observers=tuple(observation.observers),
                )
                for run_index, observation in self._known[key].items()
            ]
            return TestMetrics(
                test_id=test_id,
                execution_id=execution_id,
                attempts=attempts,
                run_index_known=True,
            )
        observation = self._legacy[key]
        return TestMetrics(
            test_id=test_id,
            execution_id=execution_id,
            attempts=[
                TestAttempt(
                    test_id,
                    execution_id,
                    0,
                    observation.outcome,
                    observers=tuple(observation.observers),
                )
            ],
            run_index_known=False,
        )

    @staticmethod
    def _lookup(
        names: Sequence[str],
        attributes: dict[str, str],
        properties: dict[str, str],
    ) -> str | None:
        for name in names:
            if name in attributes:
                return attributes[name]
        for name in names:
            if name in properties:
                return properties[name]
        return None

    @staticmethod
    def _element_properties(element) -> dict[str, str]:
        properties: dict[str, str] = {}
        if isinstance(element, XUnit2TestCase):
            iterator = element.properties()
        else:
            props = element.child(Properties)
            iterator = props if props is not None else ()
        for prop in iterator:
            properties[prop.name] = prop.value
        return properties

    def _case_execution_id(
        self,
        case: JUnitTestCase,
        suite_properties: dict[str, str],
    ) -> str | None:
        return self._lookup(
            self._execution_id_names,
            dict(case._elem.attrib),
            {**suite_properties, **self._element_properties(case)},
        )

    def _case_run_index(
        self,
        case: JUnitTestCase,
        suite_properties: dict[str, str],
    ) -> tuple[int, bool]:
        raw = self._lookup(
            self._run_index_names,
            dict(case._elem.attrib),
            {**suite_properties, **self._element_properties(case)},
        )
        if raw is None:
            return 0, False
        try:
            index = int(raw)
        except (TypeError, ValueError):
            return 0, False
        if index < 0:
            return 0, False
        return index, True

    def _test_id(self, case: JUnitTestCase) -> str:
        return self._test_id_strategy(case)

    @staticmethod
    def _final_outcome(case: JUnitTestCase) -> str:
        results = case.result
        if any(isinstance(result, JUnitFailure) for result in results):
            return OUTCOME_FAILED
        if any(isinstance(result, JUnitError) for result in results):
            return OUTCOME_ERROR
        if any(isinstance(result, Skipped) for result in results):
            return OUTCOME_SKIPPED
        return OUTCOME_PASSED

    @staticmethod
    def _interim_outcome(interim: InterimResult) -> str:
        if isinstance(interim, (RerunFailure, FlakyFailure)):
            return OUTCOME_FAILED
        if isinstance(interim, (RerunError, FlakyError)):
            return OUTCOME_ERROR
        # Interim results are non-passing by definition.
        return OUTCOME_FAILED

    def _add_case(
        self,
        case: JUnitTestCase,
        *,
        default_execution_id: str | None,
        source: str | None,
        suite_properties: dict[str, str],
    ) -> None:
        execution_id = (
            self._case_execution_id(case, suite_properties) or default_execution_id
        )
        if execution_id is None:
            raise JUnitXmlError(
                "Cannot attribute testcase to a pipeline execution: "
                "no execution id found on the report and no default was given."
            )
        test_id = self._test_id(case)
        key = (test_id, execution_id)
        run_index, index_known = self._case_run_index(case, suite_properties)
        interim = case.interim_result if isinstance(case, XUnit2TestCase) else []

        if not index_known and not interim:
            # Legacy report: only the final result is known, prior attempts
            # cannot be reconstructed, so no first-try claim is possible.
            observation = self._legacy.setdefault(
                key, _Observation(self._final_outcome(case))
            )
            observation.add_observer(source)
            return

        known = self._known.setdefault(key, {})
        if not index_known:
            # xunit2 case without an explicit index: the k interim results are
            # attempts 0..k-1 and the final result is attempt k.
            run_index = len(interim)

        # Same attempt reported by the other tester: count it only once while
        # keeping every reporting source for diagnostics.
        final_observation = known.setdefault(
            run_index, _Observation(self._final_outcome(case))
        )
        final_observation.add_observer(source)

        # Interim results describe the attempts preceding the final one. In
        # document order the first interim result is the earliest attempt.
        for attempt_index, result in enumerate(interim):
            if attempt_index > run_index:
                # Malformed report: more interim results than attempts.
                break
            observation = known.setdefault(
                attempt_index,
                _Observation(self._interim_outcome(result)),
            )
            observation.add_observer(source)


@dataclass(frozen=True)
class MetricsReport:
    """Aggregated metrics ready for a daily report.

    Final outcomes are always exact. The first-try pass rate and the extra
    attempt count are only exact when the relevant rerun histories are fully
    known; otherwise determinable ranges or lower bounds and the affected test
    ids are provided.
    """

    tests: tuple[TestMetrics, ...]

    @property
    def total_tests(self) -> int:
        """Number of distinct test/execution pairs."""
        return len(self.tests)

    @property
    def final_passed(self) -> int:
        """Number of tests whose last attempt passed."""
        return sum(1 for test in self.tests if test.final_passed)

    @property
    def final_failed(self) -> int:
        """Number of tests whose last attempt did not pass."""
        return self.total_tests - self.final_passed

    @property
    def final_pass_rate(self) -> float:
        """Final pass rate; always exactly determinable."""
        if not self.tests:
            return 0.0
        return self.final_passed / self.total_tests

    @property
    def first_pass_determinable_tests(self) -> tuple[TestMetrics, ...]:
        """Tests whose first attempt (index 0) was observed."""
        return tuple(test for test in self.tests if test.has_first_attempt)

    @property
    def unknown_history_tests(self) -> tuple[tuple[str, str], ...]:
        """``(execution_id, test_id)`` pairs whose first try is unknowable."""
        return tuple(
            (test.execution_id, test.test_id)
            for test in self.tests
            if not test.has_first_attempt
        )

    @property
    def first_passed(self) -> int:
        """Number of tests known to have passed on the first attempt."""
        return sum(
            1
            for test in self.first_pass_determinable_tests
            if test.first_attempt_passed
        )

    @property
    def first_pass_rate(self) -> float | None:
        """First-try pass rate over tests whose first try is known.

        ``None`` when no test has a determinable first try. Legacy tests are
        excluded rather than optimistically counted as first-try passes.
        """
        determinable = self.first_pass_determinable_tests
        if not determinable:
            return None
        return self.first_passed / len(determinable)

    @property
    def first_pass_rate_bounds(self) -> tuple[float, float] | None:
        """Inclusive ``(lower, upper)`` bounds of the first-try pass rate.

        Computed over *all* tests: the lower bound assumes every test with an
        unknown first try failed it, the upper bound assumes all of them
        passed. ``None`` when there are no tests at all.
        """
        if not self.tests:
            return None
        unknown = len(self.unknown_history_tests)
        lower = self.first_passed / self.total_tests
        upper = (self.first_passed + unknown) / self.total_tests
        return lower, upper

    @property
    def extra_attempts(self) -> int | None:
        """Total extra attempts when every history is complete, else ``None``."""
        if any(not test.is_history_complete for test in self.tests):
            return None
        return sum(test.extra_attempts for test in self.tests)

    @property
    def extra_attempts_minimum(self) -> int:
        """Lower bound of extra attempts, counting only observed attempts."""
        return sum(
            test.attempt_count - 1 for test in self.tests if test.run_index_known
        )

    def summary(self) -> dict:
        """Plain-data summary suitable for a daily report."""
        return {
            "total_tests": self.total_tests,
            "final_passed": self.final_passed,
            "final_failed": self.final_failed,
            "final_pass_rate": self.final_pass_rate,
            "first_pass_determinable_tests": len(self.first_pass_determinable_tests),
            "first_passed": self.first_passed,
            "first_pass_rate": self.first_pass_rate,
            "first_pass_rate_bounds": self.first_pass_rate_bounds,
            "extra_attempts": self.extra_attempts,
            "extra_attempts_minimum": self.extra_attempts_minimum,
            "unknown_history_tests": list(self.unknown_history_tests),
        }
