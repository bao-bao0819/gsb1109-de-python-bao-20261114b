"""Split a shared suite-level ``system-out`` stream into per-test-case logs.

Stress-test runners commonly write the output of several parallel test cases
into a single ``<system-out>`` element.  Each execution prefixes its output
with a monotonically increasing fragment number, and a few lines additionally
carry an explicit execution ID.

The stream is modelled as a constraint problem:

* a fragment block belongs to exactly one test case;
* the fragment numbers assigned to a case must follow that case's declared
  order;
* explicit case names and execution IDs restrict which case a block may
  belong to.

A line is archived automatically only when every feasible interleaving assigns
it to the same case.  Otherwise the possible cases are reported as candidate
executions.  Lines keep their original one-based source line numbers, and two
occurrences of identical text remain two separate lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Pattern

from .junitparser import JUnitXml, JUnitXmlError, TestSuite

__all__ = [
    "CaseLog",
    "CaseSpec",
    "DEFAULT_CASE",
    "DEFAULT_EXECUTION_ID",
    "DEFAULT_FRAGMENT",
    "DEFAULT_MAX_STATES",
    "LineAssignment",
    "LogLine",
    "LogSplitError",
    "LogSplitter",
    "SplitResult",
    "SystemOutSplitter",
    "split_log",
    "split_system_out",
]

PatternLike = str | Pattern[str]

#: Default search bound.  Exhausting it is reported instead of guessing.
DEFAULT_MAX_STATES = 2_000_000

#: Default fragment marker: ``--- fragment 3 ---``, ``FRAG 3``, ``片段 3``.
DEFAULT_FRAGMENT = (
    r"(?i)^\s*[-=*#>\s]*(?:fragment|frag|片段)\s*[:#]?\s*(?P<fragment>\d+)\b"
)

#: Default execution ID: ``execution id: worker-1``, ``exec_id=worker-1``.
DEFAULT_EXECUTION_ID = (
    r"(?i)\b(?:exec(?:ution)?[ _-]?id|execution|执行\s*id)"
    r"\s*[:=#]\s*(?P<execution_id>[^\s,;()\[\]{}]+)"
)

#: Default explicit case label: ``case: test_login``, ``testcase: test_login``.
DEFAULT_CASE = (
    r"(?i)\b(?:test\s*case|testcase|case|用例)\s*[:=#]\s*"
    r"(?P<case>[^\s,;()\[\]{}]+)"
)


class LogSplitError(JUnitXmlError):
    """Raised when a ``system-out`` stream or its split configuration is invalid."""


@dataclass(frozen=True)
class CaseSpec:
    """Description of one test-case execution in a shared log.

    Args:
        name: Case name, normally :attr:`junitparser.TestCase.name`.
        fragments: Fragment numbers in the order the case wrote them.  Numbers
            must be strictly increasing; gaps are allowed because a fragment
            may have been lost by the runner.
        execution_ids: Execution IDs that identify this case.
        execution_id: Convenience singular form of *execution_ids*.
    """

    name: str
    fragments: tuple[int, ...] = ()
    execution_ids: tuple[str, ...] = ()
    execution_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise LogSplitError("Case name must be a non-empty string.")

        raw_fragments = self.fragments
        if isinstance(raw_fragments, int) and not isinstance(raw_fragments, bool):
            raw_fragments = (raw_fragments,)
        if isinstance(raw_fragments, (str, bytes)):
            raise LogSplitError(f"Fragments for case {self.name!r} must be integers.")
        try:
            fragments = tuple(int(number) for number in raw_fragments)
        except (TypeError, ValueError) as exc:
            raise LogSplitError(
                f"Fragments for case {self.name!r} must be integers."
            ) from exc
        if len(set(fragments)) != len(fragments):
            raise LogSplitError(
                f"Fragments for case {self.name!r} must not contain duplicates."
            )
        if any(left >= right for left, right in zip(fragments, fragments[1:])):
            raise LogSplitError(
                f"Fragments for case {self.name!r} must be strictly increasing."
            )

        raw_execution_ids = self.execution_ids
        if isinstance(raw_execution_ids, str):
            execution_ids = (raw_execution_ids,)
        else:
            execution_ids = tuple(raw_execution_ids)
        if self.execution_id is not None:
            execution_ids = (self.execution_id, *execution_ids)
        execution_ids = tuple(
            dict.fromkeys(str(execution_id) for execution_id in execution_ids)
        )
        if any(not execution_id for execution_id in execution_ids):
            raise LogSplitError("Execution IDs must be non-empty strings.")

        object.__setattr__(self, "fragments", fragments)
        object.__setattr__(self, "execution_ids", execution_ids)


@dataclass(frozen=True)
class LogLine:
    """One physical line of the original ``system-out`` stream.

    Attributes:
        number: One-based line number in the original text.
        text: Line content without the trailing newline.
        role: ``"content"``, ``"start"``, ``"end"``, ``"fragment"``, or
            ``"outside"``.
        fragment: Fragment number carried by a marker line, if any.
        case: Case name carried explicitly by the line, if any.
        execution_id: Execution ID carried explicitly by the line, if any.
    """

    number: int
    text: str
    role: str = "content"
    fragment: int | None = None
    case: str | None = None
    execution_id: str | None = None

    @property
    def lineno(self) -> int:
        """Alias for :attr:`number`."""
        return self.number

    @property
    def line_number(self) -> int:
        """Alias for :attr:`number`."""
        return self.number


@dataclass(frozen=True)
class LineAssignment:
    """Assignment, or candidates, for one content line."""

    line: LogLine
    case: str | None
    candidates: tuple[str, ...] = ()

    @property
    def number(self) -> int:
        """One-based source line number."""
        return self.line.number

    @property
    def lineno(self) -> int:
        """Alias for :attr:`number`."""
        return self.line.number

    @property
    def text(self) -> str:
        """Original line content."""
        return self.line.text

    @property
    def archived(self) -> bool:
        """Whether the line was archived to a single case."""
        return self.case is not None

    @property
    def pending(self) -> bool:
        """Whether the line still needs a decision."""
        return self.case is None

    @property
    def ambiguous(self) -> bool:
        """Whether more than one case remains possible."""
        return self.case is None and len(self.candidates) > 1

    @property
    def unresolved(self) -> bool:
        """Whether no case is compatible with the supplied constraints."""
        return self.case is None and not self.candidates

    @property
    def status(self) -> str:
        """``"archived"``, ``"ambiguous"``, or ``"unresolved"``."""
        if self.case is not None:
            return "archived"
        return "ambiguous" if self.candidates else "unresolved"

    @property
    def candidate_executions(self) -> tuple[str, ...]:
        """Cases still possible for this line."""
        return self.candidates


@dataclass(frozen=True)
class CaseLog:
    """Archived lines of one case, in original order."""

    name: str
    lines: tuple[LogLine, ...] = ()
    execution_ids: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Archived lines joined with newlines, without a trailing newline."""
        return "\n".join(line.text for line in self.lines)

    @property
    def line_numbers(self) -> tuple[int, ...]:
        """Original one-based line numbers, in order."""
        return tuple(line.number for line in self.lines)

    @property
    def source_lines(self) -> tuple[LogLine, ...]:
        """Alias for :attr:`lines` for callers building a source view."""
        return self.lines

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation."""
        return {
            "name": self.name,
            "execution_ids": list(self.execution_ids),
            "lines": [
                {"number": line.number, "text": line.text} for line in self.lines
            ],
        }


@dataclass(frozen=True)
class SplitResult:
    """Result of :func:`split_system_out`.

    Attributes:
        lines: Every physical line of the input, including markers.
        cases: Archived lines for every configured case.
        assignments: Assignments for every content line, in source order.
        feasible: Whether a complete interleaving satisfying all constraints
            exists.
        complete: Whether the bounded search finished.  When ``False`` no line
            is archived automatically.
    """

    lines: tuple[LogLine, ...]
    cases: dict[str, CaseLog]
    assignments: tuple[LineAssignment, ...]
    feasible: bool = True
    complete: bool = True

    @property
    def archived(self) -> dict[str, CaseLog]:
        """Alias for :attr:`cases`."""
        return self.cases

    @property
    def candidates(self) -> tuple[LineAssignment, ...]:
        """Content lines that were not archived, including unresolved ones."""
        return tuple(
            assignment for assignment in self.assignments if not assignment.archived
        )

    @property
    def ambiguous(self) -> tuple[LineAssignment, ...]:
        """Content lines with more than one feasible case."""
        return tuple(
            assignment for assignment in self.assignments if assignment.ambiguous
        )

    @property
    def unresolved(self) -> tuple[LineAssignment, ...]:
        """Content lines for which no case is compatible with the constraints."""
        return tuple(
            assignment for assignment in self.assignments if assignment.unresolved
        )

    @property
    def outside(self) -> tuple[LogLine, ...]:
        """Lines outside the configured start/end region."""
        return tuple(line for line in self.lines if line.role == "outside")

    @property
    def candidate_executions(self) -> dict[int, tuple[str, ...]]:
        """Map original line numbers to candidate cases for pending lines."""
        return {
            assignment.number: assignment.candidates for assignment in self.candidates
        }

    def for_case(self, name: str) -> CaseLog:
        """Return the archived log for *name*.

        Unknown names raise :class:`KeyError`; a configured case without
        archived lines returns an empty :class:`CaseLog`.
        """
        try:
            return self.cases[name]
        except KeyError as exc:
            raise KeyError(f"Unknown case: {name!r}") from exc

    def archived_text(self) -> dict[str, str]:
        """Return ``{case name: archived text}``."""
        return {name: case_log.text for name, case_log in self.cases.items()}

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation for a failure detail page."""
        return {
            "feasible": self.feasible,
            "complete": self.complete,
            "line_count": len(self.lines),
            "cases": {
                name: case_log.to_dict() for name, case_log in self.cases.items()
            },
            "pending": [
                {
                    "number": assignment.number,
                    "text": assignment.text,
                    "status": assignment.status,
                    "candidate_executions": list(assignment.candidates),
                }
                for assignment in self.candidates
            ],
            "outside_lines": [line.number for line in self.outside],
        }


