# ------------------------------------------------------------------------------
# pycparser: test_array_init.py
#
# Tests for pycparser.array_init, which reconstructs the concrete memory
# layout of C array initializers (including designators and brace elision).
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# ------------------------------------------------------------------------------
import sys
import unittest

sys.path.insert(0, "..")

from pycparser import c_parser
from pycparser.array_init import (
    ArrayInitError,
    Cell,
    extract_layout,
    extract_layouts,
    parse_and_extract,
)


def parse_one(source: str):
    ast = c_parser.CParser().parse(source)
    layouts = extract_layouts(ast)
    assert len(layouts) == 1, f"expected one layout, got {len(layouts)}"
    return layouts[0]


class TestScalarLayouts(unittest.TestCase):
    def test_sequential(self):
        layout = parse_one("int a[4] = { 10, 20, 30, 40 };")
        self.assertEqual(layout.dims, (4,))
        self.assertEqual(layout.grid(), [10, 20, 30, 40])
        self.assertTrue(all(cell.explicit for cell in layout.cells.values()))
        self.assertEqual(layout.stats()["implicit_zero"], 0)

    def test_implicit_zero_fill(self):
        layout = parse_one("int a[4] = { 1, 2 };")
        self.assertEqual(layout.grid(), [1, 2, 0, 0])
        stats = layout.stats()
        self.assertEqual(stats["explicit"], 2)
        self.assertEqual(stats["implicit_zero"], 2)
        self.assertTrue(layout.get(0).explicit)
        self.assertFalse(layout.get(2).explicit)
        self.assertIsNone(layout.get(2).source)
        self.assertIsNone(layout.get(2).coord)

    def test_explicit_zero_distinct_from_implicit(self):
        layout = parse_one("int a[4] = { 0, 1, 0 };")
        stats = layout.stats()
        self.assertEqual(stats["explicit_zero"], 2)
        self.assertEqual(stats["explicit_nonzero"], 1)
        self.assertEqual(stats["implicit_zero"], 1)
        zero_cell = layout.get(0)
        self.assertTrue(zero_cell.explicit)
        self.assertEqual(zero_cell.value, 0)
        self.assertEqual(zero_cell.source, "0")

    def test_designated_continuation(self):
        layout = parse_one("int a[5] = { 1, [3] = 30, 31 };")
        self.assertEqual(layout.grid(), [1, 0, 0, 30, 31])
        self.assertTrue(layout.get(3).designated)
        self.assertFalse(layout.get(4).designated)
        self.assertEqual(layout.get(4).source, "31")
        self.assertEqual(layout.stats()["implicit_zero"], 2)

    def test_last_write_wins_and_overwrite_count(self):
        layout = parse_one("int a[4] = { 1, [2] = 2, 3, [2] = 22 };")
        self.assertEqual(layout.grid(), [1, 0, 22, 3])
        self.assertEqual(layout.overwrites, 1)
        winner = layout.get(2)
        self.assertEqual(winner.value, 22)
        self.assertEqual(winner.source, "22")
        self.assertTrue(winner.designated)

    def test_unsized_array_inference(self):
        layout = parse_one("int a[] = { [3] = 30, 31 };")
        self.assertEqual(layout.declared_dims, (None,))
        self.assertEqual(layout.dims, (5,))
        self.assertEqual(layout.grid(), [0, 0, 0, 30, 31])

    def test_integer_literal_bases_and_signs(self):
        layout = parse_one("int a[5] = { -1, -0x10, 010, 0b101, 7u };")
        self.assertEqual(layout.grid(), [-1, -16, 8, 5, 7])

    def test_char_literal(self):
        layout = parse_one("int a[2] = { 'A', '\\n' };")
        self.assertEqual(layout.grid(), [65, 10])

    def test_float_literals(self):
        layout = parse_one("float a[3] = { 1.5f, .25, 1e2 };")
        self.assertEqual(layout.grid(), [1.5, 0.25, 100.0])

    def test_enum_constants(self):
        layout = parse_one(
            "enum { E_BASE = 10, E_NEXT };\nint a[3] = { E_BASE, E_NEXT, E_BASE + 1 };"
        )
        self.assertEqual(layout.grid(), [10, 11, 11])

    def test_non_constant_keeps_source_but_no_value(self):
        layout = parse_one("int ext;\nint a[2] = { ext, 4 };")
        self.assertIsNone(layout.get(0).value)
        self.assertEqual(layout.get(0).source, "ext")
        self.assertTrue(layout.get(0).explicit)
        self.assertEqual(layout.get(1).value, 4)

    def test_c_division_and_remainder_truncation(self):
        layout = parse_one("int a[4] = { -7 / 2, 7 / -2, -7 % 2, 7 % -2 };")
        self.assertEqual(layout.grid(), [-3, -3, -1, 1])

    def test_out_of_bounds_designator_warns_and_grows_grid(self):
        layout = parse_one("int a[2] = { [5] = 1, 2, 3 };")
        self.assertEqual(layout.declared_dims, (2,))
        self.assertEqual(layout.dims, (8,))
        self.assertTrue(any("exceeds declared size" in w for w in layout.warnings))
        self.assertEqual(layout.get(5).value, 1)
        self.assertEqual(layout.get(6).value, 2)
        self.assertEqual(layout.get(7).value, 3)
        # Grown grid has 8 cells but only 3 were written; the rest are
        # zero-filled, including the two within the declared size.
        self.assertEqual(layout.stats()["implicit_zero"], 5)


