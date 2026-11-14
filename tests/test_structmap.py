import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from structmap import (
    Array,
    Ptr,
    RecordRef,
    business_merge_table,
    compare_modules,
    extract_module,
    records_equivalent,
    render_schema,
)
from structmap.layout import (
    PointerClassifier,
    expand_record,
    strongly_connected_components,
    record_edges,
)
from structmap.merge import table_to_dict

C_FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "c_files")
MODULE_A = os.path.join(C_FILES, "log_module_a.i")
MODULE_B = os.path.join(C_FILES, "log_module_b.i")
MODULE_C = os.path.join(C_FILES, "log_module_c.i")
MODULE_EDGE = os.path.join(C_FILES, "log_edge.i")
MODULE_RENAME = os.path.join(C_FILES, "log_rename.i")
MODULE_RENAME2 = os.path.join(C_FILES, "log_rename2.i")
MODULE_CHAINS = os.path.join(C_FILES, "log_chains.i")


def field(record, name):
    for item in record.fields:
        if item.name == name:
            return item
    raise KeyError(name)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.schema_a = extract_module(MODULE_A)
        self.schema_b = extract_module(MODULE_B)

    def test_each_module_keeps_its_own_payload(self):
        payload_a = self.schema_a.records["Payload"]
        payload_b = self.schema_b.records["Payload"]
        self.assertEqual(
            [f.name for f in payload_a.fields], ["ts", "level", "hdr", "msg", "id"]
        )
        self.assertEqual(
            [f.name for f in payload_b.fields], ["ts", "level", "hdr", "msg", "flags"]
        )
        self.assertNotEqual(
            [f.descriptor.to_dict() for f in payload_a.fields],
            [f.descriptor.to_dict() for f in payload_b.fields],
        )

    def test_typedef_aliases_are_fully_expanded(self):
        payload_a = self.schema_a.records["Payload"]
        payload_b = self.schema_b.records["Payload"]
        self.assertEqual(
            field(payload_a, "ts").descriptor, field(payload_b, "ts").descriptor
        )
        self.assertEqual(
            field(payload_a, "ts").descriptor.to_dict(),
            {"type": "prim", "names": ["unsigned", "int"]},
        )

    def test_array_dimensions_are_captured(self):
        msg_a = field(self.schema_a.records["Payload"], "msg").descriptor
        msg_b = field(self.schema_b.records["Payload"], "msg").descriptor
        self.assertIsInstance(msg_a, Array)
        self.assertEqual(msg_a.dim, 32)
        self.assertEqual(msg_b.dim, 64)

    def test_alias_table_flags_same_spelling_different_layout(self):
        report = compare_modules(self.schema_a, self.schema_b)
        status = next(row for row in report.alias_rows if row["alias"] == "Status")
        self.assertEqual(status["status"], "layout_differs")
        self.assertFalse(status["layout_equal"])


class LayoutEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.schema_a = extract_module(MODULE_A)
        self.schema_b = extract_module(MODULE_B)

    def test_equivalence_ignores_type_spelling(self):
        self.assertTrue(
            records_equivalent(self.schema_a, "LinkA", self.schema_b, "LinkB")
        )
        self.assertTrue(
            records_equivalent(self.schema_a, "HeaderA", self.schema_b, "HeaderB")
        )

    def test_equivalence_detects_real_layout_differences(self):
        self.assertFalse(
            records_equivalent(self.schema_a, "Payload", self.schema_b, "Payload")
        )
        self.assertFalse(
            records_equivalent(self.schema_a, "WrapA", self.schema_b, "WrapB")
        )

    def test_field_level_diff_explains_incompatibility(self):
        report = compare_modules(self.schema_a, self.schema_b)
        payload_match = next(
            m
            for m in report.record_matches
            if m.rid_a == "Payload" and m.rid_b == "Payload"
        )
        self.assertEqual(payload_match.verdict, "incompatible")
        rows = {row.name_a: row for row in payload_match.fields}
        self.assertEqual(rows["msg"].status, "type_mismatch")
        self.assertIn("array dim", rows["msg"].reason)
        self.assertEqual(rows["id"].status, "type_mismatch")
        self.assertIn("primitive", rows["id"].reason)
        self.assertEqual(rows["ts"].status, "same")

    def test_incompatible_records_are_paired_by_shape(self):
        report = compare_modules(self.schema_a, self.schema_b)
        wrap = next(m for m in report.record_matches if m.rid_a == "WrapA")
        self.assertEqual(wrap.rid_b, "WrapB")
        self.assertEqual(wrap.verdict, "incompatible")


