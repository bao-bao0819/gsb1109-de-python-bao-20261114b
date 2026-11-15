import json
from pathlib import Path

import pytest

from src.junitparser import (
    CONTINUE,
    END,
    SPLIT,
    Failure,
    JUnitXml,
    Properties,
    Property,
    TestCase,
    TestSuite,
    triage,
)
from src.junitparser import cli
from src.junitparser.triage import to_markdown, write_evidence_files


def _case(
    classname, name, category, message="failed", text=None, type_="AssertionError"
):
    case = TestCase(name=name, classname=classname)
    failure = Failure(message, type_)
    failure.text = text
    case.result = [failure]
    if category:
        props = Properties()
        props.add_property(Property("failure_category", category))
        case.append(props)
    return case


def write_archive(path, cases):
    xml = JUnitXml()
    suite = TestSuite("suite")
    for case in cases:
        suite._add_testcase_no_update_stats(case)
    xml.add_testsuite(suite)
    xml.update_statistics()
    xml.write(str(path))
    return path


def sample_id(classname, name):
    return f"{classname}::{name}"


@pytest.fixture
def archives(tmp_path):
    old_cases = [
        _case("pkg", f"test_t{i}", category)
        for i, category in [(1, "A"), (2, "A"), (3, "A"), (4, "A"), (5, "B"), (6, "B")]
    ]
    old_cases.append(_case("pkg", "test_t7", "C", message="Boom", text="traceback-7"))
    new_cases = [
        _case("pkg", f"test_t{i}", category)
        for i, category in [
            (1, "X"),
            (2, "X"),
            (3, "X"),
            (4, "Y"),
            (5, "Y"),
            (6, "Z"),
        ]
    ]
    # Same error text as t7, but a brand-new sample: text-only overlap.
    new_cases.append(_case("pkg", "test_t8", "W", message="Boom", text="traceback-7"))
    old_path = write_archive(tmp_path / "old.xml", old_cases)
    new_path = write_archive(tmp_path / "new.xml", new_cases)
    return old_path, new_path


SUBSCRIPTIONS = [
    {"alert_id": "alert-a", "old_category": "A"},
    {"alert_id": "alert-b", "old_category": "B"},
    {"alert_id": "alert-c", "old_category": "C"},
]


def test_decisions_and_unanimous_links(tmp_path, archives):
    old_path, new_path = archives
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(json.dumps(SUBSCRIPTIONS))

    report = triage(
        [str(old_path)],
        [str(new_path)],
        subscriptions_file=str(subscriptions),
    )
    decisions = {decision.alert_id: decision for decision in report.decisions}

    # A -> X is forced (weight 3); A -> Y and B -> Y both have weight 1,
    # so Y can hang off either old category: two equally good explanations.
    assert decisions["alert-a"].decision == CONTINUE
    assert decisions["alert-a"].target_categories == ["X"]
    assert decisions["alert-a"].relation == "one_to_one"
    assert decisions["alert-a"].supporting_sample_ids == sorted(
        sample_id("pkg", f"test_t{i}") for i in (1, 2, 3)
    )
    candidate_y = next(
        edge
        for edge in decisions["alert-a"].candidate_edges
        if edge["new_category"] == "Y"
    )
    assert candidate_y["unanimous"] is False

    # B -> Z is forced; B -> Y remains contested, so only Z is renewed.
    assert decisions["alert-b"].decision == CONTINUE
    assert decisions["alert-b"].target_categories == ["Z"]
    assert decisions["alert-b"].supporting_sample_ids == [sample_id("pkg", "test_t6")]

    # C has no shared sample at all.
    assert decisions["alert-c"].decision == END
    assert decisions["alert-c"].confidence == "no_shared_sample"
    assert decisions["alert-c"].target_categories == []

    # Two equally good explanations (Y may hang off A or B).
    assert report.optimal_weight == 5
    assert len(report.explanations) == 2
    assert decisions["alert-a"].explanation_count == 2