class TestMultidimensionalLayouts(unittest.TestCase):
    def test_nested_designator_with_continuation(self):
        layout = parse_one(
            "int a[6][3] = {\n"
            "    {1, 2, 3},\n"
            "    [2] = { [1] = 9, 8 },\n"
            "    7, 0,\n"
            "    [5][0] = 50, 51,\n"
            "};"
        )
        self.assertEqual(
            layout.grid(),
            [
                [1, 2, 3],
                [0, 0, 0],
                [0, 9, 8],
                [7, 0, 0],
                [0, 0, 0],
                [50, 51, 0],
            ],
        )
        self.assertTrue(layout.get(2, 1).designated)
        self.assertFalse(layout.get(2, 2).designated)
        self.assertTrue(layout.get(5, 0).designated)
        self.assertFalse(layout.get(5, 1).designated)
        # The engineer wrote one zero; nine other cells are C zero-filled.
        stats = layout.stats()
        self.assertEqual(stats["explicit_zero"], 1)
        self.assertEqual(stats["implicit_zero"], 9)

    def test_brace_elision_continues_into_next_row(self):
        layout = parse_one("int a[3][3] = { [1][0] = 9, 8, 99, [2][0] = 7 };")
        self.assertEqual(
            layout.grid(),
            [[0, 0, 0], [9, 8, 99], [7, 0, 0]],
        )

    def test_closing_brace_skips_remainder_of_row(self):
        layout = parse_one("int a[3][3] = { { 1 }, [1] = { 2 }, { 3, 4, 5 } };")
        self.assertEqual(
            layout.grid(),
            [[1, 0, 0], [2, 0, 0], [3, 4, 5]],
        )

    def test_chained_designator_brace_elision(self):
        layout = parse_one("int a[2][2] = { [0][0] = 1, 2, 3, 4 };")
        self.assertEqual(layout.grid(), [[1, 2], [3, 4]])

    def test_three_dimensional(self):
        layout = parse_one("int a[2][2][2] = { [1][0][1] = 9, 10 };")
        self.assertEqual(layout.get(1, 0, 1).value, 9)
        self.assertEqual(layout.get(1, 1, 0).value, 10)
        self.assertEqual(layout.stats()["implicit_zero"], 6)


class TestStringInitializers(unittest.TestCase):
    def test_one_dimensional_string(self):
        layout = parse_one('char a[6] = "abc";')
        self.assertEqual(layout.grid(), [97, 98, 99, 0, 0, 0])
        stats = layout.stats()
        # The terminating NUL is part of the written initializer, while the
        # two trailing cells are compiler zero-fill.
        self.assertEqual(stats["explicit_zero"], 1)
        self.assertEqual(stats["implicit_zero"], 2)
        self.assertEqual(layout.get(0).source, '"abc"')

    def test_two_dimensional_strings(self):
        layout = parse_one('char a[2][4] = { "ab", "cd" };')
        self.assertEqual(
            layout.grid(),
            [[97, 98, 0, 0], [99, 100, 0, 0]],
        )
        self.assertEqual(layout.stats()["implicit_zero"], 2)

    def test_unsized_string(self):
        layout = parse_one('char a[] = "xy";')
        self.assertEqual(layout.dims, (3,))
        self.assertEqual(layout.grid(), [120, 121, 0])


