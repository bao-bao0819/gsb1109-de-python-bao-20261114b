# -*- coding: utf-8 -*-
import decimal
import json

import pytest

import canmatrix
from canmatrix.reconcile import (
    ALL_PARTITIONS,
    InconsistentReport,
    ReconciliationError,
    Scenario,
    UnknownStatus,
    can_coexist,
    from_canmatrix,
    reconcile,
)


OLD = {0: "待机", 1: "待机", 2: "运行"}
NEW = {0: "待机", 1: "维护", 2: "运行"}


def test_ambiguous_old_label_splits_across_new_statuses():
    model = reconcile(OLD, NEW, {"待机": 180, "运行": 20})
    assert model.ranges()["待机"] == (0, 180)
    assert model.ranges()["维护"] == (0, 180)
    assert model.ranges()["运行"] == (20, 20)


def test_joint_feasibility_is_stricter_than_individual_ranges():
    model = reconcile(OLD, NEW, {"待机": 180, "运行": 20})
    assert model.can_coexist({"维护": 100, "待机": 80})
    assert not model.can_coexist({"维护": 100, "待机": 100})
    assert can_coexist(OLD, NEW, {"待机": 180, "运行": 20},
                       {"维护": 100, "待机": 80})


def test_witness_satisfies_report_and_query():
    model = reconcile(OLD, NEW, {"待机": 180, "运行": 20})
    witness = model.witness({"维护": 100, "待机": 80})
    assert witness is not None
    assert witness[None][0] + witness[None][1] == 180
    assert witness[None][1] == 100
    assert model.status_total(witness, "维护") == 100
    assert model.status_total(witness, "待机") == 80


def test_subtotal_pins_one_raw_code():
    model = reconcile(OLD, NEW, {"待机": 500, "运行": 20}, [(1, 120)])
    assert model.ranges()["维护"] == (120, 120)
    assert model.ranges()["待机"] == (380, 380)


def test_subtotal_for_unknown_code_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile(OLD, NEW, {"待机": 500}, [(99, 10)])


def test_report_disagreement_is_rejected():
    with pytest.raises(InconsistentReport):
        reconcile(OLD, NEW, {"待机": 500}, [(1, 600)])


def test_unknown_status_is_rejected():
    model = reconcile(OLD, NEW, {"待机": 500})
    with pytest.raises(UnknownStatus):
        model.maximum("停机")
    with pytest.raises(UnknownStatus):
        model.can_coexist({"停机": 1})


def test_same_label_in_two_sections_is_not_merged():
    old = {0: "待机", 1: "待机", 2: "待机", 3: "运行"}
    new = {0: "待机", 1: "维护", 2: "待机", 3: "运行"}
    model = reconcile(old, new, {"A": {"待机": 100}, "B": {"待机": 10, "运行": 5}},
                      partitions=["A", "B"])
    assert model.ranges("A")["待机"] == (0, 100)
    assert model.ranges("A")["维护"] == (0, 100)
    assert model.ranges("B")["待机"] == (0, 10)
    assert model.ranges("B")["维护"] == (0, 10)
    assert model.ranges()["待机"] == (0, 110)


def test_scoped_subtotal_only_constrains_its_section():
    old = {0: "待机", 1: "待机", 2: "待机"}
    new = {0: "待机", 1: "维护", 2: "待机"}
    report = [("A", "待机", 100), ("B", "待机", 10)]
    model = reconcile(old, new, report, [{"partition": "B", "code": 0, "count": 7}],
                      partitions=["A", "B"])
    assert model.ranges("A")["维护"] == (0, 100)
    assert model.ranges("B")["维护"] == (0, 3)
    assert model.ranges("B")["待机"] == (7, 10)


def test_membership_restricts_codes_to_sections():
    old = {0: "待机", 1: "待机", 2: "运行"}
    new = {0: "待机", 1: "维护", 2: "运行"}
    model = reconcile(old, new, [("A", "待机", 5), ("B", "待机", 3)],
                      membership={0: "A", 1: "B"}, partitions=["A", "B"])
    assert model.ranges("A")["维护"] == (0, 0)
    assert model.ranges("B")["维护"] == (3, 3)
    assert model.ranges()["维护"] == (3, 3)


def test_missing_new_code_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile({0: "待机", 1: "待机"}, {0: "待机"}, {"待机": 10})


def test_unbounded_status_is_reported():
    model = reconcile({0: "待机", 1: "运行"}, {0: "待机", 1: "运行"}, {"待机": 10})
    assert model.ranges()["运行"] == (0, float("inf"))


def test_negative_count_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile(OLD, NEW, {"待机": -1})


def test_non_integer_count_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile(OLD, NEW, {"待机": 1.5})


def test_same_count_for_duplicate_label_rows_is_allowed():
    model = reconcile(OLD, NEW, [("A", "待机", 10), ("A", "待机", 10)],
                      partitions=["A"])
    assert model.ranges("A")["待机"] == (0, 10)


def test_conflicting_duplicate_report_rows_are_rejected():
    with pytest.raises(ReconciliationError):
        reconcile(OLD, NEW, [("A", "待机", 10), ("A", "待机", 11)],
                  partitions=["A"])