def test_text_only_overlap_is_not_continuity(tmp_path, archives):
    old_path, new_path = archives
    report = triage([str(old_path)], [str(new_path)])

    matrix = report.weight_matrix
    assert "W" not in matrix.get("C", {})
    overlaps = {
        (item["old_category"], item["new_category"]): item
        for item in report.text_only_overlaps
    }
    assert ("C", "W") in overlaps
    assert overlaps[("C", "W")]["shared_error_signatures"]


def test_split_into_two_unanimous_targets(tmp_path):
    old_cases = [_case("pkg", f"test_s{i}", "D") for i in (1, 2, 3)]
    new_cases = [
        _case("pkg", "test_s1", "P"),
        _case("pkg", "test_s2", "P"),
        _case("pkg", "test_s3", "Q"),
    ]
    old_path = write_archive(tmp_path / "old.xml", old_cases)
    new_path = write_archive(tmp_path / "new.xml", new_cases)

    report = triage([str(old_path)], [str(new_path)])
    decision = report.decisions[0]

    assert len(report.explanations) == 1
    assert decision.decision == SPLIT
    assert decision.target_categories == ["P", "Q"]
    assert decision.supporting_sample_ids == sorted(
        sample_id("pkg", f"test_s{i}") for i in (1, 2, 3)
    )


def test_merge_contested_ends_both_alerts(tmp_path):
    old_cases = [
        _case("pkg", "test_u1", "M"),
        _case("pkg", "test_u2", "M"),
        _case("pkg", "test_u2", "N"),
        _case("pkg", "test_u3", "N"),
    ]
    new_cases = [_case("pkg", f"test_u{i}", "R") for i in (1, 2, 3)]
    old_path = write_archive(tmp_path / "old.xml", old_cases)
    new_path = write_archive(tmp_path / "new.xml", new_cases)

    report = triage([str(old_path)], [str(new_path)])
    decisions = {decision.old_category: decision for decision in report.decisions}

    # R may come from M or N: two equal-weight explanations, no unanimous edge.
    assert report.weight_matrix["M"]["R"] == 2
    assert report.weight_matrix["N"]["R"] == 2
    assert len(report.explanations) == 2
    assert decisions["M"].decision == END
    assert decisions["N"].decision == END
    assert decisions["M"].confidence == "contested"
    assert decisions["M"].supporting_sample_ids == []


def test_asymmetric_merge_marks_rival_relation(tmp_path):
    old_cases = [
        _case("pkg", "test_m1", "M"),
        _case("pkg", "test_m2", "M"),
        _case("pkg", "test_m3", "M"),
        _case("pkg", "test_m1", "N"),
        _case("pkg", "test_n2", "N"),
    ]
    new_cases = [
        _case("pkg", "test_m1", "R"),
        _case("pkg", "test_m2", "R"),
        _case("pkg", "test_m3", "R"),
        _case("pkg", "test_n2", "S"),
    ]
    old_path = write_archive(tmp_path / "old.xml", old_cases)
    new_path = write_archive(tmp_path / "new.xml", new_cases)

    report = triage([str(old_path)], [str(new_path)])
    decisions = {decision.old_category: decision for decision in report.decisions}

    # M -> R is forced by weight 3; N's thin overlap with R cannot compete.
    assert len(report.explanations) == 1
    assert decisions["M"].decision == CONTINUE
    assert decisions["M"].relation == "merge_contested"
    assert decisions["M"].rivals == ["N"]
    assert decisions["N"].decision == CONTINUE
    assert decisions["N"].relation == "one_to_one"
    assert decisions["N"].target_categories == ["S"]


def test_classification_file_overrides_inline_categories(tmp_path):
    old_cases = [_case("pkg", "test_v1", None), _case("pkg", "test_v2", None)]
    new_cases = [_case("pkg", "test_v1", None)]
    old_path = write_archive(tmp_path / "old.xml", old_cases)
    new_path = write_archive(tmp_path / "new.xml", new_cases)
    classification = tmp_path / "classification.json"
    classification.write_text(
        json.dumps(
            {
                "old": [
                    {"category": "OLD", "classname": "pkg", "name": "test_v1"},
                    {"category": "OLD", "classname": "pkg", "name": "test_v2"},
                ],
                "new": [{"category": "NEW", "classname": "pkg", "name": "test_v1"}],
            }
        )
    )

    report = triage(
        [str(old_path)],
        [str(new_path)],
        classification_file=str(classification),
    )
    decision = report.decisions[0]
    assert decision.decision == CONTINUE
    assert decision.target_categories == ["NEW"]
    assert decision.supporting_sample_ids == [sample_id("pkg", "test_v1")]
    assert report.warnings == []


