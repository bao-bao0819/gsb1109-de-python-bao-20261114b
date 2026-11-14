# -----------------------------------------------------------------
# pycparser: array_init.py
#
# Extraction of the concrete memory layout produced by C array
# initializers.  Given the AST of a declaration such as
#
#     int calib[6][3] = {
#         {1, 2, 3},
#         [2] = { [1] = 9, 8 },
#         7, 0,
#         [5][0] = 50, 51,
#     };
#
# the machinery in this module computes, for every element:
#
#   * its logical coordinate (the multi-dimensional index, or the
#     field ordinal for arrays of structs),
#   * its final value,
#   * the source text and source coordinate of the initializer that
#     wrote it last,
#
# while keeping cells that the compiler zero-fills (C17 6.7.9 p21)
# separate from cells an engineer explicitly initialized to zero.
#
# The layout rules implemented here follow C17 6.7.9:
#   * unmarked initializers continue from the position set by the
#     preceding designator, descending into sub-aggregates when
#     braces are elided;
#   * a closing brace skips the remainder of that sub-object;
#   * later initializers override earlier ones for the same cell.
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# -----------------------------------------------------------------
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Any, Optional, Union

from . import c_ast
from .c_generator import CGenerator

__all__ = [
    "ArrayInitError",
    "Cell",
    "ArrayLayout",
    "extract_layout",
    "extract_layouts",
    "parse_and_extract",
]


class ArrayInitError(ValueError):
    """Raised when an initializer layout cannot be computed."""


# A fully evaluated leaf value.  ``None`` means the initializer is not
# a compile-time constant and its source text is kept in ``Cell.source``.
ScalarValue = Union[int, float, str, None]


@dataclass(frozen=True)
class Cell:
    """One element of an array after initialization.

    index:
        Logical coordinate of the cell, e.g. ``(2, 1)`` for ``a[2][1]``.
        Struct fields are represented by their ordinal position; the
        corresponding names live in ``ArrayLayout.field_names``.

    value:
        Final value.  ``0`` for zero-filled cells, an int/float/str for
        constant initializers, or ``None`` when the initializer is not a
        compile-time constant (see ``source``).

    source:
        Exact C source text of the initializer expression that last
        wrote this cell, or ``None`` for compiler zero-filled cells.

    coord:
        pycparser ``Coord`` of that initializer (file/line/column),
        or ``None`` for zero-filled cells.

    explicit:
        ``True`` when an initializer in the source wrote this cell
        (including an explicit zero); ``False`` when C zero-filled it.

    designated:
        ``True`` when the write was reached through a designator
        (``[i] = ...`` or ``.field = ...``).
    """

    index: tuple[int, ...]
    value: ScalarValue
    source: Optional[str]
    coord: Any
    explicit: bool
    designated: bool

    @property
    def implicit(self) -> bool:
        return not self.explicit


@dataclass
class ArrayLayout:
    """The fully resolved layout of one array declaration."""

    name: Optional[str]
    declared_dims: tuple[Optional[int], ...]
    dims: tuple[int, ...]
    field_names: tuple[Optional[list[str]], ...]
    cells: dict[tuple[int, ...], Cell]
    overwrites: int
    warnings: list[str]

    def get(self, *index: int) -> Optional[Cell]:
        """Return the cell at ``index`` (all dimensions must be given)."""
        return self.cells.get(tuple(index))

    def grid(self) -> list:
        """Nested lists of final values, shaped like the declared array.

        One-dimensional arrays yield a flat list; two-dimensional arrays
        a list of rows.  Deeper arrays are nested recursively.
        """
        return self._grid(0, ())

    def _grid(self, level: int, prefix: tuple[int, ...]) -> list:
        result = []
        for i in range(self.dims[level]):
            if level == len(self.dims) - 1:
                result.append(self.cells[prefix + (i,)].value)
            else:
                result.append(self._grid(level + 1, prefix + (i,)))
        return result

    def explicit_cells(self) -> list[Cell]:
        """Cells written by an initializer in the source."""
        return [cell for cell in self.cells.values() if cell.explicit]

    def implicit_zero_cells(self) -> list[Cell]:
        """Cells never written in source and zero-filled by the compiler."""
        return [cell for cell in self.cells.values() if not cell.explicit]

    def stats(self) -> dict[str, int]:
        """Summary counts.

        Explicit zeros (an engineer wrote ``0``) and implicit zeros
        (compiler zero-fill) are counted separately.
        """
        explicit = explicit_zero = implicit_zero = 0
        for cell in self.cells.values():
            if not cell.explicit:
                implicit_zero += 1
            else:
                explicit += 1
                if isinstance(cell.value, (int, float)) and cell.value == 0:
                    explicit_zero += 1
        return {
            "total": len(self.cells),
            "explicit": explicit,
            "explicit_nonzero": explicit - explicit_zero,
            "explicit_zero": explicit_zero,
            "implicit_zero": implicit_zero,
            "overwritten": self.overwrites,
        }