@dataclass
class _Block:
    """A run of content lines that must belong to one case."""

    fragment: int | None = None
    case_hints: set[str] = field(default_factory=set)
    execution_hints: set[str] = field(default_factory=set)
    content: list[LogLine] = field(default_factory=list)

    @property
    def has_payload(self) -> bool:
        return bool(self.content or self.fragment is not None)


def _physical_lines(text: str) -> list[str]:
    """Split *text* into lines without dropping duplicate or empty lines."""
    if not isinstance(text, str):
        raise LogSplitError("system-out must be a string.")
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    return [line[:-1] if line.endswith("\r") else line for line in raw]


def _compile_regex(pattern: PatternLike | None):
    if pattern is None:
        return None
    if isinstance(pattern, re.Pattern):
        return pattern
    if not isinstance(pattern, str):
        raise LogSplitError(f"Expected a string or compiled pattern: {pattern!r}")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise LogSplitError(f"Invalid regular expression {pattern!r}: {exc}") from exc


def _search(pattern, text: str):
    return pattern.search(text) if pattern is not None else None


def _group(match, *names: str) -> str | None:
    if match is None:
        return None
    groups = match.groupdict()
    for name in names:
        value = groups.get(name)
        if value:
            return value
    return None


def _fragment_value(match) -> int | None:
    value = _group(match, "fragment", "frag", "index", "seq")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LogSplitError(f"Fragment number {value!r} is not an integer.") from exc


