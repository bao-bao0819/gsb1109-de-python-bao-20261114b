"""Tests for the worst-case recheck planner."""

import json
from pathlib import Path

import pytest

from src.junitparser import Error, Failure, JUnitXml
from src.junitparser import cli
from src.junitparser.xunit2 import TestCase, TestSuite
from src.junitparser.recheck import (
    InvalidDurationError,
    PrerequisiteCycleError,
    RecheckError,
    UnknownCaseError,
    case_id,
    format_plan,
    load_history,
    plan_as_dict,
    plan_as_json,
    plan_recheck,
)


def write_report(path: Path, cases, *, prefix: str | None = None) -> Path:
    """Write a JUnit report from a list of case specifications."""
    suite = TestSuite("history")
    for spec in cases:
        case = TestCase(spec["name"], spec.get("classname", "tests.suite"))
        if spec.get("time") is not None:
            case.time = spec["time"]
        for failure_type in spec.get("failures", []):
            case.result = (case.result or []) + [Failure("boom", failure_type)]
        for failure_type in spec.get("errors", []):
            case.result = (case.result or []) + [Error("boom", failure_type)]
        properties = spec.get("properties", {})
        if properties:
            for key, value in properties.items():
                case.add_property(key, value)
        suite.add_testcase(case)
    xml = JUnitXml(prefix)
    xml.add_testsuite(suite)
    xml.update_statistics()
    xml.write(str(path))
    return path


def build_repo(tmp_path: Path, sample: dict[str, dict] | None = None) -> Path:
    cases = []
    for name, spec in (sample or DEFAULT_HISTORY).items():
        entry = {"name": name, **spec}
        cases.append(entry)
    return write_report(tmp_path / "history.xml", cases)


DEFAULT_HISTORY = {
    "parse": {
        "failures": ["xml"],
        "properties": {"duration_min": "1.0", "duration_max": "4.0"},
    },
    "serialize": {
        "failures": ["xml"],
        "properties": {
            "duration_min": "0.5",
            "duration_max": "6.0",
            "depends_on": "parse",
        },
    },
    "locale": {
        "failures": ["locale"],
        "properties": {"duration_min": "1.0", "duration_max": "5.0"},
    },
    "cli": {
        "failures": ["cli"],
        "properties": {"duration_min": "0.5", "duration_max": "3.0"},
    },
    "flaky": {
        "failures": ["flaky"],
        "properties": {"duration_min": "0.5", "duration_max": "2.0"},
    },
    "merge": {
        "failures": ["xml", "cli"],
        "properties": {"duration_min": "1.0", "duration_max": "7.0"},
    },
    "slow-device": {
        "failures": ["hardware"],
        "properties": {"duration_min": "1.0", "duration_max": "10.0"},
    },
}


class TestCaseIdentity:
    def test_classname_and_name(self):
        assert case_id("tests.A", "test_x") == "tests.A::test_x"

    def test_name_only(self):
        assert case_id(None, "test_x") == "test_x"


class TestHistoryLoading:
    def test_reads_case_duration_interval(self, tmp_path):
        path = build_repo(tmp_path)
        records = load_history([path])
        record = records["tests.suite::parse"]
        assert record.low == 1.0
        assert record.high == 4.0

    def test_reads_failure_classes(self, tmp_path):
        path = build_repo(tmp_path)
        records = load_history([path])
        assert records["tests.suite::merge"].classes == {"xml", "cli"}

    def test_unclassified_when_result_has_no_type(self, tmp_path):
        path = write_report(
            tmp_path / "plain.xml",
            [{"name": "broken", "failures": [None]}],
        )
        records = load_history([path])
        assert records["tests.suite::broken"].classes == {"unclassified"}

    def test_merges_samples_across_reports(self, tmp_path):
        first = write_report(tmp_path / "a.xml", [{"name": "t", "time": 2.0}])
        second = write_report(tmp_path / "b.xml", [{"name": "t", "time": 9.0}])
        records = load_history([first, second])
        record = records["tests.suite::t"]
        assert (record.low, record.high) == (2.0, 9.0)

    def test_case_without_duration_is_unbounded(self, tmp_path):
        path = write_report(tmp_path / "nodur.xml", [{"name": "t"}])
        records = load_history([path])
        assert records["tests.suite::t"].high is None

    def test_rejects_negative_duration_property(self, tmp_path):
        path = write_report(
            tmp_path / "bad.xml",
            [{"name": "t", "properties": {"duration_max": "-3"}}],
        )
        with pytest.raises(InvalidDurationError):
            load_history([path])

    def test_override_supplies_upper_bound(self, tmp_path):
        path = write_report(tmp_path / "nodur.xml", [{"name": "t"}])
        records = load_history(
            [path],
            overrides={"tests.suite::t": {"high": 3.5, "classes": "io"}},
        )
        record = records["tests.suite::t"]
        assert record.high == 3.5
        assert record.classes == {"io"}
        assert "tests.suite::t" in records

    def test_override_resolves_by_name(self, tmp_path):
        path = write_report(tmp_path / "one.xml", [{"name": "unique"}])
        records = load_history([path], overrides={"unique": {"high": 2.0}})
        assert records["tests.suite::unique"].high == 2.0
        assert len(records) == 1


