from fractions import Fraction

import pytest

from src.junitparser import JUnitXml, JUnitXmlError
from src.junitparser.timingaudit import (
    EXCEEDED,
    OK,
    SHORTFALL,
    UNKNOWN,
    audit_timing,
    format_report,
    parse_time_interval,
)


def audit_xml(text, prep_range=(Fraction(0), Fraction(0))):
    xml = JUnitXml.fromstring(text)
    return audit_timing(xml, prep_range=prep_range)


def finding_for(findings, name):
    for finding in findings:
        if finding.name == name:
            return finding
    raise AssertionError(f"No finding for {name!r}")


class TestParseTimeInterval:
    def test_decimals_define_rounding_unit(self):
        interval = parse_time_interval("1.234")
        assert interval.low == Fraction(12335, 10000)
        assert interval.high == Fraction(12345, 10000)

    def test_integer_has_half_second_unit(self):
        interval = parse_time_interval("2")
        assert interval.low == Fraction(3, 2)
        assert interval.high == Fraction(5, 2)

    def test_real_zero_is_known(self):
        interval = parse_time_interval("0.000")
        assert interval.low == 0
        assert interval.high == Fraction(5, 10000)

    def test_exponent_notation(self):
        interval = parse_time_interval("1.5e-3")
        assert interval.low == Fraction(145, 100000)
        assert interval.high == Fraction(155, 100000)

    def test_thousands_separator(self):
        interval = parse_time_interval("1,234.5")
        assert interval.value == Fraction(2469, 2)

    def test_unparseable_raises(self):
        with pytest.raises(JUnitXmlError):
            parse_time_interval("not-a-number")


class TestMissingTime:
    def test_missing_case_time_is_unknown_not_zero(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="1.000">
              <testcase name="timed" time="0.000"/>
              <testcase name="untimed"/>
            </testsuite>
            """
        )
        finding = finding_for(findings, "s")
        assert finding.status == UNKNOWN
        assert [c.name for c in finding.unknown_children] == ["untimed"]
        timed = next(c for c in finding.children if c.name == "timed")
        assert timed.interval is not None
        assert timed.interval.low == 0

    def test_missing_suite_time_is_unknown(self):
        findings = audit_xml(
            """
            <testsuite name="s">
              <testcase name="a" time="0.500"/>
            </testsuite>
            """
        )
        finding = finding_for(findings, "s")
        assert finding.status == UNKNOWN
        assert finding.suite_interval is None

    def test_known_overflow_still_detected_with_unknown_siblings(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="0.100">
              <testcase name="a" time="0.200"/>
              <testcase name="b"/>
            </testsuite>
            """
        )
        finding = finding_for(findings, "s")
        assert finding.status == EXCEEDED
        assert finding.min_excess == Fraction(99, 1000)


class TestRoundingExplains:
    def test_difference_within_rounding_is_ok(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="1.001">
              <testcase name="a" time="0.333"/>
              <testcase name="b" time="0.667"/>
            </testsuite>
            """
        )
        assert finding_for(findings, "s").status == OK

    def test_mixed_precision_sources(self):
        # Case times from a 6-decimal source, suite from a 3-decimal source.
        findings = audit_xml(
            """
            <testsuite name="s" time="0.500">
              <testcase name="a" time="0.250123"/>
              <testcase name="b" time="0.249877"/>
            </testsuite>
            """
        )
        assert finding_for(findings, "s").status == OK


class TestExceeded:
    def test_sequential_cases_slower_than_suite(self):
        # The release report: cases sum to 1.530s but the suite says 1.495s.
        findings = audit_xml(
            """
            <testsuite name="release" time="1.495">
              <testcase name="a" time="0.512"/>
              <testcase name="b" time="0.498"/>
              <testcase name="c" time="0.520"/>
            </testsuite>
            """
        )
        finding = finding_for(findings, "release")
        assert finding.status == EXCEEDED
        # Even with the most favourable rounding, 33ms cannot be explained.
        assert finding.min_excess == Fraction(33, 1000)
        assert {c.name for c in finding.culprit} == {"a", "b", "c"}

    def test_siblings_fit_but_parent_overflows(self):
        findings = audit_xml(
            """
            <testsuite name="parent" time="1.000">
              <testsuite name="A" time="0.600">
                <testcase name="a1" time="0.300"/>
                <testcase name="a2" time="0.299"/>
              </testsuite>
              <testsuite name="B" time="0.600">
                <testcase name="b1" time="0.301"/>
                <testcase name="b2" time="0.298"/>
              </testsuite>
              <testsuite name="C" time="0.010">
                <testcase name="c1" time="0.010"/>
              </testsuite>
            </testsuite>
            """
        )
        # Each sibling suite is internally consistent.
        for name in ("A", "B", "C"):
            assert finding_for(findings, name).status == OK
        parent = finding_for(findings, "parent")
        assert parent.status == EXCEEDED
        assert parent.min_excess == Fraction(208, 1000)
        # A and B alone already contradict the parent; C is not to blame.
        assert {c.name for c in parent.culprit} == {"A", "B"}
        assert parent.culprit_min_excess == Fraction(1985, 10000)

    def test_report_mentions_subset_and_excess(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="1.000">
              <testcase name="a" time="0.700"/>
              <testcase name="b" time="0.700"/>
            </testsuite>
            """
        )
        report = format_report(findings)
        assert "EXCEEDED" in report
        assert "min excess 0.3985s" in report
        assert "a" in report and "b" in report


