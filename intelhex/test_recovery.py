"""Tests for row/column XOR checksum recovery (intelhex.recovery)."""

import itertools
import random
import unittest
from io import StringIO

from intelhex import IntelHex
from intelhex.recovery import (
    READ,
    INFERRED,
    FieldLayout,
    PageLayout,
    LayoutError,
    recover_page,
    recover_pages,
    apply_recovery,
)


def xor_all(iterable):
    value = 0
    for item in iterable:
        value ^= item
    return value


def build_grid(width, height, rng, base=0x1000):
    '''Fill a grid and append valid row/column/corner XOR checksums.'''
    data = [rng.randrange(256) for _ in range(width * height)]
    checksum_col = width - 1
    checksum_row = height - 1
    for row in range(height - 1):
        data[row * width + checksum_col] = xor_all(
            data[row * width + col] for col in range(width - 1))
    for col in range(width - 1):
        data[checksum_row * width + col] = xor_all(
            data[row * width + col] for row in range(height - 1))
    data[checksum_row * width + checksum_col] = xor_all(
        data[row * width + checksum_col] for row in range(height - 1))
    return data


def row_field_layout(base, width, height, prefix='f'):
    return [
        FieldLayout('%s%d' % (prefix, row),
                    base + row * width, width - 1)
        for row in range(height - 1)
    ]


def image_with_holes(data, base, holes):
    ih = IntelHex()
    for index, byte in enumerate(data):
        if index not in holes:
            ih[base + index] = byte
    return ih


def exhaustive_completions(layout, ih):
    '''Enumerate every checksum-consistent completion.

    @return: address -> set of bytes the address can take.
    '''
    cells = layout.cells()
    known = dict((addr, ih[addr]) for addr in cells if addr in ih)
    unknown = sorted(addr for addr in cells if addr not in known)
    possible = dict((addr, set()) for addr in unknown)
    base = layout.base
    width = layout.width
    height = layout.height
    checksum_col = layout.row_checksum_col
    checksum_row = layout.col_checksum_row
    for combo in itertools.product(range(256), repeat=len(unknown)):
        values = dict(known)
        values.update(dict(zip(unknown, combo)))
        consistent = True
        for row in range(height):
            row_addrs = [base + row * width + col
                         for col in range(width) if col != checksum_col]
            if xor_all(values[addr] for addr in row_addrs) != \
                    values[base + row * width + checksum_col]:
                consistent = False
                break
        if not consistent:
            continue
        for col in range(width):
            if col == checksum_col:
                continue
            col_addrs = [base + row * width + col
                         for row in range(height) if row != checksum_row]
            if xor_all(values[addr] for addr in col_addrs) != \
                    values[base + checksum_row * width + col]:
                consistent = False
                break
        if not consistent:
            continue
        for addr in unknown:
            possible[addr].add(values[addr])
    return possible


def propagation_oracle(layout, ih):
    '''Independent reference: values forced by single-unknown lines.

    The unknown cells are edges of a bipartite graph of row/column line
    nodes; iterative leaf removal identifies exactly the edges that do not
    belong to any constraint cycle and are therefore unique.  Every line
    equation is included (the algebraically redundant row-checksum column
    too), so each unknown cell is incident on exactly two line nodes, which
    is what makes the graph characterization exact.
    '''
    cells = layout.cells()
    values = dict((addr, ih[addr]) for addr in cells if addr in ih)
    base = layout.base
    width = layout.width
    height = layout.height

    def line_ids():
        for cid in range(height + width):
            yield cid

    def line_cells(cid):
        if cid < height:
            return [base + cid * width + col for col in range(width)]
        col = cid - height
        return [base + row * width + col for row in range(height)]

    membership = {}
    state = {}
    for cid in line_ids():
        accumulator = 0
        missing = []
        for addr in line_cells(cid):
            if addr in values:
                accumulator ^= values[addr]
            else:
                missing.append(addr)
                membership.setdefault(addr, []).append(cid)
        state[cid] = [accumulator, missing]

    forced = {}
    queue = [cid for cid, (_acc, missing) in state.items()
             if len(missing) == 1]
    while queue:
        cid = queue.pop()
        accumulator, missing = state[cid]
        if len(missing) != 1:
            continue
        addr = missing[0]
        if addr in forced:
            continue
        value = accumulator
        forced[addr] = value
        values[addr] = value
        state[cid] = [accumulator, []]
        for other in membership[addr]:
            if other == cid:
                continue
            other_acc, other_missing = state[other]
            state[other] = [
                other_acc ^ value,
                [item for item in other_missing if item != addr],
            ]
            if len(state[other][1]) == 1:
                queue.append(other)
    return forced


