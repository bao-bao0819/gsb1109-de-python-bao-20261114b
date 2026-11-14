import io

import pytest

from intelhex import IntelHex

from measurements import (
    ChannelDefinition,
    DuplicateByteError,
    build_word_table,
)


def make_hex(byte_map):
    hex_data = IntelHex()
    for address, value in byte_map.items():
        hex_data[address] = value
    return hex_data


def reload_hex(hex_data):
    buffer = io.StringIO()
    hex_data.write_hex_file(buffer)
    buffer.seek(0)
    return IntelHex(buffer)


def make_definition(name, byte_map, regions, swapped_ranges=None):
    return ChannelDefinition(
        name=name,
        hex_data=make_hex(byte_map),
        regions=regions,
        swapped_ranges=swapped_ranges,
    )


def test_basic_word_assembly_with_provenance():
    low = make_definition(
        'low',
        {0x10: 0x34, 0x12: 0x78},
        [(0x00, 0x10, 4, 'low-cal')],
    )
    high = make_definition(
        'high',
        {0x21: 0x12, 0x23: 0x9A},
        [(0x00, 0x20, 4, 'high-cal')],
    )

    table = build_word_table(high, low)

    assert len(table) == 2
    assert table.words() == [0x1234, 0x9A78]
    first, second = table
    assert first.address == 0x00
    assert first.high.channel == 'high'
    assert first.high.local_address == 0x21
    assert first.high.physical_address == 0x01
    assert first.high.region == 'high-cal'
    assert first.low.channel == 'low'
    assert first.low.local_address == 0x10
    assert first.low.physical_address == 0x00
    assert first.low.region == 'low-cal'
    assert second.high.local_address == 0x23
    assert second.low.local_address == 0x12


def test_sparse_hex_files_round_trip():
    low_hex = make_hex({0x40: 0x11, 0x42: 0x33})
    high_hex = make_hex({0x81: 0x22, 0x83: 0x44})

    low = ChannelDefinition(
        'low', reload_hex(low_hex), [(0x00, 0x40, 4, 'low-cal')])
    high = ChannelDefinition(
        'high', reload_hex(high_hex), [(0x00, 0x80, 4, 'high-cal')])

    table = build_word_table(high, low)

    assert table.words() == [0x2211, 0x4433]
    assert all(sample.complete for sample in table)


def test_independent_start_address_calibration():
    low = make_definition(
        'low', {0x05: 0xBB}, [(0x100, 0x05, 2, 'low-cal')])
    high = make_definition(
        'high', {0x71: 0xAA}, [(0x100, 0x70, 2, 'high-cal')])

    table = build_word_table(high, low, word_physical_start=0x100,
                             word_count=1)

    sample = table[0]
    assert sample.word == 0xAABB
    assert sample.low.local_address == 0x05
    assert sample.high.local_address == 0x71
    assert sample.address == 0x100


def test_adjacent_regions_with_word_crossing_boundary():
    low = make_definition(
        'low',
        {0x00: 0xCD, 0x10: 0xEF},
        [
            (0x10, 0x00, 2, 'low-A'),
            (0x12, 0x10, 2, 'low-B'),
        ],
    )
    high = make_definition(
        'high',
        {0x41: 0x12, 0x51: 0x34},
        [
            (0x10, 0x40, 2, 'high-A'),
            (0x12, 0x50, 2, 'high-B'),
        ],
    )

    table = build_word_table(high, low)

    crossing = table[0]
    assert crossing.address == 0x10
    assert crossing.word == 0x12CD
    assert crossing.low.region == 'low-A'
    assert crossing.high.region == 'high-A'

    boundary_word = table[1]
    assert boundary_word.address == 0x12
    assert boundary_word.word == 0x34EF
    assert boundary_word.low.region == 'low-B'
    assert boundary_word.high.region == 'high-B'