class PointerCycleTests(unittest.TestCase):
    def setUp(self):
        self.schema_c = extract_module(MODULE_C)
        self.classifier = PointerClassifier(self.schema_c)

    def test_self_reference_is_classified_and_terminates(self):
        self.assertEqual(self.classifier.classify("Node1", "Node1"), "self")
        self.assertEqual(self.classifier.classify("Node2", "Node2"), "self")
        expanded = expand_record(self.schema_c, "Node1")
        marker = expanded["fields"][1]["layout"]["to"]
        self.assertEqual(marker["cycle"], "self")
        json.dumps(expanded)

    def test_mutual_reference_is_distinct_from_self(self):
        self.assertEqual(self.classifier.classify("Node1", "Node2"), "mutual")
        self.assertEqual(self.classifier.classify("Node2", "Node1"), "mutual")
        components = strongly_connected_components(record_edges(self.schema_c))
        self.assertEqual(components["Node1"], components["Node2"])
        self.assertNotEqual(components["Node1"], components["Holder"])
        expanded = expand_record(self.schema_c, "Node1")
        peer_back = expanded["fields"][2]["layout"]["to"]["fields"][1]["layout"]["to"]
        self.assertEqual(peer_back["cycle"], "mutual")

    def test_pointer_to_another_struct_is_external(self):
        schema_a = extract_module(MODULE_A)
        classifier = PointerClassifier(schema_a)
        self.assertEqual(classifier.classify("HeaderA", "LinkA"), "external")
        link = field(schema_a.records["HeaderA"], "link").descriptor
        self.assertIsInstance(link, Ptr)
        self.assertIsInstance(link.pointee, RecordRef)
        self.assertEqual(link.pointee.rid, "LinkA")

    def test_forward_declaration_resolves_to_definition(self):
        self.assertIn("Node2", self.schema_c.records)
        self.assertEqual(
            [f.name for f in self.schema_c.records["Node2"].fields],
            ["value", "back", "self_link"],
        )


class ScopeShadowingTests(unittest.TestCase):
    def setUp(self):
        self.schema_a = extract_module(MODULE_A)

    def test_block_scope_typedef_shadows_outer_alias(self):
        outer = self.schema_a.records["LocalRec"]
        inner = self.schema_a.records["LocalRec#2"]
        self.assertEqual([f.name for f in outer.fields], ["outer_field"])
        self.assertEqual([f.name for f in inner.fields], ["a", "b"])
        box_r = field(self.schema_a.records["BoxA"], "r").descriptor
        self.assertEqual(box_r.pointee.rid, "LocalRec#2")
        expanded = expand_record(self.schema_a, "BoxA")
        names = [f["name"] for f in expanded["fields"][0]["layout"]["to"]["fields"]]
        self.assertEqual(names, ["a", "b"])

    def test_outer_fields_keep_outer_interpretation(self):
        outer = self.schema_a.records["LocalRec"]
        self.assertEqual(
            outer.fields[0].descriptor.to_dict(),
            {"type": "prim", "names": ["unsigned", "int"]},
        )


class UnionAndBitfieldTests(unittest.TestCase):
    def setUp(self):
        self.schema_c = extract_module(MODULE_C)

    def test_union_kind_and_members(self):
        holder = self.schema_c.records["Holder"]
        self.assertEqual(holder.kind, "union")
        self.assertEqual([f.name for f in holder.fields], ["i", "f"])

    def test_bitfield_sizes(self):
        bits = self.schema_c.records["Bits"]
        self.assertEqual(field(bits, "a").bitsize, 3)
        self.assertEqual(field(bits, "b").bitsize, 5)