class TestStructArrays(unittest.TestCase):
    SOURCE = (
        "struct cal { int id; int gain; };\n"
        "struct cal devs[3] = { {1, 2}, [2].gain = 9 };"
    )

    def test_field_designator_and_zeros(self):
        layout = parse_one(self.SOURCE)
        self.assertEqual(layout.field_names, (None, ["id", "gain"]))
        self.assertEqual(layout.grid(), [[1, 2], [0, 0], [0, 9]])
        gain_cell = layout.get(2, 1)
        self.assertTrue(gain_cell.designated)
        self.assertEqual(gain_cell.source, "9")
        self.assertEqual(layout.stats()["implicit_zero"], 3)

    def test_inline_struct_definition(self):
        layout = parse_one("struct p { int x; int y; } pts[2] = { [1].y = 5 };")
        self.assertEqual(layout.grid(), [[0, 0], [0, 5]])

    def test_unknown_field_raises(self):
        with self.assertRaises(ArrayInitError):
            parse_one("struct p { int x; };\nstruct p pts[2] = { [0].nope = 1 };")


class TestSourcesAndCoords(unittest.TestCase):
    def test_coord_flows_through(self):
        layout = parse_and_extract("int a[2] = { [1] = 7 };", filename="firmware.c")[0]
        cell = layout.get(1)
        self.assertEqual(cell.coord.file, "firmware.c")
        self.assertEqual(cell.coord.line, 1)
        self.assertIsNone(layout.get(0).coord)

    def test_source_text_for_expression(self):
        layout = parse_one("int a[2] = { 1 + 2 * 3, (4 << 1) };")
        self.assertEqual(layout.get(0).value, 7)
        self.assertEqual(layout.get(0).source, "1 + (2 * 3)")
        self.assertEqual(layout.get(1).value, 8)

    def test_overwrite_keeps_last_source(self):
        layout = parse_one("int a[1] = { 1, [0] = 2 };")
        self.assertEqual(layout.get(0).source, "2")
        self.assertEqual(layout.overwrites, 1)


class TestApiGuards(unittest.TestCase):
    def test_non_array_decl_returns_none(self):
        ast = c_parser.CParser().parse("int scalar = 3;")
        decl = ast.ext[0]
        self.assertIsNone(extract_layout(decl))

    def test_uninitialized_array_returns_none(self):
        ast = c_parser.CParser().parse("int a[3];")
        self.assertIsNone(extract_layout(ast.ext[0]))

    def test_multiple_arrays_in_one_tu(self):
        layouts = parse_and_extract("int a[1] = { 1 };\nint b[2] = { 2 };")
        self.assertEqual([layout.name for layout in layouts], ["a", "b"])

    def test_too_many_designators_raises(self):
        with self.assertRaises(ArrayInitError):
            parse_one("int a[2] = { [0][1] = 1 };")

    def test_scalar_braces_take_first_element_only(self):
        layout = parse_one("int a[2] = { { 5, 6 }, 7 };")
        self.assertEqual(layout.grid(), [5, 7])
        self.assertTrue(any("excess" in w for w in layout.warnings))

    def test_redundant_braces_around_single_scalar(self):
        layout = parse_one("int a[2] = { { 5 }, 6 };")
        self.assertEqual(layout.grid(), [5, 6])
        self.assertEqual(layout.warnings, [])

    def test_node_input_accepted_directly(self):
        ast = c_parser.CParser().parse("int a[2] = { 1 };")
        layout = extract_layout(ast.ext[0])
        self.assertEqual(layout.grid(), [1, 0])
        self.assertIsInstance(layout.cells[(0,)], Cell)


if __name__ == "__main__":
    unittest.main()