def _apply_hints(
    block: _Block,
    match,
    *,
    fallback_case: str | None = None,
    fallback_execution: str | None = None,
):
    fragment = _fragment_value(match)
    if fragment is not None:
        block.fragment = fragment
    case_name = _group(match, "case") or fallback_case
    execution_id = _group(match, "execution_id") or fallback_execution
    if case_name:
        block.case_hints.add(case_name)
    if execution_id:
        block.execution_hints.add(execution_id)


def _parse(
    text: str,
    *,
    start_pattern,
    end_pattern,
    fragment_pattern,
    execution_pattern,
    case_pattern,
) -> tuple[list[LogLine], list[_Block]]:
    """Parse *text* into physical lines and fragment blocks."""
    raw_lines = _physical_lines(text)
    lines: list[LogLine] = []
    blocks: list[_Block] = []

    current: _Block | None = None
    # Hints declared by a start marker apply to every fragment in its region.
    region_cases: set[str] = set()
    region_executions: set[str] = set()
    # Without a start marker, a log that has fragment markers starts at the
    # first one; a text without any fragment marker is one free block.
    has_fragments = fragment_pattern is not None and any(
        _search(fragment_pattern, raw) is not None for raw in raw_lines
    )
    region_active = start_pattern is None and not has_fragments
    if region_active:
        current = _Block()

    def discard(block: _Block | None):
        if block is not None and block.has_payload:
            blocks.append(block)

    def close():
        nonlocal current
        discard(current)
        current = None

    def begin(fragment: int | None = None) -> _Block:
        nonlocal current
        current = _Block(fragment=fragment)
        current.case_hints.update(region_cases)
        current.execution_hints.update(region_executions)
        return current

    def hint_conflict(execution_id: str | None, case_name: str | None) -> bool:
        if current is None or not current.content:
            return False
        if (
            execution_id is not None
            and current.execution_hints
            and execution_id not in current.execution_hints
        ):
            return True
        return (
            case_name is not None
            and current.case_hints
            and case_name not in current.case_hints
        )

    for number, raw in enumerate(raw_lines, start=1):
        start_match = _search(start_pattern, raw)
        end_match = _search(end_pattern, raw)
        fragment_match = _search(fragment_pattern, raw)
        execution_match = _search(execution_pattern, raw)
        case_match = _search(case_pattern, raw)
        execution_id = _group(execution_match, "execution_id")
        case_name = _group(case_match, "case")

        role = "outside"
        fragment = None

        if start_match is not None:
            close()
            # A new region must not inherit the previous region's case hints.
            region_cases.clear()
            region_executions.clear()
            begin()
            region_active = True
            role = "start"
            _apply_hints(
                current,
                start_match,
                fallback_case=case_name,
                fallback_execution=execution_id,
            )
            if fragment_match is not None:
                _apply_hints(
                    current,
                    fragment_match,
                    fallback_case=case_name,
                    fallback_execution=execution_id,
                )
            # A start marker that names a case opens a per-case region, and its
            # hints apply to every fragment in that region.  A bare delimiter
            # such as ``BEGIN`` (possibly carrying an execution ID for the
            # marker line only) must not pin the whole region.
            marker_case = _group(start_match, "case")
            if marker_case:
                region_cases.update(current.case_hints)
                region_executions.update(current.execution_hints)
            fragment = current.fragment
        elif end_match is not None:
            close()
            region_active = False
            region_cases.clear()
            region_executions.clear()
            role = "end"
        elif fragment_match is not None and (region_active or start_pattern is None):
            fragment = _fragment_value(fragment_match)
            if current is not None and current.has_payload:
                close()
            if current is None:
                begin()
            elif current.fragment is not None:
                # Two consecutive fragment markers without content in between:
                # keep them as separate blocks so ordering remains checkable.
                close()
                begin()
            current.fragment = fragment
            _apply_hints(
                current,
                fragment_match,
                fallback_case=case_name,
                fallback_execution=execution_id,
            )
            region_active = True
            role = "fragment"
        elif region_active:
            if hint_conflict(execution_id, case_name):
                inherited = current.fragment if current is not None else None
                close()
                begin(inherited)
            if current is None:
                begin()
            role = "content"
            current.content.append(
                LogLine(
                    number=number,
                    text=raw,
                    role="content",
                    case=case_name,
                    execution_id=execution_id,
                )
            )
            if case_name:
                current.case_hints.add(case_name)
            if execution_id:
                current.execution_hints.add(execution_id)
        else:
            close()
            role = "outside"

        lines.append(
            LogLine(
                number=number,
                text=raw,
                role=role,
                fragment=fragment,
                case=case_name,
                execution_id=execution_id,
            )
        )

    close()
    return lines, [block for block in blocks if block.has_payload]


