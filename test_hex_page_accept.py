"""Acceptance tests for hex_page_accept.

Covers the handover scenarios:

* records reordered / per-line byte counts changed -> still accepted;
* a page explicitly filled with 0xFF vs. the same page left as holes;
* "only the entry point differs" vs. "the bytes the entry points to
  also changed".
"""

import io

import intelhex
from intelhex import Record

import hex_page_accept
from hex_page_accept import compare


def make_hex(records, start=None):
    """Build HEX text from (addr, bytes) data records in the given order.

    ``start`` may be ('EIP', value) or ('CS:IP', cs, ip) to append a
    start-address record.
    """
    lines = []
    for addr, data in records:
        lines.append(Record.data(addr, list(data)))
    if start is not None:
        if start[0] == 'EIP':
            lines.append(Record.start_linear_address(start[1]))
        else:
            lines.append(Record.start_segment_address(start[1], start[2]))
    lines.append(Record.eof())
    return '\n'.join(lines) + '\n'


def load_text(text):
    return intelhex.IntelHex(io.StringIO(text))


def test_record_order_and_byte_count_are_ignored():
    # delivery 1: two 16-byte records in ascending order
    text1 = make_hex([
        (0x100, range(0x00, 0x10)),
        (0x110, range(0x10, 0x20)),
    ])
    # delivery 2: same bytes, records reordered and split unevenly
    text2 = make_hex([
        (0x118, range(0x18, 0x20)),
        (0x100, range(0x00, 0x08)),
        (0x110, range(0x10, 0x18)),
        (0x108, range(0x08, 0x10)),
    ])
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.ok
    assert result.verdict == 'identical'
    assert result.data_equal and result.entry_equal


def test_explicit_ff_page_equals_all_holes():
    # page 1 (0x100-0x1FF) explicitly filled with 0xFF
    text1 = make_hex([
        (0x000, [0x11, 0x22]),
        (0x100, [0xFF] * 16),
        (0x110, [0xFF] * 16),
        (0x120, [0xFF] * 16),
        (0x130, [0xFF] * 16),
        (0x140, [0xFF] * 16),
        (0x150, [0xFF] * 16),
        (0x160, [0xFF] * 16),
        (0x170, [0xFF] * 16),
        (0x180, [0xFF] * 16),
        (0x190, [0xFF] * 16),
        (0x1A0, [0xFF] * 16),
        (0x1B0, [0xFF] * 16),
        (0x1C0, [0xFF] * 16),
        (0x1D0, [0xFF] * 16),
        (0x1E0, [0xFF] * 16),
        (0x1F0, [0xFF] * 16),
    ])
    # same delivery with page 1 left as holes (no records at all)
    text2 = make_hex([(0x000, [0x11, 0x22])])
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.ok
    assert result.verdict == 'identical'


def test_explicit_ff_page_differs_from_holes_with_other_pad():
    text1 = make_hex([(0x100, [0xFF] * 16)])
    text2 = make_hex([(0x000, [0x00])])
    result = compare(load_text(text1), load_text(text2),
                     page_size=256, pad=0x00)
    assert not result.data_equal
    assert [d['index'] for d in result.diff_pages] == [1]


def test_entry_only_difference():
    records = [(0x200, [0xAA, 0xBB, 0xCC, 0xDD])]
    text1 = make_hex(records, start=('EIP', 0x200))
    text2 = make_hex(records, start=('EIP', 0x202))
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.verdict == 'entry-only'
    assert result.data_equal
    assert not result.entry_equal
    # both entries point at bytes holding the same content in each file
    assert result.entry_bytes_changed is False


def test_entry_pointed_bytes_also_changed():
    text1 = make_hex([(0x200, [0xAA, 0xBB, 0xCC, 0xDD])],
                     start=('EIP', 0x200))
    # entry moved AND the byte it now points to differs from before
    text2 = make_hex([(0x200, [0xAA, 0xBB, 0x99, 0xDD])],
                     start=('EIP', 0x202))
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.verdict == 'entry-and-data'
    assert not result.entry_equal
    assert result.entry_bytes_changed is True


def test_entry_same_address_but_pointed_byte_changed():
    text1 = make_hex([(0x200, [0xAA])], start=('EIP', 0x200))
    text2 = make_hex([(0x200, [0x55])], start=('EIP', 0x200))
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.verdict == 'data-only'
    assert result.entry_equal
    assert result.entry_bytes_changed is True


def test_entry_declared_vs_absent():
    records = [(0x200, [0xAA])]
    text1 = make_hex(records, start=('CS:IP', 0x0000, 0x0200))
    text2 = make_hex(records)
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.verdict == 'entry-only'
    assert result.entry1 == ('CS:IP', 0x200)
    assert result.entry2 is None
    assert result.entry_bytes_changed is None


def test_segment_and_linear_declarations_not_equated():
    records = [(0x200, [0xAA])]
    text1 = make_hex(records, start=('CS:IP', 0x0000, 0x0200))
    text2 = make_hex(records, start=('EIP', 0x200))
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert not result.entry_equal
    assert result.verdict == 'entry-only'


def test_data_difference_reports_page_and_offset():
    text1 = make_hex([(0x000, [0x00] * 4), (0x105, [0x00])])
    text2 = make_hex([(0x000, [0x00] * 4), (0x105, [0x7E])])
    result = compare(load_text(text1), load_text(text2), page_size=256)
    assert result.verdict == 'data-only'
    assert len(result.diff_pages) == 1
    page = result.diff_pages[0]
    assert page['index'] == 1
    assert page['start'] == 0x100
    assert page['diff_count'] == 1
    assert page['first_offset'] == 0x05


def test_page_size_controls_page_boundaries():
    text1 = make_hex([(0x10, [0x01])])
    text2 = make_hex([(0x10, [0x02])])
    result = compare(load_text(text1), load_text(text2), page_size=16)
    assert result.diff_pages[0]['index'] == 1
    assert result.diff_pages[0]['first_offset'] == 0


def test_report_mentions_verdict_and_entry():
    text1 = make_hex([(0x200, [0xAA])], start=('EIP', 0x200))
    text2 = make_hex([(0x200, [0xAA])], start=('EIP', 0x300))
    result = compare(load_text(text1), load_text(text2), page_size=256)
    report = result.format(name1='a.hex', name2='b.hex')
    assert 'entry-only' in report
    assert 'EIP=0x00000200' in report
    assert 'EIP=0x00000300' in report


def test_cli_accept_and_reject(tmp_path, capsys):
    f1 = tmp_path / 'a.hex'
    f2 = tmp_path / 'b.hex'
    f1.write_text(make_hex([(0x100, [0x01, 0x02])], start=('EIP', 0x100)))
    f2.write_text(make_hex([(0x101, [0x02]), (0x100, [0x01])],
                           start=('EIP', 0x100)))
    assert hex_page_accept.main([str(f1), str(f2)]) == 0
    out = capsys.readouterr().out
    assert 'verdict: identical' in out

    f2.write_text(make_hex([(0x100, [0x01, 0x03])], start=('EIP', 0x100)))
    assert hex_page_accept.main([str(f1), str(f2)]) == 1
    out = capsys.readouterr().out
    assert 'verdict: data-only' in out