# -----------------------------------------------------------------
# Constant expression evaluation
# -----------------------------------------------------------------
def _trunc_div(a, b):
    """C division: truncates toward zero for integers; true divide for floats."""
    if isinstance(a, float) or isinstance(b, float):
        return a / b
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _c_mod(a, b):
    """C remainder: the result takes the sign of the dividend."""
    if isinstance(a, float) or isinstance(b, float):
        return a - _trunc_div(a, b) * b
    remainder = abs(a) % abs(b)
    return -remainder if a < 0 else remainder


_BINARY_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": _trunc_div,
    "%": _c_mod,
    "&": operator.and_,
    "|": operator.or_,
    "^": operator.xor,
    "<<": operator.lshift,
    ">>": operator.rshift,
    "&&": lambda a, b: 1 if (a and b) else 0,
    "||": lambda a, b: 1 if (a or b) else 0,
    "==": lambda a, b: int(a == b),
    "!=": lambda a, b: int(a != b),
    "<": lambda a, b: int(a < b),
    ">": lambda a, b: int(a > b),
    "<=": lambda a, b: int(a <= b),
    ">=": lambda a, b: int(a >= b),
}


def _parse_int_literal(token: str) -> int:
    text = token.rstrip("uUlL")
    if text[:2] in ("0x", "0X"):
        return int(text, 16)
    if text[:2] in ("0b", "0B"):
        return int(text, 2)
    if len(text) > 1 and text[0] == "0":
        return int(text, 8)
    return int(text, 10)


def _parse_float_literal(token: str) -> float:
    return float(token.rstrip("fFlL"))


def _decode_char_literal(token: str) -> int:
    text = token.lstrip("uUL")
    body = text[1:-1]
    value = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            byte = ord(ch)
            i += 1
        else:
            i += 1
            esc = body[i]
            simple = {
                "a": 7,
                "b": 8,
                "f": 12,
                "n": 10,
                "r": 13,
                "t": 9,
                "v": 11,
                "\\": ord("\\"),
                "'": ord("'"),
                '"': ord('"'),
                "?": ord("?"),
                "0": 0,
            }
            if esc in simple:
                byte = simple[esc]
                i += 1
            elif esc == "x":
                j = i + 1
                while j < len(body) and body[j] in "0123456789abcdefABCDEF":
                    j += 1
                byte = int(body[i + 1 : j], 16)
                i = j
            elif esc in "01234567":
                j = i
                while j < len(body) and j - i < 3 and body[j] in "01234567":
                    j += 1
                byte = int(body[i:j], 8)
                i = j
            else:
                byte = ord(esc)
                i += 1
        value = value * 256 + byte
    return value


def _decode_string_literal(token: str) -> str:
    text = token.lstrip("uUL")
    body = text[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        esc = body[i]
        simple = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            "'": "'",
            '"': '"',
            "?": "?",
        }
        if esc in simple:
            out.append(simple[esc])
            i += 1
        elif esc == "x":
            j = i + 1
            while j < len(body) and body[j] in "0123456789abcdefABCDEF":
                j += 1
            out.append(chr(int(body[i + 1 : j], 16)))
            i = j
        elif esc in "01234567":
            j = i
            while j < len(body) and j - i < 3 and body[j] in "01234567":
                j += 1
            out.append(chr(int(body[i:j], 8)))
            i = j
        else:
            out.append(esc)
            i += 1
    return "".join(out)


