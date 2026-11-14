# -----------------------------------------------------------------
# pycparser: sampling_analysis.py
#
# AST-based reachability analysis for sampling/write-back pipelines.
#
# Typical target: a C program has exactly one function that actually
# writes rows to a database, and a stack of thin wrappers forwards or
# permutes (channel, field, scale) arguments on the way there.  This
# module interprets the pycparser AST (not just a call graph) and
# reports which concrete combinations arrive at the write point.
#
# Design notes
# ------------
# * Interpretation is path-sensitive.  Branches are executed with
#   separate environments, so values selected by the same branch keep
#   their association (a temperature channel is never paired with a
#   humidity scale merely because both are "possible").
# * Calls are inlined with per-call parameter bindings, so a wrapper
#   that swaps arguments is reported with the swapped values.
# * Recursive calls and calls to functions without an AST definition
#   truncate only that path.  Sibling branches and other entry points
#   keep producing results, and every truncation is reported.
# * Loops are unrolled up to a bound; timers, I/O callbacks and
#   function-pointer calls are not guessed.
#
# Each reported tuple carries a call path from the entry point down to
# the write call, with a source coordinate for every frame, plus the
# expression that produced each reported value.
# -----------------------------------------------------------------
"""Path-sensitive reachability and argument-correlation for C sampling code."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
import re
from typing import Any, Iterator, Sequence

from . import c_ast


__all__ = [
    "UNKNOWN",
    "Endpoint",
    "EntryPoint",
    "Frame",
    "Reachability",
    "ReachabilityAnalyzer",
    "Tracked",
    "Truncation",
    "ValueRange",
    "analyze_file",
]


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


class _Unknown:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class StructValue:
    """A struct/union value with tracked members."""

    members: tuple[tuple[str, "Tracked"], ...]

    def get(self, name: str) -> "Tracked | None":
        for member_name, value in self.members:
            if member_name == name:
                return value
        return None

    def with_member(self, name: str, value: "Tracked") -> "StructValue":
        members = [(n, v) for n, v in self.members if n != name]
        members.append((name, value))
        return StructValue(tuple(members))


@dataclass(frozen=True)
class ArrayValue:
    """An array value with tracked elements."""

    items: tuple["Tracked", ...]


@dataclass(frozen=True)
class Tracked:
    """A value together with the expression and location that produced it."""

    value: Any
    source: str
    coord: str | None = None

    def is_known(self) -> bool:
        return self.value is not UNKNOWN

    def render(self) -> str:
        return render_tracked(self)


Value = int | float | str | StructValue | ArrayValue | _Unknown


def render_value(value: Value) -> str:
    if value is UNKNOWN:
        return "?"
    if isinstance(value, StructValue):
        return "{" + ", ".join(f".{n}={v.render()}" for n, v in value.members) + "}"
    if isinstance(value, ArrayValue):
        return "[" + ", ".join(item.render() for item in value.items) + "]"
    if isinstance(value, str):
        return value
    return str(value)


def render_tracked(tracked: Tracked) -> str:
    if tracked.value is UNKNOWN:
        return "?" if tracked.source in {"", "?"} else f"?({tracked.source})"
    if isinstance(tracked.value, (StructValue, ArrayValue)):
        return render_value(tracked.value)
    if tracked.source and tracked.source != str(tracked.value):
        return f"{render_value(tracked.value)}({tracked.source})"
    return render_value(tracked.value)


# ---------------------------------------------------------------------------
# Public configuration and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueRange:
    """Candidate values for one entry-point parameter.

    Values may be numbers or symbolic labels; labels keep their spelling
    in the report.  Use this to describe a constant range instead of
    constructing one ``EntryPoint`` per combination.
    """

    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("ValueRange needs at least one candidate")


@dataclass(frozen=True)
class Endpoint:
    """The single function that writes to the database.

    ``parameters`` names the write payload fields of interest.  Positions
    are resolved from the function declaration by name.  If the
    declaration has unnamed parameters, pass ``positions`` explicitly.
    """

    function: str
    parameters: tuple[str, ...] = ("channel", "field", "scale")
    positions: tuple[int, ...] | None = None


@dataclass(frozen=True)
class EntryPoint:
    """A sampling entry point and its possible argument values.

    Each positional item is either a plain value or a :class:`ValueRange`.
    Ranges are expanded as a Cartesian product.  Arguments that are not
    supplied stay symbolic and may truncate paths that need them.
    """

    function: str
    arguments: tuple[Any, ...] = ()
    coord: str | None = None


@dataclass(frozen=True)
class Frame:
    """One frame of a call path."""

    function: str
    coord: str | None
    arguments: tuple[Tracked, ...]

    def render(self) -> str:
        args = ", ".join(argument.render() for argument in self.arguments)
        return f"{self.function}({args})@{self.coord or '?'}"


@dataclass(frozen=True)
class Reachability:
    """One concrete payload combination that reaches the write point."""

    values: tuple[Tracked, ...]
    path: tuple[Frame, ...]
    write_coord: str | None
    entry: str
    payload_names: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "values": {
                name: {
                    "value": _jsonable(tracked.value),
                    "source": tracked.source,
                    "origin": tracked.coord,
                }
                for name, tracked in zip(self.payload_names, self.values)
            },
            "call_path": [
                {
                    "function": frame.function,
                    "coord": frame.coord,
                    "arguments": [
                        {
                            "value": _jsonable(argument.value),
                            "source": argument.source,
                            "origin": argument.coord,
                        }
                        for argument in frame.arguments
                    ],
                }
                for frame in self.path
            ],
            "write_coord": self.write_coord,
        }

    def render(self) -> str:
        values = ", ".join(
            f"{name}={tracked.render()}"
            for name, tracked in zip(self.payload_names, self.values)
        )
        calls = " -> ".join(frame.render() for frame in self.path)
        return f"{values}  [{calls}]"

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(tracked.value for tracked in self.values)


@dataclass(frozen=True)
class Truncation:
    """A path that was deliberately not followed further."""

    reason: str
    function: str
    coord: str | None
    detail: str
    call_path: tuple[str, ...] = ()

    def render(self) -> str:
        path = " -> ".join(self.call_path) if self.call_path else "<entry>"
        return f"{self.reason}: {self.detail} at {self.coord or '?'} [{path}]"


# ---------------------------------------------------------------------------
# Interpretation state
# ---------------------------------------------------------------------------


@dataclass
class _State:
    env: dict[str, Tracked]
    stack: tuple[Frame, ...]
    globals: dict[str, Tracked] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(dict(self.env), self.stack, dict(self.globals))

    def with_frame(self, frame: Frame) -> "_State":
        return _State(dict(self.env), self.stack + (frame,), dict(self.globals))


@dataclass
class _Flow:
    """Result of executing a statement for one state.

    ``live`` continue after the statement; ``returns`` leave the current
    function; ``breaks``/``continues`` leave the innermost loop (or switch).
    """

    live: list[_State] = field(default_factory=list)
    returns: list[tuple[_State, Tracked]] = field(default_factory=list)
    breaks: list[_State] = field(default_factory=list)
    continues: list[_State] = field(default_factory=list)

    def merge(self, other: "_Flow") -> "_Flow":
        self.live.extend(other.live)
        self.returns.extend(other.returns)
        self.breaks.extend(other.breaks)
        self.continues.extend(other.continues)
        return self


@dataclass(frozen=True)
class _ExprOutcome:
    state: _State
    value: Tracked


class _Stop(Exception):
    """Raised when the current path must be abandoned.

    The analyzer records the reason and drops only the state on which
    this was raised.
    """

    def __init__(self, reason: str, detail: str, node: c_ast.Node | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.node = node


def _format_coord(node: c_ast.Node | None) -> str | None:
    if node is None or node.coord is None:
        return None
    return str(node.coord)


def _jsonable(value: Any) -> Any:
    if value is UNKNOWN:
        return None
    if isinstance(value, StructValue):
        return {name: _jsonable(member.value) for name, member in value.members}
    if isinstance(value, ArrayValue):
        return [_jsonable(item.value) for item in value.items]
    return value


# ---------------------------------------------------------------------------
# Constant helpers
# ---------------------------------------------------------------------------


_INT_TYPES = {
    "char",
    "signed char",
    "unsigned char",
    "short",
    "short int",
    "unsigned short",
    "unsigned short int",
    "int",
    "unsigned",
    "unsigned int",
    "long",
    "long int",
    "unsigned long",
    "unsigned long int",
    "long long",
    "unsigned long long",
    "_Bool",
    "bool",
}

_FLOAT_TYPES = {"float", "double", "long double", "_Float16", "__fp16"}

_TYPE_SIZES = {
    "char": 1,
    "signed char": 1,
    "unsigned char": 1,
    "_Bool": 1,
    "bool": 1,
    "short": 2,
    "short int": 2,
    "unsigned short": 2,
    "int": 4,
    "unsigned": 4,
    "unsigned int": 4,
    "long": 8,
    "long int": 8,
    "unsigned long": 8,
    "long long": 8,
    "unsigned long long": 8,
    "float": 4,
    "double": 8,
    "long double": 16,
}


def _parse_constant(type_name: str, text: str) -> Any:
    if type_name == "string":
        pieces: list[str] = []
        rest = text
        while rest:
            rest = rest.lstrip()
            if not rest:
                break
            if rest[0] not in "'\"":
                pieces.append(rest)
                break
            quote = rest[0]
            end = 1
            while end < len(rest):
                if rest[end] == "\\":
                    end += 2
                    continue
                if rest[end] == quote:
                    break
                end += 1
            pieces.append(rest[1:end])
            rest = rest[end + 1 :]
        return "".join(pieces)
    if type_name == "char":
        # Character constants are commonly used as small integers.
        cleaned = text.replace("'", "")
        try:
            return int(cleaned, 0)
        except ValueError:
            pass
        if len(cleaned) == 1:
            return ord(cleaned)
        return UNKNOWN
    cleaned = text.replace("'", "").rstrip("uUlL")
    try:
        if type_name in _INT_TYPES:
            return int(cleaned, 0)
        if type_name in _FLOAT_TYPES:
            return float(cleaned.rstrip("fFdD"))
    except ValueError:
        return UNKNOWN
    try:
        return int(cleaned, 0)
    except ValueError:
        try:
            return float(cleaned.rstrip("fF"))
        except ValueError:
            return UNKNOWN


def _binary_value(op: str, left: Any, right: Any) -> Any:
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if isinstance(left, (StructValue, ArrayValue)) or isinstance(
        right, (StructValue, ArrayValue)
    ):
        return UNKNOWN
    try:
        if op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return UNKNOWN
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if right == 0:
                return UNKNOWN
            if isinstance(left, int) and isinstance(right, int):
                return int(left / right)
            return left / right
        if op == "%":
            if not isinstance(left, int) or not isinstance(right, int) or right == 0:
                return UNKNOWN
            return left % right
        if op == "<<":
            return (
                left << right
                if isinstance(left, int) and isinstance(right, int)
                else UNKNOWN
            )
        if op == ">>":
            return (
                left >> right
                if isinstance(left, int) and isinstance(right, int)
                else UNKNOWN
            )
        if op == "&":
            return (
                left & right
                if isinstance(left, int) and isinstance(right, int)
                else UNKNOWN
            )
        if op == "|":
            return (
                left | right
                if isinstance(left, int) and isinstance(right, int)
                else UNKNOWN
            )
        if op == "^":
            return (
                left ^ right
                if isinstance(left, int) and isinstance(right, int)
                else UNKNOWN
            )
        if op == "==":
            return int(left == right)
        if op == "!=":
            return int(left != right)
        if op == "<":
            return int(left < right)
        if op == "<=":
            return int(left <= right)
        if op == ">":
            return int(left > right)
        if op == ">=":
            return int(left >= right)
    except TypeError:
        return UNKNOWN
    return UNKNOWN


def _strip_type(node: c_ast.Node | None) -> c_ast.Node | None:
    """Remove Decl/Typename/TypeDecl wrappers from a type node."""
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, (c_ast.Decl, c_ast.Typename, c_ast.TypeDecl)):
            node = node.type
        else:
            break
    return node


def _funcdecl_of(decl: c_ast.Decl) -> c_ast.FuncDecl | None:
    return decl.type if isinstance(decl.type, c_ast.FuncDecl) else None


def _parameter_names(func: c_ast.FuncDecl) -> tuple[str | None, ...]:
    if func.args is None:
        return ()
    names: list[str | None] = []
    for param in func.args.params:
        if isinstance(param, c_ast.Decl):
            names.append(param.name)
        elif isinstance(param, c_ast.EllipsisParam):
            names.append(None)
        else:
            names.append(None)
    return tuple(names)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class ReachabilityAnalyzer:
    """Interpret a translation unit and enumerate writes reachable from entries.

    Parameters
    ----------
    ast:
        Parsed ``c_ast.FileAST``.
    endpoint:
        The function that performs the database write.
    max_depth:
        Maximum number of nested inlined calls.  Beyond it a path is
        truncated and reported.
    max_loop_iterations:
        Unroll bound for loops.
    max_entry_combinations:
        Safety cap for the Cartesian expansion of entry ranges.
    max_results:
        Safety cap for the number of reported payload combinations.
    """

    def __init__(
        self,
        ast: c_ast.FileAST,
        endpoint: Endpoint,
        *,
        max_depth: int = 64,
        max_loop_iterations: int = 16,
        max_entry_combinations: int = 4096,
        max_results: int = 4096,
    ):
        self.ast = ast
        self.endpoint = endpoint
        self.max_depth = max_depth
        self.max_loop_iterations = max_loop_iterations
        self.max_entry_combinations = max_entry_combinations
        self.max_results = max_results

        self._functions: dict[str, c_ast.FuncDef] = {}
        self._function_decls: dict[str, c_ast.Decl] = {}
        self._globals: dict[str, Tracked] = {}
        self._enums: dict[str, Tracked] = {}
        self._typedefs: dict[str, c_ast.Node] = {}
        # Tag -> member list, so a local declared with `struct sample s;`
        # can be initialized without the definition being inlined in the
        # declaration's type node.
        self._struct_defs: dict[str, c_ast.Struct] = {}
        self._union_defs: dict[str, c_ast.Union] = {}

        self._results: list[Reachability] = []
        self._truncations: list[Truncation] = []
        self._result_limit_reported = False

        self._index(self.ast)
        self._payload_positions = self._resolve_payload_positions()

    # -- indexing ---------------------------------------------------------

    def _index(self, node: c_ast.Node) -> None:
        if isinstance(node, c_ast.FileAST):
            for ext in node.ext:
                self._index(ext)
        elif isinstance(node, c_ast.FuncDef):
            func = _funcdecl_of(node.decl)
            name = node.decl.name
            if name is not None:
                self._functions[name] = node
                self._function_decls[name] = node.decl
            del func
        elif isinstance(node, c_ast.Decl):
            self._index_decl_type(node.type)
            if isinstance(node.type, c_ast.FuncDecl):
                if node.name is not None:
                    self._function_decls.setdefault(node.name, node)
            elif node.name is not None and node.init is not None:
                if node.name not in self._globals:
                    self._globals[node.name] = self._static_aggregate(
                        node.init, node.type
                    )
            elif node.name is None:
                self._index_decl_type(node.type)
        elif isinstance(node, c_ast.Typedef):
            self._index_decl_type(node.type)
            self._typedefs[node.name] = node.type
        elif isinstance(node, c_ast.DeclList):
            for decl in node.decls:
                self._index(decl)
        elif isinstance(node, c_ast.Struct):
            if node.name is not None and node.decls is not None:
                self._struct_defs.setdefault(node.name, node)
        elif isinstance(node, c_ast.Union):
            if node.name is not None and node.decls is not None:
                self._union_defs.setdefault(node.name, node)
        elif isinstance(node, c_ast.Enum):
            self._index_enum(node)
        # Other top-level nodes (pragmas, static asserts) carry no functions.

    def _index_decl_type(self, node: c_ast.Node | None) -> None:
        """Index enum/struct declarations nested in a type specifier."""
        seen: set[int] = set()
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            if isinstance(node, c_ast.Enum):
                self._index_enum(node)
                return
            if isinstance(node, (c_ast.Struct, c_ast.Union)):
                if node.name is not None and node.decls is not None:
                    if isinstance(node, c_ast.Struct):
                        self._struct_defs.setdefault(node.name, node)
                    else:
                        self._union_defs.setdefault(node.name, node)
                for decl in node.decls or []:
                    if isinstance(decl, c_ast.Decl):
                        self._index_decl_type(decl.type)
                return
            if isinstance(node, c_ast.ArrayDecl):
                node = node.type
            elif isinstance(node, c_ast.PtrDecl):
                node = node.type
            elif isinstance(node, (c_ast.TypeDecl, c_ast.Typename, c_ast.Decl)):
                node = node.type
            elif isinstance(node, c_ast.TypeOf):
                return
            elif isinstance(node, c_ast.FuncDecl):
                node = node.type
            else:
                return

    def _index_enum(self, node: c_ast.Enum) -> None:
        if node.values is None:
            return
        previous: int | None = -1
        for enumerator in node.values.enumerators:
            if not isinstance(enumerator, c_ast.Enumerator):
                continue
            if enumerator.value is None:
                value = 0 if previous is None else previous + 1
            else:
                tracked = self._static_tracked(enumerator.value)
                value = tracked.value
                if not isinstance(value, int):
                    value = UNKNOWN
            if enumerator.name not in self._enums:
                self._enums[enumerator.name] = Tracked(
                    value, enumerator.name, _format_coord(enumerator)
                )
            previous = value if isinstance(value, int) else None

    def _static_tracked(self, node: c_ast.Node | None) -> Tracked:
        """Evaluate an initializer without an execution state.

        Only constants, enum names, previously indexed globals and simple
        expressions are supported.  Anything else becomes UNKNOWN rather
        than a guessed value.
        """
        if node is None:
            return Tracked(UNKNOWN, "?", None)
        if isinstance(node, c_ast.Constant):
            return Tracked(
                _parse_constant(node.type, node.value),
                node.value,
                _format_coord(node),
            )
        if isinstance(node, c_ast.ID):
            if node.name in self._enums:
                return self._enums[node.name]
            if node.name in self._globals:
                return self._globals[node.name]
            return Tracked(UNKNOWN, node.name, _format_coord(node))
        if isinstance(node, c_ast.UnaryOp) and node.op in {"-", "+", "~", "!"}:
            inner = self._static_tracked(node.expr)
            if not inner.is_known() or not isinstance(inner.value, (int, float)):
                return Tracked(UNKNOWN, node.op, _format_coord(node))
            if node.op == "-":
                value = -inner.value
            elif node.op == "+":
                value = +inner.value
            elif node.op == "~":
                value = ~inner.value if isinstance(inner.value, int) else UNKNOWN
            else:
                value = int(not inner.value)
            return Tracked(value, f"({node.op}{inner.source})", _format_coord(node))
        if isinstance(node, c_ast.BinaryOp):
            left = self._static_tracked(node.left)
            right = self._static_tracked(node.right)
            value = _binary_value(node.op, left.value, right.value)
            return Tracked(
                value,
                f"({left.source} {node.op} {right.source})",
                _format_coord(node),
            )
        if isinstance(node, c_ast.Cast):
            return self._static_tracked(node.expr)
        if isinstance(node, c_ast.ExprList):
            latest = Tracked(UNKNOWN, "?", _format_coord(node))
            for expr in node.exprs:
                latest = self._static_tracked(expr)
            return latest
        if isinstance(node, c_ast.InitList):
            return Tracked(
                ArrayValue(tuple(self._static_tracked(item) for item in node.exprs)),
                "initializer",
                _format_coord(node),
            )
        if isinstance(node, c_ast.NamedInitializer):
            # A single designated field in isolation; the enclosing
            # InitList combines these through _static_aggregate.
            return self._static_tracked(node.expr)
        return Tracked(UNKNOWN, node.__class__.__name__, _format_coord(node))

    def _static_aggregate(
        self, node: c_ast.Node, type_node: c_ast.Node | None
    ) -> Tracked:
        if not isinstance(node, c_ast.InitList):
            return self._static_tracked(node)
        type_node = _strip_type(type_node)
        if isinstance(type_node, c_ast.Struct) and type_node.decls:
            members: list[tuple[str, Tracked]] = []
            field_names = [
                decl.name
                for decl in type_node.decls
                if isinstance(decl, c_ast.Decl) and decl.name is not None
            ]
            for index, item in enumerate(node.exprs):
                if isinstance(item, c_ast.NamedInitializer):
                    name: str | None = None
                    if item.name:
                        last = item.name[-1]
                        if isinstance(last, c_ast.ID):
                            name = last.name
                    if name is None:
                        continue
                    members.append((name, self._static_tracked(item.expr)))
                elif index < len(field_names):
                    members.append((field_names[index], self._static_tracked(item)))
            return Tracked(
                StructValue(tuple(members)), "initializer", _format_coord(node)
            )
        if isinstance(type_node, c_ast.ArrayDecl):
            return Tracked(
                ArrayValue(tuple(self._static_tracked(item) for item in node.exprs)),
                "initializer",
                _format_coord(node),
            )
        return Tracked(
            ArrayValue(tuple(self._static_tracked(item) for item in node.exprs)),
            "initializer",
            _format_coord(node),
        )

    # -- endpoint resolution ---------------------------------------------

    def _resolve_payload_positions(self) -> tuple[int, ...]:
        if self.endpoint.positions is not None:
            return self.endpoint.positions
        decl = self._function_decls.get(self.endpoint.function)
        if decl is None:
            raise ValueError(
                f"endpoint {self.endpoint.function!r} is neither defined nor declared; "
                "pass Endpoint(positions=...) to identify payload arguments"
            )
        func = _funcdecl_of(decl)
        if func is None:
            raise ValueError(f"endpoint {self.endpoint.function!r} is not a function")
        names = _parameter_names(func)
        if any(name is None for name in names):
            raise ValueError(
                f"endpoint {self.endpoint.function!r} has unnamed parameters; "
                "pass Endpoint(positions=...) instead"
            )
        lookup = {name: index for index, name in enumerate(names)}
        missing = [p for p in self.endpoint.parameters if p not in lookup]
        if missing:
            raise ValueError(
                f"endpoint {self.endpoint.function!r} has no parameter(s): "
                + ", ".join(missing)
            )
        return tuple(lookup[p] for p in self.endpoint.parameters)

    # -- diagnostics ------------------------------------------------------

    @property
    def truncations(self) -> list[Truncation]:
        return list(self._truncations)

    def _truncate(
        self,
        state: _State,
        reason: str,
        detail: str,
        node: c_ast.Node | None = None,
    ) -> None:
        call_path = tuple(frame.function for frame in state.stack)
        truncation = Truncation(
            reason=reason,
            function=state.stack[-1].function if state.stack else "<none>",
            coord=_format_coord(node),
            detail=detail,
            call_path=call_path,
        )
        if truncation not in self._truncations:
            self._truncations.append(truncation)

    # -- public API -------------------------------------------------------

    def analyze(self, entries: Sequence[EntryPoint]) -> list[Reachability]:
        """Analyze ``entries`` and return concrete write payloads.

        Results are ordered by entry point, then by traversal order.
        Duplicate payloads on the same call path are removed.
        """
        self._results = []
        self._truncations = []
        self._result_limit_reported = False

        for entry in entries:
            for expanded in self._expand_entry(entry):
                self._run_entry(expanded)
        return list(self._results)

    def _expand_entry(self, entry: EntryPoint) -> Iterator[EntryPoint]:
        domains: list[tuple[Any, ...]] = []
        for argument in entry.arguments:
            if isinstance(argument, ValueRange):
                domains.append(argument.values)
            else:
                domains.append((argument,))
        if not domains:
            yield entry
            return
        combinations = 1
        for domain in domains:
            combinations *= len(domain)
        if combinations > self.max_entry_combinations:
            self._truncations.append(
                Truncation(
                    reason="entry_limit",
                    function=entry.function,
                    coord=entry.coord,
                    detail=(
                        f"{combinations} entry combinations exceed limit "
                        f"{self.max_entry_combinations}"
                    ),
                    call_path=(entry.function,),
                )
            )
            return
        for values in product(*domains):
            yield EntryPoint(entry.function, tuple(values), entry.coord)

    def _run_entry(self, entry: EntryPoint) -> None:
        func = self._functions.get(entry.function)
        if func is None:
            self._truncations.append(
                Truncation(
                    reason="unknown_entry",
                    function=entry.function,
                    coord=entry.coord,
                    detail=f"no definition for entry point {entry.function}",
                    call_path=(entry.function,),
                )
            )
            return
        func_decl = _funcdecl_of(func.decl)
        assert func_decl is not None
        names = _parameter_names(func_decl)
        entry_coord = entry.coord or _format_coord(func.decl)
        env: dict[str, Tracked] = {}
        frame_args: list[Tracked] = []
        for index, name in enumerate(names):
            if index < len(entry.arguments):
                argument = entry.arguments[index]
                tracked = Tracked(
                    argument,
                    _render_entry_argument(argument),
                    entry_coord,
                )
            else:
                tracked = Tracked(UNKNOWN, "?", entry_coord)
            frame_args.append(tracked)
            if name is not None:
                env[name] = tracked
        frame = Frame(entry.function, entry_coord, tuple(frame_args))
        state = _State(env, (frame,), dict(self._globals))
        flow = self._exec_block(func.body, state)
        for leaked in flow.breaks + flow.continues:
            self._truncate(
                leaked, "stray_jump", "break/continue outside loop or switch"
            )

    # -- statement execution ---------------------------------------------

    def _exec_block(self, node: c_ast.Compound, state: _State) -> _Flow:
        items = node.block_items or []
        return self._exec_many(items, _Flow(live=[state]))

    def _exec_stmt(self, node: c_ast.Node, state: _State) -> _Flow:
        try:
            return self._exec_stmt_inner(node, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()

    def _exec_stmt_inner(self, node: c_ast.Node, state: _State) -> _Flow:
        if isinstance(node, c_ast.Compound):
            return self._exec_block(node, state)
        if isinstance(node, c_ast.Decl):
            return self._exec_decl(node, state)
        if isinstance(node, c_ast.DeclList):
            return self._exec_many(node.decls, _Flow(live=[state]))
        if isinstance(node, c_ast.Return):
            if node.expr is None:
                return _Flow(returns=[(state, Tracked(UNKNOWN, "void"))])
            return self._exec_return(node, state)
        if isinstance(node, c_ast.If):
            return self._exec_if(node, state)
        if isinstance(node, c_ast.Switch):
            return self._exec_switch(node, state)
        if isinstance(node, c_ast.For):
            return self._exec_for(node, state)
        if isinstance(node, c_ast.While):
            return self._exec_while(node, state)
        if isinstance(node, c_ast.DoWhile):
            return self._exec_do_while(node, state)
        if isinstance(node, c_ast.Break):
            return _Flow(breaks=[state])
        if isinstance(node, c_ast.Continue):
            return _Flow(continues=[state])
        if isinstance(node, (c_ast.Goto, c_ast.Label)):
            # Labels and gotos are not modelled; explicit jump targets would
            # need a control-flow graph.  Skip the labelled statement but
            # never invent a target.
            if isinstance(node, c_ast.Label):
                return self._exec_stmt(node.stmt, state)
            return _Flow(live=[state])
        if isinstance(node, c_ast.Case):
            return self._exec_many(node.stmts, _Flow(live=[state]))
        if isinstance(node, c_ast.Default):
            return self._exec_many(node.stmts, _Flow(live=[state]))
        if isinstance(node, c_ast.Assignment):
            return self._exec_assignment(node, state)
        if isinstance(node, c_ast.FuncCall):
            return self._exec_call_statement(node, state)
        if isinstance(node, c_ast.UnaryOp) and node.op in {"++", "--", "p++", "p--"}:
            return self._exec_incdec(node, state)
        if isinstance(node, (c_ast.Pragma, c_ast.EmptyStatement, c_ast.StaticAssert)):
            return _Flow(live=[state])
        if isinstance(node, c_ast.AttributedStatement):
            return self._exec_stmt(node.stmt, state)
        if isinstance(node, c_ast.ExprList):
            flow = _Flow(live=[state])
            for expr in node.exprs:
                flow = self._exec_expression_statement(expr, flow)
            return flow
        if isinstance(node, c_ast.Cast):
            return self._exec_expression_statement(node.expr, _Flow(live=[state]))
        if isinstance(
            node,
            (
                c_ast.ID,
                c_ast.Constant,
                c_ast.BinaryOp,
                c_ast.TernaryOp,
                c_ast.StructRef,
                c_ast.ArrayRef,
            ),
        ):
            return self._exec_expression_statement(node, _Flow(live=[state]))
        # Fallback: execute child statements, never evaluate unknown syntax
        # as a value.
        flow = _Flow(live=[state])
        for _, child in node.children():
            flow = self._exec_many([child], flow)
        return flow

    def _exec_many(self, items: Sequence[c_ast.Node], flow: _Flow) -> _Flow:
        result = flow
        for item in items:
            next_flow = _Flow()
            for current in result.live:
                piece = self._exec_stmt(item, current)
                next_flow.live.extend(piece.live)
                next_flow.returns.extend(piece.returns)
                next_flow.breaks.extend(piece.breaks)
                next_flow.continues.extend(piece.continues)
            result = next_flow
            if not result.live:
                break
        return result

    def _exec_expression_statement(self, node: c_ast.Node, flow: _Flow) -> _Flow:
        result = _Flow()
        for state in flow.live:
            try:
                outcomes = self._eval_expr(node, state)
            except _Stop as stop:
                self._truncate(state, stop.reason, stop.detail, stop.node)
                continue
            result.live.extend(outcome.state for outcome in outcomes)
        result.returns.extend(flow.returns)
        result.breaks.extend(flow.breaks)
        result.continues.extend(flow.continues)
        return result

    def _exec_decl(self, node: c_ast.Decl, state: _State) -> _Flow:
        if node.name is None:
            return _Flow(live=[state])
        if isinstance(node.type, c_ast.FuncDecl):
            return _Flow(live=[state])
        if node.init is None:
            aggregate = self._empty_aggregate(node.type)
            state.env[node.name] = aggregate or Tracked(
                UNKNOWN, node.name, _format_coord(node)
            )
            return _Flow(live=[state])
        try:
            outcomes = self._eval_expr(node.init, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        flow = _Flow()
        for outcome in outcomes:
            # Aggregates need their declared type to map positional
            # initializers to fields.  Evaluate each initializer in the
            # current state so locals used inside are still tracked.
            aggregate = self._exec_state_aggregate(node.init, node.type, outcome.state)
            if isinstance(aggregate.value, (StructValue, ArrayValue)):
                value = aggregate
            else:
                value = outcome.value
            outcome.state.env[node.name] = value
            flow.live.append(outcome.state)
        return flow

    def _empty_aggregate(self, type_node: c_ast.Node | None) -> Tracked | None:
        """Create a placeholder aggregate for an uninitialized local.

        Members start UNKNOWN so that assignments to individual fields
        can be tracked without pretending the whole object is known.
        """
        type_node = _strip_type(type_node)
        if isinstance(type_node, c_ast.Struct):
            definition = type_node
            if definition.decls is None and definition.name in self._struct_defs:
                definition = self._struct_defs[definition.name]
            if definition.decls:
                members = tuple(
                    (
                        decl.name,
                        Tracked(UNKNOWN, f".{decl.name}", _format_coord(decl)),
                    )
                    for decl in definition.decls
                    if isinstance(decl, c_ast.Decl) and decl.name is not None
                )
                return Tracked(StructValue(members), "struct", _format_coord(type_node))
        if isinstance(type_node, c_ast.Union):
            definition = type_node
            if definition.decls is None and definition.name in self._union_defs:
                definition = self._union_defs[definition.name]
            if definition.decls:
                members = tuple(
                    (
                        decl.name,
                        Tracked(UNKNOWN, f".{decl.name}", _format_coord(decl)),
                    )
                    for decl in definition.decls
                    if isinstance(decl, c_ast.Decl) and decl.name is not None
                )
                return Tracked(StructValue(members), "union", _format_coord(type_node))
        if isinstance(type_node, c_ast.ArrayDecl):
            dim = self._eval_const_expr(type_node.dim)
            if isinstance(dim, int) and 0 <= dim <= 4096:
                inner = Tracked(UNKNOWN, "[]", _format_coord(type_node))
                return Tracked(
                    ArrayValue(tuple(inner for _ in range(dim))),
                    "array",
                    _format_coord(type_node),
                )
        return None

    def _exec_state_aggregate(
        self,
        node: c_ast.Node,
        type_node: c_ast.Node | None,
        state: _State,
    ) -> Tracked:
        """Evaluate an initializer list in the current state.

        Falls back to static evaluation for constants, but resolves IDs
        such as locals and parameters through ``state`` so a struct or
        array built from runtime values is still tracked.
        """
        if not isinstance(node, c_ast.InitList):
            results = self._eval_expr(node, state)
            return results[0].value if results else Tracked(UNKNOWN, "?", None)
        stripped = _strip_type(type_node)
        if isinstance(stripped, (c_ast.Struct, c_ast.Union)):
            definition = stripped
            if definition.decls is None:
                definition = (
                    self._struct_defs.get(definition.name)
                    if isinstance(definition, c_ast.Struct)
                    else self._union_defs.get(definition.name)
                )
            members: list[tuple[str, Tracked]] = []
            field_names = [
                decl.name
                for decl in (definition.decls or [])
                if isinstance(decl, c_ast.Decl) and decl.name is not None
            ]
            for index, item in enumerate(node.exprs):
                if isinstance(item, c_ast.NamedInitializer):
                    name = None
                    if item.name and isinstance(item.name[-1], c_ast.ID):
                        name = item.name[-1].name
                    if name is None:
                        continue
                    members.append((name, self._tracked_in_state(item.expr, state)))
                elif index < len(field_names):
                    members.append(
                        (field_names[index], self._tracked_in_state(item, state))
                    )
            return Tracked(
                StructValue(tuple(members)), "initializer", _format_coord(node)
            )
        return Tracked(
            ArrayValue(
                tuple(self._tracked_in_state(item, state) for item in node.exprs)
            ),
            "initializer",
            _format_coord(node),
        )

    def _tracked_in_state(self, node: c_ast.Node, state: _State) -> Tracked:
        results = self._eval_expr(node, state)
        return (
            results[0].value if results else Tracked(UNKNOWN, "?", _format_coord(node))
        )

    def _exec_return(self, node: c_ast.Return, state: _State) -> _Flow:
        try:
            outcomes = self._eval_expr(node.expr, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        return _Flow(returns=[(outcome.state, outcome.value) for outcome in outcomes])

    def _exec_assignment(self, node: c_ast.Assignment, state: _State) -> _Flow:
        try:
            outcomes = self._eval_expr(node.rvalue, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        flow = _Flow()
        for outcome in outcomes:
            value = outcome.value
            if node.op != "=":
                current = self._read_lvalue(node.lvalue, outcome.state)
                if current is None or not current.is_known():
                    self._truncate(
                        outcome.state,
                        "unknown_value",
                        f"compound assignment to {_lvalue_name(node.lvalue)} has no concrete value",
                        node,
                    )
                    continue
                combined = _binary_value(node.op[:-1], current.value, value.value)
                value = Tracked(
                    combined,
                    f"({current.source} {node.op[:-1]} {value.source})",
                    _format_coord(node),
                )
            updated = self._write_lvalue(node.lvalue, value, outcome.state)
            if updated is None:
                # The target is an aggregate we do not track (for example a
                # member of an unknown struct).  Truncate this path but keep
                # its diagnostic rather than silently treating it as known.
                self._truncate(
                    outcome.state,
                    "unknown_lvalue",
                    f"cannot write {_lvalue_name(node.lvalue)}",
                    node,
                )
                continue
            flow.live.append(updated)
        return flow

    def _exec_incdec(self, node: c_ast.UnaryOp, state: _State) -> _Flow:
        current = self._read_lvalue(node.expr, state)
        if current is None:
            return _Flow(live=[state])
        if not isinstance(current.value, (int, float)):
            value = Tracked(UNKNOWN, current.source, _format_coord(node))
        else:
            delta = 1 if node.op in {"++", "p++"} else -1
            value = Tracked(current.value + delta, current.source, _format_coord(node))
        updated = self._write_lvalue(node.expr, value, state)
        return _Flow(live=[updated or state])

    def _exec_if(self, node: c_ast.If, state: _State) -> _Flow:
        try:
            outcomes = self._eval_expr(node.cond, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        flow = _Flow()
        for outcome in outcomes:
            cond = outcome.value
            if cond.value is UNKNOWN:
                # Unknown condition: both outcomes are possible.  Each arm
                # gets its own environment copy so values chosen in one arm
                # cannot leak into the other.
                if node.iftrue is not None:
                    flow.merge(self._exec_stmt(node.iftrue, outcome.state.copy()))
                if node.iffalse is not None:
                    flow.merge(self._exec_stmt(node.iffalse, outcome.state.copy()))
                else:
                    flow.live.append(outcome.state.copy())
                continue
            branch = node.iftrue if _truthy(cond.value) else node.iffalse
            if branch is None:
                flow.live.append(outcome.state)
            else:
                flow.merge(self._exec_stmt(branch, outcome.state))
        return flow

    def _exec_switch(self, node: c_ast.Switch, state: _State) -> _Flow:
        try:
            outcomes = self._eval_expr(node.cond, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        body = node.stmt
        if not isinstance(body, c_ast.Compound):
            return _Flow(live=[outcome.state for outcome in outcomes])
        items = body.block_items or []
        groups: list[tuple[str, c_ast.Node]] = []
        for item in items:
            if isinstance(item, c_ast.Case):
                groups.append(("case", item))
            elif isinstance(item, c_ast.Default):
                groups.append(("default", item))
            else:
                groups.append(("stmt", item))
        flow = _Flow()
        for outcome in outcomes:
            cond = outcome.value
            if not isinstance(cond.value, int):
                self._truncate(
                    outcome.state,
                    "unknown_condition",
                    "switch condition is not a concrete integer",
                    node,
                )
                continue
            start: int | None = None
            default_index: int | None = None
            for index, (kind, item) in enumerate(groups):
                if kind == "case":
                    assert isinstance(item, c_ast.Case)
                    case_value = self._static_tracked(item.expr)
                    if case_value.value == cond.value:
                        start = index
                        break
                elif kind == "default":
                    default_index = index
            if start is None and default_index is not None:
                start = default_index
            if start is None:
                flow.live.append(outcome.state)
                continue
            # Execute from the selected label through the rest of the body,
            # allowing fall-through; switch-level break ends this switch.
            inner = _Flow(live=[outcome.state])
            for index in range(start, len(groups)):
                if not inner.live:
                    break
                kind, item = groups[index]
                if kind == "case":
                    assert isinstance(item, c_ast.Case)
                    stmts = item.stmts
                elif kind == "default":
                    assert isinstance(item, c_ast.Default)
                    stmts = item.stmts
                else:
                    stmts = [item]
                inner = self._exec_many(stmts, inner)
            flow.live.extend(inner.live)
            flow.live.extend(inner.breaks)
            flow.returns.extend(inner.returns)
            flow.continues.extend(inner.continues)
        return flow

    def _exec_for(self, node: c_ast.For, state: _State) -> _Flow:
        flow = _Flow(live=[state])
        if node.init is not None:
            flow = self._exec_many([node.init], flow)
        return self._unroll_loop(node, flow, node.cond, node.next)

    def _exec_while(self, node: c_ast.While, state: _State) -> _Flow:
        return self._unroll_loop(node, _Flow(live=[state]), node.cond, None)

    def _exec_do_while(self, node: c_ast.DoWhile, state: _State) -> _Flow:
        flow = _Flow(live=[state])
        result = _Flow()
        for _ in range(self.max_loop_iterations):
            inner = self._exec_many([node.stmt], flow)
            result.returns.extend(inner.returns)
            result.live.extend(inner.breaks)
            result.continues.extend(inner.continues)
            active = inner.live + inner.continues
            if not active:
                return result
            # Evaluate the condition as an expression for each active state.
            next_live: list[_State] = []
            for current in active:
                try:
                    outcomes = self._eval_expr(node.cond, current)
                except _Stop as stop:
                    self._truncate(current, stop.reason, stop.detail, stop.node)
                    continue
                for outcome in outcomes:
                    if outcome.value.value is UNKNOWN:
                        self._truncate(
                            outcome.state,
                            "unknown_condition",
                            "do-while condition is not concrete",
                            node,
                        )
                    elif _truthy(outcome.value.value):
                        next_live.append(outcome.state)
                    else:
                        result.live.append(outcome.state)
            flow = _Flow(live=next_live)
            if not flow.live:
                return result
        for leftover in flow.live:
            self._truncate(
                leftover,
                "loop_limit",
                f"do-while exceeded {self.max_loop_iterations} iterations",
                node,
            )
        return result

    def _unroll_loop(
        self,
        node: c_ast.Node,
        flow: _Flow,
        cond: c_ast.Node | None,
        next_expr: c_ast.Node | None,
    ) -> _Flow:
        result = _Flow()
        result.returns.extend(flow.returns)
        # Breaks/continues produced before the loop body belong to an
        # enclosing construct; they are not consumed here.
        result.breaks.extend(flow.breaks)
        result.continues.extend(flow.continues)
        active = flow.live
        for _ in range(self.max_loop_iterations):
            exit_states: list[_State] = []
            body_states: list[_State] = []
            for current in active:
                if cond is None:
                    body_states.append(current)
                    continue
                try:
                    outcomes = self._eval_expr(cond, current)
                except _Stop as stop:
                    self._truncate(current, stop.reason, stop.detail, stop.node)
                    continue
                for outcome in outcomes:
                    if outcome.value.value is UNKNOWN:
                        self._truncate(
                            outcome.state,
                            "unknown_condition",
                            "loop condition is not concrete",
                            cond,
                        )
                    elif _truthy(outcome.value.value):
                        body_states.append(outcome.state)
                    else:
                        exit_states.append(outcome.state)
            result.live.extend(exit_states)
            if not body_states:
                return result
            body_flow = self._exec_many([node.stmt], _Flow(live=body_states))
            result.returns.extend(body_flow.returns)
            # A break inside the loop leaves the loop, so it becomes live
            # after the loop.  Continues are folded back into the next
            # iteration and never escape.
            result.live.extend(body_flow.breaks)
            continuing = body_flow.live + body_flow.continues
            if next_expr is not None and continuing:
                step_flow = self._exec_many([next_expr], _Flow(live=continuing))
                continued = step_flow.live
                result.returns.extend(step_flow.returns)
                result.breaks.extend(step_flow.breaks)
                result.continues.extend(step_flow.continues)
            else:
                continued = continuing
            active = continued
            if not active:
                return result
        for leftover in active:
            self._truncate(
                leftover,
                "loop_limit",
                f"loop exceeded {self.max_loop_iterations} iterations",
                node,
            )
        return result

    # -- lvalues ----------------------------------------------------------

    def _read_lvalue(self, node: c_ast.Node, state: _State) -> Tracked | None:
        if isinstance(node, c_ast.ID):
            return self._lookup_id(node, state)
        if isinstance(node, c_ast.StructRef):
            base = self._read_lvalue(node.name, state)
            if base is None or not isinstance(base.value, StructValue):
                return None
            member = base.value.get(node.field.name)
            return member
        if isinstance(node, c_ast.ArrayRef):
            base = self._read_lvalue(node.name, state)
            index = self._tracked_in_state(node.subscript, state).value
            if (
                base is None
                or not isinstance(base.value, ArrayValue)
                or not isinstance(index, int)
            ):
                return None
            if not 0 <= index < len(base.value.items):
                return None
            return base.value.items[index]
        return None

    def _write_lvalue(
        self, node: c_ast.Node, value: Tracked, state: _State
    ) -> _State | None:
        if isinstance(node, c_ast.ID):
            if node.name in state.env:
                state.env[node.name] = value
            elif node.name in state.globals or node.name in self._globals:
                state.globals[node.name] = value
            else:
                state.env[node.name] = value
            return state
        if isinstance(node, c_ast.StructRef):
            base = self._read_lvalue(node.name, state)
            if base is None or not isinstance(base.value, StructValue):
                return None
            updated = base.value.with_member(node.field.name, value)
            return self._write_lvalue(
                node.name, Tracked(updated, base.source, base.coord), state
            )
        if isinstance(node, c_ast.ArrayRef):
            base = self._read_lvalue(node.name, state)
            index = self._tracked_in_state(node.subscript, state).value
            if (
                base is None
                or not isinstance(base.value, ArrayValue)
                or not isinstance(index, int)
            ):
                return None
            if not 0 <= index < len(base.value.items):
                return None
            items = list(base.value.items)
            items[index] = value
            updated = ArrayValue(tuple(items))
            return self._write_lvalue(
                node.name, Tracked(updated, base.source, base.coord), state
            )
        return None

    # -- expression evaluation -------------------------------------------

    def _eval_expr(self, node: c_ast.Node | None, state: _State) -> list[_ExprOutcome]:
        if node is None:
            return [_ExprOutcome(state, Tracked(UNKNOWN, "?", None))]
        if isinstance(node, c_ast.Constant):
            return [
                _ExprOutcome(
                    state,
                    Tracked(
                        _parse_constant(node.type, node.value),
                        node.value,
                        _format_coord(node),
                    ),
                )
            ]
        if isinstance(node, c_ast.ID):
            return [_ExprOutcome(state, self._lookup_id(node, state))]
        if isinstance(node, c_ast.BinaryOp):
            return self._eval_binary(node, state)
        if isinstance(node, c_ast.UnaryOp):
            return self._eval_unary(node, state)
        if isinstance(node, c_ast.Cast):
            return self._eval_expr(node.expr, state)
        if isinstance(node, c_ast.TernaryOp):
            return self._eval_ternary(node, state)
        if isinstance(node, c_ast.FuncCall):
            return self._eval_call(node, state)
        if isinstance(node, c_ast.StructRef):
            base = self._eval_expr(node.name, state)
            outcomes: list[_ExprOutcome] = []
            for outcome in base:
                if isinstance(outcome.value.value, StructValue):
                    member = outcome.value.value.get(node.field.name)
                    if member is not None:
                        outcomes.append(
                            _ExprOutcome(
                                outcome.state,
                                Tracked(
                                    member.value,
                                    f"{outcome.value.source}.{node.field.name}",
                                    member.coord or _format_coord(node),
                                ),
                            )
                        )
                        continue
                outcomes.append(
                    _ExprOutcome(
                        outcome.state,
                        Tracked(
                            UNKNOWN,
                            f"{outcome.value.source}.{node.field.name}",
                            _format_coord(node),
                        ),
                    )
                )
            return outcomes
        if isinstance(node, c_ast.ArrayRef):
            base_outcomes = self._eval_expr(node.name, state)
            outcomes: list[_ExprOutcome] = []
            for outcome in base_outcomes:
                indices = self._eval_expr(node.subscript, outcome.state)
                for index_outcome in indices:
                    index = index_outcome.value.value
                    if (
                        isinstance(outcome.value.value, ArrayValue)
                        and isinstance(index, int)
                        and 0 <= index < len(outcome.value.value.items)
                    ):
                        item = outcome.value.value.items[index]
                        outcomes.append(
                            _ExprOutcome(
                                index_outcome.state,
                                Tracked(
                                    item.value,
                                    f"{outcome.value.source}[{index}]",
                                    item.coord or _format_coord(node),
                                ),
                            )
                        )
                    else:
                        outcomes.append(
                            _ExprOutcome(
                                index_outcome.state,
                                Tracked(
                                    UNKNOWN,
                                    f"{outcome.value.source}[?]",
                                    _format_coord(node),
                                ),
                            )
                        )
            return outcomes
        if isinstance(node, c_ast.ExprList):
            outcomes = [_ExprOutcome(state, Tracked(UNKNOWN, "?"))]
            for expr in node.exprs:
                next_outcomes: list[_ExprOutcome] = []
                for current in outcomes:
                    next_outcomes.extend(self._eval_expr(expr, current.state))
                outcomes = next_outcomes
            return outcomes
        if isinstance(node, c_ast.CompoundLiteral):
            value = self._static_aggregate(node.init, node.type)
            return [_ExprOutcome(state, value)]
        if isinstance(node, c_ast.InitList):
            return [_ExprOutcome(state, self._static_aggregate(node, None))]
        if isinstance(node, c_ast.NamedInitializer):
            return [_ExprOutcome(state, self._static_tracked(node.expr))]
        if isinstance(node, c_ast.Typename):
            return [
                _ExprOutcome(state, Tracked(UNKNOWN, "typename", _format_coord(node)))
            ]
        return [
            _ExprOutcome(
                state, Tracked(UNKNOWN, node.__class__.__name__, _format_coord(node))
            )
        ]

    def _eval_binary(self, node: c_ast.BinaryOp, state: _State) -> list[_ExprOutcome]:
        if node.op in {"&&", "||"}:
            return self._eval_logical(node, state)
        left_outcomes = self._eval_expr(node.left, state)
        outcomes: list[_ExprOutcome] = []
        for left in left_outcomes:
            right_outcomes = self._eval_expr(node.right, left.state)
            for right in right_outcomes:
                value = _binary_value(node.op, left.value.value, right.value.value)
                outcomes.append(
                    _ExprOutcome(
                        right.state,
                        Tracked(
                            value,
                            f"({left.value.source} {node.op} {right.value.source})",
                            _format_coord(node),
                        ),
                    )
                )
        return outcomes

    def _eval_logical(self, node: c_ast.BinaryOp, state: _State) -> list[_ExprOutcome]:
        outcomes: list[_ExprOutcome] = []
        for left in self._eval_expr(node.left, state):
            if left.value.value is UNKNOWN:
                outcomes.append(
                    _ExprOutcome(
                        left.state,
                        Tracked(
                            UNKNOWN,
                            f"({left.value.source} {node.op} ?)",
                            _format_coord(node),
                        ),
                    )
                )
                continue
            truth = _truthy(left.value.value)
            if node.op == "&&" and not truth:
                outcomes.append(
                    _ExprOutcome(
                        left.state, Tracked(0, left.value.source, _format_coord(node))
                    )
                )
                continue
            if node.op == "||" and truth:
                outcomes.append(
                    _ExprOutcome(
                        left.state, Tracked(1, left.value.source, _format_coord(node))
                    )
                )
                continue
            for right in self._eval_expr(node.right, left.state):
                if right.value.value is UNKNOWN:
                    value = UNKNOWN
                else:
                    value = int(_truthy(right.value.value))
                outcomes.append(
                    _ExprOutcome(
                        right.state,
                        Tracked(
                            value,
                            f"({left.value.source} {node.op} {right.value.source})",
                            _format_coord(node),
                        ),
                    )
                )
        return outcomes

    def _eval_unary(self, node: c_ast.UnaryOp, state: _State) -> list[_ExprOutcome]:
        if node.op == "sizeof":
            value = self._sizeof(node.expr)
            return [
                _ExprOutcome(
                    state,
                    Tracked(
                        value, f"sizeof({_type_name(node.expr)})", _format_coord(node)
                    ),
                )
            ]
        if node.op in {"++", "--", "p++", "p--"}:
            current = self._read_lvalue(node.expr, state)
            if current is None:
                return [
                    _ExprOutcome(state, Tracked(UNKNOWN, node.op, _format_coord(node)))
                ]
            if not isinstance(current.value, (int, float)):
                value = Tracked(UNKNOWN, current.source, _format_coord(node))
            else:
                delta = 1 if node.op in {"++", "p++"} else -1
                value = Tracked(
                    current.value + delta, current.source, _format_coord(node)
                )
            updated = self._write_lvalue(node.expr, value, state)
            return [_ExprOutcome(updated or state, value)]
        outcomes: list[_ExprOutcome] = []
        for inner in self._eval_expr(node.expr, state):
            value = inner.value.value
            if node.op == "-" and isinstance(value, (int, float)):
                tracked = Tracked(
                    -value, f"(-{inner.value.source})", _format_coord(node)
                )
            elif node.op == "+" and isinstance(value, (int, float)):
                tracked = Tracked(+value, inner.value.source, _format_coord(node))
            elif node.op == "~" and isinstance(value, int):
                tracked = Tracked(
                    ~value, f"(~{inner.value.source})", _format_coord(node)
                )
            elif node.op == "!":
                tracked = Tracked(
                    int(not _truthy(value)) if value is not UNKNOWN else UNKNOWN,
                    f"(!{inner.value.source})",
                    _format_coord(node),
                )
            elif node.op in {"*", "&"}:
                tracked = inner.value
            elif value is UNKNOWN:
                tracked = Tracked(
                    UNKNOWN, f"({node.op}{inner.value.source})", _format_coord(node)
                )
            else:
                tracked = Tracked(
                    UNKNOWN, f"({node.op}{inner.value.source})", _format_coord(node)
                )
            outcomes.append(_ExprOutcome(inner.state, tracked))
        return outcomes

    def _eval_ternary(self, node: c_ast.TernaryOp, state: _State) -> list[_ExprOutcome]:
        outcomes: list[_ExprOutcome] = []
        for cond in self._eval_expr(node.cond, state):
            if cond.value.value is UNKNOWN:
                outcomes.extend(self._eval_expr(node.iftrue, cond.state.copy()))
                outcomes.extend(self._eval_expr(node.iffalse, cond.state.copy()))
            elif _truthy(cond.value.value):
                outcomes.extend(self._eval_expr(node.iftrue, cond.state))
            else:
                outcomes.extend(self._eval_expr(node.iffalse, cond.state))
        return outcomes

    def _lookup_id(self, node: c_ast.ID, state: _State) -> Tracked:
        name = node.name
        if name in state.env:
            return state.env[name]
        if name in state.globals:
            return state.globals[name]
        if name in self._enums:
            return self._enums[name]
        if name in self._globals:
            return self._globals[name]
        return Tracked(UNKNOWN, name, _format_coord(node))

    def _eval_const_expr(self, node: c_ast.Node | None) -> Any:
        tracked = self._static_tracked(node)
        return tracked.value

    def _sizeof(self, node: c_ast.Node | None) -> Any:
        type_node = _strip_type(node)
        if isinstance(type_node, c_ast.IdentifierType):
            names = " ".join(type_node.names)
            return _TYPE_SIZES.get(names, UNKNOWN)
        if isinstance(type_node, c_ast.PtrDecl):
            return 8
        if isinstance(type_node, c_ast.ArrayDecl):
            inner = self._sizeof(type_node.type)
            dim = self._eval_const_expr(type_node.dim)
            if isinstance(inner, int) and isinstance(dim, int):
                return inner * dim
        return UNKNOWN

    # -- calls ------------------------------------------------------------

    def _exec_call_statement(self, node: c_ast.FuncCall, state: _State) -> _Flow:
        try:
            outcomes = self._eval_call(node, state)
        except _Stop as stop:
            self._truncate(state, stop.reason, stop.detail, stop.node)
            return _Flow()
        return _Flow(live=[outcome.state for outcome in outcomes])

    def _eval_call(self, node: c_ast.FuncCall, state: _State) -> list[_ExprOutcome]:
        if not isinstance(node.name, c_ast.ID):
            self._truncate(
                state,
                "indirect_call",
                "function-pointer call is not analysed",
                node,
            )
            return [
                _ExprOutcome(state, Tracked(UNKNOWN, "<call>", _format_coord(node)))
            ]
        name = node.name.name
        if name == self.endpoint.function:
            return self._record_endpoint(node, state)

        callee = self._functions.get(name)
        if callee is None:
            self._truncate(
                state, "unknown_function", f"no AST definition for {name}()", node
            )
            return [
                _ExprOutcome(
                    state,
                    Tracked(UNKNOWN, f"{name}()", _format_coord(node)),
                )
            ]

        if any(frame.function == name for frame in state.stack):
            self._truncate(
                state, "recursion", f"recursive call to {name}() truncated", node
            )
            return [
                _ExprOutcome(
                    state,
                    Tracked(UNKNOWN, f"{name}()", _format_coord(node)),
                )
            ]

        if len(state.stack) >= self.max_depth:
            self._truncate(
                state, "depth_limit", f"call depth exceeded at {name}()", node
            )
            return [
                _ExprOutcome(
                    state,
                    Tracked(UNKNOWN, f"{name}()", _format_coord(node)),
                )
            ]

        func_decl = _funcdecl_of(callee.decl)
        assert func_decl is not None
        names = _parameter_names(func_decl)
        arg_exprs = node.args.exprs if node.args is not None else []

        # Evaluate arguments left-to-right; argument evaluation can branch
        # or contain nested calls.
        argument_states: list[tuple[_State, list[Tracked]]] = [(state, [])]
        for index, expr in enumerate(arg_exprs):
            next_states: list[tuple[_State, list[Tracked]]] = []
            for current_state, collected in argument_states:
                for outcome in self._eval_expr(expr, current_state):
                    next_states.append((outcome.state, collected + [outcome.value]))
            argument_states = next_states

        outcomes: list[_ExprOutcome] = []
        for callee_state, arguments in argument_states:
            call_coord = _format_coord(node)
            frame = Frame(name, call_coord, tuple(arguments))
            env: dict[str, Tracked] = {}
            for index, param_name in enumerate(names):
                if param_name is not None:
                    env[param_name] = (
                        arguments[index]
                        if index < len(arguments)
                        else Tracked(UNKNOWN, "?", call_coord)
                    )
            nested = callee_state.with_frame(frame)
            # Callee locals live in a fresh environment.  Globals are
            # carried separately so assignments inside the callee remain
            # visible to the caller without leaking parameters/locals.
            saved_env = callee_state.env
            nested.env = env
            body_flow = self._exec_block(callee.body, nested)
            for returned_state, returned_value in body_flow.returns:
                merged = _State(
                    dict(saved_env),
                    callee_state.stack,
                    dict(returned_state.globals),
                )
                outcomes.append(_ExprOutcome(merged, returned_value))
            for live_state in body_flow.live:
                merged = _State(
                    dict(saved_env),
                    callee_state.stack,
                    dict(live_state.globals),
                )
                outcomes.append(
                    _ExprOutcome(
                        merged,
                        Tracked(UNKNOWN, f"{name}() returned no value", call_coord),
                    )
                )
            for leaked in body_flow.breaks + body_flow.continues:
                self._truncate(leaked, "stray_jump", "break/continue escaped a callee")
        return outcomes

    def _record_endpoint(
        self, node: c_ast.FuncCall, state: _State
    ) -> list[_ExprOutcome]:
        arg_exprs = node.args.exprs if node.args is not None else []
        argument_states: list[tuple[_State, list[Tracked]]] = [(state, [])]
        for expr in arg_exprs:
            next_states: list[tuple[_State, list[Tracked]]] = []
            for current_state, collected in argument_states:
                for outcome in self._eval_expr(expr, current_state):
                    next_states.append((outcome.state, collected + [outcome.value]))
            argument_states = next_states

        outcomes: list[_ExprOutcome] = []
        for call_state, arguments in argument_states:
            payload: list[Tracked] = []
            missing = False
            for position in self._payload_positions:
                if position >= len(arguments):
                    missing = True
                    break
                payload.append(arguments[position])
            if missing:
                self._truncate(
                    call_state,
                    "endpoint_mismatch",
                    f"{self.endpoint.function}() has too few arguments",
                    node,
                )
                # The call was rejected as a write, but execution continues
                # after it, so later sibling writes must still be seen.
                outcomes.append(
                    _ExprOutcome(
                        call_state,
                        Tracked(
                            UNKNOWN, f"{self.endpoint.function}()", _format_coord(node)
                        ),
                    )
                )
                continue
            if any(not tracked.is_known() for tracked in payload):
                unknowns = ", ".join(
                    name
                    for name, tracked in zip(self.endpoint.parameters, payload)
                    if not tracked.is_known()
                )
                self._truncate(
                    call_state,
                    "unknown_argument",
                    f"endpoint payload has unknown value(s): {unknowns}",
                    node,
                )
                outcomes.append(
                    _ExprOutcome(
                        call_state,
                        Tracked(
                            UNKNOWN, f"{self.endpoint.function}()", _format_coord(node)
                        ),
                    )
                )
                continue
            write_frame = Frame(
                self.endpoint.function,
                _format_coord(node),
                tuple(arguments),
            )
            path = call_state.stack + (write_frame,)
            reachability = Reachability(
                values=tuple(payload),
                path=path,
                write_coord=_format_coord(node),
                entry=call_state.stack[0].function if call_state.stack else "<entry>",
                payload_names=self.endpoint.parameters,
            )
            if len(self._results) >= self.max_results:
                if not self._result_limit_reported:
                    self._result_limit_reported = True
                    self._truncations.append(
                        Truncation(
                            reason="result_limit",
                            function=self.endpoint.function,
                            coord=_format_coord(node),
                            detail=f"more than {self.max_results} results",
                            call_path=tuple(frame.function for frame in path),
                        )
                    )
            elif reachability not in self._results:
                self._results.append(reachability)
            outcomes.append(
                _ExprOutcome(
                    call_state,
                    Tracked(
                        UNKNOWN, f"{self.endpoint.function}()", _format_coord(node)
                    ),
                )
            )
        return outcomes


# ---------------------------------------------------------------------------
# Utilities and convenience API
# ---------------------------------------------------------------------------


def _render_entry_argument(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _truthy(value: Any) -> bool:
    return bool(value)


def _lvalue_name(node: c_ast.Node) -> str:
    if isinstance(node, c_ast.ID):
        return node.name
    if isinstance(node, c_ast.StructRef):
        return f"{_lvalue_name(node.name)}.{node.field.name}"
    if isinstance(node, c_ast.ArrayRef):
        return f"{_lvalue_name(node.name)}[?]"
    return node.__class__.__name__


def _type_name(node: c_ast.Node | None) -> str:
    stripped = _strip_type(node)
    if isinstance(stripped, c_ast.IdentifierType):
        return " ".join(stripped.names)
    if isinstance(stripped, c_ast.PtrDecl):
        return _type_name(stripped.type) + " *"
    if isinstance(stripped, c_ast.ArrayDecl):
        return _type_name(stripped.type) + "[]"
    if isinstance(stripped, c_ast.Struct) and stripped.name:
        return f"struct {stripped.name}"
    if isinstance(stripped, c_ast.Union) and stripped.name:
        return f"union {stripped.name}"
    return "?"


def analyze_file(
    filename: str,
    entries: Sequence[EntryPoint],
    endpoint: Endpoint,
    *,
    use_cpp: bool = False,
    cpp_args: str = "",
    **kwargs: Any,
) -> tuple[list[Reachability], list[Truncation]]:
    """Parse ``filename``, analyze it, and return ``(results, truncations)``.

    With ``use_cpp=False`` a lightweight pre-pass removes comments and
    substitutes simple object-like ``#define`` constants, because
    pycparser itself rejects comments and directives.  Use
    ``use_cpp=True`` when full preprocessing is needed.
    """
    from . import c_parser, parse_file

    if use_cpp:
        ast = parse_file(filename, use_cpp=True, cpp_args=cpp_args)
    else:
        with open(filename, encoding="utf-8") as stream:
            source = stream.read()
        prepared = prepare_source(source)
        ast = c_parser.CParser().parse(prepared, filename=filename)
    analyzer = ReachabilityAnalyzer(ast, endpoint, **kwargs)
    results = analyzer.analyze(entries)
    return results, analyzer.truncations


_COMMENT_RE = re.compile(
    r"//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)
_DEFINE_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+([^\n]+)$", re.MULTILINE
)


_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def _blank_comments(source: str) -> str:
    """Blank comments while preserving newlines and character columns.

    Keeping line structure intact means pycparser's coordinates match
    the original file, which makes the reported call sites directly
    clickable instead of pointing into a rewritten copy.
    """

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _COMMENT_RE.sub(blank, source)


def prepare_source(source: str) -> str:
    """Remove comments and expand simple object-like macros.

    This is deliberately not a C preprocessor.  It handles comments and
    ``#define NAME literal`` definitions (numbers, strings, character
    constants and simple references), which is what sampling code most
    commonly uses for channel/field/scale constants.  Other directives
    are dropped.  Macros are not expanded inside string or character
    literals, and comments are replaced by spaces so that line numbers
    (and therefore every reported coordinate) stay exact.
    """
    without_comments = _blank_comments(source)
    macros: dict[str, str] = {}
    for match in _DEFINE_RE.finditer(without_comments):
        macros[match.group(1)] = match.group(2).strip()
    text = _DIRECTIVE_RE.sub(lambda match: " " * len(match.group(0)), without_comments)
    if not macros:
        return text

    def replace_identifier(match: re.Match[str]) -> str:
        seen: set[str] = set()
        value = match.group(0)
        while value in macros and value not in seen:
            seen.add(value)
            value = macros[value]
        return value

    pieces: list[str] = []
    position = 0
    for literal in _STRING_RE.finditer(text):
        pieces.append(
            _IDENTIFIER_RE.sub(replace_identifier, text[position : literal.start()])
        )
        pieces.append(literal.group(0))
        position = literal.end()
    pieces.append(_IDENTIFIER_RE.sub(replace_identifier, text[position:]))
    return "".join(pieces)