class TestLayout(unittest.TestCase):

    def test_field_layout_validation(self):
        field = FieldLayout('serial', 0x100, 4)
        self.assertEqual(list(field.addresses()),
                         [0x100, 0x101, 0x102, 0x103])
        self.assertRaises(LayoutError, FieldLayout, 'bad', 0x10, 0)
        self.assertRaises(LayoutError, FieldLayout, 'bad', -1, 2)

    def test_page_dimensions(self):
        self.assertRaises(LayoutError, PageLayout,
                          'p', 0, 1, 4, [])
        self.assertRaises(LayoutError, PageLayout,
                          'p', -1, 4, 4, [])

    def test_fields_may_not_claim_checksum_cells(self):
        self.assertRaises(
            LayoutError, PageLayout, 'p', 0x100, 4, 4,
            [FieldLayout('a', 0x103, 1)])
        self.assertRaises(
            LayoutError, PageLayout, 'p', 0x100, 4, 4,
            [FieldLayout('a', 0x10C, 1)])

    def test_fields_may_not_overlap_or_escape(self):
        self.assertRaises(
            LayoutError, PageLayout, 'p', 0x100, 4, 4,
            [FieldLayout('a', 0x100, 3),
             FieldLayout('b', 0x102, 2)])
        self.assertRaises(
            LayoutError, PageLayout, 'p', 0x100, 4, 4,
            [FieldLayout('a', 0x110, 1)])

    def test_duplicate_field_names_rejected(self):
        self.assertRaises(
            LayoutError, PageLayout, 'p', 0x100, 4, 4,
            [FieldLayout('a', 0x100, 2),
             FieldLayout('a', 0x104, 2)])

    def test_coordinates_and_custom_checksums(self):
        layout = PageLayout(
            'p', 0x100, 4, 4, [FieldLayout('a', 0x105, 3)],
            row_checksum_col=0, col_checksum_row=0)
        self.assertEqual(layout.coordinates(0x105), (1, 1))
        self.assertTrue(layout.is_checksum_cell(0, 2))
        self.assertFalse(layout.is_checksum_cell(1, 1))
        self.assertEqual(layout.endaddr, 0x110)
        self.assertEqual(layout.row_address(2), 0x108)
        self.assertEqual(layout.column_address(3), 0x103)


