import itertools
import random

import pytest

from src.junitparser import (
    CaseSpec,
    JUnitXml,
    LogSplitError,
    LogSplitter,
    SystemOutSplitter,
    TestSuite,
    split_log,
    split_system_out,
)

BEGIN = r"^=== BEGIN ===$"
END = r"^=== END ===$"
FRAGMENT = r"^--- fragment (?P<fragment>\d+) ---$"
EXECUTION = r"execution id: (?P<execution_id>\S+)"
CASE = r"case: (?P<case>\S+)"


def interleaved_log():
    return "\n".join(
        [
            "suite preamble",
            "=== BEGIN ===",
            "--- fragment 1 ---",
            "alpha first",
            "--- fragment 1 ---",
            "beta first",
            "--- fragment 2 ---",
            "alpha second",
            "--- fragment 2 ---",
            "shared tail",
            "=== END ===",
            "suite epilogue",
        ]
    )


def two_cases():
    return [CaseSpec("alpha", (1, 2)), CaseSpec("beta", (1, 2))]


def split(text, cases=None, **kwargs):
    options = dict(start=BEGIN, end=END, fragment=FRAGMENT)
    options.update(kwargs)
    return split_system_out(text, cases or two_cases(), **options)


class TestAmbiguity:
    def test_only_unambiguous_lines_are_archived(self):
        result = split(interleaved_log())

        assert result.feasible
        assert result.complete
        assert result.archived_text() == {"alpha": "", "beta": ""}
        assert result.candidates
        assert all(
            a.candidate_executions == ("alpha", "beta") for a in result.assignments
        )

    def test_explicit_case_names_archive_their_lines(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 ---",
                "case: alpha running setup",
                "--- fragment 1 ---",
                "case: beta running setup",
                "--- fragment 2 ---",
                "case: alpha running teardown",
                "--- fragment 2 ---",
                "case: beta running teardown",
                "=== END ===",
            ]
        )
        result = split(text, case=CASE)

        assert result.feasible
        assert result.archived_text() == {
            "alpha": "case: alpha running setup\ncase: alpha running teardown",
            "beta": "case: beta running setup\ncase: beta running teardown",
        }
        assert result.candidates == ()

    def test_execution_id_resolves_the_fragment_it_appears_on(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 ---",
                "alpha one",
                "--- fragment 1 ---",
                "beta one execution id: exec-b",
                "=== END ===",
            ]
        )
        result = split(
            text,
            [CaseSpec("alpha", (1,)), CaseSpec("beta", (1,), execution_id="exec-b")],
            execution_id=EXECUTION,
        )

        assert result.feasible
        by_text = {assignment.text: assignment for assignment in result.assignments}
        assert by_text["beta one execution id: exec-b"].case == "beta"
        assert by_text["alpha one"].case == "alpha"
        assert "alpha one" not in result.for_case("beta").text

    def test_execution_ids_map_can_name_several_cases(self):
        text = "\n".join(
            [
                "--- fragment 1 ---",
                "retry attempt",
                "--- fragment 2 ---",
                "retry result",
            ]
        )
        result = split(
            text,
            [CaseSpec("a", (1, 2)), CaseSpec("b", (1, 2))],
            start=None,
            end=None,
            executions={"retry": ("a", "b")},
            execution_id=r"(?P<execution_id>retry) attempt",
        )

        assert result.feasible
        assert all(
            set(assignment.candidates) == {"a", "b"}
            for assignment in result.assignments
        )

    def test_shared_begin_marker_execution_id_pins_only_its_block(self):
        text = "\n".join(
            [
                "=== BEGIN execution id: exec-a ===",
                "--- fragment 1 ---",
                "alpha",
                "--- fragment 1 ---",
                "beta",
                "=== END ===",
            ]
        )
        result = split(
            text,
            [
                CaseSpec("alpha", (1,), execution_id="exec-a"),
                CaseSpec("beta", (1,), execution_id="exec-b"),
            ],
            start=r"^=== BEGIN(?: execution id: (?P<execution_id>\S+))? ===$",
            execution_id=EXECUTION,
        )

        assert result.feasible
        assert result.archived_text() == {"alpha": "alpha", "beta": "beta"}

    def test_plain_shared_begin_marker_does_not_pin_the_region(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 ---",
                "alpha",
                "--- fragment 1 ---",
                "beta",
                "=== END ===",
            ]
        )
        result = split(text, [CaseSpec("alpha", (1,)), CaseSpec("beta", (1,))])

        assert result.feasible
        assert [assignment.case for assignment in result.assignments] == [None, None]
        assert all(
            a.candidate_executions == ("alpha", "beta") for a in result.assignments
        )

    def test_per_case_regions_are_archived_completely(self):
        text = "\n".join(
            [
                "=== case alpha start ===",
                "--- fragment 1 ---",
                "alpha output",
                "=== case alpha end ===",
                "=== case beta start ===",
                "--- fragment 1 ---",
                "beta output",
                "=== case beta end ===",
            ]
        )
        result = split_system_out(
            text,
            two_cases(),
            start=r"^=== case (?P<case>\w+) start ===$",
            end=r"^=== case (?P<case>\w+) end ===$",
            fragment=FRAGMENT,
        )

        assert result.archived_text() == {
            "alpha": "alpha output",
            "beta": "beta output",
        }
        assert result.candidates == ()