def test_word_straddles_two_regions_in_one_channel():
    low = make_definition(
        'low',
        {0x09: 0x01, 0x1A: 0x02},
        [
            (0x20, 0x09, 1, 'low-A'),
            (0x21, 0x19, 3, 'low-B'),
        ],
    )
    high = make_definition(
        'high', {0x51: 0x90, 0x53: 0x91},
        [(0x20, 0x50, 4, 'high-cal')],
    )

    table = build_word_table(high, low)

    first = table[0]
    assert first.address == 0x20
    assert first.low.region == 'low-A'
    assert first.high.region == 'high-cal'
    assert first.word == 0x9001

    second = table[1]
    assert second.address == 0x22
    assert second.low.region == 'low-B'
    assert second.low.local_address == 0x1A
    assert second.word == 0x9102


def test_swap_range_reroutes_channel():
    low = make_definition(
        'low',
        {0x00: 0x11, 0x06: 0x33},
        [(0x00, 0x00, 4, 'low-cal')],
        swapped_ranges=[(0x02, 0x06, 2)],
    )
    high = make_definition(
        'high',
        {0x81: 0xAA, 0x83: 0xCC},
        [(0x00, 0x80, 4, 'high-cal')],
    )

    table = build_word_table(high, low)

    normal_word = table[0]
    assert normal_word.low.local_address == 0x00
    assert normal_word.low.swapped is False
    assert normal_word.word == 0xAA11

    swapped_word = table[1]
    assert swapped_word.low.local_address == 0x06
    assert swapped_word.low.swapped is True
    assert swapped_word.low.value == 0x33
    assert swapped_word.word == 0xCC33


def test_missing_high_byte_distinct_from_measured_fill_value():
    low = make_definition(
        'low', {0x00: 0x11, 0x02: 0x22},
        [(0x00, 0x00, 4, 'low-cal')])
    high = make_definition(
        'high',
        {0x43: 0xFF},
        [(0x00, 0x40, 4, 'high-cal')],
    )

    table = build_word_table(high, low)

    missing, measured_fill = table

    assert missing.high_present is False
    assert missing.high_missing is True
    assert missing.high.value is None
    assert missing.word == 0x0011
    assert table.high_missing_samples() == [missing]

    assert measured_fill.high_present is True
    assert measured_fill.high_missing is False
    assert measured_fill.high.value == 0xFF
    assert measured_fill.word == 0xFF22
    assert table.high_fill_measured_samples() == [measured_fill]

    assert table.high_missing_samples() != table.high_fill_measured_samples()


def test_duplicate_physical_byte_reports_samples_without_choosing():
    low = make_definition(
        'low', {0x00: 0x11, 0x02: 0x33},
        [(0x00, 0x00, 4, 'low-cal')])
    high = make_definition(
        'high', {0x40: 0xAA, 0x42: 0xCC},
        [(0x00, 0x40, 4, 'high-cal')])

    with pytest.raises(DuplicateByteError) as exc_info:
        build_word_table(high, low)

    error = exc_info.value
    conflict_addresses = [address for address, _, _ in error.conflicts]
    assert 0x00 in conflict_addresses
    assert 0x02 in conflict_addresses

    first_claim = error.conflicts[0][1]
    second_claim = error.conflicts[0][2]
    assert {first_claim[0], second_claim[0]} == {'high', 'low'}

    affected = error.samples
    assert {sample.address for sample in affected} == {0x00, 0x02}
    message = str(error)
    assert 'high' in message and 'low' in message
    assert '0x0' in message


def test_duplicate_byte_inside_partial_word_only():
    low = make_definition(
        'low', {0x00: 0x11, 0x02: 0x33},
        [(0x00, 0x00, 4, 'low-cal')])
    high = make_definition(
        'high', {0x40: 0xAA},
        [(0x00, 0x40, 4, 'high-cal')])

    with pytest.raises(DuplicateByteError) as exc_info:
        build_word_table(high, low)

    affected = exc_info.value.samples
    assert len(affected) == 1
    assert affected[0].address == 0x00


