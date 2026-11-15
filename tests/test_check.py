# -*- coding: utf-8 -*-

import canmatrix
from canmatrix import check
from canmatrix.Frame import Frame
from canmatrix.Signal import Signal


def _add_mux(frame, name, start_bit=0, size=8):
    mux = Signal(name, start_bit=start_bit, size=size, is_little_endian=True,
                 is_signed=False, multiplex="Multiplexor")
    frame.add_signal(mux)
    return mux


def _add_muxed(frame, name, start_bit, size, selector, values):
    signal = Signal(name, start_bit=start_bit, size=size, is_little_endian=True,
                    is_signed=False)
    signal.muxer_for_signal = selector
    signal.mux_val_grp = [list(value) if isinstance(value, tuple) else [value, value]
                          for value in values]
    frame.add_signal(signal)
    return signal


def test_same_start_bit_in_exclusive_branches_is_not_a_conflict():
    frame = Frame("branches", size=2)
    _add_mux(frame, "mode")
    _add_muxed(frame, "left", 8, 8, "mode", [0])
    _add_muxed(frame, "right", 8, 8, "mode", [1])

    assert check.frame_conflicts(frame) == []


def test_plain_signals_with_overlapping_payload_bits_conflict():
    frame = Frame("plain", size=2)
    frame.add_signal(Signal("a", start_bit=0, size=8, is_little_endian=True))
    frame.add_signal(Signal("b", start_bit=4, size=8, is_little_endian=True))

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].signal_names == ("a", "b")
    assert conflicts[0].overlap_bits == (4, 5, 6, 7)
    assert conflicts[0].selector_values == {}


def test_same_start_bit_with_different_lengths_reports_only_real_overlap():
    frame = Frame("lengths", size=2)
    frame.add_signal(Signal("wide", start_bit=0, size=8, is_little_endian=False))
    frame.add_signal(Signal("narrow", start_bit=0, size=4, is_little_endian=False))

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].overlap_bits == (4, 5, 6, 7)
    assert conflicts[0].bits == (4, 5, 6, 7)


def test_adjacent_signals_without_shared_bits_are_not_conflicts():
    frame = Frame("adjacent", size=2)
    frame.add_signal(Signal("a", start_bit=0, size=4, is_little_endian=False))
    frame.add_signal(Signal("b", start_bit=4, size=4, is_little_endian=False))

    assert check.frame_conflicts(frame) == []


def test_same_branch_overlap_uses_a_triggerable_selector_value():
    frame = Frame("same_branch", size=2)
    _add_mux(frame, "mode")
    _add_muxed(frame, "a", 8, 4, "mode", [3])
    _add_muxed(frame, "b", 10, 4, "mode", [3])

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].selector_values == {"mode": 3}
    assert conflicts[0].overlap_bits == (10, 11)


def test_declared_range_endpoints_are_inclusive():
    frame = Frame("ranges", size=3)
    _add_mux(frame, "mode")
    _add_muxed(frame, "a", 8, 8, "mode", [("1", "5")])
    _add_muxed(frame, "b", 12, 8, "mode", [("5", "9")])

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].selector_values == {"mode": 5}
    assert conflicts[0].overlap_bits == (12, 13, 14, 15)


def test_disjoint_declared_ranges_do_not_conflict():
    frame = Frame("disjoint", size=3)
    _add_mux(frame, "mode")
    _add_muxed(frame, "a", 8, 8, "mode", [("1", "4")])
    _add_muxed(frame, "b", 12, 8, "mode", [("5", "9")])

    assert check.frame_conflicts(frame) == []


def test_parent_selector_must_also_allow_the_same_value():
    frame = Frame("nested", size=3)
    _add_mux(frame, "mode")
    sub = _add_muxed(frame, "sub", 8, 8, "mode", [("1", "1")])
    sub.multiplex_setter("Multiplexor")
    _add_muxed(frame, "a", 16, 8, "sub", [("1", "1")])
    _add_muxed(frame, "b", 16, 8, "sub", [("1", "1")])

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].selector_values == {"mode": 1, "sub": 1}


def test_parent_range_limits_the_parent_selector_value():
    frame = Frame("nested_parent_range", size=3)
    _add_mux(frame, "mode")
    sub = _add_muxed(frame, "sub", 8, 4, "mode", [("2", "3")])
    sub.multiplex_setter("Multiplexor")
    sub.muxer_for_signal = "mode"
    sub.mux_val_grp = [[2, 3]]
    _add_muxed(frame, "a", 16, 4, "sub", [("1", "1")])
    _add_muxed(frame, "b", 18, 4, "sub", [("1", "1")])

    conflicts = check.frame_conflicts(frame)

    assert len(conflicts) == 1
    assert conflicts[0].selector_values == {"mode": 2, "sub": 1}