class _ConstEvaluator:
    """Evaluates the constant expressions legal in initializers."""

    def __init__(self, enum_values: dict[str, int]):
        self.enum_values = enum_values
        self._generator = CGenerator()

    def source_text(self, node: c_ast.Node) -> str:
        if isinstance(node, c_ast.Constant):
            return node.value
        try:
            return self._generator.visit(node)
        except Exception:  # pragma: no cover - defensive fallback
            return node.__class__.__name__

    def evaluate(self, node: c_ast.Node) -> ScalarValue:
        if isinstance(node, c_ast.Constant):
            return self._constant(node)
        if isinstance(node, c_ast.ID):
            if node.name in self.enum_values:
                return self.enum_values[node.name]
            raise ArrayInitError(
                f"non-constant identifier {node.name!r} at {node.coord}"
            )
        if isinstance(node, c_ast.UnaryOp):
            return self._unary(node)
        if isinstance(node, c_ast.BinaryOp):
            return self._binary(node)
        if isinstance(node, c_ast.TernaryOp):
            return (
                self.evaluate(node.iftrue)
                if self.evaluate(node.cond)
                else self.evaluate(node.iffalse)
            )
        if isinstance(node, c_ast.Cast):
            return self.evaluate(node.expr)
        if isinstance(node, c_ast.ExprList):
            return self.evaluate(node.exprs[-1])
        raise ArrayInitError(
            f"non-constant initializer {self.source_text(node)!r} at {node.coord}"
        )

    def evaluate_index(self, node: c_ast.Node) -> int:
        value = self.evaluate(node)
        if not isinstance(value, int):
            raise ArrayInitError(
                f"array designator is not an integer constant: "
                f"{self.source_text(node)!r} at {node.coord}"
            )
        return value

    def _constant(self, node: c_ast.Constant) -> ScalarValue:
        if node.type.endswith("int"):
            return _parse_int_literal(node.value)
        if "float" in node.type or "double" in node.type:
            return _parse_float_literal(node.value)
        if node.type == "char":
            return _decode_char_literal(node.value)
        if node.type == "string":
            return _decode_string_literal(node.value)
        raise ArrayInitError(f"unsupported constant {node.value!r} at {node.coord}")

    def _unary(self, node: c_ast.UnaryOp) -> ScalarValue:
        value = self.evaluate(node.expr)
        if node.op == "-":
            return -value
        if node.op == "+":
            return value
        if node.op == "~":
            return ~value
        if node.op == "!":
            return int(not value)
        raise ArrayInitError(f"unsupported unary operator {node.op!r}")

    def _binary(self, node: c_ast.BinaryOp) -> ScalarValue:
        if node.op not in _BINARY_OPS:
            raise ArrayInitError(f"unsupported operator {node.op!r}")
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        return _BINARY_OPS[node.op](left, right)


def _collect_enum_values(ast: c_ast.Node) -> dict[str, int]:
    """Maps every enumerator name to its integer value (C enum rules)."""
    enum_values: dict[str, int] = {}

    class EnumVisitor(c_ast.NodeVisitor):
        def visit_Enum(self, node: c_ast.Enum) -> None:
            next_value = 0
            for enumerator in node.values.enumerators if node.values else []:
                if enumerator.value is None:
                    value = next_value
                else:
                    value = _ConstEvaluator(enum_values).evaluate_index(
                        enumerator.value
                    )
                enum_values[enumerator.name] = value
                next_value = value + 1
            self.generic_visit(node)

    EnumVisitor().visit(ast)
    return enum_values


def _collect_struct_defs(
    ast: c_ast.Node,
) -> dict[str, c_ast.Struct | c_ast.Union]:
    """Maps every struct/union tag to its defining node."""
    definitions: dict[str, c_ast.Struct | c_ast.Union] = {}

    class AggregateVisitor(c_ast.NodeVisitor):
        def visit_Struct(self, node: c_ast.Struct) -> None:
            if node.name and node.decls is not None:
                definitions.setdefault(node.name, node)
            self.generic_visit(node)

        def visit_Union(self, node: c_ast.Union) -> None:
            if node.name and node.decls is not None:
                definitions.setdefault(node.name, node)
            self.generic_visit(node)

    AggregateVisitor().visit(ast)
    return definitions