def test_from_canmatrix_uses_the_single_value_table():
    old_matrix = canmatrix.CanMatrix()
    old_matrix.value_tables["State"] = {0: "待机", 1: "待机", 2: "运行"}
    new_matrix = canmatrix.CanMatrix()
    new_matrix.value_tables["State"] = {0: "待机", 1: "维护", 2: "运行"}
    scenario = from_canmatrix(old_matrix, new_matrix, {"待机": 30})
    assert scenario.old_table == {0: "待机", 1: "待机", 2: "运行"}
    assert scenario.new_table == {0: "待机", 1: "维护", 2: "运行"}


def test_from_canmatrix_requires_value_table_selection():
    old_matrix = canmatrix.CanMatrix()
    old_matrix.value_tables["A"] = {0: "x"}
    old_matrix.value_tables["B"] = {1: "y"}
    new_matrix = canmatrix.CanMatrix()
    new_matrix.value_tables["A"] = {0: "x"}
    new_matrix.value_tables["B"] = {1: "y"}
    with pytest.raises(ReconciliationError):
        from_canmatrix(old_matrix, new_matrix, {"x": 1})
    scenario = from_canmatrix(old_matrix, new_matrix, {"x": 1}, value_table="A")
    assert scenario.old_table == {0: "x"}


def test_from_canmatrix_accepts_json_style_documents():
    document = {"value_tables": {"State": {"0": "待机", "1": "待机", "2": "运行"}}}
    scenario = from_canmatrix(document, document, {"待机": 4})
    assert scenario.old_table == {0: "待机", 1: "待机", 2: "运行"}


def test_plain_table_mapping_is_not_mistaken_for_tables():
    model = reconcile({0: "待机", 1: "维护"}, {0: "待机", 1: "维护"}, {"待机": 7})
    assert model.ranges()["待机"] == (7, 7)


def test_string_codes_and_section_grouped_report():
    model = reconcile({"0x0": "待机", "0x1": "待机", "0x2": "运行"},
                      {"0x0": "待机", "0x1": "维护", "0x2": "运行"},
                      {"左": {"待机": 12, "运行": 4}, "右": {"待机": 3, "运行": 1}},
                      partitions=["左", "右"])
    assert model.ranges("左")["维护"] == (0, 12)
    assert model.ranges("右")["维护"] == (0, 3)
    assert model.ranges("左")["运行"] == (4, 4)
    assert model.ranges("右")["运行"] == (1, 1)
    assert model.ranges()["运行"] == (5, 5)


def test_zero_padded_decimal_codes_are_accepted():
    model = reconcile({"00": "待机", "01": "待机"}, {"00": "待机", "01": "维护"},
                      {"待机": 9})
    assert model.ranges()["维护"] == (0, 9)


def test_json_document_inputs_can_carry_chinese_keys():
    report = {"状态": "待机", "数量": 50}
    model = reconcile(OLD, NEW, report)
    assert model.ranges()["维护"] == (0, 50)


def test_scenario_is_reusable_and_does_not_mutate_inputs():
    report = {"待机": 180, "运行": 20}
    before = json.dumps(report, ensure_ascii=False)
    scenario = Scenario(OLD, NEW, report)
    model = reconcile(OLD, NEW, report)
    assert model.ranges()["维护"] == (0, 180)
    assert json.dumps(report, ensure_ascii=False) == before
    assert scenario.old_table == OLD


def test_large_ambiguous_split_stays_exact():
    codes = list(range(200))
    old = {code: "待机" for code in codes}
    new = {code: ("维护" if code % 2 else "待机") for code in codes}
    model = reconcile(old, new, {"待机": 10000}, [(code, 1) for code in range(20)])
    assert model.ranges()["维护"] == (10, 9990)
    assert model.can_coexist({"维护": 5000, "待机": 5000})
    assert not model.can_coexist({"维护": 9991, "待机": 5000})


def test_section_ranges_do_not_include_unassigned_codes():
    model = reconcile(OLD, NEW, {"甲": {"待机": 12}, "乙": {"待机": 3, "运行": 1}},
                      partitions=["甲", "乙"])
    assert model.ranges("甲")["运行"] == (0, float("inf"))
    assert model.ranges("乙")["运行"] == (1, 1)


def test_all_partitions_sentinel_matches_default():
    assert reconcile(OLD, NEW, {"待机": 10}).ranges(ALL_PARTITIONS) == \
        reconcile(OLD, NEW, {"待机": 10}).ranges()


def test_none_means_whole_report_when_sections_exist():
    model = reconcile(OLD, NEW, {"甲": {"待机": 6}, "乙": {"待机": 4}},
                      partitions=["甲", "乙"])
    assert model.ranges(None) == model.ranges()


def test_section_list_and_record_subtotals_are_equivalent():
    old = {0: "A", 1: "A", 2: "B"}
    new = {0: "X", 1: "Y", 2: "Z"}
    report = {"甲": {"A": 100}}
    by_mapping = reconcile(old, new, report, {"甲": {"0": 30}}, partitions=["甲"])
    by_record = reconcile(old, new, report, {"甲": {"code": 0, "count": 30}},
                          partitions=["甲"])
    by_tuple = reconcile(old, new, report, {"甲": [("0", 30)]}, partitions=["甲"])
    assert by_mapping.ranges("甲") == by_record.ranges("甲") == by_tuple.ranges("甲")