def test_sharded_archive_is_merged_and_deduped(tmp_path):
    # The same failing case appears in two shards of the old archive.
    shard1 = write_archive(tmp_path / "old_1.xml", [_case("pkg", "test_w1", "G")])
    shard2 = write_archive(tmp_path / "old_2.xml", [_case("pkg", "test_w1", "G")])
    new_path = write_archive(tmp_path / "new.xml", [_case("pkg", "test_w1", "H")])

    report = triage([str(shard1), str(shard2)], [str(new_path)])
    assert report.weight_matrix["G"]["H"] == 1
    sample = report.samples[sample_id("pkg", "test_w1")]
    assert len(sample.occurrences) == 3  # two old shards + one new archive


def test_evidence_files_are_traceable_per_alert(tmp_path, archives):
    old_path, new_path = archives
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(json.dumps(SUBSCRIPTIONS))
    report = triage(
        [str(old_path)],
        [str(new_path)],
        subscriptions_file=str(subscriptions),
    )

    evidence_dir = tmp_path / "evidence"
    written = write_evidence_files(report, evidence_dir)
    names = sorted(Path(path).name for path in written)
    assert names == ["alert-a.json", "alert-b.json"]  # ended alert C gets no file

    payload = json.loads((evidence_dir / "alert-a.json").read_text())
    assert payload["decision"]["decision"] == CONTINUE
    records = {sample["sample_id"]: sample for sample in payload["supporting_samples"]}
    assert sorted(records) == [sample_id("pkg", f"test_t{i}") for i in (1, 2, 3)]
    sample = records[sample_id("pkg", "test_t1")]
    archives_seen = {occ["archive"] for occ in sample["occurrences"]}
    assert archives_seen == {"old", "new"}
    assert sample["occurrences"][0]["results"][0]["message"] == "failed"


def test_absent_subscribed_category_is_flagged(tmp_path, archives):
    old_path, new_path = archives
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(
        json.dumps([{"alert_id": "ghost", "old_category": "DOES_NOT_EXIST"}])
    )
    report = triage(
        [str(old_path)],
        [str(new_path)],
        subscriptions_file=str(subscriptions),
    )
    decision = report.decisions[0]
    assert decision.decision == END
    assert decision.confidence == "category_absent"


def test_cli_triage_writes_json_markdown_and_evidence(tmp_path, archives):
    old_path, new_path = archives
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(json.dumps(SUBSCRIPTIONS))
    json_report = tmp_path / "report.json"
    evidence_dir = tmp_path / "evidence"

    exit_code = cli.main(
        [
            "triage",
            "--old",
            str(old_path),
            "--new",
            str(new_path),
            "--subscriptions",
            str(subscriptions),
            "--evidence-dir",
            str(evidence_dir),
            str(json_report),
        ]
    )
    assert exit_code == 0
    payload = json.loads(json_report.read_text())
    assert {item["alert_id"] for item in payload["decisions"]} == {
        "alert-a",
        "alert-b",
        "alert-c",
    }
    assert (evidence_dir / "alert-a.json").exists()

    markdown_report = tmp_path / "report.md"
    assert (
        cli.main(
            [
                "triage",
                "--old",
                str(old_path),
                "--new",
                str(new_path),
                "--subscriptions",
                str(subscriptions),
                str(markdown_report),
            ]
        )
        == 0
    )
    markdown = markdown_report.read_text()
    assert "Failure alert triage" in markdown
    assert "alert-a" in markdown
    assert "Text-only overlaps" in markdown


def test_markdown_render_smoke(tmp_path, archives):
    old_path, new_path = archives
    report = triage([str(old_path)], [str(new_path)])
    markdown = to_markdown(report)
    assert "Optimal weight: 5" in markdown
    assert "optimal explanation" in markdown