class MergeTableTests(unittest.TestCase):
    def setUp(self):
        self.schema_a = extract_module(MODULE_A)
        self.schema_b = extract_module(MODULE_B)

    def test_only_mergeable_records_enter_table(self):
        table = business_merge_table(self.schema_a, self.schema_b)
        names = {item.business_name for item in table}
        self.assertEqual(names, {"LinkA", "HeaderA"})
        payload = json.dumps(table_to_dict(table))
        self.assertNotIn("Payload", payload)

    def test_renamed_fields_are_layout_compatible(self):
        left = extract_module(MODULE_RENAME)
        right = extract_module(MODULE_RENAME2)
        self.assertTrue(records_equivalent(left, "Payload", right, "Payload"))
        report = compare_modules(left, right)
        match = next(m for m in report.record_matches if m.rid_a == "Payload")
        self.assertEqual(match.verdict, "mergeable")
        statuses = {row.name_a: row.status for row in match.fields}
        self.assertEqual(statuses["timestamp"], "renamed_layout_compatible")
        self.assertEqual(statuses["severity"], "renamed_layout_compatible")
        self.assertEqual(statuses["chain"], "same")
        table = business_merge_table(left, right)
        payload_row = next(item for item in table if item.business_name == "Payload")
        notes = {field.canonical_name: field.note for field in payload_row.fields}
        self.assertIn("timestamp", notes["timestamp"])

    def test_rendered_schema_is_json_serializable(self):
        for schema in (self.schema_a, self.schema_b):
            json.dumps(render_schema(schema))


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.schema = extract_module(MODULE_EDGE)

    def test_nested_anonymous_struct_gets_own_record(self):
        edge = self.schema.records["Edge"]
        nested = field(edge, "nested").descriptor
        self.assertIsInstance(nested, RecordRef)
        self.assertEqual(
            [f.name for f in self.schema.records[nested.rid].fields],
            ["inner_a", "inner_b"],
        )

    def test_function_pointers_are_typed(self):
        edge = self.schema.records["Edge"]
        handler = field(edge, "handler").descriptor
        self.assertIsInstance(handler, Ptr)
        from structmap import FuncPtr

        self.assertIsInstance(handler.pointee, FuncPtr)

    def test_qualified_pointer_keeps_pointee_layout(self):
        label = field(self.schema.records["Outer"], "label").descriptor
        self.assertEqual(
            label.to_dict(),
            {"type": "ptr", "to": {"type": "prim", "names": ["char"]}},
        )


class TypedefChainTests(unittest.TestCase):
    def setUp(self):
        self.schema = extract_module(MODULE_CHAINS)

    def test_nested_alias_chain_resolves_to_primitive(self):
        node = self.schema.records["Node"]
        self.assertEqual(
            field(node, "v").descriptor.to_dict(),
            {"type": "prim", "names": ["unsigned", "int"]},
        )
        self.assertEqual(
            self.schema.aliases["level2_t"].descriptor.to_dict(),
            {"type": "prim", "names": ["unsigned", "int"]},
        )

    def test_typedef_to_array_keeps_dimension(self):
        data = field(self.schema.records["Node"], "data").descriptor
        self.assertIsInstance(data, Array)
        self.assertEqual(data.dim, 10)

    def test_self_alias_resolves_to_defined_record(self):
        node = self.schema.records["Node"]
        nxt = field(node, "next").descriptor
        self.assertIsInstance(nxt, Ptr)
        self.assertIsInstance(nxt.pointee, RecordRef)
        self.assertEqual(nxt.pointee.rid, "Node")


class CliTests(unittest.TestCase):
    def test_writes_all_artifacts(self):
        import contextlib

        from structmap.cli import main

        with tempfile.TemporaryDirectory() as out_dir:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                code = main([MODULE_A, MODULE_B, "--out-dir", out_dir])
            self.assertEqual(code, 0)
            for name in (
                "schema_0.json",
                "schema_1.json",
                "comparison.json",
                "merge_table.json",
            ):
                with open(os.path.join(out_dir, name), encoding="utf-8") as handle:
                    json.load(handle)


if __name__ == "__main__":
    unittest.main()