# -----------------------------------------------------------------
# Layout walking
# -----------------------------------------------------------------
@dataclass
class _Level:
    kind: str  # "array" or "struct"
    dim: Optional[int]
    fields: Optional[list[str]]


class _LayoutMapper:
    """Walks an initializer tree following C17 6.7.9 ordering rules."""

    def __init__(
        self,
        plan: list[_Level],
        char_elements: bool,
        evaluator: _ConstEvaluator,
    ):
        self.plan = plan
        self.char_elements = char_elements
        self.evaluator = evaluator
        self.path: list[int] = []
        self.cells: dict[tuple[int, ...], Cell] = {}
        self.overwrites = 0
        self.warnings: list[str] = []
        self.max_seen = [0] * len(plan)
        self._designated = False
        self._bounds_warnings: set[tuple[int, int]] = set()

    # -- type navigation ------------------------------------------------
    def map_root(self, init: c_ast.Node) -> None:
        if isinstance(init, c_ast.InitList):
            self.path = [0]
            self._map_list(init, 0)
            self.path = []
        else:
            self.path = [0]
            self._map_scalar(init, -1)
            self.path = []

    def _map_list(self, init: c_ast.InitList, frame_level: int) -> None:
        for item in init.exprs:
            if isinstance(item, c_ast.NamedInitializer):
                self._apply_designators(item.name, frame_level)
                object_frame = frame_level + len(item.name) - 1
                self._designated = True
                self._map_object(item.expr, object_frame)
            else:
                # Unmarked continuation items are never themselves designated,
                # even when the enclosing sub-object was reached by a
                # designator.
                self._designated = False
                self._map_object(item, frame_level)

    def _map_object(self, init: c_ast.Node, frame_level: int) -> None:
        while len(self.path) < frame_level + 1:
            self.path.append(0)

        if isinstance(init, c_ast.InitList):
            if frame_level >= len(self.plan) - 1:
                # Braces around a scalar (or excess brace-enclosed elements):
                # only the first initializer applies to the scalar.
                if init.exprs:
                    if len(init.exprs) > 1:
                        self.warnings.append(
                            f"excess elements in scalar initializer at {init.coord}"
                        )
                    self._map_object(init.exprs[0], frame_level)
                return
            # An opening brace resets the current object of this aggregate.
            del self.path[frame_level + 1 :]
            self.path.append(0)
            self._map_list(init, frame_level + 1)
            self.path.pop()
            self._increment(frame_level)
        elif isinstance(init, c_ast.NamedInitializer):
            # Reached when a designator's value is itself designated
            # without extra braces, e.g. [2] = [1] = 9.
            self._apply_designators(init.name, frame_level)
            object_frame = frame_level + len(init.name) - 1
            self._designated = True
            self._map_object(init.expr, object_frame)
        else:
            self._map_scalar(init, frame_level)

    def _apply_designators(
        self, designators: list[c_ast.Node], frame_level: int
    ) -> None:
        del self.path[frame_level + 1 :]
        for offset, designator in enumerate(designators):
            level = frame_level + offset
            if level >= len(self.plan):
                raise ArrayInitError(
                    f"too many designators for array type at {designator.coord}"
                )
            if isinstance(designator, c_ast.ArrayDesignator):
                index = self.evaluator.evaluate_index(designator.expr)
            elif isinstance(designator, c_ast.ID):
                fields = self.plan[level].fields
                if fields is None or designator.name not in fields:
                    raise ArrayInitError(
                        f"field designator .{designator.name} does not name a "
                        f"field at {designator.coord}"
                    )
                index = fields.index(designator.name)
            else:
                raise ArrayInitError(f"unsupported designator at {designator.coord}")
            if len(self.path) == level:
                self.path.append(index)
            else:
                self.path[level] = index

    # -- scalar / leaf handling ----------------------------------------
    def _map_scalar(self, init: c_ast.Node, frame_level: int) -> None:
        # A deep designator (e.g. [1][0][1] = 9) may leave the innermost
        # index past its dimension.  Roll the overflow through the elided
        # sub-objects before the next unmarked initializer is placed.
        level = len(self.path) - 1
        while level > frame_level and self._overflows(level):
            self.path.pop()
            level -= 1
            if level < 0:
                break
            self.path[level] += 1
        # Brace elision: descend through sub-aggregates to a leaf.
        while len(self.path) < len(self.plan):
            self.path.append(0)

        if isinstance(init, c_ast.Constant) and init.type == "string":
            if self.char_elements:
                self._map_string_into_chars(init, frame_level)
                return
            value: ScalarValue = self.evaluator.evaluate(init)
            self._write_leaf(init, value)
            self._advance_after_leaf(frame_level)
            return

        try:
            value = self.evaluator.evaluate(init)
        except ArrayInitError:
            value = None
        self._write_leaf(init, value)
        self._advance_after_leaf(frame_level)

    def _map_string_into_chars(self, init: c_ast.Constant, frame_level: int) -> None:
        text = _decode_string_literal(init.value)
        data = text.encode("latin-1", errors="strict") + b"\x00"
        dim = self.plan[-1].dim
        if dim is not None:
            data = data[:dim]
        for byte in data:
            while len(self.path) < len(self.plan):
                self.path.append(0)
            if dim is not None and self.path[-1] >= dim:
                break
            self._write_leaf(init, byte)
            self._advance_after_leaf(frame_level)
        # A string fills a whole char-array sub-object.  When it appeared
        # without enclosing braces in an outer initializer list, close that
        # sub-object and advance the enclosing array; otherwise the
        # enclosing brace's close handles the advance.
        if frame_level >= 0 and frame_level + 1 < len(self.plan):
            del self.path[frame_level + 1 :]
            self._increment(frame_level)

    def _write_leaf(self, init: c_ast.Node, value: ScalarValue) -> None:
        index = tuple(self.path)
        for level, position in enumerate(index):
            self.max_seen[level] = max(self.max_seen[level], position)
            dim = self.plan[level].dim
            if (
                dim is not None
                and position >= dim
                and (level, position) not in self._bounds_warnings
            ):
                self._bounds_warnings.add((level, position))
                self.warnings.append(
                    f"index {position} exceeds declared size {dim} at "
                    f"dimension {level} (coord {init.coord})"
                )
        if index in self.cells:
            self.overwrites += 1
        self.cells[index] = Cell(
            index=index,
            value=value,
            source=self.evaluator.source_text(init),
            coord=init.coord,
            explicit=True,
            designated=self._designated,
        )

    def _increment(self, level: int) -> None:
        self.path[level] += 1

    def _advance_after_leaf(self, stop_frame: int) -> None:
        """Move to the next sub-object, popping overflowed elided frames."""
        level = len(self.path) - 1
        self.path[level] += 1
        while level > stop_frame and self._overflows(level):
            self.path.pop()
            level -= 1
            if level < 0:
                return
            self.path[level] += 1

    def _overflows(self, level: int) -> bool:
        dim = self.plan[level].dim
        return dim is not None and self.path[level] >= dim

    # -- finalization ---------------------------------------------------
    def resolve_dims(self) -> tuple[int, ...]:
        resolved = []
        for level, declared in enumerate(plan_dims(self.plan)):
            needed = self.max_seen[level] + 1
            if declared is None:
                resolved.append(max(1, needed))
            else:
                # An out-of-bounds designator already produced a warning;
                # grow the resolved grid so every written cell is addressable,
                # while the declared size stays in ``declared_dims``.
                resolved.append(max(declared, needed))
        return tuple(resolved)

    def pad_implicit_zeros(self, dims: tuple[int, ...]) -> None:
        for index in _iter_indices(dims):
            if index not in self.cells:
                self.cells[index] = Cell(
                    index=index,
                    value=0,
                    source=None,
                    coord=None,
                    explicit=False,
                    designated=False,
                )