def _spec_from_value(name: str, value) -> CaseSpec:
    if isinstance(value, CaseSpec):
        if value.name == name:
            return value
        return CaseSpec(name, value.fragments, value.execution_ids)
    if isinstance(value, Mapping):
        return CaseSpec(
            name,
            value.get("fragments", ()),
            value.get("execution_ids", ()),
            value.get("execution_id"),
        )
    if isinstance(value, int) and not isinstance(value, bool):
        return CaseSpec(name, (value,))
    try:
        return CaseSpec(name, tuple(value))
    except TypeError as exc:
        raise LogSplitError(f"Invalid specification for case {name!r}.") from exc


def _normalise_cases(
    cases: Mapping[str, object] | Iterable[CaseSpec | object],
) -> tuple[list[CaseSpec], dict[str, CaseSpec]]:
    specs: list[CaseSpec] = []
    if isinstance(cases, Mapping):
        specs = [_spec_from_value(str(name), value) for name, value in cases.items()]
    else:
        for value in cases:
            if isinstance(value, CaseSpec):
                specs.append(value)
            elif isinstance(value, Mapping):
                if "name" not in value:
                    raise LogSplitError("A case mapping requires a 'name'.")
                specs.append(_spec_from_value(str(value["name"]), value))
            elif hasattr(value, "name"):
                specs.append(
                    _spec_from_value(
                        str(value.name),
                        {
                            "fragments": getattr(value, "fragments", ()),
                            "execution_ids": getattr(value, "execution_ids", ()),
                        },
                    )
                )
            else:
                raise LogSplitError(
                    "Cases must be CaseSpec objects or a mapping of names."
                )

    if not specs:
        raise LogSplitError("At least one case is required.")

    by_name: dict[str, CaseSpec] = {}
    for spec in specs:
        if spec.name in by_name:
            raise LogSplitError(f"Duplicate case name: {spec.name!r}")
        by_name[spec.name] = spec
    return specs, by_name