class TestWorstCaseBudget:
    def test_uses_upper_bounds_not_means(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=20.0, slots=1, weights={"xml": 5})
        used = sum(entry.high for entry in result.entries)
        assert used > 10.0
        assert result.makespan <= 20.0

    def test_mean_is_not_used_for_selection(self, tmp_path):
        # Low=0.5, high=9.0: a mean of 4.75 would look affordable in 6s.
        path = write_report(
            tmp_path / "wide.xml",
            [
                {
                    "name": "wide",
                    "failures": ["xml"],
                    "properties": {"duration_min": "0.5", "duration_max": "9.0"},
                }
            ],
        )
        result = plan_recheck([path], window=6.0, slots=1)
        assert result.feasible
        assert result.entries == ()

    def test_makespan_is_derived_from_upper_bounds(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=30.0, slots=2)
        assert result.makespan == max(entry.end for entry in result.entries)
        assert result.slack == pytest.approx(30.0 - result.makespan)

    def test_unbounded_candidate_is_never_scheduled(self, tmp_path):
        path = write_report(
            tmp_path / "mixed.xml",
            [
                {
                    "name": "known",
                    "failures": ["xml"],
                    "properties": {"duration_max": "2.0"},
                },
                {"name": "unknown", "failures": ["io"]},
            ],
        )
        result = plan_recheck([path], window=40.0, slots=2)
        assert [entry.case_id for entry in result.entries] == ["tests.suite::known"]
        assert result.unbounded == ("tests.suite::unknown",)


class TestCoverageWeight:
    def test_same_class_credited_once(self, tmp_path):
        path = write_report(
            tmp_path / "dupes.xml",
            [
                {
                    "name": "a",
                    "failures": ["xml"],
                    "properties": {"duration_max": "1.0"},
                },
                {
                    "name": "b",
                    "failures": ["xml"],
                    "properties": {"duration_max": "1.0"},
                },
                {
                    "name": "c",
                    "failures": ["locale"],
                    "properties": {"duration_max": "1.0"},
                },
            ],
        )
        result = plan_recheck(
            [path],
            window=100.0,
            slots=1,
            weights={"xml": 10},
            required=["tests.suite::a", "tests.suite::b"],
        )
        assert result.covered == {"xml": 10.0, "locale": 1.0}
        assert result.total_weight == 11.0
        credits = {entry.case_id: entry.credit for entry in result.entries}
        assert credits["tests.suite::c"] == 1.0
        assert sorted(credits.values()) == [0.0, 1.0, 10.0]

    def test_zero_gain_overlap_is_not_selected(self, tmp_path):
        path = write_report(
            tmp_path / "overlap.xml",
            [
                {
                    "name": "cheaper",
                    "failures": ["xml"],
                    "properties": {"duration_max": "1.0"},
                },
                {
                    "name": "pricey",
                    "failures": ["xml"],
                    "properties": {"duration_max": "9.0"},
                },
            ],
        )
        result = plan_recheck([path], window=100.0, slots=1)
        assert [entry.case_id for entry in result.entries] == ["tests.suite::cheaper"]

    def test_prefers_high_weight_classes(self, tmp_path):
        path = write_report(
            tmp_path / "weights.xml",
            [
                {
                    "name": "cheap-low",
                    "failures": ["low"],
                    "properties": {"duration_max": "1.0"},
                },
                {
                    "name": "cheap-high",
                    "failures": ["high"],
                    "properties": {"duration_max": "1.0"},
                },
            ],
        )
        result = plan_recheck(
            [path], window=1.0, slots=1, weights={"high": 100, "low": 1}
        )
        assert [entry.case_id for entry in result.entries] == [
            "tests.suite::cheap-high"
        ]
        assert result.total_weight == 100.0

    def test_multi_class_case_credit_is_summed_once(self, tmp_path):
        path = write_report(
            tmp_path / "multi.xml",
            [
                {
                    "name": "both",
                    "failures": ["xml"],
                    "errors": ["io"],
                    "properties": {"duration_max": "2.0"},
                }
            ],
        )
        result = plan_recheck([path], window=10.0, slots=1, weights={"xml": 3, "io": 4})
        assert result.entries[0].credit == 7.0
        assert result.total_weight == 7.0

    def test_default_weight_applies_to_unlisted_classes(self, tmp_path):
        path = write_report(
            tmp_path / "default.xml",
            [
                {
                    "name": "x",
                    "failures": ["misc"],
                    "properties": {"duration_max": "1.0"},
                }
            ],
        )
        result = plan_recheck([path], window=5.0, default_weight=2.5)
        assert result.total_weight == 2.5