def test_trace_rows_carry_full_origin():
    low = make_definition(
        'low', {0x10: 0x55}, [(0x00, 0x10, 2, 'low-cal')])
    high = make_definition(
        'high', {0x91: 0x66}, [(0x00, 0x90, 2, 'high-cal')])

    table = build_word_table(high, low)
    row = table.to_trace_rows()[0]

    assert row['word'] == 0x6655
    assert row['high_channel'] == 'high'
    assert row['high_local_address'] == 0x91
    assert row['low_channel'] == 'low'
    assert row['low_local_address'] == 0x10
    assert row['high_swapped'] is False


def test_overlapping_calibration_regions_rejected():
    with pytest.raises(ValueError):
        ChannelDefinition(
            'low', make_hex({}),
            [(0x00, 0x00, 4, 'A'), (0x02, 0x10, 4, 'B')],
        )


def test_uncovered_provenance_does_not_crash_conflict_report():
    low = make_definition(
        'low', {0x00: 0x11}, [(0x00, 0x00, 2, 'low-cal')])
    high = make_definition(
        'high', {0x40: 0xAA}, [(0x00, 0x40, 2, 'high-cal')])

    with pytest.raises(DuplicateByteError) as exc_info:
        build_word_table(high, low)

    sample = exc_info.value.samples[0]
    assert sample.high.channel == 'high'
    assert sample.low.channel == 'low'


def test_explicit_word_count_marks_extra_words_uncovered():
    low = make_definition(
        'low', {0x00: 0x11}, [(0x00, 0x00, 2, 'low-cal')])
    high = make_definition(
        'high', {0x41: 0xAA}, [(0x00, 0x40, 2, 'high-cal')])

    table = build_word_table(
        high, low, word_physical_start=0x00, word_count=3)

    assert len(table) == 3
    assert table[0].complete is True
    assert table[1].high_present is False
    assert table[1].low_present is False
    assert table[1].high.region == '<uncovered>'
    assert table[1].word == 0x0000
    assert table.complete_samples() == [table[0]]
    assert len(table.incomplete_samples()) == 2


def test_missing_low_byte_is_tracked_separately():
    low = make_definition(
        'low', {0x02: 0x22}, [(0x00, 0x00, 4, 'low-cal')])
    high = make_definition(
        'high', {0x41: 0xAA, 0x43: 0xBB},
        [(0x00, 0x40, 4, 'high-cal')])

    table = build_word_table(high, low)

    first = table[0]
    assert first.low_present is False
    assert first.low.value is None
    assert first.word == 0xAA00
    assert [sample.index for sample in table.low_missing_samples()] == [0]
    assert table[1].complete is True


def test_conflict_on_high_byte_slot_reports_owning_sample():
    low = make_definition(
        'low', {0x01: 0x11}, [(0x00, 0x00, 2, 'low-cal')])
    high = make_definition(
        'high', {0x41: 0xAA}, [(0x00, 0x40, 2, 'high-cal')])

    with pytest.raises(DuplicateByteError) as exc_info:
        build_word_table(high, low)

    error = exc_info.value
    assert [address for address, _, _ in error.conflicts] == [0x01]
    assert len(error.samples) == 1
    assert error.samples[0].address == 0x00


def test_channel_from_hex_file_reads_real_file(tmp_path):
    from measurements import channel_from_hex_file

    hex_path = tmp_path / 'sparse.hex'
    write_target = IntelHex()
    write_target[0x30] = 0x77
    write_target.write_hex_file(str(hex_path))

    channel = channel_from_hex_file(
        'io', str(hex_path), [(0x00, 0x30, 2, 'io-cal')])

    assert channel.byte_at(0x00) == 0x77
    assert channel.byte_at(0x01) is None
    provenance = channel.provenance(0x00)
    assert provenance.channel == 'io'
    assert provenance.local_address == 0x30
    assert provenance.region == 'io-cal'
