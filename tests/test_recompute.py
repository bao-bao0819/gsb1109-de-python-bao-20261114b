import copy

import pytest

from canmatrix import CanMatrix, Frame, Signal
from canmatrix.recompute import (
    RECHECK_RAW,
    RECOMPUTE,
    UNAFFECTED,
    FrameImpact,
    PartitionStats,
    RecomputePlan,
    REASON_FRAME_REMOVED,
    REASON_FRAME_SIZE_CHANGED,
    REASON_MUX_SELECTOR_CHANGED,
    REASON_SIGNAL_DECODING_CHANGED,
    REASON_SIGNAL_RENAMED,
    analyze_frame_change,
    analyze_matrix_changes,
    build_recompute_plan,
    format_plan,
)

FRAME_ID = 0x100
OTHER_ID = 0x200


def make_climate_db(selector_bit=0, temp_factor=1.0, frame_name="Climate",
                    temp_name="Temp", frame_size=8, temp_multiplex=1,
                    temp_values=None):
    db = CanMatrix()
    frame = Frame(frame_name, arbitration_id=FRAME_ID, size=frame_size)
    frame.add_signal(Signal("Mux", start_bit=selector_bit, size=4,
                            multiplex="Multiplexor"))
    frame.add_signal(Signal(temp_name, start_bit=8, size=8, factor=temp_factor,
                            multiplex=temp_multiplex, values=temp_values or {}))
    frame.add_signal(Signal("Pressure", start_bit=16, size=8, multiplex=2))
    frame.add_signal(Signal("Voltage", start_bit=24, size=8))
    db.add_frame(frame)
    other = Frame("Other", arbitration_id=OTHER_ID, size=8)
    other.add_signal(Signal("Whatever", start_bit=0, size=8))
    db.add_frame(other)
    return db


def status_map(plan):
    return {decision.partition_id: decision.status for decision in plan.decisions}


def decision_for(plan, partition_id):
    return next(d for d in plan.decisions if d.partition_id == partition_id)


def test_identical_frames_have_no_impact():
    db = make_climate_db()
    assert analyze_frame_change(db.frames[0], copy.deepcopy(db.frames[0])) is None
    assert analyze_matrix_changes(db, copy.deepcopy(db)) == []


def test_display_only_changes_never_force_recompute():
    old = make_climate_db()
    new = make_climate_db(frame_name="ClimateRenamed", temp_name="TempRenamed",
                          temp_values={1: "OneLabel"})
    old.frames[0].signal_by_name("Temp").values = {1: "OneLabel"}
    new.frames[0].signal_by_name("TempRenamed").unit = "degC"
    new.frames[0].signal_by_name("TempRenamed").comment = "renamed display"

    impact = analyze_frame_change(old.frames[0], new.frames[0])
    assert impact.display_only is True
    assert impact.affected_signals == set()
    assert impact.renames == {"Temp": "TempRenamed"}
    assert REASON_SIGNAL_RENAMED in impact.reasons

    stats = PartitionStats(
        "p", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {1}},
        derived_signal_fields={"deg_c": ["Temp"], "battery": ["Voltage"]})
    plan = build_recompute_plan(old, new, [stats])
    assert status_map(plan) == {"p": UNAFFECTED}
    assert decision_for(plan, "p").items == []


def test_value_table_label_change_is_display_only_but_key_change_recomputes():
    old = make_climate_db(temp_values={0: "Off", 1: "On"})
    relabeled = make_climate_db(temp_values={0: "Off", 1: "Enabled"})
    label_impact = analyze_frame_change(old.frames[0], relabeled.frames[0])
    assert label_impact.display_only is True
    assert label_impact.affected_signals == set()

    rekeyed = make_climate_db(temp_values={0: "Off", 2: "On"})
    key_impact = analyze_frame_change(old.frames[0], rekeyed.frames[0])
    assert key_impact.affected_signals == {"Temp"}
    assert REASON_SIGNAL_DECODING_CHANGED in key_impact.reasons


def test_signal_factor_change_targets_only_related_columns():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    stats = PartitionStats(
        "p", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {1}},
        derived_signal_fields={"deg_c": ["Temp"], "battery": ["Voltage"]})
    plan = build_recompute_plan(old, new, [stats])
    decision = decision_for(plan, "p")
    assert decision.status == RECOMPUTE
    assert len(decision.items) == 1
    item = decision.items[0]
    assert item.frame_id == FRAME_ID
    assert item.recompute_columns == ["deg_c"]
    assert item.multiplexer_values == [1]
    assert REASON_SIGNAL_DECODING_CHANGED in item.reasons


