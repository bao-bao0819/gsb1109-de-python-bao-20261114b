import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "..")
from pycparser.enum_diff import (
    EnumEvaluationError,
    diff_enums,
    diff_files,
    evaluate_enums,
)
from pycparser.enum_diff import main as enum_diff_main


class TestEnumEvaluation(unittest.TestCase):
    def test_implicit_values_follow_last_assignment(self):
        enums = evaluate_enums("enum E { A = 1, B, C = A + 5, D, E };")
        self.assertEqual(
            enums[0].values,
            {"A": 1, "B": 2, "C": 6, "D": 7, "E": 8},
        )

    def test_first_implicit_value_is_zero(self):
        self.assertEqual(
            evaluate_enums("enum E { A, B, C };")[0].values,
            {"A": 0, "B": 1, "C": 2},
        )

    def test_expression_can_reference_earlier_enum(self):
        enums = evaluate_enums("enum A { BASE = 10 }; enum B { X = BASE + 1, Y };")
        self.assertEqual(enums[0].values, {"BASE": 10})
        self.assertEqual(enums[1].values, {"X": 11, "Y": 12})

    def test_alias_expression_and_implicit_successor(self):
        enums = evaluate_enums("enum E { A = 1, B = A, C };")
        self.assertEqual(enums[0].values, {"A": 1, "B": 1, "C": 2})
        self.assertEqual(enums[0].groups[1], ("A", "B"))

    def test_integer_constant_expression_forms(self):
        enums = evaluate_enums(
            "enum E { A = 0x10, B = 'a', C = 1 << 3, "
            "D = -7 / 2, E = -7 % 2, F = ~0, G = (unsigned char)300 };"
        )
        self.assertEqual(
            enums[0].values,
            {"A": 16, "B": 97, "C": 8, "D": -3, "E": -1, "F": -1, "G": 44},
        )

    def test_ternary_and_sizeof(self):
        enums = evaluate_enums("enum E { A = 1 ? 7 : 9, B = sizeof(int) };")
        self.assertEqual(enums[0].values, {"A": 7, "B": 4})

    def test_c23_bool_constants(self):
        from pycparser import c_parser

        enums = evaluate_enums(
            "enum E { A = true, B = false, C = (bool)8 };",
            parser=c_parser.CParser(dialect="c23"),
        )
        self.assertEqual(enums[0].values, {"A": 1, "B": 0, "C": 1})

    def test_unknown_reference_is_an_error(self):
        with self.assertRaises(EnumEvaluationError):
            evaluate_enums("enum E { A = MISSING + 1 };")

    def test_divide_by_zero_is_an_error(self):
        with self.assertRaises(EnumEvaluationError):
            evaluate_enums("enum E { A = 1 / 0 };")

    def test_unrecognized_type_returns_error_not_silent_zero(self):
        with self.assertRaises(EnumEvaluationError):
            evaluate_enums("enum E { A = sizeof(struct S) };")