class TestRecovery(unittest.TestCase):

    def setUp(self):
        self.rng = random.Random(20261114)

    def test_complete_image_is_all_read(self):
        width, height, base = 5, 4, 0x2000
        data = build_grid(width, height, self.rng, base)
        layout = PageLayout(
            'p', base, width, height,
            row_field_layout(base, width, height))
        report = recover_page(image_with_holes(data, base, set()), layout)
        self.assertFalse(report.has_contradictions)
        self.assertEqual(report.inferred, {})
        self.assertEqual(set(report.origins.values()), {READ})
        self.assertTrue(
            all(result.ok for result in report.field_results.values()))

    def test_holes_are_recovered_and_tagged_inferred(self):
        width, height, base = 4, 4, 0x3000
        data = build_grid(width, height, self.rng, base)
        layout = PageLayout('p', base, width, height, [
            FieldLayout('f1', base, 2),
            FieldLayout('f2', base + 2, 1),
            FieldLayout('f3', base + width, 3),
        ])
        holes = {1, 4, 6}
        report = recover_page(image_with_holes(data, base, holes), layout)
        self.assertFalse(report.has_contradictions)
        for index in holes:
            addr = base + index
            self.assertEqual(report.origin(addr), INFERRED)
            self.assertTrue(report.is_inferred(addr))
            self.assertEqual(report.recovered[addr], data[index])
        for addr in report.origins:
            if addr - base not in holes:
                self.assertEqual(report.origin(addr), READ)
        self.assertEqual(
            sorted(report.field_results['f1'].inferred), [base + 1])
        self.assertEqual(report.field_results['f1'].missing, [])

    def test_checksum_and_corner_holes_recovered(self):
        width, height, base = 4, 4, 0x7000
        data = build_grid(width, height, self.rng, base)
        layout = PageLayout(
            'p', base, width, height,
            row_field_layout(base, width, height))
        holes = {width - 1, 2 * width + width - 1, width * height - 1}
        report = recover_page(image_with_holes(data, base, holes), layout)
        self.assertFalse(report.has_contradictions)
        for index in holes:
            addr = base + index
            self.assertEqual(report.origin(addr), INFERRED)
            self.assertEqual(report.recovered[addr], data[index])

    def test_custom_checksum_positions(self):
        width, height, base = 5, 5, 0xB000
        grid = [self.rng.randrange(256)
                for _ in range(width * height)]
        checksum_col = checksum_row = 0
        for row in range(1, height):
            grid[row * width] = xor_all(
                grid[row * width + col] for col in range(1, width))
        for col in range(1, width):
            grid[col] = xor_all(
                grid[row * width + col] for row in range(1, height))
        grid[0] = xor_all(
            grid[row * width] for row in range(1, height))
        fields = [FieldLayout('f%d' % row, base + row * width + 1,
                              width - 1)
                  for row in range(1, height)]
        layout = PageLayout(
            'p', base, width, height, fields,
            row_checksum_col=checksum_col,
            col_checksum_row=checksum_row)
        holes = {7, 12, 21}
        report = recover_page(image_with_holes(grid, base, holes), layout)
        self.assertFalse(report.has_contradictions)
        for index in holes:
            self.assertEqual(report.recovered[base + index], grid[index])
            self.assertEqual(report.origin(base + index), INFERRED)

    def test_ambiguous_bytes_are_never_inferred_bruteforce(self):
        for width, height in [(3, 3), (3, 4), (4, 3), (4, 4)]:
            base = 0x5000
            data = build_grid(width, height, self.rng, base)
            layout = PageLayout(
                'p', base, width, height,
                row_field_layout(base, width, height))
            for _trial in range(8):
                holes = set(self.rng.sample(
                    range(width * height), self.rng.randrange(1, 3)))
                ih = image_with_holes(data, base, holes)
                report = recover_page(ih, layout)
                possible = exhaustive_completions(layout, ih)
                for addr, choices in possible.items():
                    if len(choices) == 1:
                        self.assertEqual(report.origin(addr), INFERRED)
                        self.assertEqual(
                            report.recovered[addr], next(iter(choices)))
                    else:
                        self.assertIsNone(report.origin(addr))

    def test_dense_holes_match_independent_oracle(self):
        for width, height in [(4, 5), (6, 6), (8, 4)]:
            base = 0xA000
            data = build_grid(width, height, self.rng, base)
            layout = PageLayout(
                'p', base, width, height,
                row_field_layout(base, width, height))
            for _trial in range(10):
                count = self.rng.randrange(1, width * height)
                holes = set(self.rng.sample(range(width * height), count))
                ih = image_with_holes(data, base, holes)
                report = recover_page(ih, layout)
                forced = propagation_oracle(layout, ih)
                self.assertEqual(set(report.inferred), set(forced))
                for addr, value in forced.items():
                    self.assertEqual(report.recovered[addr], value)
                    self.assertEqual(value, data[addr - base])

    def test_all_erased_data_cells_yield_no_guesses(self):
        width, height, base = 4, 4, 0x4000
        data = build_grid(width, height, self.rng, base)
        layout = PageLayout(
            'p', base, width, height,
            row_field_layout(base, width, height))
        holes = set(
            row * width + col
            for row in range(height - 1) for col in range(width - 1))
        report = recover_page(image_with_holes(data, base, holes), layout)
        self.assertEqual(report.inferred, {})
        self.assertEqual(
            len(report.free_cells), len(holes))

    def test_empty_image(self):
        layout = PageLayout(
            'p', 0x1000, 4, 4, row_field_layout(0x1000, 4, 4))
        report = recover_page(IntelHex(), layout)
        self.assertFalse(report.has_contradictions)
        self.assertEqual(report.inferred, {})
        self.assertEqual(len(report.free_cells), 16)