def plan_dims(plan: list[_Level]) -> list[Optional[int]]:
    return [level.dim for level in plan]


def _iter_indices(dims: tuple[int, ...]):
    def recurse(prefix: tuple[int, ...], depth: int):
        if depth == len(dims):
            yield prefix
            return
        for i in range(dims[depth]):
            yield from recurse(prefix + (i,), depth + 1)

    yield from recurse((), 0)


# -----------------------------------------------------------------
# Type inspection
# -----------------------------------------------------------------
def _build_plan(
    arr_decl: c_ast.ArrayDecl,
    evaluator: _ConstEvaluator,
    struct_defs: Optional[dict[str, c_ast.Struct | c_ast.Union]] = None,
) -> tuple[list[_Level], c_ast.Node, bool]:
    plan: list[_Level] = []
    node: c_ast.Node = arr_decl
    while isinstance(node, c_ast.ArrayDecl):
        dim = None if node.dim is None else evaluator.evaluate_index(node.dim)
        plan.append(_Level(kind="array", dim=dim, fields=None))
        node = node.type

    char_elements = False
    if isinstance(node, c_ast.TypeDecl):
        underlying = node.type
        if isinstance(underlying, c_ast.IdentifierType):
            char_elements = "char" in underlying.names
        if isinstance(underlying, (c_ast.Struct, c_ast.Union)):
            aggregate = underlying
            if (
                aggregate.decls is None
                and aggregate.name
                and struct_defs
                and aggregate.name in struct_defs
            ):
                aggregate = struct_defs[aggregate.name]
            plan.append(_struct_level(aggregate))
    return plan, node, char_elements