def test_selector_bit_move_invalidates_derived_columns_despite_unchanged_temp():
    old = make_climate_db()
    new = make_climate_db(selector_bit=4)
    impact = analyze_frame_change(old.frames[0], new.frames[0])
    assert impact.selector_layout_changed is True
    assert REASON_MUX_SELECTOR_CHANGED in impact.reasons
    # Temperature itself kept its bits, yet it lives in a mux branch.
    assert "Temp" in impact.affected_signals
    assert "Pressure" in impact.affected_signals
    assert "Voltage" not in impact.affected_signals

    hot = PartitionStats(
        "p-hot", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {1}},
        derived_signal_fields={"deg_c": ["Temp"], "battery": ["Voltage"]})
    cold = PartitionStats(
        "p-cold", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {2}},
        derived_signal_fields={"deg_c": ["Temp"], "pressure": ["Pressure"]})
    plan = build_recompute_plan(old, new, [hot, cold])
    assert status_map(plan) == {"p-hot": RECOMPUTE, "p-cold": RECOMPUTE}

    hot_item = decision_for(plan, "p-hot").items[0]
    # Selector moved: old frames may resolve into another branch now, so the
    # Temp column cannot be dropped even though branch 1 was the only one seen.
    assert hot_item.recompute_columns == ["deg_c"]
    assert hot_item.multiplexer_values == [1]
    cold_item = decision_for(plan, "p-cold").items[0]
    assert cold_item.recompute_columns == ["deg_c", "pressure"]
    assert cold_item.multiplexer_values == [2]


def test_ordinary_branch_change_is_excluded_when_branch_never_observed():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    saw_branch_one = PartitionStats(
        "saw-one", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {1}},
        derived_signal_fields={"deg_c": ["Temp"]})
    saw_branch_two = PartitionStats(
        "saw-two", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {2}},
        derived_signal_fields={"deg_c": ["Temp"]})
    plan = build_recompute_plan(old, new, [saw_branch_one, saw_branch_two])
    assert status_map(plan) == {"saw-one": RECOMPUTE, "saw-two": UNAFFECTED}
    assert decision_for(plan, "saw-two").items == []


def test_mux_value_range_branch_uses_observed_selector_values():
    old = CanMatrix()
    frame = Frame("Ranged", arbitration_id=FRAME_ID, size=8)
    frame.add_signal(Signal("Mux", start_bit=0, size=4, multiplex="Multiplexor"))
    frame.add_signal(Signal("RangedTemp", start_bit=8, size=8,
                            mux_val_grp=[(0, 3)]))
    old.add_frame(frame)
    new = copy.deepcopy(old)
    new.frames[0].signal_by_name("RangedTemp").factor = 2.0

    inside = PartitionStats(
        "inside", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {2}},
        derived_signal_fields={"deg_c": ["RangedTemp"]})
    outside = PartitionStats(
        "outside", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {5}},
        derived_signal_fields={"deg_c": ["RangedTemp"]})
    plan = build_recompute_plan(old, new, [inside, outside])
    assert status_map(plan) == {"inside": RECOMPUTE, "outside": UNAFFECTED}
    assert decision_for(plan, "inside").items[0].multiplexer_values == [2]


def test_changed_frame_not_seen_in_partition_is_proven_unaffected():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    stats = PartitionStats(
        "p", message_ids={OTHER_ID},
        derived_signal_fields={"x": ["Whatever"]})
    plan = build_recompute_plan(old, new, [stats])
    assert status_map(plan) == {"p": UNAFFECTED}


def test_frame_size_change_touches_every_signal():
    old = make_climate_db()
    new = make_climate_db(frame_size=16)
    impact = analyze_frame_change(old.frames[0], new.frames[0])
    assert REASON_FRAME_SIZE_CHANGED in impact.reasons
    assert {"Mux", "Temp", "Pressure", "Voltage"} <= impact.affected_signals


def test_added_and_removed_frames():
    old = make_climate_db()
    new = copy.deepcopy(old)
    added = Frame("Fresh", arbitration_id=0x300, size=8)
    added.add_signal(Signal("FreshSignal", start_bit=0, size=8))
    new.add_frame(added)

    stats = PartitionStats(
        "p", message_ids={0x300},
        derived_signal_fields={"fresh_col": ["FreshSignal"]})
    plan = build_recompute_plan(old, new, [stats])
    assert status_map(plan) == {"p": RECOMPUTE}
    assert decision_for(plan, "p").items[0].frame_id == 0x300

    removal_plan = build_recompute_plan(new, old, [stats])
    decision = decision_for(removal_plan, "p")
    assert decision.status == RECOMPUTE
    assert REASON_FRAME_REMOVED in decision.items[0].reasons


