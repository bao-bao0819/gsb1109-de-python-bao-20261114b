"""End-to-end demonstration on real sparse HEX files.

Run from the repository root:

    .venv/bin/python examples/demo_wordtable.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelhex import IntelHex

from measurements import build_word_table, channel_from_hex_file


PHYSICAL_BASE = 0x0000
LOW_IMAGE_BASE = 0x1000
HIGH_IMAGE_BASE = 0x2000


def write_hex_file(path, byte_map):
    hex_data = IntelHex()
    for address, value in byte_map.items():
        hex_data[address] = value
    hex_data.write_hex_file(path)


def main():
    low_bytes = {
        LOW_IMAGE_BASE + 0x00: 0x11,
        LOW_IMAGE_BASE + 0x02: 0x22,
        LOW_IMAGE_BASE + 0x20: 0x33,
        LOW_IMAGE_BASE + 0x12: 0x44,
    }
    high_bytes = {
        HIGH_IMAGE_BASE + 0x01: 0xAA,
        HIGH_IMAGE_BASE + 0x03: 0xBB,
        HIGH_IMAGE_BASE + 0x13: 0xFF,
    }

    temp_dir = tempfile.mkdtemp(prefix='wordtable-demo-')
    low_path = os.path.join(temp_dir, 'low.hex')
    high_path = os.path.join(temp_dir, 'high.hex')
    write_hex_file(low_path, low_bytes)
    write_hex_file(high_path, high_bytes)

    low_regions = [
        (0x00, LOW_IMAGE_BASE + 0x00, 4, 'low-region-A'),
        (0x04, LOW_IMAGE_BASE + 0x10, 4, 'low-region-B'),
    ]
    high_regions = [
        (0x00, HIGH_IMAGE_BASE + 0x00, 4, 'high-region-A'),
        (0x04, HIGH_IMAGE_BASE + 0x10, 4, 'high-region-B'),
    ]

    low = channel_from_hex_file(
        'low', low_path, low_regions,
        swapped_ranges=[(0x04, LOW_IMAGE_BASE + 0x20, 2)],
    )
    high = channel_from_hex_file(
        'high', high_path, high_regions
    )

    table = build_word_table(
        high, low,
        word_physical_start=PHYSICAL_BASE,
        word_count=4,
    )

    print('HEX files written under %s' % temp_dir)
    print()
    header = (
        '%-5s %-9s %-8s | %-26s | %-26s'
        % ('idx', 'phys', 'word', 'low byte', 'high byte')
    )
    print(header)
    print('-' * len(header))
    for row in table.to_trace_rows():
        low_text = '0x%02X @local 0x%04X %s%s' % (
            row['low_value'] if row['low_present'] else 0,
            row['low_local_address'],
            row['low_region'],
            ' [swapped]' if row['low_swapped'] else '',
        )
        if not row['low_present']:
            low_text = 'MISSING ' + low_text
        if row['high_present']:
            high_text = '0x%02X @local 0x%04X %s' % (
                row['high_value'], row['high_local_address'],
                row['high_region'],
            )
        else:
            high_text = 'MISSING @local 0x%04X %s' % (
                row['high_local_address'], row['high_region'],
            )
        print('%-5d 0x%04X     0x%04X   | %-26s | %s' % (
            row['index'], row['address'], row['word'],
            low_text, high_text,
        ))

    print()
    print('High byte genuinely missing: %s' % (
        [sample.index for sample in table.high_missing_samples()],
    ))
    print('High byte measured as fill 0xFF: %s' % (
        [sample.index for sample in table.high_fill_measured_samples()],
    ))


if __name__ == '__main__':
    main()