class TestCandidates:
    def test_candidate_lines_keep_their_cases_and_order(self):
        result = split(interleaved_log())

        assert [assignment.text for assignment in result.candidates] == [
            "alpha first",
            "beta first",
            "alpha second",
            "shared tail",
        ]
        assert [assignment.number for assignment in result.candidates] == [4, 6, 8, 10]
        assert result.candidate_executions == {
            4: ("alpha", "beta"),
            6: ("alpha", "beta"),
            8: ("alpha", "beta"),
            10: ("alpha", "beta"),
        }

    def test_unknown_fragment_has_no_candidate_and_breaks_feasibility(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 7 ---",
                "orphan output",
                "=== END ===",
            ]
        )
        result = split(text)

        assert not result.feasible
        assert result.assignments[0].candidates == ()
        assert result.assignments[0].unresolved
        assert result.archived_text() == {"alpha": "", "beta": ""}

    def test_impossible_fragment_order_is_not_archived(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 2 ---",
                "second before first",
                "--- fragment 1 ---",
                "first after second",
                "=== END ===",
            ]
        )
        result = split(text, [CaseSpec("alpha", (1, 2))])

        assert not result.feasible
        assert all(assignment.case is None for assignment in result.assignments)
        assert result.archived_text() == {"alpha": ""}

    def test_gaps_between_fragments_are_allowed(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 ---",
                "first",
                "--- fragment 4 ---",
                "fourth",
                "=== END ===",
            ]
        )
        result = split(text, [CaseSpec("alpha", (1, 4))])

        assert result.feasible
        assert result.archived_text() == {"alpha": "first\nfourth"}

    def test_exhausted_search_archives_nothing(self):
        lines = []
        for fragment in range(1, 9):
            for case in ("a", "b", "c"):
                lines.extend([f"--- fragment {fragment} ---", f"{case}-{fragment}"])
        cases = [CaseSpec(name, tuple(range(1, 9))) for name in "abc"]

        result = split_system_out(
            "\n".join(lines),
            cases,
            fragment=FRAGMENT,
            max_states=1,
        )

        assert not result.complete
        assert result.archived_text() == {"a": "", "b": "", "c": ""}
        assert result.candidates
        assert result.candidate_executions


class TestSourceFidelity:
    def test_duplicate_text_lines_are_not_merged(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 ---",
                "case: alpha duplicate output",
                "--- fragment 2 ---",
                "case: alpha duplicate output",
                "=== END ===",
            ]
        )
        result = split(text, [CaseSpec("alpha", (1, 2))], case=CASE)

        lines = result.for_case("alpha").lines
        assert [line.text for line in lines] == [
            "case: alpha duplicate output",
            "case: alpha duplicate output",
        ]
        assert [line.number for line in lines] == [3, 5]
        assert result.for_case("alpha").line_numbers == (3, 5)

    def test_blank_lines_and_crlf_keep_their_numbers(self):
        text = (
            "=== BEGIN ===\r\n"
            "--- fragment 1 ---\r\n"
            "case: alpha first\r\n"
            "\r\n"
            "case: alpha last\r\n"
            "=== END ===\r\n"
        )
        result = split(text, [CaseSpec("alpha", (1,))], case=CASE)

        lines = result.for_case("alpha").lines
        assert [(line.number, line.text) for line in lines] == [
            (3, "case: alpha first"),
            (4, ""),
            (5, "case: alpha last"),
        ]
        assert result.lines[2].number == 3

    def test_outside_lines_are_reported_without_being_archived(self):
        result = split(interleaved_log())

        assert [line.number for line in result.outside] == [1, 12]
        assert [line.text for line in result.outside] == [
            "suite preamble",
            "suite epilogue",
        ]

    def test_trailing_newline_does_not_create_a_line(self):
        result = split("=== BEGIN ===\n--- fragment 1 ---\nalpha\n=== END ===\n")

        assert len(result.lines) == 4
        assert result.assignments[0].number == 3

    def test_to_dict_preserves_numbers_and_candidates(self):
        data = split(interleaved_log()).to_dict()

        assert data["feasible"] is True
        assert data["complete"] is True
        assert data["line_count"] == 12
        assert data["cases"]["alpha"] == {
            "name": "alpha",
            "execution_ids": [],
            "lines": [],
        }
        assert data["pending"][0] == {
            "number": 4,
            "text": "alpha first",
            "status": "ambiguous",
            "candidate_executions": ["alpha", "beta"],
        }
        assert data["outside_lines"] == [1, 12]