def test_incomplete_partition_requires_raw_frame_replay():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    incomplete = PartitionStats("incomplete", message_ids={FRAME_ID}, complete=False)
    no_ids = PartitionStats("no-ids", message_ids=None, complete=False,
                            derived_signal_fields={"deg_c": ["Temp"]})
    plan = build_recompute_plan(old, new, [incomplete, no_ids])
    assert status_map(plan) == {
        "incomplete": RECHECK_RAW,
        "no-ids": RECHECK_RAW,
    }
    assert decision_for(plan, "incomplete").inspect_message_ids == [FRAME_ID]
    # Unknown frame membership: the whole partition raw data must be scanned.
    assert decision_for(plan, "no-ids").inspect_message_ids == []


def test_unknown_derived_field_lists_require_raw_replay():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    stats = PartitionStats("p", message_ids={FRAME_ID},
                           derived_signal_fields=None)
    plan = build_recompute_plan(old, new, [stats])
    assert status_map(plan) == {"p": RECHECK_RAW}
    assert decision_for(plan, "p").inspect_message_ids == [FRAME_ID]


def test_plan_bucket_accessors_and_three_way_split():
    old = make_climate_db()
    new = make_climate_db(selector_bit=4)
    partitions = [
        PartitionStats("recompute", message_ids={FRAME_ID},
                       multiplexer_values={FRAME_ID: {1}},
                       derived_signal_fields={"deg_c": ["Temp"]}),
        PartitionStats("unaffected", message_ids={OTHER_ID},
                       derived_signal_fields={"x": ["Whatever"]}),
        PartitionStats("raw", message_ids=None, complete=False),
    ]
    plan = build_recompute_plan(old, new, partitions)
    assert isinstance(plan, RecomputePlan)
    assert [d.partition_id for d in plan.partitions_to_recompute] == ["recompute"]
    assert [d.partition_id for d in plan.partitions_proven_unaffected] == ["unaffected"]
    assert [d.partition_id for d in plan.partitions_needing_raw_frames] == ["raw"]
    text = format_plan(plan)
    assert "Partitions to recompute" in text
    assert "Partitions proven unaffected" in text
    assert "Partitions needing raw frame replay" in text
    assert "recompute" in text and "unaffected" in text and "raw" in text


def test_partition_stats_normalize_inputs():
    stats = PartitionStats(
        "p", message_ids=[FRAME_ID, FRAME_ID],
        multiplexer_values={str(FRAME_ID): [1, 1, 2]},
        derived_signal_fields={"col": ["Temp"]})
    assert stats.message_ids == {FRAME_ID}
    assert stats.multiplexer_values == {FRAME_ID: {1, 2}}
    assert stats.derived_signal_fields == {"col": ["Temp"]}
    assert stats.complete is True


def test_removed_muxed_signal_cannot_be_excluded_by_observed_branches():
    old = make_climate_db()
    new = copy.deepcopy(old)
    new.frames[0].signals = [
        signal for signal in new.frames[0].signals if signal.name != "Temp"
    ]
    # Only branch 2 was observed, but the removed Temp lived in branch 1.
    stats = PartitionStats(
        "p", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: {2}},
        derived_signal_fields={"deg_c": ["Temp"], "pressure": ["Pressure"]})
    plan = build_recompute_plan(old, new, [stats])
    decision = decision_for(plan, "p")
    assert decision.status == RECOMPUTE
    assert decision.items[0].recompute_columns == ["deg_c"]


def test_empty_observed_mux_values_disables_branch_narrowing():
    old = make_climate_db()
    new = make_climate_db(temp_factor=2.0)
    stats = PartitionStats(
        "p", message_ids={FRAME_ID}, multiplexer_values={FRAME_ID: set()},
        derived_signal_fields={"deg_c": ["Temp"]})
    plan = build_recompute_plan(old, new, [stats])
    item = decision_for(plan, "p").items[0]
    assert item.recompute_columns == ["deg_c"]
    assert item.multiplexer_values is None


def test_signal_multiplex_branch_change_is_detected():
    old = make_climate_db()
    new = make_climate_db(temp_multiplex=3)
    impact = analyze_frame_change(old.frames[0], new.frames[0])
    assert "Temp" in impact.affected_signals
    change = next(c for c in impact.signal_changes if c.signal_name == "Temp")
    assert change.branch_changed is True
    assert "multiplex" in change.changed_attributes


def test_impact_is_attrs_dataclass_like_container():
    impact = FrameImpact(frame_id=FRAME_ID)
    assert impact.frame_id == FRAME_ID
    assert impact.affected_signals == set()
    assert impact.signal_changes == []
    with pytest.raises(TypeError):
        FrameImpact()