class TestEnumDiff(unittest.TestCase):
    def test_reordering_is_not_a_difference(self):
        result = diff_enums(
            "enum E { A = 1, B = 2, C = 3 };",
            "enum E { C = 3, A = 1, B = 2 };",
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.value_changes, ())
        self.assertEqual(result.diffs[0].added, ())
        self.assertEqual(result.diffs[0].removed, ())

    def test_alias_group_order_is_not_a_difference(self):
        result = diff_enums(
            "enum E { A = 5, B = 5, C = 6 };",
            "enum E { C = 6, B = 5, A = 5 };",
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.renames, ())

    def test_adding_alias_is_reported_without_breaking(self):
        result = diff_enums("enum E { A = 5 };", "enum E { A = 5, B = 5 };")
        self.assertTrue(result.compatible)
        self.assertEqual(result.diffs[0].added, ())
        self.assertEqual(result.diffs[0].renames[0].kind, "alias")
        self.assertEqual(result.diffs[0].renames[0].old_names, ("A",))
        self.assertEqual(result.diffs[0].renames[0].new_names, ("A", "B"))

    def test_rename_keeps_value(self):
        result = diff_enums(
            "enum E { A = 1, OLD = 3 };",
            "enum E { A = 1, NEW = 3 };",
        )
        self.assertTrue(result.compatible)
        changes = result.diffs[0].renames
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "rename")
        self.assertEqual(changes[0].value, 3)
        self.assertEqual(changes[0].old_names, ("OLD",))
        self.assertEqual(changes[0].new_names, ("NEW",))
        # The aliased names are not double-counted as plain additions and retirements.
        self.assertEqual(result.diffs[0].added, ())
        self.assertEqual(result.diffs[0].removed, ())

    def test_same_name_with_new_value_is_the_breaking_class(self):
        result = diff_enums(
            "enum E { A = 1, B = 2 };",
            "enum E { A = 2, B = 2 };",
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.value_changes[0]["name"], "A")
        self.assertEqual(result.value_changes[0]["old"], 1)
        self.assertEqual(result.value_changes[0]["new"], 2)

    def test_a_shifted_implicit_value_is_a_value_change(self):
        result = diff_enums(
            "enum E { A = 1, B, C = 3, D };",
            "enum E { A = 1, C = 3, B, D };",
        )
        self.assertFalse(result.compatible)
        changes = {
            change["name"]: (change["old"], change["new"])
            for change in result.value_changes
        }
        self.assertEqual(changes["B"], (2, 4))
        self.assertEqual(changes["D"], (4, 5))

    def test_retired_value_without_reuse_is_not_breaking(self):
        result = diff_enums("enum E { A = 1, B = 2 };", "enum E { A = 1 };")
        self.assertTrue(result.compatible)
        self.assertEqual(result.diffs[0].removed, ("B",))
        self.assertEqual(result.diffs[0].retired_values, (2,))

    def test_retired_number_reused_by_a_different_name_is_exposed(self):
        result = diff_enums("enum E { A = 1, OLD = 7 };", "enum E { A = 1, NEW = 7 };")
        self.assertTrue(result.compatible)
        renames = result.diffs[0].renames
        self.assertEqual(renames[0].value, 7)
        self.assertEqual(renames[0].old_names, ("OLD",))
        self.assertEqual(renames[0].new_names, ("NEW",))

    def test_primary_name_designation(self):
        result = diff_enums(
            "enum E { A = 5, B = 5 };",
            "enum E { A = 5, C = 5 };",
            primary_names=["A"],
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.diffs[0].primary_for_old(5), "A")
        self.assertEqual(result.diffs[0].primary_for_new(5), "A")
        self.assertEqual(result.diffs[0].renames[0].kind, "alias")

    def test_primary_name_comment_marker(self):
        result = diff_enums(
            "#pragma primary A\nenum E { A = 5, B = 5 };",
            "#pragma primary A\nenum E { A = 5, C = 5 };",
        )
        self.assertEqual(result.diffs[0].primary_for_old(5), "A")
        self.assertEqual(result.diffs[0].primary_for_new(5), "A")
        self.assertEqual(result.diffs[0].renames[0].kind, "alias")

    def test_anonymous_typedef_enum_uses_typedef_name(self):
        enums = evaluate_enums("typedef enum { A = 1, B = 2 } EventCode;")
        self.assertEqual(len(enums), 1)
        self.assertEqual(enums[0].tag, "EventCode")

    def test_multi_declarator_enum_is_not_duplicated(self):
        enums = evaluate_enums("enum E { A = 1 } x, y;")
        self.assertEqual(len(enums), 1)
        self.assertEqual(enums[0].tag, "E")

    def test_differently_named_enums_are_separate_dictionaries(self):
        result = diff_enums(
            "enum Old { A = 1 };",
            "enum New { A = 1 };",
        )
        self.assertTrue(result.compatible)
        keys = {diff.key for diff in result.diffs}
        self.assertEqual(keys, {"Old", "New"})

    def test_line_markers_separate_source_files(self):
        result = diff_enums(
            '#line 1 "a.h"\nenum E { A = 1 };\n#line 1 "b.h"\nenum E { A = 2 };',
            '#line 1 "a.h"\nenum E { A = 1 };\n#line 1 "b.h"\nenum E { A = 3 };',
        )
        changed = {entry["name"]: entry for entry in result.value_changes}
        self.assertEqual(changed["A"]["old"], 2)
        self.assertEqual(changed["A"]["new"], 3)
        self.assertEqual({diff.key for diff in result.diffs}, {"a.h:E", "b.h:E"})

    def test_json_contains_separate_breaking_and_rename_sections(self):
        result = diff_enums(
            "enum E { A = 1, OLD = 7 };",
            "enum E { A = 2, NEW = 7 };",
        )
        payload = result.as_dict()
        self.assertFalse(payload["compatible"])
        self.assertEqual(payload["value_changes"][0]["name"], "A")
        self.assertEqual(payload["renames"][0]["kind"], "rename")
        self.assertEqual(payload["enums"][0]["value_changes"][0]["name"], "A")


class TestEnumDiffFilesAndCli(unittest.TestCase):
    def _write(self, directory, name, text):
        path = Path(directory, name)
        path.write_text(text)
        return path

    def test_diff_files(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._write(directory, "old.h", "enum E { A = 1, B = 2 };")
            new = self._write(directory, "new.h", "enum E { B = 2, A = 1 };")
            result = diff_files(old, new)
        self.assertTrue(result.compatible)

    def test_cli_exit_codes_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._write(
                directory,
                "old.h",
                '#line 1 "events.h"\nenum E { A = 1, OLD = 2 };',
            )
            new = self._write(
                directory,
                "new.h",
                '#line 1 "events.h"\nenum E { A = 1, NEW = 2 };',
            )
            self.assertEqual(enum_diff_main([str(old), str(new)]) == 0, True)
            self.assertEqual(
                enum_diff_main([str(old), str(new), "--format", "json"]),
                0,
            )

    def test_cli_reports_input_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._write(directory, "old.h", "enum E { A = 1 };")
            missing = Path(directory, "missing.h")
            self.assertEqual(enum_diff_main([str(old), str(missing)]), 2)


if __name__ == "__main__":
    unittest.main()