class TestConfiguration:
    def test_mapping_cases_accept_fragments(self):
        result = split_system_out(
            "--- fragment 1 ---\nonly\n", {"alpha": (1,)}, fragment=FRAGMENT
        )

        assert result.for_case("alpha").text == "only"

    def test_mapping_cases_accept_case_spec(self):
        result = split_system_out(
            "--- fragment 3 ---\nthird\n",
            {"alpha": CaseSpec("alpha", (3,))},
            fragment=FRAGMENT,
        )

        assert result.for_case("alpha").text == "third"

    def test_integer_mapping_shorthand(self):
        result = split_system_out(
            "--- fragment 5 ---\nfifth\n", {"alpha": 5}, fragment=FRAGMENT
        )

        assert result.for_case("alpha").text == "fifth"

    def test_iterable_of_specs(self):
        result = split_system_out(
            "--- fragment 1 ---\nx\n",
            iter([CaseSpec("alpha", (1,))]),
            fragment=FRAGMENT,
        )

        assert result.for_case("alpha").text == "x"

    def test_splitter_can_be_reused(self):
        splitter = SystemOutSplitter(
            two_cases(), start=BEGIN, end=END, fragment=FRAGMENT
        )
        first = splitter.split(interleaved_log())
        second = splitter.split(interleaved_log())

        assert first.archived_text() == second.archived_text()
        assert LogSplitter is SystemOutSplitter
        assert split_log is split_system_out

    def test_suite_helper_uses_the_suite_system_out(self):
        suite = TestSuite("suite")
        suite.system_out = interleaved_log()

        result = suite.split_system_out(
            two_cases(), start=BEGIN, end=END, fragment=FRAGMENT
        )

        assert result.candidates
        assert result.lines[0].text == "suite preamble"

    def test_junit_xml_single_suite_is_accepted(self):
        xml = JUnitXml.fromstring(
            "<testsuite name='s'><testcase name='alpha'/><testcase name='beta'/></testsuite>"
        )
        suite = next(iter(xml))
        suite.system_out = "--- fragment 1 ---\nboth\n"

        result = split_system_out(suite, two_cases(), fragment=FRAGMENT)

        assert result.assignments[0].candidates == ("alpha", "beta")

    def test_junit_xml_with_several_suites_is_rejected(self):
        xml = JUnitXml.fromstring(
            "<testsuites><testsuite name='a'/><testsuite name='b'/></testsuites>"
        )

        with pytest.raises(LogSplitError, match="multiple test suites"):
            split_system_out(xml, two_cases(), fragment=FRAGMENT)

    def test_missing_system_out_is_rejected(self):
        with pytest.raises(LogSplitError, match="system-out"):
            split_system_out(TestSuite("empty"), two_cases(), fragment=FRAGMENT)

    def test_for_case_rejects_unknown_names(self):
        result = split(interleaved_log())

        with pytest.raises(KeyError, match="unknown"):
            result.for_case("unknown")

    def test_non_string_source_is_rejected(self):
        with pytest.raises(LogSplitError, match="system-out string"):
            split_system_out(123, two_cases())
        with pytest.raises(LogSplitError, match="not bytes"):
            split_system_out(b"bytes", two_cases())


