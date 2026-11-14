import pytest

from src.junitparser import JUnitXml as PlainXml
from src.junitparser import JUnitXmlError
from src.junitparser.metrics import (
    OUTCOME_ERROR,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
    MetricsReport,
    TestAttempt,
    TestMetricsBuilder,
)
from src.junitparser.xunit2 import (
    FlakyFailure,
    JUnitXml as XUnit2Xml,
    TestCase as XUnit2TestCase,
    TestSuite as XUnit2TestSuite,
)

EXEC = "exec-1"


def xunit2_report(text: str) -> XUnit2Xml:
    return XUnit2Xml.fromstring(text)


def plain_report(text: str) -> PlainXml:
    return PlainXml.fromstring(text)


class TestTestAttempt:
    def test_passed_flag(self):
        attempt = TestAttempt("t", EXEC, 0, OUTCOME_PASSED)
        assert attempt.passed is True
        assert TestAttempt("t", EXEC, 1, OUTCOME_FAILED).passed is False

    def test_rejects_unknown_outcome(self):
        with pytest.raises(ValueError):
            TestAttempt("t", EXEC, 0, "flaky")

    @pytest.mark.parametrize("bad_index", [-1, "0", 1.5])
    def test_rejects_bad_run_index(self, bad_index):
        with pytest.raises(ValueError):
            TestAttempt("t", EXEC, bad_index, OUTCOME_PASSED)