class TestPrerequisites:
    def test_selecting_child_pulls_in_parent(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=100.0, slots=1, weights={"xml": 1})
        selected = {entry.case_id for entry in result.entries}
        if "tests.suite::serialize" in selected:
            assert "tests.suite::parse" in selected

    def test_parent_runs_before_child(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck(
            [path],
            window=100.0,
            slots=2,
            weights={"xml": 100},
            required=["tests.suite::serialize"],
        )
        times = {entry.case_id: (entry.start, entry.end) for entry in result.entries}
        assert times["tests.suite::parse"][1] <= times["tests.suite::serialize"][0]

    def test_explicit_prerequisite_edge(self, tmp_path):
        path = write_report(
            tmp_path / "edges.xml",
            [
                {
                    "name": "a",
                    "failures": ["one"],
                    "properties": {"duration_max": "2.0"},
                },
                {
                    "name": "b",
                    "failures": ["two"],
                    "properties": {"duration_max": "2.0"},
                },
            ],
        )
        result = plan_recheck(
            [path],
            window=100.0,
            slots=1,
            prerequisites={"tests.suite::b": ["tests.suite::a"]},
            required=["tests.suite::b"],
        )
        entries = {entry.case_id: entry for entry in result.entries}
        assert entries["tests.suite::a"].end <= entries["tests.suite::b"].start

    def test_unknown_prerequisite_is_rejected(self, tmp_path):
        path = build_repo(tmp_path)
        with pytest.raises(UnknownCaseError):
            plan_recheck(
                [path],
                window=100.0,
                prerequisites={"tests.suite::cli": ["tests.suite::missing"]},
            )

    def test_cycle_is_rejected(self, tmp_path):
        path = write_report(
            tmp_path / "cycle.xml",
            [
                {"name": "a", "properties": {"duration_max": "1.0"}},
                {"name": "b", "properties": {"duration_max": "1.0"}},
            ],
        )
        with pytest.raises(PrerequisiteCycleError):
            plan_recheck(
                [path],
                window=100.0,
                prerequisites={
                    "tests.suite::a": ["tests.suite::b"],
                    "tests.suite::b": ["tests.suite::a"],
                },
            )


class TestDeviceSlots:
    def test_device_cases_never_overlap(self, tmp_path):
        path = write_report(
            tmp_path / "devices.xml",
            [
                {
                    "name": f"d{index}",
                    "failures": [f"kind{index}"],
                    "properties": {
                        "duration_max": "4.0",
                        "device": "rig-a",
                    },
                }
                for index in range(4)
            ],
        )
        result = plan_recheck([path], window=100.0, slots=8)
        intervals = [
            (entry.start, entry.end)
            for entry in result.entries
            if entry.device == "rig-a"
        ]
        assert len(intervals) == 4
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            assert previous[1] <= current[0]

    def test_parallel_slots_reduce_makespan(self, tmp_path):
        path = write_report(
            tmp_path / "parallel.xml",
            [
                {
                    "name": f"c{index}",
                    "failures": [f"kind{index}"],
                    "properties": {"duration_max": "3.0"},
                }
                for index in range(4)
            ],
        )
        single = plan_recheck([path], window=100.0, slots=1)
        double = plan_recheck([path], window=100.0, slots=2)
        assert single.makespan == pytest.approx(12.0)
        assert double.makespan == pytest.approx(6.0)
        assert len({entry.lane for entry in double.entries}) == 2

    def test_more_slots_do_not_run_less(self, tmp_path):
        path = build_repo(tmp_path)
        for slots in (1, 2, 4):
            result = plan_recheck([path], window=12.0, slots=slots)
            assert result.makespan <= 12.0


class TestBottlenecks:
    def test_prerequisite_chain_blocks_window(self, tmp_path):
        path = write_report(
            tmp_path / "chain.xml",
            [
                {
                    "name": "one",
                    "properties": {"duration_max": "5.0"},
                },
                {
                    "name": "two",
                    "properties": {"duration_max": "5.0", "depends_on": "one"},
                },
                {
                    "name": "three",
                    "properties": {"duration_max": "5.0", "depends_on": "two"},
                },
            ],
        )
        result = plan_recheck(
            [path], window=10.0, slots=8, required=["tests.suite::three"]
        )
        assert not result.feasible
        assert result.bottleneck == "prerequisite_chain"
        assert "prerequisite chain" in result.evidence[0]

    def test_dedicated_device_load_blocks_window(self, tmp_path):
        path = write_report(
            tmp_path / "device.xml",
            [
                {
                    "name": f"d{index}",
                    "properties": {"duration_max": "7.0", "device": "rig-a"},
                }
                for index in range(2)
            ],
        )
        result = plan_recheck(
            [path],
            window=10.0,
            slots=8,
            required=["tests.suite::d0", "tests.suite::d1"],
        )
        assert result.bottleneck == "dedicated_device"
        assert "rig-a" in result.evidence[0]

    def test_slot_capacity_blocks_window(self, tmp_path):
        path = write_report(
            tmp_path / "capacity.xml",
            [
                {
                    "name": f"c{index}",
                    "properties": {"duration_max": "6.0"},
                }
                for index in range(3)
            ],
        )
        result = plan_recheck(
            [path],
            window=10.0,
            slots=1,
            required=[f"tests.suite::c{index}" for index in range(3)],
        )
        assert result.bottleneck == "slot_capacity"

    def test_prerequisite_chain_beats_device_when_both_exceed(self, tmp_path):
        path = write_report(
            tmp_path / "both.xml",
            [
                {
                    "name": "one",
                    "properties": {"duration_max": "8.0", "device": "rig-a"},
                },
                {
                    "name": "two",
                    "properties": {
                        "duration_max": "8.0",
                        "device": "rig-a",
                        "depends_on": "one",
                    },
                },
            ],
        )
        result = plan_recheck(
            [path],
            window=10.0,
            slots=4,
            required=["tests.suite::two"],
        )
        assert not result.feasible
        assert result.bottleneck == "prerequisite_chain"

    def test_unbounded_required_case_is_flagged(self, tmp_path):
        path = write_report(
            tmp_path / "unknown.xml",
            [{"name": "mystery"}],
        )
        result = plan_recheck(
            [path], window=100.0, slots=4, required=["tests.suite::mystery"]
        )
        assert not result.feasible
        assert result.bottleneck == "unknown_duration"
        assert "average" in result.evidence[0]

    def test_feasible_plan_has_no_bottleneck(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=60.0, slots=4)
        assert result.feasible
        assert result.bottleneck is None


class TestPlanShape:
    def test_entries_do_not_exceed_window(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=12.0, slots=3)
        for entry in result.entries:
            assert entry.end <= result.window + 1e-9

    def test_required_cases_are_always_scheduled(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck(
            [path], window=60.0, slots=4, required=["tests.suite::flaky"]
        )
        assert "tests.suite::flaky" in {entry.case_id for entry in result.entries}
        assert result.mandatory == ("tests.suite::flaky",)

    def test_required_case_without_coverage_still_runs(self, tmp_path):
        path = write_report(
            tmp_path / "green.xml",
            [{"name": "green", "properties": {"duration_max": "2.0"}}],
        )
        result = plan_recheck([path], window=5.0, required=["tests.suite::green"])
        assert [entry.case_id for entry in result.entries] == ["tests.suite::green"]
        assert result.total_weight == 0.0

    def test_plan_is_deterministic(self, tmp_path):
        path = build_repo(tmp_path)
        first = plan_as_dict(plan_recheck([path], window=12.0, slots=3))
        second = plan_as_dict(plan_recheck([path], window=12.0, slots=3))
        assert first == second

    def test_json_round_trip(self, tmp_path):
        path = build_repo(tmp_path)
        result = plan_recheck([path], window=12.0, slots=3)
        payload = json.loads(plan_as_json(result))
        assert payload["feasible"] is True
        assert len(payload["entries"]) == len(result.entries)

    def test_text_rendering_mentions_infeasibility(self, tmp_path):
        path = write_report(
            tmp_path / "tight.xml",
            [{"name": "x", "properties": {"duration_max": "20.0"}}],
        )
        result = plan_recheck([path], window=5.0, slots=1, required=["tests.suite::x"])
        text = format_plan(result)
        assert "INFEASIBLE" in text
        assert "window" in text

    def test_invalid_inputs_raise(self, tmp_path):
        path = build_repo(tmp_path)
        with pytest.raises(RecheckError):
            plan_recheck([path], window=0, slots=1)
        with pytest.raises(RecheckError):
            plan_recheck([path], window=12.0, slots=0)


class TestCli:
    def test_recheck_command_text(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(["recheck", str(path), "--window", "1m", "--slots", "2"])
        captured = capsys.readouterr()
        assert status == 0
        assert "Recheck plan" in captured.out

    def test_recheck_command_infeasible_exit_code(self, tmp_path, capsys):
        path = write_report(
            tmp_path / "tight.xml",
            [{"name": "x", "properties": {"duration_max": "30.0"}}],
        )
        status = cli.main(
            [
                "recheck",
                str(path),
                "--window",
                "5s",
                "--required",
                "tests.suite::x",
            ]
        )
        captured = capsys.readouterr()
        assert status == 1
        assert "INFEASIBLE" in captured.out

    def test_recheck_command_json(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(["recheck", str(path), "--window", "1m", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert status == 0
        assert payload["feasible"] is True

    def test_recheck_command_parses_units_and_weights(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(
            [
                "recheck",
                str(path),
                "--window",
                "720",
                "--weight",
                "xml=9",
                "--weight",
                "locale=4",
            ]
        )
        assert status == 0
        out = capsys.readouterr().out
        assert "xml" in out and "locale" in out

    def test_recheck_command_puts_diagnostics_on_stderr(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(
            [
                "recheck",
                str(path),
                "--depends",
                "tests.suite::cli=tests.suite::missing",
            ]
        )
        captured = capsys.readouterr()
        assert status == 2
        assert "unknown case" in captured.err

    def test_recheck_command_accepts_devices_and_requires(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(
            [
                "recheck",
                str(path),
                "--window",
                "1m",
                "--slots",
                "3",
                "--device",
                "tests.suite::slow-device=rig-a",
                "--required",
                "tests.suite::flaky",
                "--depends",
                "tests.suite::serialize=tests.suite::parse",
            ]
        )
        assert status == 0
        assert "slow-device" in capsys.readouterr().out

    def test_recheck_help_lists_options(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["recheck", "--help"])
        captured = capsys.readouterr()
        assert excinfo.value.code == 0
        for option in ("--window", "--slots", "--weight", "--depends", "--device"):
            assert option in captured.out


class TestSpecFile:
    def test_spec_supplies_window_and_weights(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        spec = tmp_path / "plan.json"
        spec.write_text(
            json.dumps(
                {
                    "window": "2m",
                    "slots": 2,
                    "weights": {"xml": 50, "locale": 5},
                    "required": ["tests.suite::cli"],
                }
            )
        )
        status = cli.main(["recheck", str(path), "--spec", str(spec)])
        out = capsys.readouterr().out
        assert status == 0
        assert "window 120.000s" in out
        assert "tests.suite::cli (required)" in out

    def test_spec_can_be_overridden_by_flags(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        spec = tmp_path / "plan.json"
        spec.write_text(json.dumps({"window": 60, "slots": 1}))
        status = cli.main(
            ["recheck", str(path), "--spec", str(spec), "--window", "30s"]
        )
        assert status == 0
        assert "window 30.000s" in capsys.readouterr().out

    def test_spec_carries_overrides(self, tmp_path, capsys):
        path = write_report(tmp_path / "nodur.xml", [{"name": "t"}])
        spec = tmp_path / "plan.json"
        spec.write_text(
            json.dumps(
                {
                    "window": 10,
                    "overrides": {"tests.suite::t": {"high": 2.0, "classes": "io"}},
                }
            )
        )
        status = cli.main(["recheck", str(path), "--spec", str(spec)])
        out = capsys.readouterr().out
        assert status == 0
        assert "tests.suite::t" in out

    def test_missing_spec_reports_error(self, tmp_path, capsys):
        path = build_repo(tmp_path)
        status = cli.main(
            ["recheck", str(path), "--spec", str(tmp_path / "absent.json")]
        )
        assert status == 2
        assert "recheck:" in capsys.readouterr().err