class TestValidation:
    @pytest.mark.parametrize("fragments", [(2, 1), (1, 1), (1, "x")])
    def test_invalid_case_fragments_are_rejected(self, fragments):
        with pytest.raises(LogSplitError):
            CaseSpec("alpha", fragments)

    def test_case_name_must_not_be_empty(self):
        with pytest.raises(LogSplitError):
            CaseSpec("")

    def test_execution_id_must_not_be_empty(self):
        with pytest.raises(LogSplitError):
            CaseSpec("alpha", execution_id="")

    def test_duplicate_case_names_are_rejected(self):
        with pytest.raises(LogSplitError, match="Duplicate"):
            split_system_out(
                "",
                [CaseSpec("alpha"), CaseSpec("alpha")],
            )

    def test_no_cases_are_rejected(self):
        with pytest.raises(LogSplitError, match="At least one"):
            split_system_out("", [])

    def test_invalid_fragment_regex_is_rejected(self):
        with pytest.raises(LogSplitError, match="regular expression"):
            split_system_out("", two_cases(), fragment="(?P<fragment>")

    def test_non_integer_fragment_group_is_rejected(self):
        with pytest.raises(LogSplitError, match="integer"):
            split_system_out(
                "frag x\nline\n",
                two_cases(),
                fragment=r"frag (?P<fragment>\S+)",
            )

    def test_max_states_must_be_positive(self):
        with pytest.raises(LogSplitError, match="max_states"):
            split_system_out("", two_cases(), max_states=0)


class TestFragmentPatterns:
    @pytest.mark.parametrize("group", ["fragment", "frag", "index", "seq"])
    def test_accepted_group_names(self, group):
        text = "--- fragment 1 ---\nline\n"
        result = split_system_out(
            text, {"alpha": (1,)}, fragment=rf"fragment (?P<{group}>\d+)"
        )

        assert result.for_case("alpha").text == "line"

    def test_compiled_patterns_are_accepted(self):
        import re

        result = split_system_out(
            "--- fragment 1 ---\nline\n",
            {"alpha": (1,)},
            fragment=re.compile(FRAGMENT),
        )

        assert result.for_case("alpha").text == "line"

    def test_fragment_marker_may_carry_the_case(self):
        text = "\n".join(
            [
                "=== BEGIN ===",
                "--- fragment 1 case: alpha ---",
                "alpha line",
                "--- fragment 1 case: beta ---",
                "beta line",
                "=== END ===",
            ]
        )
        result = split_system_out(
            text,
            two_cases(),
            start=BEGIN,
            end=END,
            fragment=r"^--- fragment (?P<fragment>\d+) case: (?P<case>\S+) ---$",
        )

        assert result.archived_text() == {"alpha": "alpha line", "beta": "beta line"}

    def test_no_structural_markers_treats_text_as_free_blocks(self):
        result = split_system_out("line one\nline two\n", two_cases())

        assert [a.candidates for a in result.assignments] == [
            ("alpha", "beta"),
            ("alpha", "beta"),
        ]


def _brute_force(fragments, names, specs):
    """Enumerate every assignment satisfying the fragment-order constraints."""
    ranks = {
        spec.name: {fragment: rank for rank, fragment in enumerate(spec.fragments)}
        for spec in specs
    }
    solutions = []
    for assignment in itertools.product(names, repeat=len(fragments)):
        last = {name: -1 for name in names}
        valid = True
        for fragment, name in zip(fragments, assignment):
            rank = ranks[name].get(fragment)
            if rank is None or rank <= last[name]:
                valid = False
                break
            last[name] = rank
        if valid:
            solutions.append(assignment)
    return solutions


class TestAgainstBruteForce:
    def test_candidates_and_archive_match_exhaustive_enumeration(self):
        random.seed(20261114)
        for _ in range(300):
            names = [f"c{index}" for index in range(random.randint(1, 3))]
            specs = [
                CaseSpec(
                    name,
                    tuple(sorted(random.sample(range(1, 4), random.randint(1, 3)))),
                )
                for name in names
            ]
            fragments = [random.randint(1, 3) for _ in range(random.randint(1, 5))]
            text = "\n".join(
                line
                for index, fragment in enumerate(fragments)
                for line in (f"F{fragment}", f"line {index}")
            )

            result = split_system_out(text, specs, fragment=r"^F(?P<fragment>\d+)$")
            solutions = _brute_force(fragments, names, specs)
            domains = {
                index: tuple(spec.name for spec in specs if fragment in spec.fragments)
                for index, fragment in enumerate(fragments)
            }

            if not solutions:
                assert [a.candidates for a in result.assignments] == [
                    domains[index] for index in range(len(fragments))
                ]
                assert all(a.case is None for a in result.assignments)
                continue

            expected_candidates = [
                tuple(
                    name
                    for name in names
                    if any(solution[index] == name for solution in solutions)
                )
                for index in range(len(fragments))
            ]
            expected_cases = [
                solution[index] if len({s[index] for s in solutions}) == 1 else None
                for index in range(len(fragments))
                for solution in solutions[:1]
            ]

            assert [a.candidates for a in result.assignments] == expected_candidates
            assert [a.case for a in result.assignments] == expected_cases
            assert result.feasible