def _normalise_executions(
    executions: Mapping[str, str | Iterable[str]] | None,
    specs: Iterable[CaseSpec] = (),
) -> dict[str, set[str]]:
    specs = tuple(specs)
    known = {spec.name for spec in specs}
    mapping: dict[str, set[str]] = {
        execution_id: {spec.name}
        for spec in specs
        for execution_id in spec.execution_ids
    }
    if not executions:
        return mapping
    for execution_id, value in executions.items():
        names = (value,) if isinstance(value, str) else tuple(value)
        unknown = {str(name) for name in names} - known
        if unknown:
            raise LogSplitError(
                f"Execution {execution_id!r} refers to unknown case(s): "
                + ", ".join(sorted(unknown))
            )
        mapping.setdefault(str(execution_id), set()).update(str(name) for name in names)
    return mapping


def _case_domains(
    blocks: list[_Block],
    specs: list[CaseSpec],
    by_name: Mapping[str, CaseSpec],
    executions: Mapping[str, set[str]],
) -> list[tuple[str, ...]]:
    names = tuple(spec.name for spec in specs)
    fragments_by_case = {spec.name: set(spec.fragments) for spec in specs}

    domains: list[tuple[str, ...]] = []
    for block in blocks:
        if block.fragment is None:
            domain = set(names)
        else:
            domain = {
                name for name in names if block.fragment in fragments_by_case[name]
            }

        for hint in block.case_hints:
            if hint not in by_name:
                domain.clear()
            else:
                domain &= {hint}

        for execution_id in block.execution_hints:
            allowed = executions.get(execution_id)
            if allowed is None:
                allowed = {execution_id} & set(names)
            domain &= set(allowed)

        domains.append(tuple(name for name in names if name in domain))
    return domains


