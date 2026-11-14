import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, "..")

from pycparser import c_parser
from pycparser.sampling_analysis import (
    Endpoint,
    EntryPoint,
    ReachabilityAnalyzer,
    ValueRange,
    analyze_file,
    prepare_source,
)

FIXTURE = "c_files/sampling_pipeline.c"


def read_fixture(filename: str = FIXTURE) -> str:
    path = os.path.join(os.path.dirname(__file__), filename)
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def parse_fixture(filename: str = FIXTURE):
    path = os.path.join(os.path.dirname(__file__), filename)
    return c_parser.CParser().parse(
        prepare_source(read_fixture(filename)), filename=path
    )


def analyze(entries, filename: str = FIXTURE):
    ast = parse_fixture(filename)
    analyzer = ReachabilityAnalyzer(ast, Endpoint("db_write"))
    return analyzer.analyze(entries), analyzer.truncations


def tuples(results):
    return [result.as_tuple() for result in results]


class TestArgumentPermutation(unittest.TestCase):
    def test_four_wrapper_layers_keep_payloads_correlated(self):
        results, _ = analyze(
            [EntryPoint("sample_environment", (ValueRange((0, 1)), 1))]
        )
        self.assertEqual(tuples(results), [(16, 3, 0.1), (32, 4, 0.01)])

    def test_every_frame_reports_a_verifiable_call_site(self):
        results, _ = analyze([EntryPoint("sample_environment", (0, 1))])
        self.assertEqual(len(results), 1)
        path = results[0].path
        self.assertEqual(
            [frame.function for frame in path],
            [
                "sample_environment",
                "push_sample",
                "dispatch",
                "send_row",
                "emit",
                "db_write",
            ],
        )
        for frame in path:
            self.assertIsNotNone(frame.coord, f"missing coord for {frame.function}")
            self.assertRegex(frame.coord, r"sampling_pipeline\.c:\d+:\d+")

    def test_swap_is_visible_in_the_intermediate_frames(self):
        results, _ = analyze([EntryPoint("sample_environment", (0, 1))])
        frames = {frame.function: frame for frame in results[0].path}
        # push_sample receives (raw, channel, field, scale); dispatch
        # receives (scale, field, channel, raw); emit receives payload order.
        self.assertEqual(
            [value.value for value in frames["push_sample"].arguments],
            [1, 3, 16, 0.1],
        )
        self.assertEqual(
            [value.value for value in frames["dispatch"].arguments],
            [0.1, 16, 3, 1],
        )
        self.assertEqual(
            [value.value for value in frames["db_write"].arguments],
            [16, 3, 0.1, 1],
        )


class TestBranchCorrelation(unittest.TestCase):
    def test_same_branch_locals_stay_together(self):
        results, _ = analyze(
            [EntryPoint("sample_environment_local", (ValueRange((0, 1)), 2))]
        )
        self.assertEqual(tuples(results), [(16, 3, 0.1), (32, 4, 0.01)])

    def test_temperature_channel_never_gets_humidity_scale(self):
        results, _ = analyze([EntryPoint("sample_shared", (ValueRange((0, 1)), 3))])
        pairs = {(result.as_tuple()[0], result.as_tuple()[2]) for result in results}
        self.assertEqual(pairs, {(16, 0.1), (16, 0.01)})
        self.assertNotIn((4, 0.1), pairs)

    def test_independent_branches_keep_their_own_association(self):
        # Both branches are controlled by the same parameter, so exactly two
        # combinations must appear, not four.
        results, _ = analyze([EntryPoint("sample_shared", (ValueRange((0, 1)), 3))])
        self.assertEqual(len(results), 2)

    def test_switch_only_reports_the_matching_case(self):
        results, _ = analyze([EntryPoint("sample_switch", (ValueRange((0, 1, 2)), 7))])
        self.assertEqual(tuples(results), [(16, 3, 0.1), (32, 4, 0.01)])
        self.assertTrue(all(result.entry == "sample_switch" for result in results))


class TestAggregatesAndRanges(unittest.TestCase):
    def test_struct_members_follow_designated_assignments(self):
        # emit() swaps the first two payload fields, so the write point
        # sees field=3/channel=16 for the temperature branch.
        results, _ = analyze([EntryPoint("sample_struct", (ValueRange((0, 1)), 4))])
        self.assertEqual(tuples(results), [(16, 3, 0.1), (32, 4, 0.01)])

    def test_struct_provenance_points_at_member_assignments(self):
        results, _ = analyze([EntryPoint("sample_struct", (0, 4))])
        channel, field, scale = results[0].values
        self.assertEqual(channel.value, 16)
        self.assertEqual(channel.source, "struct.field")
        # The member assignment site is retained as the origin.
        self.assertRegex(channel.coord, r"sampling_pipeline\.c:\d+:\d+")

    def test_value_range_expands_the_entry_argument(self):
        results, _ = analyze([EntryPoint("sample_loop", (8,))])
        self.assertEqual(tuples(results), [(16, 3, 0.1), (16, 4, 0.1)])

    def test_invalid_entry_arguments_become_unknown_and_do_not_invent_values(self):
        results, truncations = analyze([EntryPoint("sample_direct", ())])
        self.assertEqual(results, [])
        self.assertTrue(
            any(item.reason == "unknown_argument" for item in truncations),
            [item.render() for item in truncations],
        )