class TestContradictions(unittest.TestCase):

    def setUp(self):
        self.rng = random.Random(99)
        self.width, self.height, self.base = 6, 5, 0x6000
        self.data = build_grid(self.width, self.height, self.rng,
                               self.base)
        self.layout = PageLayout(
            'p', self.base, self.width, self.height,
            row_field_layout(self.base, self.width, self.height))

    def test_single_corruption_localizes_row_and_column(self):
        data = list(self.data)
        data[1 * self.width + 2] ^= 0x5A
        report = recover_page(
            image_with_holes(data, self.base, set()), self.layout)
        self.assertTrue(report.has_contradictions)
        self.assertEqual(report.bad_rows, [1])
        self.assertEqual(report.bad_columns, [2])
        text = '\n'.join(report.contradictions())
        self.assertIn('row 1', text)
        self.assertIn('column 2', text)

    def test_read_bytes_are_kept_not_overwritten(self):
        data = list(self.data)
        bad_addr = self.base + 1 * self.width + 2
        data[1 * self.width + 2] ^= 0x5A
        report = recover_page(
            image_with_holes(data, self.base, set()), self.layout)
        self.assertEqual(report.origin(bad_addr), READ)
        self.assertEqual(report.recovered[bad_addr],
                         self.data[1 * self.width + 2] ^ 0x5A)

    def test_unrelated_narrow_fields_still_export(self):
        data = list(self.data)
        data[1 * self.width + 2] ^= 0x5A
        layout = PageLayout('p', self.base, self.width, self.height, [
            FieldLayout('good', self.base, 2),
            FieldLayout('bad', self.base + self.width,
                        self.width - 1),
            FieldLayout('far', self.base + 3 * self.width, 2),
        ])
        holes = {0, 18}
        report = recover_page(
            image_with_holes(data, self.base, holes), layout)
        self.assertEqual(report.bad_rows, [1])
        self.assertEqual(report.bad_columns, [2])
        self.assertTrue(report.field_results['good'].ok)
        self.assertTrue(report.field_results['far'].ok)
        self.assertFalse(report.field_results['good'].affected)
        self.assertTrue(report.field_results['bad'].affected)
        # Holes on the good lines are still inferred.
        self.assertEqual(
            report.recovered[self.base], self.data[0])
        self.assertEqual(
            report.recovered[self.base + 18], self.data[18])

    def test_jointly_indistinguishable_group_is_reported(self):
        data = list(self.data)
        data[1 * self.width + 2] ^= 0x55
        holes = {1 * self.width, 1 * self.width + 1}
        report = recover_page(
            image_with_holes(data, self.base, holes), self.layout)
        # The bad row is certain; the donor column checksums cannot be
        # distinguished from it, so all involved lines are reported.
        self.assertEqual(report.bad_rows, [1])
        self.assertEqual(report.bad_columns, [0, 1, 2])
        field = report.field_results['f1']
        self.assertTrue(field.affected)
        self.assertEqual(
            sorted(field.missing),
            [self.base + 1 * self.width,
             self.base + 1 * self.width + 1])

    def test_corrupt_row_checksum_with_hole(self):
        data = list(self.data)
        data[1 * self.width + self.width - 1] ^= 0x77
        report = recover_page(
            image_with_holes(data, self.base, {1 * self.width}),
            self.layout)
        self.assertEqual(report.bad_rows, [1])

    def test_double_corruption_reports_both_lines(self):
        data = list(self.data)
        data[1 * self.width + 2] ^= 0x11
        data[3 * self.width + 4] ^= 0x22
        report = recover_page(
            image_with_holes(data, self.base, set()), self.layout)
        self.assertEqual(report.bad_rows, [1, 3])
        self.assertEqual(report.bad_columns, [2, 4])


class TestMultiPage(unittest.TestCase):

    def test_pages_are_independent(self):
        rng = random.Random(3)
        ih = IntelHex()
        layouts = []
        originals = {}
        for index, base in enumerate([0x8000, 0x9000]):
            width, height = 4, 4
            data = build_grid(width, height, rng, base)
            originals[base] = data
            layouts.append(PageLayout(
                'p%d' % index, base, width, height,
                row_field_layout(base, width, height)))
            for cell, byte in enumerate(data):
                if cell not in {2, 7}:
                    ih[base + cell] = byte
        reports = recover_pages(ih, layouts)
        self.assertEqual(len(reports), 2)
        for report in reports:
            base = report.layout.base
            for addr, value in report.inferred.items():
                self.assertEqual(value, originals[base][addr - base])

    def test_apply_recovery_fills_only_holes(self):
        rng = random.Random(4)
        width, height, base = 4, 4, 0xC000
        data = build_grid(width, height, rng, base)
        layout = PageLayout(
            'p', base, width, height,
            row_field_layout(base, width, height))
        ih = image_with_holes(data, base, {2, 7})
        report = recover_page(ih, layout)
        repaired = apply_recovery(ih, report)
        for addr in report.inferred:
            self.assertEqual(repaired[addr], report.recovered[addr])
        for addr in ih.addresses():
            self.assertEqual(repaired[addr], ih[addr])
        self.assertNotIn(base + 2, ih)
        self.assertIn(base + 2, repaired)

    def test_roundtrip_through_hex_file(self):
        rng = random.Random(5)
        width, height, base = 4, 4, 0xD000
        data = build_grid(width, height, rng, base)
        layout = PageLayout(
            'p', base, width, height,
            row_field_layout(base, width, height))
        source = image_with_holes(data, base, {1, 5})
        buffer = StringIO()
        source.write_hex_file(buffer)
        buffer.seek(0)
        reloaded = IntelHex(buffer)
        report = recover_page(reloaded, layout)
        for index in (1, 5):
            self.assertEqual(
                report.recovered[base + index], data[index])
            self.assertEqual(report.origin(base + index), INFERRED)


if __name__ == '__main__':
    unittest.main()