class _Budget:
    def __init__(self, states: int | None):
        self.remaining = states

    def spend(self):
        if self.remaining is None:
            return
        self.remaining -= 1
        if self.remaining < 0:
            raise _SearchLimit


class _SearchLimit(Exception):
    pass


def _possible_cases(
    blocks: list[_Block],
    domains: list[tuple[str, ...]],
    specs: list[CaseSpec],
    budget: _Budget,
) -> tuple[list[tuple[str, ...]], bool, bool]:
    """Return possible cases per block, feasibility, and search completeness.

    Only fragment-bearing blocks constrain the order.  A block without a
    fragment number can join any allowed case without changing the sequence of
    fragment numbers seen by that case, so such blocks never need the search.

    A block with an empty domain cannot be assigned at all.  It is reported
    with no candidates and makes the whole split infeasible, but the remaining
    blocks are still analysed so callers keep their candidate executions.

    Cases that have the same fragment sequence and belong to exactly the same
    blocks' domains are interchangeable.  They are searched as one class whose
    state is the multiset of fragment progress values, which keeps symmetric
    stress reports (many identical parallel workers) tractable.
    """
    spec_by_name = {spec.name: spec for spec in specs}

    # Group interchangeable cases.  Two cases are interchangeable when every
    # block either accepts both or neither, and both declare the same fragments.
    classes_by_key: dict[tuple, list[str]] = {}
    for spec in specs:
        membership = tuple(
            spec.name in domains[block_index] for block_index in range(len(blocks))
        )
        key = (spec.fragments, membership)
        classes_by_key.setdefault(key, []).append(spec.name)

    classes = list(classes_by_key.values())
    class_of = {
        name: class_index
        for class_index, members in enumerate(classes)
        for name in members
    }
    class_fragments = []
    class_accepts = []
    for members in classes:
        fragment_ranks = {
            fragment: rank
            for rank, fragment in enumerate(spec_by_name[members[0]].fragments)
        }
        class_fragments.append(fragment_ranks)
        # Every member has the same domain membership, so one check per class
        # is enough.  Empty domains exclude a block from the search entirely.
        class_accepts.append(
            [members[0] in domains[block_index] for block_index in range(len(blocks))]
        )

    variable_indices = [
        index
        for index, block in enumerate(blocks)
        if block.fragment is not None and domains[index]
    ]
    feasible = all(domains)
    if not variable_indices:
        return [domain for domain in domains], feasible, True

    class_count = len(classes)

    def initial_state() -> tuple[tuple[int, ...], ...]:
        return tuple(tuple(-1 for _ in members) for members in classes)

    def updated_state(state, class_index: int, entry: int, rank: int):
        progress = list(state[class_index])
        progress[entry] = rank
        progress.sort()
        updated = list(state)
        updated[class_index] = tuple(progress)
        return tuple(updated)

    def search(fixed_position: int | None, fixed_class: int | None):
        """Return ``(feasible, complete)`` for an optional fixed class.

        The depth-first search uses an explicit stack because a shared log may
        contain far more blocks than Python's recursion limit.
        """
        failed: set[tuple] = set()

        def candidates(order: int, state):
            block_index = variable_indices[order]
            fragment = blocks[block_index].fragment
            class_indices = (
                (fixed_class,) if order == fixed_position else range(class_count)
            )
            for class_index in class_indices:
                if not class_accepts[class_index][block_index]:
                    continue
                fragment_ranks = class_fragments[class_index]
                if fragment not in fragment_ranks:
                    continue
                rank = fragment_ranks[fragment]
                progress = state[class_index]
                tried: set[int] = set()
                # Branch over distinct progress values to avoid exploring
                # label permutations of interchangeable cases.
                for entry, last_rank in enumerate(progress):
                    if last_rank >= rank or last_rank in tried:
                        continue
                    tried.add(last_rank)
                    yield class_index, entry, rank

        start = (0, initial_state())
        stack = [(start, candidates(start[0], start[1]))]
        try:
            while stack:
                (order, state), pending = stack[-1]
                budget.spend()
                if order == len(variable_indices):
                    return True, True
                if (order, state) in failed:
                    stack.pop()
                    continue
                advanced = False
                for class_index, entry, rank in pending:
                    next_state = updated_state(state, class_index, entry, rank)
                    next_order = order + 1
                    if (next_order, next_state) in failed:
                        continue
                    stack.append(
                        (
                            (next_order, next_state),
                            candidates(next_order, next_state),
                        )
                    )
                    advanced = True
                    break
                if not advanced:
                    failed.add((order, state))
                    stack.pop()
            return False, True
        except _SearchLimit:
            return True, False

    order_feasible, complete = search(None, None)
    feasible = feasible and order_feasible
    if not complete:
        return [domain for domain in domains], feasible, False
    if not feasible:
        # No assignment satisfies all constraints.  Report the locally allowed
        # cases instead of pretending the order search still narrows them.
        return [domain for domain in domains], False, True

    possible: list[tuple[str, ...]] = [domain for domain in domains]
    for order, block_index in enumerate(variable_indices):
        possible_classes: set[int] = set()
        for class_index in range(class_count):
            if not class_accepts[class_index][block_index]:
                continue
            class_feasible, class_complete = search(order, class_index)
            if not class_complete:
                return [domain for domain in domains], feasible, False
            if class_feasible:
                possible_classes.add(class_index)
        possible[block_index] = tuple(
            name for name in domains[block_index] if class_of[name] in possible_classes
        )
    return possible, feasible, True


