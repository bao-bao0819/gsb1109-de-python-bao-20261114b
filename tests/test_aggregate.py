import io

import pytest

from src.junitparser import (
    CaseKey,
    Error,
    Failure,
    JUnitXml,
    ReportConflict,
    ReportMode,
    ShardReportStore,
    ShardVersionError,
    TestCase,
    TestSuite,
    merge_shard_reports,
)
from src.junitparser.aggregate import _version_key


def make_case(name, classname="pkg", result=None):
    case = TestCase(name, classname, 1.0)
    if result == "failure":
        case.result = [Failure("boom", "AssertionError")]
    elif result == "error":
        case.result = [Error("bang", "RuntimeError")]
    elif result == "skipped":
        from src.junitparser import Skipped

        case.result = [Skipped("later")]
    return case


def make_report(cases):
    xml = JUnitXml()
    suite = TestSuite("suite")
    for case in cases:
        suite._add_testcase_no_update_stats(case)
    xml.add_testsuite(suite)
    xml.update_statistics()
    return xml


def key(name, classname="pkg"):
    return CaseKey(classname, name)


class TestVersionKey:
    def test_numeric_chunks_compare_as_numbers(self):
        assert _version_key("v9") < _version_key("v10")
        assert _version_key("1.2.9") < _version_key("1.2.10")

    def test_text_chunks_break_ties(self):
        assert _version_key("v1-alpha") < _version_key("v1-beta")


class TestIngestModes:
    def test_replace_withdraws_cases_missing_from_new_report(self):
        store = ShardReportStore()
        store.ingest(
            "dev-1",
            "1",
            make_report([make_case("a"), make_case("b", result="failure")]),
        )
        store.ingest("dev-1", "2", make_report([make_case("a")]))

        assert set(store.effective_cases("dev-1")) == {key("a")}
        assert store.failure_count("dev-1") == 0
        assert store.current_version("dev-1") == "2"

    def test_append_keeps_existing_cases(self):
        store = ShardReportStore()
        store.ingest("dev-1", "1", make_report([make_case("a", result="failure")]))
        store.ingest(
            "dev-1",
            "2",
            make_report([make_case("b", result="error")]),
            mode=ReportMode.APPEND,
        )

        assert set(store.effective_cases("dev-1")) == {key("a"), key("b")}
        assert store.failure_count("dev-1") == 2

    def test_append_mode_accepts_string_value(self):
        store = ShardReportStore()
        store.ingest("dev-1", "1", make_report([make_case("a")]))
        receipt = store.ingest(
            "dev-1", "2", make_report([make_case("b")]), mode="append"
        )
        assert receipt.mode is ReportMode.APPEND
        assert len(store.effective_cases("dev-1")) == 2

    def test_ingest_accepts_string_bytes_and_file_object(self):
        xml = make_report([make_case("a")])
        text = xml.tostring()

        for shard, report in (
            ("str", text.decode("utf-8")),
            ("bytes", text),
            ("file", io.BytesIO(text)),
        ):
            store = ShardReportStore()
            receipt = store.ingest(shard, "1", report)
            assert receipt.stats["tests"] == 1
            assert store.current_version(shard) == "1"

    def test_ingest_file_reads_path(self, tmp_path):
        path = tmp_path / "report.xml"
        make_report([make_case("a", result="failure")]).write(str(path))

        store = ShardReportStore()
        receipt = store.ingest_file("dev-1", "1", path)
        assert receipt.failures == 1
        assert store.failure_count("dev-1") == 1


class TestSameVersionEvidence:
    def test_identical_report_is_duplicate(self):
        store = ShardReportStore()
        report = make_report([make_case("a")])
        first = store.ingest("dev-1", "1", report)
        second = store.ingest("dev-1", "1", make_report([make_case("a")]))

        assert first.conflict is ReportConflict.NONE
        assert second.conflict is ReportConflict.DUPLICATE
        assert len(store.applied_receipts("dev-1")) == 1
        assert len(store.receipts("dev-1")) == 2

    def test_conflicting_same_version_keeps_both_copies(self):
        store = ShardReportStore()
        first = store.ingest(
            "dev-1", "1", make_report([make_case("a", result="failure")])
        )
        second = store.ingest("dev-1", "1", make_report([make_case("a")]))

        assert second.conflict is ReportConflict.CONFLICT
        assert len(store.receipts("dev-1")) == 2
        assert len(store.conflicts("dev-1")) == 1
        # Both copies survive as evidence, neither is destroyed.
        assert first.evidence is not second.evidence
        assert first.stats["failures"] == 1
        assert second.stats["failures"] == 0

    def test_conflict_resolution_is_independent_of_arrival_order(self):
        report_fail = make_report([make_case("a", result="failure")])
        report_pass = make_report([make_case("a")])

        forward = ShardReportStore()
        forward.ingest("dev-1", "1", report_fail)
        forward.ingest("dev-1", "1", report_pass)

        reverse = ShardReportStore()
        reverse.ingest("dev-1", "1", report_pass)
        reverse.ingest("dev-1", "1", report_fail)

        assert forward.reconcile_stats() == reverse.reconcile_stats()
        assert forward.reconcile().tostring() == reverse.reconcile().tostring()


