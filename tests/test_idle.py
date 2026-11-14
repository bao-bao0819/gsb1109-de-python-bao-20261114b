import pytest

from src.junitparser import (
    InfeasibleScheduleError,
    JUnitXml,
    JUnitXmlError,
    TestSuite,
    idle_bounds,
)


def make_suite(text):
    return TestSuite.fromstring(text)


class TestIdleBoundsBasic:
    def test_exact_when_serial(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T10:00:00" time="100">'
            '<testcase name="a" time="10"/>'
            '<testcase name="b" time="20"/>'
            '<testcase name="c" time="30"/>'
            "</testsuite>"
        )
        bounds = idle_bounds(suite, concurrency=1)
        assert bounds.min_idle == pytest.approx(40)
        assert bounds.max_idle == pytest.approx(40)
        assert len(bounds.machines) == 1
        assert bounds.machines[0].suite == "s"
        assert bounds.machines[0].window == pytest.approx(100)

    def test_concurrency_widens_bounds(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T10:00:00" time="100">'
            '<testcase name="a" time="10"/>'
            '<testcase name="b" time="20"/>'
            '<testcase name="c" time="30"/>'
            "</testsuite>"
        )
        bounds = idle_bounds(suite, concurrency=2)
        # Busy at least 60/2 = 30s, at most 60s of spread-out work.
        assert bounds.min_idle == pytest.approx(40)
        assert bounds.max_idle == pytest.approx(70)

    def test_empty_suite_is_all_idle(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T10:00:00" time="50"/>'
        )
        bounds = idle_bounds(suite, concurrency=1)
        assert bounds.min_idle == pytest.approx(50)
        assert bounds.max_idle == pytest.approx(50)

    def test_invalid_concurrency(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T10:00:00" time="50"/>'
        )
        with pytest.raises(ValueError):
            idle_bounds(suite, concurrency=0)

    def test_missing_timestamp_or_time(self):
        with pytest.raises(JUnitXmlError):
            idle_bounds(make_suite('<testsuite name="s" time="50"/>'))
        with pytest.raises(JUnitXmlError):
            idle_bounds(
                make_suite('<testsuite name="s" timestamp="2026-11-14T10:00:00"/>')
            )


class TestFixedStarts:
    def test_fixed_starts_pin_busy_union(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T09:00:00" time="100">'
            '<testcase name="a" time="10" timestamp="2026-11-14T09:00:00"/>'
            '<testcase name="b" time="10" timestamp="2026-11-14T09:00:20"/>'
            '<testcase name="c" time="5"/>'
            "</testsuite>"
        )
        bounds = idle_bounds(suite, concurrency=1)
        # Fixed union is 20s; the 5s free case can hide under either fixed
        # case's concurrency shadow or extend the busy time by 5s.
        assert bounds.min_idle == pytest.approx(75)
        assert bounds.max_idle == pytest.approx(75)

    def test_case_starts_mapping_forms(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T09:00:00" time="100">'
            '<testcase name="a" classname="C" time="10"/>'
            '<testcase name="b" classname="C" time="10"/>'
            '<testcase name="c" classname="C" time="10"/>'
            "</testsuite>"
        )
        case_c = next(case for case in suite if case.name == "c")
        starts = {
            "a": "2026-11-14T09:00:00",
            ("C", "b"): "2026-11-14T09:00:20",
            case_c: "2026-11-14T09:00:40",
        }
        bounds = idle_bounds(suite, concurrency=1, case_starts=starts)
        # Fixed union is 30s spread across the window; all work is fixed.
        assert bounds.min_idle == pytest.approx(70)
        assert bounds.max_idle == pytest.approx(70)

    def test_overlapping_fixed_starts_infeasible(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T09:00:00" time="100">'
            '<testcase name="a" time="5" timestamp="2026-11-14T09:00:05"/>'
            '<testcase name="b" time="5" timestamp="2026-11-14T09:00:06"/>'
            "</testsuite>"
        )
        with pytest.raises(InfeasibleScheduleError):
            idle_bounds(suite, concurrency=1)
        bounds = idle_bounds(suite, concurrency=2)
        assert bounds.min_idle == pytest.approx(94)
        assert bounds.max_idle == pytest.approx(94)

    def test_fixed_start_outside_window(self):
        suite = make_suite(
            '<testsuite name="s" timestamp="2026-11-14T09:00:00" time="10">'
            '<testcase name="a" time="5" timestamp="2026-11-14T09:01:00"/>'
            "</testsuite>"
        )
        with pytest.raises(InfeasibleScheduleError):
            idle_bounds(suite, concurrency=1)


