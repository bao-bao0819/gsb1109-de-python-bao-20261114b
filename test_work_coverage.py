import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from work_coverage import (DEMO_DATA, build_coverage, descendants,
                           union_length)


def test_union_length_merges_overlaps():
    assert union_length([(0, 100), (50, 200)]) == 200
    assert union_length([(0, 100), (100, 200)]) == 200
    assert union_length([(0, 100), (300, 400)]) == 200
    assert union_length([]) == 0


def test_descendants_transitive():
    parts = {"A": ["B"], "B": ["C"], "C": []}
    assert descendants("A", parts) == {"B", "C"}
    assert descendants("C", parts) == set()


def test_medley_not_double_counted():
    report = build_coverage(DEMO_DATA)
    whole = report["W"]
    # naive sum of R-medley relations would be 300000+120000+130000 = 550000;
    # the union per recording is 300000, plus the other recordings' unions
    assert whole["potential_ms"] == 300000 + 90000 + 60000
    assert whole["certain_ms"] == 250000 + 60000
    assert whole["possible_only_ms"] == 50000 + 90000


def test_unmarked_relation_is_possible_not_split():
    report = build_coverage(DEMO_DATA)
    mov3 = report["W3"]
    # R-partial (90s, unmarked) stays possible; it is NOT divided among
    # movements, and R-shared's 60s certain segment is kept separately.
    assert mov3["certain_ms"] == 60000
    assert mov3["potential_ms"] == 150000
    assert mov3["possible_only_ms"] == 90000


def test_shared_segment_keeps_both_references():
    report = build_coverage(DEMO_DATA)
    mov2_refs = [(s["recording"], s["via_work"]) for s in report["W2"]["segments"]]
    mov3_refs = [(s["recording"], s["via_work"]) for s in report["W3"]["segments"]]
    assert ("R-shared", "W2") in mov2_refs
    assert ("R-shared", "W3") in mov3_refs
    # parent detail lists each contributing relation separately
    whole_refs = [(s["recording"], s["via_work"]) for s in report["W"]["segments"]]
    assert ("R-shared", "W2") in whole_refs
    assert ("R-shared", "W3") in whole_refs


def test_unknown_length_unmarked_is_unquantifiable():
    data = {
        "works": {"A": {"parts": []}},
        "recordings": [{"id": "R", "relations": [{"work": "A"}]}],
    }
    entry = build_coverage(data)["A"]
    assert entry["certain_ms"] == 0
    assert entry["potential_ms"] == 0
    assert entry["unquantifiable_segments"] == 1