class TestStaleReports:
    def test_late_older_report_cannot_resurrect_withdrawn_case(self):
        store = ShardReportStore()
        store.ingest(
            "dev-1",
            "1",
            make_report(
                [make_case("a", result="failure"), make_case("b", result="failure")]
            ),
        )
        # Version 2 withdraws case b (it was run by mistake).
        store.ingest("dev-1", "2", make_report([make_case("a", result="failure")]))
        assert store.failure_count("dev-1") == 1

        # The older, superseded report arrives late and still mentions b.
        stale = store.ingest(
            "dev-1",
            "1",
            make_report(
                [make_case("a", result="failure"), make_case("b", result="failure")]
            ),
        )
        # Evidence is retained, but the fold order keeps the withdrawal intact.
        assert stale.conflict is ReportConflict.DUPLICATE
        assert key("b") not in store.effective_cases("dev-1")
        assert store.failure_count("dev-1") == 1
        assert store.current_version("dev-1") == "2"

    def test_late_distinct_version_folds_before_newer_replace(self):
        store = ShardReportStore()
        store.ingest("dev-1", "2", make_report([make_case("a")]))
        late = store.ingest("dev-1", "1", make_report([make_case("a"), make_case("b")]))

        assert late.conflict is ReportConflict.NONE
        # Both versions feed the fold, in version order regardless of arrival.
        assert [receipt.version for receipt in store.applied_receipts("dev-1")] == [
            "1",
            "2",
        ]
        # v2 REPLACE runs after the late v1, so b stays withdrawn.
        assert set(store.effective_cases("dev-1")) == {key("a")}
        assert {receipt.version for receipt in store.receipts("dev-1")} == {"1", "2"}


class TestFailureDiff:
    def _build_store(self):
        store = ShardReportStore()
        store.ingest(
            "dev-1",
            "1",
            make_report(
                [
                    make_case("t", result="failure"),
                    make_case("x", result="failure"),
                    make_case("y", result="failure"),
                    make_case("z"),
                ]
            ),
        )
        store.ingest(
            "dev-1",
            "2",
            make_report(
                [
                    make_case("x"),  # resolved
                    make_case("y", result="error"),  # failure -> error
                    make_case("w", result="failure"),  # new failing case
                    # t withdrawn, z withdrawn (z was passing)
                ]
            ),
        )
        return store

    def test_diff_defaults_to_two_newest_versions(self):
        change = self._build_store().failure_diff("dev-1")

        assert change.from_version == "1"
        assert change.to_version == "2"
        assert (change.before, change.after, change.delta) == (3, 2, -1)
        assert change.added == (key("w"),)
        assert change.resolved == (key("x"),)
        assert change.withdrawn == (key("t"),)
        assert change.regressed == ()
        assert change.status_changed == ((key("y"), "failure", "error"),)

    def test_reasons_text_explains_each_movement(self):
        reasons = self._build_store().failure_diff("dev-1").reasons()
        text = "\n".join(reasons)

        assert "3 -> 2 (-1)" in text
        assert "new failing case: pkg.w" in text
        assert "no longer failing: pkg.x" in text
        assert "withdrawn from the shard report: pkg.t" in text
        assert "pkg.y: failure -> error" in text

    def test_regressed_case_is_distinct_from_new_case(self):
        store = ShardReportStore()
        store.ingest("dev-1", "1", make_report([make_case("p")]))
        store.ingest("dev-1", "2", make_report([make_case("p", result="failure")]))

        change = store.failure_diff("dev-1")
        assert (change.before, change.after) == (0, 1)
        assert change.regressed == (key("p"),)
        assert change.added == ()

    def test_diff_with_explicit_from_version(self):
        store = ShardReportStore()
        for version, failing in (("1", "a"), ("2", "a"), ("3", None)):
            cases = (
                [make_case(failing, result="failure")] if failing else [make_case("a")]
            )
            store.ingest("dev-1", version, make_report(cases))

        change = store.failure_diff("dev-1", from_version="1")
        assert change.to_version == "2"
        assert change.delta == 0

    def test_diff_errors(self):
        store = ShardReportStore()
        with pytest.raises(ShardVersionError):
            store.failure_diff("missing")

        store.ingest("dev-1", "1", make_report([make_case("a")]))
        with pytest.raises(ShardVersionError):
            store.failure_diff("dev-1")
        with pytest.raises(ShardVersionError):
            store.failure_diff("dev-1", from_version="nope")
        with pytest.raises(ShardVersionError):
            store.failure_diff("dev-1", from_version="1")


