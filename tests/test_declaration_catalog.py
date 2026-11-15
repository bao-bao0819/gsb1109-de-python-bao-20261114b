import unittest

from pycparser import CParser
from pycparser.c_parser import Coord
from pycparser.declaration_catalog import (
    DeclarationCatalog,
    LogicalLocation,
    PhysicalLocation,
)


def parse(text, filename="bundled.i"):
    return CParser().parse(text, filename=filename)


class TestDeclarationCatalog(unittest.TestCase):
    def test_keeps_both_logical_and_physical_locations(self):
        text = '# 1 "vendor.h"\nint width;\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)

        self.assertEqual(len(catalog), 1)
        entry = catalog.entries[0]
        self.assertEqual(entry.logical, LogicalLocation("vendor.h", 1))
        # The declaration sits on raw input line 2 of the concatenated file.
        self.assertEqual(entry.physical, PhysicalLocation("bundled.i", 2, 5))

    def test_logical_location_does_not_include_column_in_identity(self):
        # Two declarations on the same logical line share the LogicalLocation
        # even though their columns differ.
        text = '# 3 "vendor.c"\nint a; int b;\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        locations = set(catalog.logical_locations())
        self.assertEqual(locations, {LogicalLocation("vendor.c", 3)})

    def test_reverse_lookup_returns_all_expanded_instances(self):
        # The same vendor file/line is reached twice via repeated markers.
        text = '# 1 "vendor.h"\nint width;\n# 1 "vendor.h"\nint width;\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)

        instances = catalog.instances_at("vendor.h", 1)
        self.assertEqual(len(instances), 2)
        self.assertEqual([e.field_path for e in instances], ["width", "width"])
        # The two expansions are distinguished by their physical positions.
        self.assertEqual([e.physical.line for e in instances], [2, 4])

    def test_lookup_unknown_location_returns_empty(self):
        catalog = DeclarationCatalog.from_ast(parse("int a;\n"))
        self.assertEqual(catalog.instances_at("missing.h", 99), ())
        self.assertEqual(catalog.lookup("missing.h", 99, name="a"), ())

    def test_declarations_on_one_logical_line_distinguished(self):
        # Multiple declarations packed on one logical line must be told apart
        # by their owning type and field path.
        text = '# 7 "vendor.h"\ntypedef int T; int field; enum E { K };\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)

        entries = catalog.instances_at("vendor.h", 7)
        by_key = {(e.kind, e.field_path): e for e in entries}
        self.assertEqual(
            set(by_key),
            {
                ("Typedef", "T"),
                ("Decl", "field"),
                ("Enumerator", "E.K"),
            },
        )
        # Each keeps a distinct physical column on the same physical line.
        columns = {e.physical.column for e in entries}
        self.assertEqual(len(columns), 3)

    def test_struct_member_field_paths(self):
        text = '# 4 "vendor.h"\nstruct config { int flags; long base; };\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        paths = {
            e.field_path
            for e in catalog.instances_at("vendor.h", 4)
            if e.kind == "Decl"
        }
        self.assertEqual(paths, {"config.flags", "config.base"})

    def test_enumerator_field_paths(self):
        text = '# 9 "vendor.h"\nenum color { RED, GREEN };\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        entries = catalog.instances_at("vendor.h", 9)
        self.assertEqual(
            sorted(e.field_path for e in entries),
            ["color.GREEN", "color.RED"],
        )

    def test_function_definition_recorded_once(self):
        text = '# 20 "vendor.c"\nint main(void) { int local = 1; return local; }\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        entries = catalog.instances_at("vendor.c", 20)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "FuncDef")
        self.assertEqual(entries[0].field_path, "main")

    def test_repeated_marker_switching_with_flags(self):
        # Real cpp output carries numeric flags after the filename; they must
        # not be interpreted as, or turned into, a macro definition stack.
        text = (
            '# 1 "vendor.h" 1 3\n'
            "typedef unsigned long size_t;\n"
            '# 2 "vendor.c" 2\n'
            "int run(void);\n"
        )
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        self.assertEqual(
            [e.logical for e in catalog],
            [LogicalLocation("vendor.h", 1), LogicalLocation("vendor.c", 2)],
        )
        # Entries only carry file/line logically; there is no fabricated stack.
        for entry in catalog:
            self.assertIsInstance(entry.logical, LogicalLocation)
            self.assertFalse(hasattr(entry, "macro_stack"))

    def test_physical_coordinates_survive_marker_rewrites(self):
        text = '# 5 "a.h"\nint x;\n# 5 "a.h"\nint y;\n'
        ast = parse(text, filename="out.i")
        catalog = DeclarationCatalog.from_ast(ast)
        for entry in catalog:
            self.assertEqual(entry.physical.file, "out.i")
        self.assertEqual([e.physical.line for e in catalog], [2, 4])

    def test_name_filter_in_lookup(self):
        text = '# 3 "vendor.c"\nint a; int b;\n'
        ast = parse(text)
        catalog = DeclarationCatalog.from_ast(ast)
        only_a = catalog.lookup("vendor.c", 3, name="a")
        self.assertEqual([e.field_path for e in only_a], ["a"])

    def test_entries_are_frozen(self):
        entry = DeclarationCatalog.from_ast(parse("int a;\n")).entries[0]
        with self.assertRaises(Exception):
            entry.field_path = "other"  # type: ignore[misc]

    def test_coord_physical_fields_excluded_from_equality(self):
        logical = Coord(file="v.h", line=1, column=2)
        with_phys = Coord(
            file="v.h",
            line=1,
            column=2,
            phys_file="out.i",
            phys_line=9,
            phys_column=3,
        )
        self.assertEqual(logical, with_phys)
        # Physical data is still accessible and preserved.
        self.assertEqual(with_phys.phys_line, 9)
        self.assertEqual(str(with_phys), "v.h:1:2")


if __name__ == "__main__":
    unittest.main()