class TestTruncation(unittest.TestCase):
    def test_recursion_truncates_only_that_path(self):
        results, truncations = analyze([EntryPoint("sample_recursive", (5,))])
        reasons = {(item.reason, item.function) for item in truncations}
        self.assertIn(("recursion", "accumulate"), reasons)
        # The direct write with concrete parameters is still reported...
        self.assertIn((3, 16, 0.1), tuples(results))
        # ...and the sibling wrapper path too.
        self.assertIn((16, 3, 0.1), tuples(results))

    def test_recursion_does_not_report_the_unknown_dependent_write(self):
        results, _ = analyze([EntryPoint("sample_recursive", (5,))])
        for result in results:
            self.assertEqual(len(result.values), 3)
            for value in result.values:
                self.assertTrue(value.is_known())
        self.assertNotIn(UNKNOWN_SENTINEL, tuples(results))

    def test_unknown_function_truncates_only_that_path(self):
        results, truncations = analyze([EntryPoint("sample_unknown", (6,))])
        reasons = {(item.reason, item.function) for item in truncations}
        self.assertIn(("unknown_function", "sample_unknown"), reasons)
        self.assertIn((4, 32, 0.01), tuples(results))
        self.assertIn((32, 4, 0.01), tuples(results))

    def test_truncation_coordinate_points_at_the_call(self):
        _, truncations = analyze([EntryPoint("sample_unknown", (6,))])
        recursion = [item for item in truncations if item.reason == "unknown_function"]
        self.assertEqual(len(recursion), 1)
        self.assertRegex(recursion[0].coord, r"sampling_pipeline\.c:\d+:\d+")
        self.assertEqual(recursion[0].call_path, ("sample_unknown",))


UNKNOWN_SENTINEL = object()


class TestEntryPoints(unittest.TestCase):
    def test_unknown_entry_is_reported_not_silently_ignored(self):
        results, truncations = analyze([EntryPoint("does_not_exist")])
        self.assertEqual(results, [])
        self.assertEqual([item.reason for item in truncations], ["unknown_entry"])

    def test_multiple_entries_are_analyzed_independently(self):
        results, _ = analyze(
            [
                EntryPoint("sample_environment", (0, 1)),
                EntryPoint("sample_struct", (1, 2)),
            ]
        )
        entries = [result.entry for result in results]
        self.assertIn("sample_environment", entries)
        self.assertIn("sample_struct", entries)

    def test_endpoint_parameters_are_resolved_by_name(self):
        ast = parse_fixture()
        analyzer = ReachabilityAnalyzer(ast, Endpoint("db_write", ("scale", "channel")))
        results = analyzer.analyze([EntryPoint("sample_environment", (0, 1))])
        self.assertEqual(results[0].as_tuple(), (0.1, 16))
        self.assertEqual(results[0].payload_names, ("scale", "channel"))

    def test_missing_endpoint_parameter_is_an_error(self):
        ast = parse_fixture()
        with self.assertRaises(ValueError):
            ReachabilityAnalyzer(ast, Endpoint("db_write", ("bogus",)))


class TestReporting(unittest.TestCase):
    def test_jsonable_dict_contains_values_and_call_path(self):
        results, _ = analyze([EntryPoint("sample_environment", (0, 1))])
        payload = results[0].as_dict()
        self.assertEqual(payload["values"]["channel"]["value"], 16)
        self.assertEqual(payload["values"]["field"]["value"], 3)
        self.assertEqual(payload["values"]["scale"]["value"], 0.1)
        self.assertEqual(payload["write_coord"], results[0].write_coord)
        self.assertEqual(
            [frame["function"] for frame in payload["call_path"]][-1], "db_write"
        )

    def test_analyze_file_helper_returns_results_and_truncations(self):
        path = os.path.join(os.path.dirname(__file__), FIXTURE)
        results, truncations = analyze_file(
            path, [EntryPoint("sample_environment", (0, 1))], Endpoint("db_write")
        )
        self.assertEqual(tuples(results), [(16, 3, 0.1)])
        self.assertEqual(truncations, [])

    def test_duplicate_tuples_from_the_same_call_path_are_collapsed(self):
        results, _ = analyze([EntryPoint("sample_environment", (0, 1))])
        self.assertEqual(len(results), 1)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [
                sys.executable,
                os.path.join(root, "examples", "sampling_reachability.py"),
                *args,
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )

    def test_cli_text_reports_values_and_locations(self):
        completed = self.run_cli(
            FIXTURE_PATH,
            "--entry",
            "sample_environment",
            "--arg",
            "sample_environment=0,1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("channel=16", completed.stdout)
        self.assertIn("field=3", completed.stdout)
        self.assertIn("scale=0.1", completed.stdout)
        self.assertRegex(completed.stdout, r"sampling_pipeline\.c:\d+:\d+")

    def test_cli_json_is_parseable(self):
        completed = self.run_cli(
            FIXTURE_PATH,
            "--entry",
            "sample_environment",
            "--arg",
            "sample_environment=0,1",
            "--json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["results"][0]["values"]["channel"]["value"], 16)


FIXTURE_PATH = os.path.join(os.path.dirname(__file__), FIXTURE)


if __name__ == "__main__":
    unittest.main()