class TestPrepRange:
    def test_prep_range_explains_gap(self):
        text = """
            <testsuite name="s" time="1.010">
              <testcase name="a" time="0.500"/>
              <testcase name="b" time="0.500"/>
            </testsuite>
            """
        assert finding_for(audit_xml(text), "s").status == SHORTFALL
        ok = audit_xml(text, prep_range=(Fraction(0), Fraction(2, 100)))
        assert finding_for(ok, "s").status == OK

    def test_shortfall_amount(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="1.010">
              <testcase name="a" time="0.500"/>
              <testcase name="b" time="0.500"/>
            </testsuite>
            """
        )
        finding = finding_for(findings, "s")
        assert finding.min_shortfall == Fraction(85, 10000)

    def test_invalid_prep_range(self):
        xml = JUnitXml.fromstring('<testsuite name="s" time="1"/>')
        with pytest.raises(ValueError):
            audit_timing(xml, prep_range=(Fraction(1), Fraction(0)))


class TestNesting:
    def test_findings_cover_all_suites_with_paths(self):
        findings = audit_xml(
            """
            <testsuites name="root" time="1.500">
              <testsuite name="outer" time="1.500">
                <testsuite name="inner" time="0.500">
                  <testcase name="a" time="0.500"/>
                </testsuite>
                <testcase name="b" time="1.000"/>
              </testsuite>
            </testsuites>
            """
        )
        by_name = {f.name: f for f in findings}
        assert set(by_name) == {"root", "outer", "inner"}
        assert by_name["inner"].path == ("outer", "inner")
        assert by_name["root"].status == OK
        assert by_name["outer"].status == OK
        assert by_name["inner"].status == OK

    def test_root_without_time_is_unknown(self):
        findings = audit_xml(
            """
            <testsuite name="s" time="1.000">
              <testcase name="a" time="1.000"/>
            </testsuite>
            """
        )
        root = finding_for(findings, "<testsuites>")
        assert root.status == UNKNOWN


class TestCli:
    def test_timeaudit_exit_codes(self, tmp_path, capsys):
        from src.junitparser import cli

        bad = tmp_path / "bad.xml"
        bad.write_text(
            '<testsuite name="s" time="1.000">'
            '<testcase name="a" time="0.700"/>'
            '<testcase name="b" time="0.700"/>'
            "</testsuite>"
        )
        good = tmp_path / "good.xml"
        good.write_text(
            '<testsuite name="s" time="1.000">'
            '<testcase name="a" time="0.500"/>'
            '<testcase name="b" time="0.499"/>'
            "</testsuite>"
        )
        assert cli.main(["timeaudit", str(bad)]) == 1
        assert "EXCEEDED" in capsys.readouterr().out
        assert cli.main(["timeaudit", str(good)]) == 0

    def test_timeaudit_prep_range_option(self, tmp_path, capsys):
        from src.junitparser import cli

        path = tmp_path / "report.xml"
        path.write_text(
            '<testsuite name="s" time="1.010">'
            '<testcase name="a" time="0.500"/>'
            '<testcase name="b" time="0.500"/>'
            "</testsuite>"
        )
        assert cli.main(["timeaudit", str(path)]) == 1
        assert "SHORTFALL" in capsys.readouterr().out
        assert cli.main(["timeaudit", "--prep-max", "0.02", str(path)]) == 0