class TestReconciliation:
    def _entries(self):
        return [
            (
                "alpha",
                "1",
                make_report([make_case("a1"), make_case("a2", result="failure")]),
            ),
            (
                "alpha",
                "2",
                make_report([make_case("a1", result="failure"), make_case("a3")]),
            ),
            (
                "beta",
                "1",
                make_report([make_case("b1", result="error")]),
            ),
            (
                "beta",
                "2",
                make_report([make_case("b2", result="failure")]),
            ),
        ]

    def test_reconcile_emits_one_suite_per_shard_with_stats(self):
        store = ShardReportStore()
        store.ingest(
            "alpha",
            "1",
            make_report([make_case("a1"), make_case("a2", result="failure")]),
        )
        store.ingest(
            "alpha",
            "2",
            make_report([make_case("a1", result="failure"), make_case("a3")]),
        )
        store.ingest("beta", "1", make_report([make_case("b1", result="error")]))
        # beta's second delivery is partial: append an extra case.
        store.ingest(
            "beta",
            "2",
            make_report([make_case("b2", result="failure")]),
            mode=ReportMode.APPEND,
        )

        merged = store.reconcile()
        assert [suite.name for suite in merged] == ["alpha", "beta"]

        stats = store.reconcile_stats()
        assert stats["alpha"] == {
            "tests": 2,
            "failures": 1,
            "errors": 0,
            "skipped": 0,
        }
        assert stats["beta"]["tests"] == 2
        assert stats["beta"]["failures"] == 1
        assert stats["beta"]["errors"] == 1
        assert merged.tests == 4

    def test_reverse_order_import_reconciles_to_same_counts(self):
        entries = self._entries()

        forward = merge_shard_reports(entries)
        reverse = merge_shard_reports(reversed(entries))

        assert forward.reconcile_stats() == reverse.reconcile_stats()
        assert forward.reconcile().tostring() == reverse.reconcile().tostring()
        for shard in ("alpha", "beta"):
            assert forward.failure_count(shard) == reverse.failure_count(shard)
            assert forward.current_version(shard) == reverse.current_version(shard)

    def test_reverse_order_with_stale_and_conflicting_copies(self):
        good_v1 = make_report(
            [make_case("a", result="failure"), make_case("b", result="failure")]
        )
        good_v2 = make_report([make_case("a", result="failure")])
        conflicting_v1 = make_report([make_case("a"), make_case("b")])

        forward = ShardReportStore()
        forward.ingest("dev-1", "1", good_v1)
        forward.ingest("dev-1", "1", conflicting_v1)
        forward.ingest("dev-1", "2", good_v2)

        backward = ShardReportStore()
        backward.ingest("dev-1", "2", good_v2)
        backward.ingest("dev-1", "1", conflicting_v1)
        backward.ingest("dev-1", "1", good_v1)

        assert forward.reconcile_stats() == backward.reconcile_stats()
        assert forward.reconcile().tostring() == backward.reconcile().tostring()
        assert len(forward.conflicts()) == len(backward.conflicts()) == 1
        assert forward.failure_count("dev-1") == backward.failure_count("dev-1") == 1

    def test_write_reconciled_roundtrip(self, tmp_path):
        store = merge_shard_reports(self._entries())
        path = tmp_path / "reconciled.xml"
        store.write_reconciled(str(path))

        reloaded = JUnitXml.fromfile(str(path))
        assert [suite.name for suite in reloaded] == ["alpha", "beta"]
        assert reloaded.tests == store.reconcile().tests

    def test_skipped_cases_are_counted(self):
        store = ShardReportStore()
        store.ingest(
            "dev-1",
            "1",
            make_report([make_case("a", result="skipped"), make_case("b")]),
        )
        assert store.reconcile_stats()["dev-1"]["skipped"] == 1