class TestNestedSuites:
    def test_nested_suite_shares_machine(self):
        # Nested suites must not get an extra machine: 120s of work cannot
        # fit a 100s rental window with concurrency 1.
        suite = make_suite(
            '<testsuite name="top" timestamp="2026-11-14T10:00:00" time="100">'
            '<testcase name="a" time="60"/>'
            '<testsuite name="nested">'
            '<testcase name="b" time="60"/>'
            "</testsuite>"
            "</testsuite>"
        )
        with pytest.raises(InfeasibleScheduleError):
            idle_bounds(suite, concurrency=1)
        bounds = idle_bounds(suite, concurrency=2)
        assert bounds.min_idle == pytest.approx(0)
        assert bounds.max_idle == pytest.approx(40)

    def test_narrow_nested_window_infeasible(self):
        # Two long cases squeezed into the same narrow window, concurrency 1:
        # report infeasible instead of a negative idle time.
        suite = make_suite(
            '<testsuite name="top" timestamp="2026-11-14T10:00:00" time="100">'
            '<testsuite name="narrow" timestamp="2026-11-14T10:00:00" time="10">'
            '<testcase name="long1" time="8"/>'
            '<testcase name="long2" time="8"/>'
            "</testsuite>"
            "</testsuite>"
        )
        with pytest.raises(InfeasibleScheduleError):
            idle_bounds(suite, concurrency=1)
        # With concurrency 2 the same records are schedulable.
        bounds = idle_bounds(suite, concurrency=2)
        assert bounds.min_idle >= 0
        assert bounds.max_idle == pytest.approx(92)

    def test_nested_window_narrows_cases(self):
        # Nested window [+10, +30] inside a 100s rental window.
        suite = make_suite(
            '<testsuite name="top" timestamp="2026-11-14T10:00:00" time="100">'
            '<testsuite name="nested" timestamp="2026-11-14T10:00:10" time="20">'
            '<testcase name="a" time="25"/>'
            "</testsuite>"
            "</testsuite>"
        )
        with pytest.raises(InfeasibleScheduleError):
            idle_bounds(suite, concurrency=1)


class TestMultipleMachines:
    def test_totals_sum_over_suites(self):
        xml = JUnitXml.fromstring(
            "<testsuites>"
            '<testsuite name="s1" timestamp="2026-11-14T10:00:00" time="100">'
            '<testcase name="a" time="40"/>'
            "</testsuite>"
            '<testsuite name="s2" timestamp="2026-11-14T10:00:00" time="200">'
            '<testcase name="b" time="50"/>'
            '<testcase name="c" time="50"/>'
            "</testsuite>"
            "</testsuites>"
        )
        bounds = idle_bounds(xml, concurrency=1)
        assert [m.suite for m in bounds.machines] == ["s1", "s2"]
        assert bounds.min_idle == pytest.approx(60 + 100)
        assert bounds.max_idle == pytest.approx(60 + 100)

    def test_xml_without_suites(self):
        bounds = idle_bounds(JUnitXml.fromstring("<testsuites/>"))
        assert bounds.min_idle == 0
        assert bounds.max_idle == 0
        assert bounds.machines == ()