def _struct_level(aggregate: c_ast.Struct | c_ast.Union) -> _Level:
    if aggregate.decls is None:
        raise ArrayInitError(
            f"struct/union {aggregate.name!r} is used but never defined"
        )
    fields = [decl.name for decl in aggregate.decls]
    for decl in aggregate.decls:
        if isinstance(decl.type, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
            raise ArrayInitError(
                f"nested aggregate struct fields are not supported at {decl.coord}"
            )
    return _Level(kind="struct", dim=len(fields), fields=fields)


# -----------------------------------------------------------------
# Public API
# -----------------------------------------------------------------
def extract_layout(
    decl: c_ast.Decl,
    enum_values: Optional[dict[str, int]] = None,
    struct_defs: Optional[dict[str, c_ast.Struct | c_ast.Union]] = None,
) -> Optional[ArrayLayout]:
    """Compute the :class:`ArrayLayout` for one array declaration.

    Returns ``None`` when ``decl`` is not an initialized array.
    """
    if not isinstance(decl, c_ast.Decl) or not isinstance(decl.type, c_ast.ArrayDecl):
        return None
    if decl.init is None:
        return None

    evaluator = _ConstEvaluator(enum_values or {})
    plan, _base, char_elements = _build_plan(decl.type, evaluator, struct_defs)

    mapper = _LayoutMapper(plan, char_elements, evaluator)
    mapper.map_root(decl.init)
    dims = mapper.resolve_dims()
    mapper.pad_implicit_zeros(dims)

    return ArrayLayout(
        name=decl.name,
        declared_dims=tuple(level.dim for level in plan),
        dims=dims,
        field_names=tuple(level.fields for level in plan),
        cells=dict(sorted(mapper.cells.items())),
        overwrites=mapper.overwrites,
        warnings=list(mapper.warnings),
    )


def extract_layouts(ast: c_ast.Node) -> list[ArrayLayout]:
    """Extract layouts of every initialized array declaration under ``ast``."""
    enum_values = _collect_enum_values(ast)
    struct_defs = _collect_struct_defs(ast)
    layouts: list[ArrayLayout] = []

    class ArrayDeclVisitor(c_ast.NodeVisitor):
        def visit_Decl(self, node: c_ast.Decl) -> None:
            if isinstance(node.type, c_ast.ArrayDecl) and node.init is not None:
                layout = extract_layout(node, enum_values, struct_defs)
                if layout is not None:
                    layouts.append(layout)
            self.generic_visit(node)

    ArrayDeclVisitor().visit(ast)
    return layouts


def parse_and_extract(text: str, filename: str = "<src>") -> list[ArrayLayout]:
    """Parse C source ``text`` and return its array layouts."""
    from .c_parser import CParser

    ast = CParser().parse(text, filename=filename)
    return extract_layouts(ast)