class TestDefaults:
    def test_default_markers_are_used_without_regex_arguments(self):
        text = "\n".join(
            [
                "--- fragment 1 ---",
                "execution id: e1 alpha start",
                "--- fragment 1 ---",
                "execution id: e2 beta start",
                "--- fragment 2 ---",
                "execution id: e1 alpha end",
            ]
        )
        result = split_system_out(
            text,
            [
                CaseSpec("alpha", (1, 2), execution_id="e1"),
                CaseSpec("beta", (1,), execution_id="e2"),
            ],
        )

        assert result.archived_text() == {
            "alpha": "execution id: e1 alpha start\nexecution id: e1 alpha end",
            "beta": "execution id: e2 beta start",
        }

    def test_default_case_labels_are_recognised(self):
        text = "\n".join(
            [
                "--- fragment 1 ---",
                "case: alpha step",
                "--- fragment 1 ---",
                "testcase: beta step",
                "--- fragment 2 ---",
                "用例: alpha done",
            ]
        )
        result = split_system_out(
            text,
            [CaseSpec("alpha", (1, 2)), CaseSpec("beta", (1, 2))],
        )

        assert result.feasible
        assert [assignment.case for assignment in result.assignments] == [
            "alpha",
            "beta",
            "alpha",
        ]

    def test_explicit_none_disables_fragment_markers(self):
        text = "\n".join(
            [
                "first line",
                "second line",
                "--- fragment 1 ---",
            ]
        )
        result = split_system_out(text, two_cases(), fragment=None)

        assert [assignment.text for assignment in result.assignments] == [
            "first line",
            "second line",
            "--- fragment 1 ---",
        ]

    def test_fragment_marker_variants(self):
        for marker in ("FRAG 1", "片段 1", "fragment: 1", "--- fragment 1 ---"):
            result = split_system_out(
                f"{marker}\nline\n",
                [CaseSpec("alpha", (1,))],
            )
            assert result.archived_text() == {"alpha": "line"}


class TestStatuses:
    def test_line_status_distinguishes_ambiguous_and_unresolved(self):
        result = split(interleaved_log())
        assert {assignment.status for assignment in result.assignments} == {"ambiguous"}

        text = "=== BEGIN ===\n--- fragment 9 ---\norphan\n=== END ===\n"
        unresolved = split(text)
        assert unresolved.assignments[0].status == "unresolved"
        assert unresolved.assignments[0].candidate_executions == ()

    def test_archived_lines_have_archived_status(self):
        text = "--- fragment 1 ---\ncase: alpha only\n"
        result = split_system_out(
            text,
            [CaseSpec("alpha", (1,))],
        )

        assert result.assignments[0].status == "archived"
        assert result.assignments[0].archived
        assert result.candidates == ()


class TestDefaultPunctuation:
    @pytest.mark.parametrize(
        "suffix",
        ["", ".", ")", "]", ",", ";", ":"],
    )
    def test_execution_id_stops_before_trailing_punctuation(self, suffix):
        text = "\n".join(
            [
                "--- fragment 1 ---",
                f"beta work (execution id: b1){suffix}",
            ]
        )
        result = split_system_out(
            text,
            [CaseSpec("alpha", (1,)), CaseSpec("beta", (1,), execution_id="b1")],
        )

        assert result.assignments[0].case == "beta"
        assert result.assignments[0].candidates == ("beta",)

    def test_case_label_stops_before_trailing_punctuation(self):
        text = "--- fragment 1 ---\ncase: alpha, done\n"
        result = split_system_out(
            text,
            [CaseSpec("alpha", (1,))],
        )

        assert result.assignments[0].case == "alpha"
