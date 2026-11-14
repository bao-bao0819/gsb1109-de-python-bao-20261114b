# -----------------------------------------------------------------
# pycparser: test_struct_layout.py
#
# Unit tests for examples/struct_layout.py. The expected numbers were
# cross-checked against the platform C compiler for the same declarations;
# the tests themselves run without a compiler by feeding already-parsed
# declarations to the layout engine.
#
# Eli Bendersky [https://elibendersky.net/]
# License: BSD
# -----------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pycparser import c_parser

from examples.struct_layout import (
    ABIParams,
    LayoutEngine,
    LayoutError,
    iter_leaf_fields,
)


def build(source, params=None):
    ast = c_parser.CParser(dialect="c23").parse(source)
    engine = LayoutEngine(params or ABIParams.arm64_aapcs())
    layouts = {layout.name: layout for layout in engine.build(ast)}
    return engine, layouts


class StructLayoutTest(unittest.TestCase):
    def test_primitive_padding_and_alignment(self):
        _, layouts = build("struct M1 { short s; char c; int i; };")
        layout = layouts["M1"]
        self.assertEqual((layout.size, layout.align), (8, 4))
        offsets = {path: f.offset for path, f in iter_leaf_fields(layout)}
        self.assertEqual(offsets, {"s": 0, "c": 2, "i": 4})

    def test_packed_struct(self):
        _, layouts = build("struct PP { char a; int b; } [[gnu::packed]];")
        layout = layouts["PP"]
        self.assertTrue(layout.packed)
        self.assertEqual((layout.size, layout.align), (5, 1))
        offsets = {path: f.offset for path, f in iter_leaf_fields(layout)}
        self.assertEqual(offsets, {"a": 0, "b": 1})

    def test_itanium_bitfields_share_unit(self):
        _, layouts = build("struct N2 { unsigned a:3; unsigned b:4; unsigned c:5; };")
        fields = dict(iter_leaf_fields(layouts["N2"]))
        self.assertEqual((fields["a"].offset, fields["a"].bit_offset), (0, 0))
        self.assertEqual((fields["b"].offset, fields["b"].bit_offset), (0, 3))
        self.assertEqual((fields["c"].offset, fields["c"].bit_offset), (0, 7))
        self.assertEqual(layouts["N2"].size, 4)

    def test_packed_bitfields_are_a_bit_stream(self):
        _, layouts = build(
            "struct P1 { char a:1; char b:1; int c:30; } [[gnu::packed]];"
        )
        fields = dict(iter_leaf_fields(layouts["P1"]))
        self.assertEqual((fields["a"].offset, fields["a"].bit_offset), (0, 0))
        self.assertEqual((fields["b"].offset, fields["b"].bit_offset), (0, 1))
        self.assertEqual((fields["c"].offset, fields["c"].bit_offset), (0, 2))
        self.assertEqual(layouts["P1"].size, 4)
        self.assertEqual(layouts["P1"].align, 1)

    def test_zero_width_bitfield_forces_boundary(self):
        _, layouts = build("struct Z1 { char a; int :0; char b; };")
        fields = dict(iter_leaf_fields(layouts["Z1"]))
        self.assertEqual(fields["a"].offset, 0)
        self.assertEqual(fields["b"].offset, 4)
        self.assertEqual(layouts["Z1"].size, 5)

    def test_anonymous_union_members_overlap_and_promote(self):
        _, layouts = build("struct AU1 { char c; union { int i; short s; }; };")
        fields = dict(iter_leaf_fields(layouts["AU1"]))
        self.assertEqual(fields["c"].offset, 0)
        self.assertEqual(fields["i"].offset, 4)
        self.assertEqual(fields["s"].offset, 4)
        self.assertIsNone(fields["i"].nested)

    def test_named_nested_record_keeps_hierarchy_and_dotted_paths(self):
        _, layouts = build(
            "struct Inner { int x; int y; };struct Outer { char a; struct Inner m; };"
        )
        outer = layouts["Outer"]
        container = outer.fields[1]
        self.assertEqual(container.path, "m")
        self.assertIsNotNone(container.nested)
        leaves = dict(iter_leaf_fields(outer))
        self.assertEqual(leaves["m.x"].offset, 4)
        self.assertEqual(leaves["m.y"].offset, 8)

    def test_flexible_array_reports_stride_not_length(self):
        _, layouts = build("struct FAM { int a; long arr[]; };")
        field = layouts["FAM"].fields[1]
        self.assertTrue(field.fam)
        self.assertEqual(field.offset, 8)
        self.assertEqual(field.element_stride, 8)
        self.assertEqual(field.size, 0)
        # The FAM does not pad the record to the array's alignment again.
        self.assertEqual(layouts["FAM"].size, 8)

    def test_zero_length_array_treated_like_flexible(self):
        _, layouts = build("struct ZARR { int a; char z[0]; };")
        field = layouts["ZARR"].fields[1]
        self.assertTrue(field.zero_length)
        self.assertEqual(field.element_stride, 1)
        self.assertEqual(field.offset, 4)

    def test_flexible_array_in_union_is_rejected(self):
        with self.assertRaises(LayoutError):
            build("union Bad { int a; long tail[]; };")

    def test_unknown_attribute_warns_and_marks_offset(self):
        engine, layouts = build(
            "struct Weird { char a; int b [[vendor::mystery(7)]]; char c; };"
        )
        self.assertEqual(len(engine.warnings), 1)
        self.assertEqual(engine.warnings[0].offset, 1)
        self.assertIn("mystery", engine.warnings[0].message)
        self.assertTrue(layouts["Weird"].fields[1].warnings)

    def test_overlap_between_ordinary_fields_is_an_error(self):
        engine = LayoutEngine(ABIParams.arm64_aapcs())
        claimed = [(0, 4, "first", True)]
        with self.assertRaises(LayoutError):
            engine._claim(claimed, 2, 2, "second")

    def test_field_alignment_attribute(self):
        _, layouts = build(
            "struct AL { short s; int i [[gnu::aligned(16)]]; char c; };"
        )
        fields = dict(iter_leaf_fields(layouts["AL"]))
        self.assertEqual(fields["i"].offset, 16)
        self.assertEqual(fields["c"].offset, 20)
        self.assertEqual(layouts["AL"].align, 16)
        self.assertEqual(layouts["AL"].size, 32)

    def test_big_endian_reverses_bit_offset(self):
        params = ABIParams.arm64_aapcs()
        params.endian = "big"
        _, layouts = build("struct BE { unsigned a:3; unsigned b:5; };", params)
        fields = dict(iter_leaf_fields(layouts["BE"]))
        self.assertEqual(fields["a"].bit_offset, 7)
        self.assertEqual(fields["b"].bit_offset, 4)

    def test_ilp32_pointer_and_long_are_four_bytes(self):
        _, layouts = build("struct SD { char c; long l; void *p; };", ABIParams.ilp32())
        fields = dict(iter_leaf_fields(layouts["SD"]))
        self.assertEqual(fields["l"].offset, 4)
        self.assertEqual(fields["l"].size, 4)
        self.assertEqual(fields["p"].offset, 8)
        self.assertEqual(fields["p"].size, 4)

    def test_fixed_underlying_enum_size(self):
        _, layouts = build(
            "enum EB : unsigned char { K = 200 };struct Use { char c; enum EB e; };"
        )
        fields = dict(iter_leaf_fields(layouts["Use"]))
        self.assertEqual(fields["e"].offset, 1)
        self.assertEqual(fields["e"].size, 1)

    def test_typedef_resolution_across_records(self):
        _, layouts = build("typedef unsigned int u32;struct Box { u32 a; u32 b; };")
        self.assertEqual(layouts["Box"].size, 8)
        fields = dict(iter_leaf_fields(layouts["Box"]))
        self.assertEqual(fields["a"].type_name, "unsigned int")

    def test_undefined_tag_is_reported(self):
        with self.assertRaises(LayoutError):
            build("struct Use { struct Missing m; };")

    def test_forward_declaration_resolves_to_later_definition(self):
        _, layouts = build(
            "struct X;struct X { int a; int b; };struct Use { struct X x; char c; };"
        )
        self.assertEqual(layouts["X"].size, 8)
        fields = dict(iter_leaf_fields(layouts["Use"]))
        self.assertEqual(fields["x.a"].offset, 0)
        self.assertEqual(fields["x.b"].offset, 4)
        self.assertEqual(fields["c"].offset, 8)

    def test_typedef_anonymous_record_is_listed_under_typedef_name(self):
        _, layouts = build(
            "typedef struct { int a; char b; } Record;struct S { Record r; };"
        )
        self.assertIn("Record", layouts)
        record = layouts["Record"]
        self.assertEqual(record.size, 8)
        self.assertEqual(record.fields[0].path, "a")
        self.assertEqual(layouts["S"].fields[0].type_name, "struct Record")

    def test_pragma_pack_two(self):
        _, layouts = build(
            "#pragma pack(push, 2)\n"
            "struct DPK { char a; int b; long c; };\n"
            "#pragma pack(pop)\n"
        )
        fields = dict(iter_leaf_fields(layouts["DPK"]))
        self.assertEqual(fields["a"].offset, 0)
        self.assertEqual(fields["b"].offset, 2)
        self.assertEqual(fields["c"].offset, 6)
        self.assertEqual(layouts["DPK"].size, 14)

    def test_packed_union_alignment_not_storage(self):
        _, layouts = build("union PU { int a:30; char b; } [[gnu::packed]];")
        layout = layouts["PU"]
        self.assertEqual(layout.size, 4)
        self.assertEqual(layout.align, 1)
        self.assertEqual(layout.fields[0].storage_size, 4)

    def test_nested_record_keeps_own_internal_layout_under_pack(self):
        _, layouts = build(
            "struct INNER { char x; int y; char z; };"
            "struct OUTN { char a; struct INNER m; int w; } [[gnu::packed]];"
        )
        fields = dict(iter_leaf_fields(layouts["OUTN"]))
        self.assertEqual(fields["m.x"].offset, 1)
        self.assertEqual(fields["m.y"].offset, 5)
        self.assertEqual(fields["m.z"].offset, 9)
        self.assertEqual(fields["w"].offset, 13)
        self.assertEqual(layouts["OUTN"].size, 17)

    def test_anonymous_nested_struct_inside_packed(self):
        _, layouts = build(
            "struct OUT { char a; struct { char x; int y; char z; }; int w; }"
            " [[gnu::packed]];"
        )
        fields = dict(iter_leaf_fields(layouts["OUT"]))
        self.assertEqual(
            {name: f.offset for name, f in fields.items()},
            {"a": 0, "x": 1, "y": 5, "z": 9, "w": 13},
        )

    def test_flexible_only_record_has_zero_size(self):
        _, layouts = build("struct ONLY { int a[]; };")
        layout = layouts["ONLY"]
        self.assertEqual(layout.size, 0)
        self.assertEqual(layout.align, 4)
        self.assertTrue(layout.fields[0].fam)

    def test_bitint_storage_rounds_to_power_of_two(self):
        _, layouts = build("struct BI { _BitInt(12) x; unsigned _BitInt(20) y; };")
        fields = dict(iter_leaf_fields(layouts["BI"]))
        self.assertEqual((fields["x"].offset, fields["x"].size), (0, 2))
        self.assertEqual((fields["y"].offset, fields["y"].size), (4, 4))
        self.assertEqual(layouts["BI"].size, 8)


if __name__ == "__main__":
    unittest.main()
