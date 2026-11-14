import json

import pytest

from src.junitparser.coverage import (
    CoverageReport,
    PlanItem,
    PlatformCoverage,
    Verdict,
    compute_coverage,
    load_capabilities,
    load_plan,
    main,
    read_report_verdicts,
)


def write_xml(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def plan():
    return [
        PlanItem("suite.test_pass"),
        PlanItem("suite.test_fail"),
        PlanItem("suite.test_skip"),
        PlanItem("suite.test_missing"),
        PlanItem("suite.test_gpu_only", requires=frozenset({"gpu"})),
        PlanItem("suite.test_not_arm", forbids=frozenset({"arm"})),
        PlanItem(
            "suite.test_contradictory",
            requires=frozenset({"gpu"}),
            forbids=frozenset({"gpu"}),
        ),
    ]


@pytest.fixture
def capabilities():
    return {
        "device-x": ["gpu"],
        "device-y": ["arm"],
        "device-z": ["gpu", "arm"],
    }


@pytest.fixture
def report_x(tmp_path):
    return write_xml(
        tmp_path,
        "x.xml",
        """<testsuites><testsuite name="s" tests="5" failures="1" errors="1" skipped="1">
  <testcase classname="suite" name="test_pass"/>
  <testcase classname="suite" name="test_fail"><failure/></testcase>
  <testcase classname="suite" name="test_skip"><skipped/></testcase>
  <testcase classname="suite" name="test_gpu_only"><error/></testcase>
  <testcase classname="suite" name="test_not_arm"/>
</testsuite></testsuites>""",
    )


@pytest.fixture
def report_y(tmp_path):
    return write_xml(
        tmp_path,
        "y.xml",
        """<testsuite name="s" tests="3" failures="0" errors="0" skipped="0">
  <testcase classname="suite" name="test_pass"/>
  <testcase classname="suite" name="test_fail"/>
  <testcase classname="suite" name="test_skip"/>
</testsuite>""",
    )


class TestComputeCoverage:
    def test_firm_coverage_and_unknowns(self, plan, capabilities, report_x, report_y):
        # device-z never submits a report.
        report = compute_coverage(
            plan, capabilities, {"device-x": report_x, "device-y": report_y}
        )
        cov_x = report.platforms["device-x"]
        assert cov_x.passed == ["suite.test_pass", "suite.test_not_arm"]
        assert cov_x.failed == ["suite.test_fail"]
        assert cov_x.errored == ["suite.test_gpu_only"]
        assert cov_x.unknown == {
            "suite.test_skip": "skipped",
            "suite.test_missing": "missing",
        }
        # Firm coverage only counts firm verdicts.
        assert cov_x.coverage == pytest.approx(2 / 4)

    def test_applicability_by_capability(self, plan, capabilities, report_y):
        report = compute_coverage(plan, capabilities, {"device-y": report_y})
        cov_y = report.platforms["device-y"]
        # device-y has no gpu and has arm.
        assert cov_y.not_applicable == ["suite.test_gpu_only", "suite.test_not_arm"]
        assert cov_y.passed == ["suite.test_pass", "suite.test_fail", "suite.test_skip"]
        assert cov_y.unknown == {"suite.test_missing": "missing"}
        assert cov_y.coverage == 1.0

    def test_no_report_platform(self, plan, capabilities, report_x):
        report = compute_coverage(plan, capabilities, {"device-x": report_x})
        cov_z = report.platforms["device-z"]
        assert cov_z.passed == [] and cov_z.failed == [] and cov_z.errored == []
        assert set(cov_z.unknown) == {
            "suite.test_pass",
            "suite.test_fail",
            "suite.test_skip",
            "suite.test_missing",
            "suite.test_gpu_only",
        }
        assert all(v == "no_report" for v in cov_z.unknown.values())
        assert cov_z.coverage is None

    def test_contradictory_cells_listed_separately(
        self, plan, capabilities, report_x, report_y
    ):
        report = compute_coverage(
            plan, capabilities, {"device-x": report_x, "device-y": report_y}
        )
        assert report.contradictory == ["suite.test_contradictory"]
        for cov in report.platforms.values():
            assert cov.contradictory == ["suite.test_contradictory"]
            assert "suite.test_contradictory" not in cov.denominator
            assert "suite.test_contradictory" not in cov.unknown
            assert "suite.test_contradictory" not in cov.not_applicable

    def test_bounds_assuming_unknowns_pass_or_fail(
        self, plan, capabilities, report_x
    ):
        report = compute_coverage(plan, capabilities, {"device-x": report_x})
        cov_x = report.platforms["device-x"]
        # Applicable cells on device-x: 4 firm + 2 unknown = 6.
        assert cov_x.applicable_total == 6
        # All unknowns pass: (2 + 2) / 6; all fail: 2 / 6.
        assert cov_x.coverage_best == pytest.approx(4 / 6)
        assert cov_x.coverage_worst == pytest.approx(2 / 6)

    def test_denominator_traces_back_to_plan_items(
        self, plan, capabilities, report_x
    ):
        report = compute_coverage(plan, capabilities, {"device-x": report_x})
        cov_x = report.platforms["device-x"]
        assert sorted(cov_x.denominator) == [
            "suite.test_fail",
            "suite.test_gpu_only",
            "suite.test_not_arm",
            "suite.test_pass",
        ]
        plan_ids = {item.id for item in plan}
        assert set(cov_x.denominator) <= plan_ids


class TestSuiteLevelSkip:
    def test_platform_skip_does_not_erase_case_evidence(self, tmp_path):
        path = write_xml(
            tmp_path,
            "skipped.xml",
            """<testsuite name="s" tests="3" failures="1" errors="0" skipped="3">
  <testcase classname="suite" name="test_a"><failure/></testcase>
  <testcase classname="suite" name="test_b"/>
  <testcase classname="suite" name="test_c"><skipped/></testcase>
</testsuite>""",
        )
        plan = [
            PlanItem("suite.test_a"),
            PlanItem("suite.test_b"),
            PlanItem("suite.test_c"),
        ]
        report = compute_coverage(plan, {"dev": []}, {"dev": path})
        cov = report.platforms["dev"]
        # The received failure survives the suite-level skip marker.
        assert cov.failed == ["suite.test_a"]
        # Cases without their own evidence inherit the platform skip.
        assert cov.unknown == {
            "suite.test_b": "skipped",
            "suite.test_c": "skipped",
        }
        assert cov.coverage == 0.0

    def test_normal_suite_no_result_means_passed(self, tmp_path):
        path = write_xml(
            tmp_path,
            "normal.xml",
            """<testsuite name="s" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="suite" name="test_a"/>
</testsuite>""",
        )
        verdicts = read_report_verdicts(path)
        assert verdicts["suite.test_a"] is Verdict.PASSED


class TestReportReading:
    def test_duplicate_entries_firm_verdict_wins(self, tmp_path):
        path = write_xml(
            tmp_path,
            "dup.xml",
            """<testsuites>
  <testsuite name="s1" tests="1" skipped="1">
    <testcase classname="suite" name="test_a"><skipped/></testcase>
  </testsuite>
  <testsuite name="s2" tests="1">
    <testcase classname="suite" name="test_a"/>
  </testsuite>
</testsuites>""",
        )
        verdicts = read_report_verdicts(path)
        assert verdicts["suite.test_a"] is Verdict.PASSED

    def test_bare_name_fallback(self, tmp_path):
        path = write_xml(
            tmp_path,
            "bare.xml",
            """<testsuite name="s" tests="1">
  <testcase name="test_a"/>
</testsuite>""",
        )
        plan = [PlanItem("any.prefix.test_a")]
        report = compute_coverage(plan, {"dev": []}, {"dev": path})
        assert report.platforms["dev"].passed == ["any.prefix.test_a"]


class TestIO:
    def test_load_plan_and_capabilities(self, tmp_path):
        plan_path = write_xml(
            tmp_path,
            "plan.json",
            json.dumps(
                [{"id": "a", "requires": ["gpu"], "forbids": ["arm"]}, {"id": "b"}]
            ),
        )
        caps_path = write_xml(
            tmp_path, "caps.json", json.dumps({"dev": ["gpu"]})
        )
        plan = load_plan(plan_path)
        assert plan[0] == PlanItem("a", frozenset({"gpu"}), frozenset({"arm"}))
        assert plan[1] == PlanItem("b")
        assert load_capabilities(caps_path) == {"dev": ["gpu"]}

    def test_cli(self, tmp_path, capsys):
        plan_path = write_xml(
            tmp_path, "plan.json", json.dumps([{"id": "suite.test_a"}])
        )
        caps_path = write_xml(
            tmp_path, "caps.json", json.dumps({"dev-ok": [], "dev-silent": []})
        )
        xml_path = write_xml(
            tmp_path,
            "ok.xml",
            """<testsuite name="s" tests="1">
  <testcase classname="suite" name="test_a"/>
</testsuite>""",
        )
        rc = main(
            [
                "--plan",
                str(plan_path),
                "--capabilities",
                str(caps_path),
                "--report",
                f"dev-ok={xml_path}",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["platforms"]["dev-ok"]["coverage"] == 1.0
        assert out["platforms"]["dev-ok"]["denominator"] == ["suite.test_a"]
        assert out["platforms"]["dev-silent"]["unknown"] == {
            "suite.test_a": "no_report"
        }
        assert out["platforms"]["dev-silent"]["coverage"] is None