def _source_text(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, bytes):
        raise LogSplitError("system-out must be a string, not bytes.")
    if not isinstance(source, (TestSuite, JUnitXml)):
        raise LogSplitError(
            "source must be a system-out string, a TestSuite, or a JUnitXml."
        )

    if isinstance(source, TestSuite):
        text = source.system_out
        if text is None:
            raise LogSplitError("The test suite has no system-out text.")
        return text

    suites = list(source)
    if len(suites) > 1:
        raise LogSplitError(
            "The report contains multiple test suites; pass the suite whose "
            "system-out should be split."
        )
    if not suites:
        raise LogSplitError("The report contains no test suites.")
    text = suites[0].system_out
    if text is None:
        raise LogSplitError("The test suite has no system-out text.")
    return text


def split_system_out(
    source: str | object,
    cases: Mapping[str, object] | Iterable[CaseSpec | object],
    *,
    start: PatternLike | None = None,
    end: PatternLike | None = None,
    fragment: PatternLike | None = DEFAULT_FRAGMENT,
    execution_id: PatternLike | None = DEFAULT_EXECUTION_ID,
    case: PatternLike | None = DEFAULT_CASE,
    executions: Mapping[str, str | Iterable[str]] | None = None,
    max_states: int | None = None,
) -> SplitResult:
    """Split a monolithic ``system-out`` stream into per-case logs.

    Args:
        source: The text of a suite-level ``<system-out>`` element, or an
            object such as :class:`junitparser.TestSuite` providing
            ``system_out``.
        cases: Case specifications: a mapping of case name to fragment numbers
            / :class:`CaseSpec` / mapping, or an iterable of :class:`CaseSpec`.
        start: Regular expression starting a block.  It may define the
            ``case``, ``execution_id``, and ``fragment`` groups.  When it names
            a case, its hints apply to every fragment in that region.
        end: Regular expression ending a block.
        fragment: Regular expression starting a fragment block.  It must expose
            a ``fragment`` group (``frag``, ``index``, and ``seq`` are also
            accepted).  Defaults to :data:`DEFAULT_FRAGMENT`; pass ``None`` to
            treat the text as one block without fragment markers.
        execution_id: Regular expression locating an execution ID on any line,
            using the ``execution_id`` group.  Defaults to
            :data:`DEFAULT_EXECUTION_ID`.
        case: Regular expression locating an explicit case name on any line,
            using the ``case`` group.  Defaults to :data:`DEFAULT_CASE`.
        executions: Optional execution ID to case name mapping.  Values may be
            one case name or an iterable of names.
        max_states: Optional bound on the constraint search.  If it is
            exhausted, the result has ``complete=False`` and no line is
            auto-archived.  Defaults to :data:`DEFAULT_MAX_STATES`; symmetric
            cases are collapsed into equivalence classes and failed states are
            memoised, so realistic stress reports stay tractable.

    Returns:
        A :class:`SplitResult`.

    Notes:
        All marker arguments are regular expressions; escape literal text with
        :func:`re.escape`.  A case's fragments must be strictly increasing, but
        need not be contiguous.  Fragment numbers that no case claims leave
        their block without a candidate.  Every content line is a distinct
        object, so duplicate text is never merged and original line numbers are
        kept.
    """
    if max_states is not None and max_states <= 0:
        raise LogSplitError("max_states must be a positive integer.")

    text = _source_text(source)
    specs, by_name = _normalise_cases(cases)
    execution_map = _normalise_executions(executions, specs)

    lines, blocks = _parse(
        text,
        start_pattern=_compile_regex(start),
        end_pattern=_compile_regex(end),
        fragment_pattern=_compile_regex(fragment),
        execution_pattern=_compile_regex(execution_id),
        case_pattern=_compile_regex(case),
    )
    domains = _case_domains(blocks, specs, by_name, execution_map)
    possible, feasible, complete = _possible_cases(
        blocks,
        domains,
        specs,
        _Budget(DEFAULT_MAX_STATES if max_states is None else max_states),
    )

    archived: dict[str, list[LogLine]] = {spec.name: [] for spec in specs}
    assignments: list[LineAssignment] = []
    for block, candidates in zip(blocks, possible):
        case_name = (
            candidates[0] if complete and feasible and len(candidates) == 1 else None
        )
        for line in block.content:
            assignments.append(
                LineAssignment(
                    line=line,
                    case=case_name,
                    candidates=tuple(candidates),
                )
            )
            if case_name is not None:
                archived[case_name].append(line)

    assignments.sort(key=lambda assignment: assignment.number)
    case_logs = {
        spec.name: CaseLog(
            name=spec.name,
            lines=tuple(archived[spec.name]),
            execution_ids=spec.execution_ids,
        )
        for spec in specs
    }
    return SplitResult(
        lines=tuple(lines),
        cases=case_logs,
        assignments=tuple(assignments),
        feasible=feasible,
        complete=complete,
    )


split_log = split_system_out


class SystemOutSplitter:
    """Reusable splitter with a fixed log layout.

    Arguments are the same as :func:`split_system_out` except *source*.
    """

    def __init__(
        self,
        cases: Mapping[str, object] | Iterable[CaseSpec | object],
        *,
        start: PatternLike | None = None,
        end: PatternLike | None = None,
        fragment: PatternLike | None = DEFAULT_FRAGMENT,
        execution_id: PatternLike | None = DEFAULT_EXECUTION_ID,
        case: PatternLike | None = DEFAULT_CASE,
        executions: Mapping[str, str | Iterable[str]] | None = None,
        max_states: int | None = None,
    ):
        self.cases = cases
        self.start = start
        self.end = end
        self.fragment = fragment
        self.execution_id = execution_id
        self.case = case
        self.executions = executions
        self.max_states = max_states

    def split(self, source: str | object) -> SplitResult:
        """Split *source* using this splitter's configuration."""
        return split_system_out(
            source,
            self.cases,
            start=self.start,
            end=self.end,
            fragment=self.fragment,
            execution_id=self.execution_id,
            case=self.case,
            executions=self.executions,
            max_states=self.max_states,
        )


LogSplitter = SystemOutSplitter