def test_child_signals_of_exclusive_parent_branches_do_not_conflict():
    frame = Frame("nested_exclusive", size=3)
    _add_mux(frame, "mode")
    left = _add_muxed(frame, "left", 8, 8, "mode", [("0", "0")])
    left.multiplex_setter("Multiplexor")
    right = _add_muxed(frame, "right", 8, 8, "mode", [("1", "1")])
    right.multiplex_setter("Multiplexor")
    _add_muxed(frame, "a", 16, 8, "left", [("0", "0")])
    _add_muxed(frame, "b", 16, 8, "right", [("0", "0")])

    assert check.frame_conflicts(frame) == []


def test_wide_selector_ranges_are_not_materialized():
    frame = Frame("wide", size=4)
    _add_mux(frame, "mode", size=16)
    _add_muxed(frame, "a", 16, 8, "mode", [(0, 0x3FFE)])
    _add_muxed(frame, "b", 20, 8, "mode", [(0x3FFF, 0xFFFF)])

    assert check.frame_conflicts(frame) == []


def test_conflict_result_exposes_signals_bits_and_selectors():
    frame = Frame("same_branch", size=2)
    _add_mux(frame, "mode")
    _add_muxed(frame, "a", 8, 4, "mode", [3])
    _add_muxed(frame, "b", 10, 4, "mode", [3])

    conflict = check.frame_conflicts(frame)[0]

    assert conflict.frame_name == "same_branch"
    assert conflict.signal_names == ("a", "b")
    assert conflict.signals[0].name == "a"
    assert conflict.signals[1].name == "b"
    assert conflict.bits == (10, 11)
    assert conflict.selectors == {"mode": 3}
    assert conflict.overlap_bit_ranges == ((10, 11),)


def test_matrix_conflicts_covers_every_frame():
    matrix = canmatrix.CanMatrix()
    first = Frame("first", size=1)
    first.add_signal(Signal("a", start_bit=0, size=8, is_little_endian=True))
    first.add_signal(Signal("b", start_bit=0, size=8, is_little_endian=True))
    second = Frame("second", size=1)
    second.add_signal(Signal("c", start_bit=0, size=4, is_little_endian=True))
    second.add_signal(Signal("d", start_bit=8, size=4, is_little_endian=True))
    matrix.add_frame(first)
    matrix.add_frame(second)

    conflicts = check.matrix_conflicts(matrix)

    assert len(conflicts) == 1
    assert conflicts[0].frame_name == "first"


def test_real_dbc_keeps_distinct_start_bit_columns():
    matrix = canmatrix.formats.loadp_flat("tests/files/dbc/test_frame_decoding.dbc")

    conflicts = check.matrix_conflicts(matrix)

    assert all(conflict.overlap_bits for conflict in conflicts)
    assert all(conflict.selector_values for conflict in conflicts)


def test_wire_bits_match_real_payload_encoding():
    frame = Frame("encoding", size=4)
    frame.add_signal(Signal("little_crossing", start_bit=13, size=8, is_little_endian=True))
    frame.add_signal(Signal("big", start_bit=6, size=4, is_little_endian=False))

    little = frame.signal_by_name("little_crossing")
    assert check.signal_bits(little, frame) == (13, 14, 15, 16, 17, 18, 19, 20)
    data = frame.encode({"little_crossing": 0xFF}, fill_value=0)
    occupied = {
        byte * 8 + bit
        for byte, value in enumerate(data)
        for bit in range(8)
        if value >> bit & 1
    }
    assert occupied == set(check.signal_bits(little, frame))

    big = frame.signal_by_name("big")
    assert check.signal_bits(big, frame) == (0, 1, 14, 15)


def test_out_of_payload_signals_do_not_create_phantom_overlaps():
    frame = Frame("truncated", size=2)
    frame.add_signal(Signal("a", start_bit=8, size=12, is_little_endian=True))
    frame.add_signal(Signal("b", start_bit=12, size=12, is_little_endian=True))

    for conflict in check.frame_conflicts(frame):
        assert all(bit < frame.size * 8 for bit in conflict.overlap_bits)


def test_matrix_and_frame_expose_conflict_results():
    matrix = canmatrix.CanMatrix()
    frame = Frame("objects", size=1)
    frame.add_signal(Signal("a", start_bit=0, size=8, is_little_endian=True))
    frame.add_signal(Signal("b", start_bit=0, size=8, is_little_endian=True))
    matrix.add_frame(frame)

    assert check.matrix_conflicts(matrix) == matrix.signal_conflicts()
    assert check.frame_conflicts(frame) == frame.signal_conflicts()
    assert canmatrix.check_matrix(matrix) == matrix.check_conflicts()


def test_string_representation_contains_bits_signals_and_selectors():
    frame = Frame("same_branch", size=2)
    _add_mux(frame, "mode")
    _add_muxed(frame, "a", 8, 4, "mode", [3])
    _add_muxed(frame, "b", 10, 4, "mode", [3])

    text = str(check.frame_conflicts(frame)[0])

    assert "same_branch" in text
    assert "a" in text
    assert "b" in text
    assert "10,11" in text
    assert "mode=3" in text