class TestXUnit2Interim:
    def test_reruns_reconstruct_attempt_sequence(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="flaky" execution_id="{EXEC}">
    <rerunFailure message="boom" type="AssertionError"/>
    <rerunError message="setup" type="Error"/>
  </testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(xunit2_report(text)).build()
        assert report.total_tests == 1
        metrics = report.tests[0]
        assert metrics.test_id == "pkg.flaky"
        assert metrics.run_index_known is True
        assert metrics.is_history_complete is True
        outcomes = {
            attempt.run_index: attempt.outcome for attempt in metrics.ordered_attempts
        }
        assert outcomes == {0: OUTCOME_FAILED, 1: OUTCOME_ERROR, 2: OUTCOME_PASSED}
        assert metrics.first_attempt_passed is False
        assert metrics.final_passed is True
        assert metrics.extra_attempts == 2
        assert report.first_passed == 0
        assert report.final_passed == 1
        assert report.final_pass_rate == 1.0
        assert report.first_pass_rate == 0.0
        assert report.extra_attempts == 2
        assert report.extra_attempts_minimum == 2

    def test_flaky_interim_maps_to_failures(self):
        case = XUnit2TestCase("flaky")
        case.add_interim_result(FlakyFailure("m", "t"))
        suite = XUnit2TestSuite("s")
        suite.add_testcase(case)
        xml = XUnit2Xml()
        xml.add_testsuite(suite)
        report = TestMetricsBuilder(execution_id=EXEC).add_report(xml).build()
        metrics = report.tests[0]
        assert metrics.ordered_attempts[0].outcome == OUTCOME_FAILED
        assert metrics.ordered_attempts[1].outcome == OUTCOME_PASSED
        assert metrics.extra_attempts == 1

    def test_explicit_run_index_anchors_interim_results(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}" run_index="2">
    <rerunFailure message="m" type="t"/>
    <rerunFailure message="m2" type="t2"/>
  </testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(xunit2_report(text)).build()
        metrics = report.tests[0]
        indices = sorted(attempt.run_index for attempt in metrics.attempts)
        assert indices == [0, 1, 2]
        assert metrics.is_history_complete is True

    def test_failure_without_interim_is_single_attempt(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}"><failure message="m"/></testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(xunit2_report(text)).build()
        metrics = report.tests[0]
        # A final-only failure without sequence data is treated like a legacy
        # report: its first try cannot be claimed, but the final outcome is exact.
        assert metrics.attempt_count == 1
        assert metrics.run_index_known is False
        assert metrics.first_attempt_passed is None
        assert metrics.final_passed is False
        assert report.unknown_history_tests == ((EXEC, "t"),)


class TestSplitCases:
    def test_each_execution_is_separate_indexed_case(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="t" execution_id="{EXEC}">
    <properties><property name="run_index" value="0"/></properties>
    <failure message="m"/>
  </testcase>
  <testcase classname="pkg" name="t" execution_id="{EXEC}">
    <properties><property name="run_index" value="1"/></properties>
    <error message="m"/>
  </testcase>
  <testcase classname="pkg" name="t" execution_id="{EXEC}">
    <properties><property name="run_index" value="2"/></properties>
  </testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        assert report.total_tests == 1
        metrics = report.tests[0]
        assert [a.run_index for a in metrics.ordered_attempts] == [0, 1, 2]
        assert metrics.ordered_attempts[0].outcome == OUTCOME_FAILED
        assert metrics.ordered_attempts[1].outcome == OUTCOME_ERROR
        assert metrics.ordered_attempts[2].outcome == OUTCOME_PASSED
        assert metrics.first_attempt_passed is False
        assert metrics.final_passed is True
        assert metrics.extra_attempts == 2

    def test_skipped_outcome(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}" run_index="0"><skipped/></testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        metrics = report.tests[0]
        assert metrics.ordered_attempts[0].outcome == OUTCOME_SKIPPED
        assert metrics.final_passed is False
        assert metrics.first_attempt_passed is False


class TestCrossTesterDedup:
    def test_same_attempt_from_both_testers_counted_once(self):
        # Tester A: xunit2, one rerun failure then a pass (attempts 0, 1).
        text_a = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="t" execution_id="{EXEC}">
    <rerunFailure message="boom" type="AssertionError"/>
  </testcase>
</testsuite></testsuites>"""
        # Tester B: split cases, reports attempt 0 (failure) and attempt 1 (pass).
        text_b = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="t" execution_id="{EXEC}" run_index="0">
    <failure message="boom"/>
  </testcase>
  <testcase classname="pkg" name="t" execution_id="{EXEC}" run_index="1"/>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder()
        builder.add_report(xunit2_report(text_a), source="jenkins")
        builder.add_report(plain_report(text_b), source="pytest")
        report = builder.build()

        assert report.total_tests == 1
        metrics = report.tests[0]
        assert metrics.attempt_count == 2
        assert sorted(a.run_index for a in metrics.attempts) == [0, 1]
        assert set(metrics.sources) == {"jenkins", "pytest"}
        assert metrics.extra_attempts == 1
        assert report.first_passed == 0
        assert report.final_passed == 1
        assert report.extra_attempts_minimum == 1

    def test_distinct_executions_are_not_merged(self):
        text = """\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="exec-a" run_index="0"/>
  <testcase name="t" execution_id="exec-b" run_index="0"><failure/></testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        assert report.total_tests == 2
        by_exec = {m.execution_id: m for m in report.tests}
        assert by_exec["exec-a"].final_passed is True
        assert by_exec["exec-b"].final_passed is False


class TestLegacyReports:
    def test_final_only_report_is_not_first_try_pass(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="legacy-pass" execution_id="{EXEC}"/>
  <testcase classname="pkg" name="legacy-fail" execution_id="{EXEC}">
    <failure message="m"/>
  </testcase>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        assert report.total_tests == 2
        assert set(report.unknown_history_tests) == {
            (EXEC, "pkg.legacy-pass"),
            (EXEC, "pkg.legacy-fail"),
        }
        for metrics in report.tests:
            assert metrics.run_index_known is False
            assert metrics.has_first_attempt is False
            assert metrics.first_attempt_passed is None
            assert metrics.extra_attempts is None
        # Final outcomes remain exact.
        assert report.final_passed == 1
        assert report.final_failed == 1
        assert report.final_pass_rate == 0.5
        # Nothing can be claimed about first-try behavior.
        assert report.first_passed == 0
        assert report.first_pass_rate is None
        assert report.first_pass_rate_bounds == (0.0, 1.0)
        assert report.extra_attempts is None
        assert report.extra_attempts_minimum == 0

    def test_mixed_known_and_legacy_ranges(self):
        # Two tests with a known history (one first-pass, one rerun-pass),
        # one legacy test whose first try is unknowable.
        known = f"""\
<testsuites><testsuite name="s">
  <testcase name="clean" execution_id="{EXEC}" run_index="0"/>
  <testcase name="rerun" execution_id="{EXEC}" run_index="0"><failure/></testcase>
  <testcase name="rerun" execution_id="{EXEC}" run_index="1"/>
</testsuite></testsuites>"""
        legacy = f"""\
<testsuites><testsuite name="s">
  <testcase name="mystery" execution_id="{EXEC}"/>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder()
        builder.add_report(plain_report(known))
        builder.add_report(plain_report(legacy))
        report = builder.build()

        assert report.total_tests == 3
        assert len(report.first_pass_determinable_tests) == 2
        assert report.first_passed == 1
        assert report.first_pass_rate == 0.5
        lower, upper = report.first_pass_rate_bounds
        assert (lower, upper) == (1 / 3, 2 / 3)
        assert report.extra_attempts is None
        assert report.extra_attempts_minimum == 1
        assert report.final_passed == 3
        assert report.final_pass_rate == 1.0

    def test_detailed_report_supersedes_legacy_observation(self):
        legacy = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}"/>
</testsuite></testsuites>"""
        detailed = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}">
    <rerunFailure message="m" type="t"/>
  </testcase>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder()
        builder.add_report(plain_report(legacy), source="old-tester")
        builder.add_report(xunit2_report(detailed), source="new-tester")
        report = builder.build()
        metrics = report.tests[0]
        assert metrics.run_index_known is True
        assert metrics.is_history_complete is True
        assert metrics.attempt_count == 2
        assert metrics.first_attempt_passed is False
        assert report.unknown_history_tests == ()


class TestPartialHistory:
    def test_gap_in_sequence_keeps_first_try_unknown(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}" run_index="1"><failure/></testcase>
  <testcase name="t" execution_id="{EXEC}" run_index="2"/>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        metrics = report.tests[0]
        assert metrics.has_first_attempt is False
        assert metrics.first_attempt_passed is None
        assert metrics.is_history_complete is False
        assert metrics.extra_attempts is None
        assert metrics.final_passed is True
        assert report.extra_attempts_minimum == 1
        assert report.unknown_history_tests == ((EXEC, "t"),)


class TestExecutionIdLookup:
    def test_execution_id_from_suite_property(self):
        text = """\
<testsuites><testsuite name="s">
  <properties><property name="execution_id" value="suite-exec"/></properties>
  <testcase name="t" run_index="0"/>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        assert report.tests[0].execution_id == "suite-exec"

    def test_default_execution_id(self):
        text = """\
<testsuites><testsuite name="s">
  <testcase name="t" run_index="0"/>
</testsuite></testsuites>"""
        report = (
            TestMetricsBuilder(execution_id="default-exec")
            .add_report(plain_report(text))
            .build()
        )
        assert report.tests[0].execution_id == "default-exec"

    def test_per_report_execution_id_overrides_default(self):
        text = """\
<testsuites><testsuite name="s">
  <testcase name="t" run_index="0"/>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder(execution_id="default-exec")
        builder.add_report(plain_report(text), execution_id="report-exec")
        assert builder.build().tests[0].execution_id == "report-exec"

    def test_missing_execution_id_raises(self):
        text = """\
<testsuites><testsuite name="s">
  <testcase name="t" run_index="0"/>
</testsuite></testsuites>"""
        with pytest.raises(JUnitXmlError):
            TestMetricsBuilder().add_report(plain_report(text)).build()

    def test_custom_attribute_names(self):
        text = """\
<testsuites><testsuite name="s">
  <testcase name="t" build="42" attempt="1"/>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder(
            execution_id="fallback",
            execution_id_names=("build",),
            run_index_names=("attempt",),
        )
        report = builder.add_report(plain_report(text)).build()
        metrics = report.tests[0]
        assert metrics.execution_id == "42"
        assert [a.run_index for a in metrics.attempts] == [1]

    def test_invalid_run_index_falls_back_to_legacy(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="t" execution_id="{EXEC}" run_index="not-a-number"/>
</testsuite></testsuites>"""
        report = TestMetricsBuilder().add_report(plain_report(text)).build()
        assert report.tests[0].run_index_known is False
        assert report.unknown_history_tests == ((EXEC, "t"),)


class TestBuilderApi:
    def test_get_len_and_iteration(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="a" execution_id="{EXEC}" run_index="0"/>
  <testcase name="b" execution_id="{EXEC}" run_index="0"/>
</testsuite></testsuites>"""
        builder = TestMetricsBuilder().add_report(plain_report(text))
        assert len(builder) == 2
        assert builder.get("a", EXEC).test_id == "a"
        assert builder.get("missing", EXEC) is None
        assert {m.test_id for m in builder} == {"a", "b"}

    def test_add_report_from_file(self, tmp_path):
        path = tmp_path / "report.xml"
        path.write_text(
            f'<testsuites><testsuite name="s">'
            f'<testcase name="t" execution_id="{EXEC}" run_index="0"/>'
            f"</testsuite></testsuites>"
        )
        report = TestMetricsBuilder().add_report(path).build()
        assert report.tests[0].sources == (str(path),)

    def test_summary_payload(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase name="ok" execution_id="{EXEC}" run_index="0"/>
  <testcase name="old" execution_id="{EXEC}"/>
</testsuite></testsuites>"""
        summary = TestMetricsBuilder().add_report(plain_report(text)).build().summary()
        assert summary == {
            "total_tests": 2,
            "final_passed": 2,
            "final_failed": 0,
            "final_pass_rate": 1.0,
            "first_pass_determinable_tests": 1,
            "first_passed": 1,
            "first_pass_rate": 1.0,
            "first_pass_rate_bounds": (0.5, 1.0),
            "extra_attempts": None,
            "extra_attempts_minimum": 0,
            "unknown_history_tests": [(EXEC, "old")],
        }

    def test_empty_report(self):
        report = MetricsReport(())
        assert report.total_tests == 0
        assert report.final_passed == 0
        assert report.final_pass_rate == 0.0
        assert report.first_pass_rate is None
        assert report.first_pass_rate_bounds is None
        assert report.extra_attempts == 0
        assert report.extra_attempts_minimum == 0


class TestCustomTestId:
    def test_custom_strategy_groups_cases(self):
        text = f"""\
<testsuites><testsuite name="s">
  <testcase classname="pkg" name="t" execution_id="{EXEC}" run_index="0"/>
  <testcase classname="pkg" name="t" execution_id="{EXEC}" run_index="1"/>
</testsuite></testsuites>"""
        report = (
            TestMetricsBuilder(test_id_strategy=lambda case: case.name)
            .add_report(plain_report(text))
            .build()
        )
        assert report.total_tests == 1
        assert report.tests[0].test_id == "t"
