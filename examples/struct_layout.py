# -----------------------------------------------------------------
# pycparser: struct_layout.py
#
# Generate field position tables from preprocessed C declarations.
#
# Instrument vendors often ship nothing but C declarations for the binary
# records their machines produce. This example parses a preprocessed header
# with pycparser and, given the ABI parameters of the target machine, works
# out where every field lives: byte offsets, sizes, alignments, bit-field
# positions and flexible array members.
#
# The same ABI parameters drive every decision, so nested structures,
# unions, alignment attributes and bit-fields are all interpreted
# consistently. Overlapping members are expected inside (including
# anonymous) unions; an accidental overlap between ordinary struct fields
# is reported as an error. Flexible array members are reported with their
# start offset and element stride instead of a fabricated fixed length.
# Attributes whose layout effect is not understood produce a warning that
# marks the byte offset from which later positions may be wrong.
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# -----------------------------------------------------------------
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

# This is not required if you've installed pycparser into
# your site-packages/ with setup.py
sys.path.extend([".", ".."])

from pycparser import c_ast, c_generator, parse_file
from pycparser.c_parser import CParser


class LayoutError(Exception):
    """Raised when a record cannot be laid out under the given ABI."""


# ----------------------------------------------------------------------------
# ABI parameters
# ----------------------------------------------------------------------------

# Normalized IdentifierType name tuple -> (size in bytes, alignment in bytes).
_LP64_TYPES: dict[tuple[str, ...], tuple[int, int]] = {
    ("char",): (1, 1),
    ("signed", "char"): (1, 1),
    ("unsigned", "char"): (1, 1),
    ("short",): (2, 2),
    ("short", "int"): (2, 2),
    ("signed", "short"): (2, 2),
    ("unsigned", "short"): (2, 2),
    ("unsigned", "short", "int"): (2, 2),
    ("int",): (4, 4),
    ("signed", "int"): (4, 4),
    ("unsigned",): (4, 4),
    ("unsigned", "int"): (4, 4),
    ("long",): (8, 8),
    ("long", "int"): (8, 8),
    ("unsigned", "long"): (8, 8),
    ("unsigned", "long", "int"): (8, 8),
    ("long", "long"): (8, 8),
    ("long", "long", "int"): (8, 8),
    ("unsigned", "long", "long"): (8, 8),
    ("unsigned", "long", "long", "int"): (8, 8),
    ("float",): (4, 4),
    ("double",): (8, 8),
    ("_Bool",): (1, 1),
    ("bool",): (1, 1),
    ("float", "_Complex"): (8, 4),
    ("double", "_Complex"): (16, 8),
}

_ILP32_OVERRIDES = {
    ("long",): (4, 4),
    ("long", "int"): (4, 4),
    ("unsigned", "long"): (4, 4),
    ("unsigned", "long", "int"): (4, 4),
}


@dataclass
class ABIParams:
    """Machine parameters used to interpret every declaration."""

    name: str = "custom"
    pointer_size: int = 8
    pointer_align: int = 8
    endian: str = "little"
    types: dict[tuple[str, ...], tuple[int, int]] = field(default_factory=dict)

    # Only the Itanium/ARM-style allocation-unit bit-field algorithm is
    # implemented; this is what clang and GCC use on arm64 and x86_64.
    bitfield_abi: str = "itanium"

    def primitive(self, names: tuple[str, ...]) -> tuple[int, int]:
        try:
            return self.types[names]
        except KeyError as exc:
            raise LayoutError(
                f"unknown primitive type {' '.join(names)}; "
                "extend ABIParams.types for this target"
            ) from exc

    @classmethod
    def arm64_aapcs(cls) -> ABIParams:
        types = dict(_LP64_TYPES)
        types[("long", "double")] = (8, 8)
        return cls(name="arm64-aapcs", types=types)

    @classmethod
    def x86_64_sysv(cls) -> ABIParams:
        types = dict(_LP64_TYPES)
        types[("long", "double")] = (16, 16)
        types[("long", "double", "_Complex")] = (32, 16)
        return cls(name="x86_64-sysv", types=types)

    @classmethod
    def ilp32(cls) -> ABIParams:
        types = dict(_LP64_TYPES)
        types.update(_ILP32_OVERRIDES)
        types[("long", "double")] = (12, 4)
        return cls(
            name="ilp32",
            pointer_size=4,
            pointer_align=4,
            types=types,
        )


ABI_PRESETS = {
    "arm64-aapcs": ABIParams.arm64_aapcs,
    "x86_64-sysv": ABIParams.x86_64_sysv,
    "ilp32": ABIParams.ilp32,
}


# ----------------------------------------------------------------------------
# Result objects
# ----------------------------------------------------------------------------


@dataclass
class LayoutWarning:
    """Something the tool could not interpret with full confidence."""

    offset: int
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"offset": self.offset, "message": self.message}


@dataclass
class FieldLayout:
    """One row of the field position table."""

    path: str
    type_name: str
    offset: int
    size: int
    align: int
    bit_offset: int | None = None
    bit_width: int | None = None
    storage_size: int | None = None
    fam: bool = False
    zero_length: bool = False
    element_stride: int | None = None
    element_count: int | None = None
    nested: RecordLayout | None = None
    warnings: list[LayoutWarning] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "type": self.type_name,
            "offset": self.offset,
            "size": self.size,
            "align": self.align,
        }
        if self.bit_width is not None:
            result["bit_offset"] = self.bit_offset
            result["bit_width"] = self.bit_width
            result["storage_size"] = self.storage_size
        if self.fam or self.zero_length:
            result["size"] = 0
            result["element_stride"] = self.element_stride
            result["flexible"] = self.fam
            result["zero_length"] = self.zero_length
        if self.element_count is not None:
            result["element_count"] = self.element_count
        if self.nested is not None:
            result["nested"] = self.nested.as_dict()
        if self.warnings:
            result["warnings"] = [w.as_dict() for w in self.warnings]
        return result


@dataclass
class RecordLayout:
    """The complete layout of one struct or union."""

    kind: str
    name: str
    size: int
    align: int
    fields: list[FieldLayout]
    warnings: list[LayoutWarning] = field(default_factory=list)
    packed: bool = False

    def as_dict(self) -> dict[str, object]:
        result = {
            "kind": self.kind,
            "name": self.name,
            "size": self.size,
            "align": self.align,
            "packed": self.packed,
            "fields": [f.as_dict() for f in self.fields],
        }
        if self.warnings:
            result["warnings"] = [w.as_dict() for w in self.warnings]
        return result


# ----------------------------------------------------------------------------
# Constant expression evaluation
# ----------------------------------------------------------------------------


class _ExprEvaluator:
    """Evaluates the integer constant expressions found in declarations."""

    def __init__(self, engine: LayoutEngine):
        self.engine = engine
        self.enum_constants: dict[str, int] = {}

    def evaluate(self, node: c_ast.Node) -> int:
        visitor = getattr(self, "visit_" + type(node).__name__, None)
        if visitor is None:
            raise LayoutError(
                f"cannot evaluate constant expression {type(node).__name__} "
                f"at {node.coord}"
            )
        return visitor(node)

    def visit_Constant(self, node: c_ast.Constant) -> int:
        if node.type == "int":
            return self._parse_int(node.value)
        if node.type == "char":
            return self._parse_char(node.value)
        raise LayoutError(f"non-integer constant {node.value!r} at {node.coord}")

    @staticmethod
    def _parse_int(text: str) -> int:
        clean = text.replace("'", "").lower()
        sign = 1
        if clean[:1] in ("-", "+"):
            sign = -1 if clean[0] == "-" else 1
            clean = clean[1:]
        if clean.startswith("0x"):
            value = int(clean, 16)
        elif clean.startswith("0b"):
            value = int(clean, 2)
        else:
            value = int(clean.rstrip("ul"), 0)
        return sign * value

    @staticmethod
    def _parse_char(text: str) -> int:
        body = text[1:-1]
        if not body.startswith("\\"):
            return ord(body)
        escapes = {
            "0": 0,
            "n": 10,
            "t": 9,
            "r": 13,
            "\\": 92,
            "'": 39,
            '"': 34,
            "a": 7,
            "b": 8,
            "f": 12,
            "v": 11,
        }
        if body[1:] in escapes:
            return escapes[body[1:]]
        if body[1:2] in ("x", "u", "U"):
            return int(body[2:], 16)
        if body[1:].isdigit():
            return int(body[1:], 8)
        raise LayoutError(f"unsupported character escape {text!r}")

    def visit_ID(self, node: c_ast.ID) -> int:
        if node.name in self.enum_constants:
            return self.enum_constants[node.name]
        raise LayoutError(f"unknown constant identifier {node.name!r} at {node.coord}")

    def visit_UnaryOp(self, node: c_ast.UnaryOp) -> int:
        if node.op == "sizeof":
            return self.engine.sizeof_node(node.expr)
        value = self.evaluate(node.expr)
        if node.op == "-":
            return -value
        if node.op == "+":
            return value
        if node.op == "~":
            return ~value
        if node.op == "!":
            return int(not value)
        raise LayoutError(f"unsupported unary operator {node.op!r} at {node.coord}")

    def visit_BinaryOp(self, node: c_ast.BinaryOp) -> int:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        operations = {
            "+": lambda a, b: a + b,
            "-": lambda a, b: a - b,
            "*": lambda a, b: a * b,
            "/": lambda a, b: a // b,
            "%": lambda a, b: a % b,
            "&": lambda a, b: a & b,
            "|": lambda a, b: a | b,
            "^": lambda a, b: a ^ b,
            "<<": lambda a, b: a << b,
            ">>": lambda a, b: a >> b,
            "&&": lambda a, b: int(bool(a and b)),
            "||": lambda a, b: int(bool(a or b)),
            "<": lambda a, b: int(a < b),
            "<=": lambda a, b: int(a <= b),
            ">": lambda a, b: int(a > b),
            ">=": lambda a, b: int(a >= b),
            "==": lambda a, b: int(a == b),
            "!=": lambda a, b: int(a != b),
        }
        if node.op not in operations:
            raise LayoutError(f"unsupported operator {node.op!r} at {node.coord}")
        return operations[node.op](left, right)

    def visit_TernaryOp(self, node: c_ast.TernaryOp) -> int:
        condition = self.evaluate(node.cond)
        return self.evaluate(node.iftrue if condition else node.iffalse)

    def visit_Cast(self, node: c_ast.Cast) -> int:
        return self.evaluate(node.expr)

    def visit_ExprList(self, node: c_ast.ExprList) -> int:
        return self.evaluate(node.exprs[-1])


# ----------------------------------------------------------------------------
# Layout engine
# ----------------------------------------------------------------------------

# Attributes that only affect code generation or diagnostics, never layout.
_NOOP_ATTRIBUTES = {
    (None, "deprecated"),
    (None, "maybe_unused"),
    (None, "nodiscard"),
    (None, "noreturn"),
    (None, "fallthrough"),
    (None, "likely"),
    (None, "unlikely"),
    (None, "assume"),
    ("gnu", "unused"),
    ("gnu", "used"),
    ("gnu", "deprecated"),
    ("gnu", "noinline"),
    ("gnu", "always_inline"),
    ("gnu", "may_alias"),
    ("gnu", "const"),
    ("gnu", "malloc"),
}

_PACK_PRAGMA = re.compile(r"^pack\s*(?:\((.*)\))?\s*$")


def _iter_attributes(node: c_ast.Node) -> list[c_ast.Attribute]:
    result: list[c_ast.Attribute] = []
    for specifier in getattr(node, "attributes", None) or []:
        result.extend(specifier.attributes)
    return result


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def _align_down(value: int, alignment: int) -> int:
    return value - value % alignment


def _next_power_of_two(value: int) -> int:
    return 1 << (value - 1).bit_length()


def _shift_field(field: FieldLayout, delta: int) -> FieldLayout:
    return FieldLayout(
        path=field.path,
        type_name=field.type_name,
        offset=field.offset + delta,
        size=field.size,
        align=field.align,
        bit_offset=field.bit_offset,
        bit_width=field.bit_width,
        storage_size=field.storage_size,
        fam=field.fam,
        zero_length=field.zero_length,
        element_stride=field.element_stride,
        element_count=field.element_count,
        nested=field.nested,
        warnings=list(field.warnings),
    )


class LayoutEngine:
    """Builds RecordLayout objects from a parsed translation unit."""

    def __init__(self, params: ABIParams):
        self.params = params
        self.evaluator = _ExprEvaluator(self)
        self._generator = c_generator.CGenerator()
        self._typedefs: dict[str, c_ast.Node] = {}
        self._tags: dict[tuple[str, str], c_ast.Node] = {}
        self._pragma_pack: dict[int, int | None] = {}
        self._record_cache: dict[int, RecordLayout] = {}
        self.warnings: list[LayoutWarning] = []

    # -- entry point and first pass ----------------------------------------

    def build(self, ast: c_ast.FileAST) -> list[RecordLayout]:
        self._collect_declarations(ast)
        layouts: list[RecordLayout] = []
        seen: set[int] = set()
        for node in ast.ext:
            if isinstance(node, c_ast.Decl):
                record = self._underlying_record(node.type)
                if record is None or record.decls is None:
                    continue
                layout = self._emit_record(record, node, seen)
                if layout is not None:
                    layouts.append(layout)
            elif isinstance(node, c_ast.Typedef):
                record = self._underlying_record(node.type)
                if record is None or record.decls is None:
                    continue
                layout = self._emit_record(record, None, seen)
                if layout is None:
                    continue
                # A typedef is the only handle to an otherwise anonymous
                # record; surface that name in the table.
                if record.name is None:
                    layout.name = node.name
                layouts.append(layout)
        return layouts

    def _emit_record(
        self,
        record: c_ast.Struct | c_ast.Union,
        outer_decl: c_ast.Decl | None,
        seen: set[int],
    ) -> RecordLayout | None:
        # The same record node can be reached from a typedef and a later
        # tagged declaration; emit it only once.
        if id(record) in seen:
            return None
        seen.add(id(record))
        return self.layout_record(record, outer_decl=outer_decl)

    def _collect_declarations(self, ast: c_ast.FileAST) -> None:
        pack_stack: list[int | None] = []

        def walk(node: c_ast.Node) -> None:
            if isinstance(node, c_ast.Pragma):
                self._apply_pack_pragma(node, pack_stack)
                return
            if isinstance(node, c_ast.Typedef):
                self._typedefs[node.name] = node.type
            if isinstance(node, (c_ast.Struct, c_ast.Union)):
                if node.name:
                    if node.decls is not None:
                        # A definition supersedes any earlier forward
                        # declaration stored under the same tag name.
                        self._tags[("tag", node.name)] = node
                    else:
                        self._tags.setdefault(("tag", node.name), node)
                if node.decls is not None:
                    self._pragma_pack[id(node)] = pack_stack[-1] if pack_stack else None
            if isinstance(node, c_ast.Enum):
                if node.name:
                    if node.values is not None:
                        self._tags[("enum", node.name)] = node
                    else:
                        self._tags.setdefault(("enum", node.name), node)
                self._register_enum_constants(node)
            for child in node:
                walk(child)

        for node in ast.ext:
            walk(node)

    def _apply_pack_pragma(self, node: c_ast.Pragma, stack: list[int | None]) -> None:
        match = _PACK_PRAGMA.match(node.string.strip())
        if not match:
            return
        arguments = match.group(1)
        if arguments is None or not arguments.strip():
            stack.append(None)
            return
        parts = [part.strip() for part in arguments.split(",") if part.strip()]
        if parts[0] == "pop":
            if stack:
                stack.pop()
            return
        if parts[0] == "push":
            parts = parts[1:]
        value = None
        for part in parts:
            if part.lstrip("-").isdigit():
                value = int(part)
        stack.append(value)

    def _register_enum_constants(self, node: c_ast.Enum) -> None:
        if node.values is None:
            return
        next_value = 0
        for enumerator in node.values.enumerators:
            if not isinstance(enumerator, c_ast.Enumerator):
                continue
            if enumerator.value is not None:
                next_value = self.evaluator.evaluate(enumerator.value)
            self.evaluator.enum_constants.setdefault(enumerator.name, next_value)
            next_value += 1

    @staticmethod
    def _underlying_record(node: c_ast.Node) -> c_ast.Struct | c_ast.Union | None:
        while isinstance(
            node,
            (c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl, c_ast.AttributedType),
        ):
            node = node.type
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            return node
        return None

    # -- type sizes ---------------------------------------------------------

    def _resolve_tag(
        self, node: c_ast.Struct | c_ast.Union
    ) -> c_ast.Struct | c_ast.Union:
        """Resolve a possibly forward-declared tag reference to its definition."""
        if node.decls is not None or not node.name:
            return node
        definition = self._tags.get(("tag", node.name))
        if definition is None or definition.decls is None:
            raise LayoutError(
                f"{type(node).__name__.lower()} {node.name} is used but never defined"
            )
        return definition

    def sizeof_node(self, node: c_ast.Node) -> int:
        if isinstance(node, c_ast.Typename):
            size, _, _, _ = self.resolve_type(node.type)
            return size
        # sizeof(expression): fall back to the declared type where visible.
        size, _, _, _ = self.resolve_type(node)
        return size

    def resolve_type(
        self, type_node: c_ast.Node
    ) -> tuple[int, int, str, c_ast.Node | None]:
        """Return (size, alignment, readable name, record node if any)."""
        node = type_node
        if isinstance(node, c_ast.ArrayDecl):
            element_size, element_align, _, record = self.resolve_type(node.type)
            count = self._array_dimension(node)
            return (
                element_size * count,
                element_align,
                self.type_name(type_node),
                record,
            )
        if isinstance(node, c_ast.PtrDecl):
            return (
                self.params.pointer_size,
                self.params.pointer_align,
                self.type_name(type_node),
                None,
            )
        if isinstance(node, c_ast.TypeDecl):
            return self.resolve_type(node.type)
        if isinstance(node, c_ast.AttributedType):
            return self.resolve_type(node.type)
        if isinstance(node, c_ast.IdentifierType):
            names = tuple(node.names)
            if len(names) == 1 and names[0] in self._typedefs:
                target = self._typedefs[names[0]]
                size, align, name, record = self.resolve_type(target)
                if (
                    isinstance(record, (c_ast.Struct, c_ast.Union))
                    and record.name is None
                ):
                    # typedef is the only handle to an anonymous record.
                    return (
                        size,
                        align,
                        f"{type(record).__name__.lower()} {names[0]}",
                        record,
                    )
                return size, align, name, record
            size, align = self.params.primitive(names)
            return size, align, " ".join(names), None
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            resolved = self._resolve_tag(node)
            layout = self.layout_record(resolved)
            return layout.size, layout.align, self.type_name(node), resolved
        if isinstance(node, c_ast.Enum):
            resolved = self._tags.get(("enum", node.name)) if node.name else None
            size, align = self._enum_size(resolved or node)
            return size, align, self.type_name(node), None
        if isinstance(node, c_ast.BitIntType):
            width = self.evaluator.evaluate(node.width)
            # clang rounds the storage of a bit-precise integer up to the
            # next power of two and uses that as its alignment too.
            size = _next_power_of_two(max(1, (width + 7) // 8))
            prefix = "unsigned " if node.signedness == "unsigned" else ""
            return size, size, f"{prefix}_BitInt({width})", None
        if isinstance(node, c_ast.FuncDecl):
            raise LayoutError("function-typed fields cannot be laid out as data")
        raise LayoutError(f"unsupported type node {type(node).__name__}")

    def _array_dimension(self, node: c_ast.ArrayDecl) -> int:
        if node.dim is None:
            raise LayoutError("flexible array reached through a fixed-size type")
        count = self.evaluator.evaluate(node.dim)
        if count < 0:
            raise LayoutError(f"negative array dimension at {node.coord}")
        return count

    def _enum_size(self, node: c_ast.Enum) -> tuple[int, int]:
        if node.underlying_type is not None:
            base = node.underlying_type.type
            if isinstance(base, c_ast.TypeDecl):
                base = base.type
            if isinstance(base, c_ast.IdentifierType):
                names = tuple(base.names)
                if len(names) == 1 and names[0] in self._typedefs:
                    base_size, base_align, _, _ = self.resolve_type(
                        self._typedefs[names[0]]
                    )
                    return base_size, base_align
                return self.params.primitive(names)
        values = self._enumerator_values(node)
        int_size, int_align = self.params.primitive(("int",))
        if not values:
            return int_size, int_align
        int_min = -(2 ** (int_size * 8 - 1))
        int_max = 2 ** (int_size * 8)
        if all(int_min <= value < int_max for value in values):
            return int_size, int_align
        for names in (
            ("unsigned", "int"),
            ("long",),
            ("unsigned", "long"),
            ("long", "long"),
            ("unsigned", "long", "long"),
        ):
            size, align = self.params.primitive(names)
            lower = 0 if "unsigned" in names else -(2 ** (size * 8 - 1))
            upper = 2 ** (size * 8)
            if all(lower <= value < upper for value in values):
                return size, align
        raise LayoutError(
            f"enumeration {node.name or '<anonymous>'} values fit no integer type"
        )

    def _enumerator_values(self, node: c_ast.Enum) -> list[int]:
        if node.values is None:
            return []
        values: list[int] = []
        current = 0
        for enumerator in node.values.enumerators:
            if not isinstance(enumerator, c_ast.Enumerator):
                continue
            if enumerator.value is not None:
                current = self.evaluator.evaluate(enumerator.value)
            values.append(current)
            current += 1
        return values

    def type_name(self, node: c_ast.Node) -> str:
        try:
            return self._generator.visit(node).strip()
        except Exception:
            return type(node).__name__

    # -- attributes ---------------------------------------------------------

    def _scan_attributes(
        self, nodes: list[c_ast.Node]
    ) -> tuple[bool, int | None, list[c_ast.Attribute]]:
        packed = False
        aligned: int | None = None
        unknown: list[c_ast.Attribute] = []
        for node in nodes:
            for attr in _iter_attributes(node):
                key = (attr.namespace, attr.name)
                if key == ("gnu", "packed"):
                    packed = True
                elif key == ("gnu", "aligned"):
                    aligned = max(aligned or 1, self._aligned_value(attr))
                elif key in _NOOP_ATTRIBUTES:
                    continue
                else:
                    unknown.append(attr)
            for alignas in getattr(node, "align", None) or []:
                if isinstance(alignas, c_ast.Alignas):
                    value = self.evaluator.evaluate(alignas.alignment)
                    aligned = max(aligned or 1, value)
        return packed, aligned, unknown

    def _aligned_value(self, attr: c_ast.Attribute) -> int:
        if not attr.arguments:
            # Bare aligned() asks for the maximum useful alignment.
            return self.params.primitive(("long", "long"))[1]
        text = attr.arguments.strip()
        try:
            return int(text, 0)
        except ValueError as exc:
            raise LayoutError(
                f"non-constant aligned() argument {attr.arguments!r}"
            ) from exc

    def _record_attribute_nodes(
        self, record: c_ast.Struct | c_ast.Union, outer_decl: c_ast.Decl | None
    ) -> list[c_ast.Node]:
        nodes: list[c_ast.Node] = [record]
        if outer_decl is not None:
            nodes.append(outer_decl)
            type_node = outer_decl.type
            while isinstance(type_node, c_ast.AttributedType):
                nodes.append(type_node)
                type_node = type_node.type
        return nodes

    def _field_attribute_nodes(self, decl: c_ast.Decl) -> list[c_ast.Node]:
        nodes: list[c_ast.Node] = [decl]
        type_node = decl.type
        while isinstance(type_node, (c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl)):
            nodes.append(type_node)
            type_node = type_node.type
        return nodes

    def _warn(
        self,
        unknown: list[c_ast.Attribute],
        offset: int,
        warnings: list[LayoutWarning],
    ) -> None:
        for attr in unknown:
            spelling = attr.name
            if attr.namespace:
                spelling = f"{attr.namespace}::{spelling}"
            warning = LayoutWarning(
                offset,
                f"unknown attribute [[{spelling}]]; offsets of subsequent "
                "fields may be affected",
            )
            warnings.append(warning)
            self.warnings.append(warning)

    # -- record layout ------------------------------------------------------

    def layout_record(
        self,
        record: c_ast.Struct | c_ast.Union,
        outer_decl: c_ast.Decl | None = None,
    ) -> RecordLayout:
        cached = self._record_cache.get(id(record))
        if cached is not None:
            return cached
        if record.decls is None:
            raise LayoutError(
                f"{type(record).__name__.lower()} {record.name or '<anonymous>'} "
                "is used but never defined"
            )

        packed, forced_align, unknown = self._scan_attributes(
            self._record_attribute_nodes(record, outer_decl)
        )
        pragma_pack = self._pragma_pack.get(id(record))
        pack = 1 if packed else pragma_pack

        layout = RecordLayout(
            kind=type(record).__name__.lower(),
            name=record.name or "<anonymous>",
            size=0,
            align=1,
            fields=[],
            packed=pack == 1,
        )
        self._record_cache[id(record)] = layout
        self._warn(unknown, 0, layout.warnings)

        if isinstance(record, c_ast.Union):
            self._layout_union(record, layout, pack)
        else:
            self._layout_struct(record, layout, pack)

        if forced_align is not None:
            layout.align = max(layout.align, forced_align)
        if not layout.fields or not self._has_tail_flexible(layout):
            layout.size = _align_up(layout.size, layout.align)
        return layout

    @staticmethod
    def _has_tail_flexible(layout: RecordLayout) -> bool:
        return bool(layout.fields) and (
            layout.fields[-1].fam or layout.fields[-1].zero_length
        )

    def _layout_struct(
        self,
        record: c_ast.Struct,
        layout: RecordLayout,
        pack: int | None,
    ) -> None:
        cursor = 0
        # Absolute bit position of the next packed bit-field; None when no
        # packed bit stream is currently open.
        packed_bits: int | None = None
        # Open Itanium allocation unit for ordinary bit-fields.
        unit: dict | None = None
        max_align = 1
        # (start, end, name, strict): strict ranges may never overlap one
        # another; bit-unit and anonymous-union ranges are allowed to share.
        claimed: list[tuple[int, int, str, bool]] = []
        tail_array = False

        for index, member in enumerate(record.decls):
            if not isinstance(member, c_ast.Decl):
                continue
            base = self._strip_declarators(member.type)
            if member.name is None and isinstance(base, c_ast.Enum):
                continue  # anonymous enum only introduces constants

            field_packed, field_aligned, unknown = self._scan_attributes(
                self._field_attribute_nodes(member)
            )
            member_pack = 1 if field_packed else pack
            field_warnings: list[LayoutWarning] = []
            self._warn(unknown, cursor, field_warnings)

            if tail_array:
                raise LayoutError(
                    f"flexible array member must be the last field of {layout.name}"
                )

            width = self._bit_width(member)
            if width is not None:
                cursor, packed_bits, unit, max_align = self._place_bitfield(
                    member,
                    width,
                    cursor,
                    packed_bits,
                    unit,
                    member_pack,
                    layout,
                    max_align,
                    field_warnings,
                    claimed,
                )
                continue

            # A plain field ends any open bit-field stream; the tail bytes
            # of the last allocation unit remain available for reuse.
            unit = None
            packed_bits = None

            is_fam, is_zero, array_chain = self._array_form(member)
            if is_fam or is_zero:
                stride, stride_align, _, _ = self.resolve_type(array_chain[-1].type)
                align = min(stride_align, member_pack or stride_align)
                if field_aligned is not None:
                    align = max(align, field_aligned)
                start = _align_up(cursor, align)
                layout.fields.append(
                    FieldLayout(
                        path=member.name,
                        type_name=f"{self.type_name(array_chain[-1].type)}[]",
                        offset=start,
                        size=0,
                        align=align,
                        fam=is_fam,
                        zero_length=is_zero,
                        element_stride=stride,
                        warnings=field_warnings,
                    )
                )
                cursor = start
                max_align = max(max_align, align)
                tail_array = True
                continue

            size, align, type_name, nested_record = self.resolve_type(member.type)
            effective_align = self._effective_align(align, member_pack, field_aligned)
            start = _align_up(cursor, effective_align)
            nested_layout = (
                self.layout_record(nested_record) if nested_record is not None else None
            )

            if nested_record is not None and member.name is None:
                self._promote_anonymous(
                    nested_record,
                    nested_layout,
                    start,
                    layout,
                    field_warnings,
                    claimed,
                )
            else:
                if size:
                    self._claim(claimed, start, size, member.name or type_name)
                layout.fields.append(
                    FieldLayout(
                        path=member.name or f"$anon{index}",
                        type_name=type_name,
                        offset=start,
                        size=size,
                        align=effective_align,
                        element_count=self._array_element_count(array_chain),
                        nested=nested_layout,
                        warnings=field_warnings,
                    )
                )

            cursor = start + size
            max_align = max(max_align, effective_align)

        layout.size = cursor
        layout.align = max_align

    def _layout_union(
        self,
        record: c_ast.Union,
        layout: RecordLayout,
        pack: int | None,
    ) -> None:
        max_size = 0
        max_align = 1

        for index, member in enumerate(record.decls):
            if not isinstance(member, c_ast.Decl):
                continue
            base = self._strip_declarators(member.type)
            if member.name is None and isinstance(base, c_ast.Enum):
                continue

            field_packed, field_aligned, unknown = self._scan_attributes(
                self._field_attribute_nodes(member)
            )
            member_pack = 1 if field_packed else pack
            field_warnings: list[LayoutWarning] = []
            self._warn(unknown, 0, field_warnings)

            is_fam, is_zero, array_chain = self._array_form(member)
            if is_fam:
                raise LayoutError(
                    f"flexible array member is not allowed in union {layout.name}"
                )
            width = self._bit_width(member)
            if width is not None:
                if width == 0:
                    continue
                storage_size, storage_align = self._bitfield_storage(member)
                field_align = min(storage_align, member_pack or storage_align)
                layout.fields.append(
                    self._bitfield_row(
                        member,
                        0,
                        self._bit_in_byte(0),
                        width,
                        storage_size,
                        field_align,
                        field_warnings,
                    )
                )
                max_size = max(max_size, storage_size)
                max_align = max(max_align, field_align)
                continue

            size, align, type_name, nested_record = self.resolve_type(member.type)
            effective_align = self._effective_align(align, member_pack, field_aligned)
            nested_layout = (
                self.layout_record(nested_record) if nested_record is not None else None
            )
            if nested_record is not None and member.name is None:
                self._promote_anonymous(
                    nested_record,
                    nested_layout,
                    0,
                    layout,
                    field_warnings,
                    None,
                )
            else:
                layout.fields.append(
                    FieldLayout(
                        path=member.name or f"$anon{index}",
                        type_name=type_name,
                        offset=0,
                        size=size,
                        align=effective_align,
                        element_count=self._array_element_count(array_chain),
                        nested=nested_layout,
                        warnings=field_warnings,
                    )
                )
            max_size = max(max_size, size)
            max_align = max(max_align, effective_align)

        layout.size = max_size
        layout.align = max_align

    # -- bit fields ---------------------------------------------------------

    def _place_bitfield(
        self,
        member: c_ast.Decl,
        width: int,
        cursor: int,
        packed_bits: int | None,
        unit: dict | None,
        pack: int | None,
        layout: RecordLayout,
        max_align: int,
        warnings: list[LayoutWarning],
        claimed: list[tuple[int, int, str, bool]],
    ) -> tuple[int, int | None, dict | None, int]:
        storage_size, storage_align = self._bitfield_storage(member)

        if width == 0:
            # Unnamed :0 forces the next field onto a fresh storage boundary.
            # It contributes no bytes itself and never raises the record's
            # alignment, even inside a packed struct (the boundary follows
            # the declared type, not the packing).
            boundary = _align_up(cursor, storage_align)
            return boundary, None, None, max_align

        if width > storage_size * 8:
            raise LayoutError(
                f"bit-field {member.name or '<anonymous>'} of width {width} "
                f"does not fit in {storage_size} bytes at {member.coord}"
            )

        if pack == 1:
            return self._place_packed_bitfield(
                member,
                width,
                cursor,
                packed_bits,
                storage_size,
                layout,
                max_align,
                warnings,
                claimed,
            )
        cursor, unit, max_align = self._place_itanium_bitfield(
            member,
            width,
            storage_size,
            storage_align,
            cursor,
            unit,
            layout,
            max_align,
            warnings,
            claimed,
        )
        return cursor, None, unit, max_align

    def _place_packed_bitfield(
        self,
        member,
        width,
        cursor,
        packed_bits,
        storage_size,
        layout,
        max_align,
        warnings,
        claimed,
    ) -> tuple[int, int | None, dict | None, int]:
        # Packed layout is a pure bit stream: bits are packed one after
        # another and the allocation unit grows backwards over earlier bytes.
        if packed_bits is None:
            packed_bits = cursor * 8
        start = packed_bits // 8
        bit_in_byte = self._bit_in_byte(packed_bits)
        end_bit = packed_bits + width
        end_byte = (end_bit + 7) // 8
        unit_size = max(1, end_byte - start)
        # Bit units may share bytes with earlier bit units of the same run;
        # they are still checked against ordinary strict fields.
        self._claim(
            claimed,
            start,
            unit_size,
            member.name or "<anonymous bit-field>",
            strict=False,
        )
        layout.fields.append(
            self._bitfield_row(
                member, start, bit_in_byte, width, unit_size, 1, warnings
            )
        )
        return end_byte, end_bit, None, max(max_align, 1)

    def _place_itanium_bitfield(
        self,
        member,
        width,
        storage_size,
        storage_align,
        cursor,
        unit,
        layout,
        max_align,
        warnings,
        claimed,
    ) -> tuple[int, dict | None, int]:
        if unit is not None:
            joined = self._try_join_unit(unit, width, storage_size, storage_align)
            if joined is not None:
                bit_in_unit, grown_size, grown_align = joined
                start = unit["start"]
                unit["size"] = grown_size
                unit["align"] = grown_align
                unit["used"] = bit_in_unit + width
                absolute_bit = start * 8 + bit_in_unit
                self._claim(
                    claimed,
                    start,
                    grown_size,
                    member.name or "<anonymous bit-field>",
                    strict=False,
                )
                layout.fields.append(
                    self._bitfield_row(
                        member,
                        absolute_bit // 8,
                        self._bit_in_byte(absolute_bit),
                        width,
                        grown_size,
                        grown_align,
                        warnings,
                    )
                )
                cursor = max(cursor, start + (bit_in_unit + width + 7) // 8)
                return cursor, unit, max(max_align, grown_align)

        aligned_start = _align_up(cursor, storage_align)
        fits_here = (
            cursor - _align_down(cursor, storage_align)
        ) * 8 + width <= storage_size * 8
        start = cursor if fits_here else aligned_start
        unit = {
            "start": start,
            "size": storage_size,
            "align": storage_align,
            "used": width,
        }
        self._claim(
            claimed,
            start,
            storage_size,
            member.name or "<anonymous bit-field>",
            strict=False,
        )
        layout.fields.append(
            self._bitfield_row(
                member,
                start,
                self._bit_in_byte(start * 8),
                width,
                storage_size,
                storage_align,
                warnings,
            )
        )
        return start + (width + 7) // 8, unit, max(max_align, storage_align)

    def _try_join_unit(
        self,
        unit: dict,
        width: int,
        storage_size: int,
        storage_align: int,
    ) -> tuple[int, int, int] | None:
        """Try to place a bit-field inside the open allocation unit.

        Returns (bit offset inside unit, grown unit size, grown unit
        alignment), or None when a fresh unit is required.
        """
        start = unit["start"]
        used = unit["used"]
        candidate = used
        # The field must not cross an alignment boundary of its own type.
        absolute = start * 8 + candidate
        boundary = 8 * storage_align
        if absolute // boundary != (absolute + width - 1) // boundary:
            candidate = ((absolute // boundary) + 1) * boundary - start * 8
        grown_size = max(unit["size"], storage_size)
        grown_align = max(unit["align"], storage_align)
        # Widening the unit backwards is only legal when its start stays
        # naturally aligned for the wider type.
        if storage_size > unit["size"] and start % storage_align != 0:
            return None
        if candidate + width > grown_size * 8:
            return None
        return candidate, grown_size, grown_align

    def _bitfield_row(
        self,
        member: c_ast.Decl,
        byte_offset: int,
        bit_in_byte: int,
        width: int,
        storage_size: int,
        align: int,
        warnings: list[LayoutWarning],
    ) -> FieldLayout:
        return FieldLayout(
            path=member.name or "<anonymous bit-field>",
            type_name=self.type_name(self._strip_declarators(member.type)),
            offset=byte_offset,
            size=storage_size,
            align=align,
            bit_offset=bit_in_byte,
            bit_width=width,
            storage_size=storage_size,
            warnings=warnings,
        )

    def _bitfield_storage(self, decl: c_ast.Decl) -> tuple[int, int]:
        base = self._strip_declarators(decl.type)
        if isinstance(base, c_ast.IdentifierType):
            names = tuple(base.names)
            if len(names) == 1 and names[0] in self._typedefs:
                size, align, _, _ = self.resolve_type(self._typedefs[names[0]])
                return size, align
            return self.params.primitive(names)
        if isinstance(base, c_ast.Enum):
            return self._enum_size(base)
        size, align, _, _ = self.resolve_type(base)
        return size, align

    def _bit_in_byte(self, absolute_bit: int) -> int:
        within = absolute_bit % 8
        if self.params.endian == "big":
            return 7 - within
        return within

    # -- anonymous records and overlap checking -----------------------------

    def _promote_anonymous(
        self,
        record: c_ast.Struct | c_ast.Union,
        nested_layout: RecordLayout,
        start: int,
        layout: RecordLayout,
        warnings: list[LayoutWarning],
        claimed: list[tuple[int, int, str, bool]] | None,
    ) -> None:
        allow_overlap = isinstance(record, c_ast.Union)
        for sub in nested_layout.fields:
            if claimed is not None and sub.size:
                self._claim(
                    claimed,
                    start + sub.offset,
                    sub.size,
                    sub.path,
                    strict=not allow_overlap,
                )
            promoted = _shift_field(sub, start)
            promoted.warnings = list(warnings) + promoted.warnings
            layout.fields.append(promoted)

    def _claim(
        self,
        claimed: list[tuple[int, int, str, bool]],
        start: int,
        size: int,
        name: str,
        strict: bool = True,
    ) -> None:
        if size <= 0:
            return
        end = start + size
        for previous_start, previous_end, previous_name, previous_strict in claimed:
            if not (start < previous_end and previous_start < end):
                continue
            # Two ordinary fields may never share bytes. A plain field may
            # reuse the trailing bytes of an open bit-field allocation unit,
            # but a bit-field unit must never grow over an ordinary field.
            if strict:
                if previous_strict:
                    raise LayoutError(
                        f"unexpected field overlap: {name!r} [{start}..{end}) "
                        f"overlaps {previous_name!r} "
                        f"[{previous_start}..{previous_end})"
                    )
            elif previous_strict:
                raise LayoutError(
                    f"unexpected field overlap: bit-field {name!r} "
                    f"[{start}..{end}) overlaps ordinary field {previous_name!r} "
                    f"[{previous_start}..{previous_end})"
                )
        claimed.append((start, end, name, strict))

    # -- small helpers ------------------------------------------------------

    def _bit_width(self, member: c_ast.Decl) -> int | None:
        if member.bitsize is None:
            return None
        return self.evaluator.evaluate(member.bitsize)

    @staticmethod
    def _strip_declarators(node: c_ast.Node) -> c_ast.Node:
        while isinstance(node, (c_ast.TypeDecl, c_ast.PtrDecl)):
            node = node.type
        return node

    @staticmethod
    def _array_form(member: c_ast.Decl) -> tuple[bool, bool, list[c_ast.ArrayDecl]]:
        chain: list[c_ast.ArrayDecl] = []
        node = member.type
        while isinstance(node, c_ast.ArrayDecl):
            chain.append(node)
            node = node.type
        if not chain:
            return False, False, []
        outer = chain[0]
        is_fam = outer.dim is None
        is_zero = (
            not is_fam
            and isinstance(outer.dim, c_ast.Constant)
            and outer.dim.value == "0"
        )
        return is_fam, is_zero, chain

    def _array_element_count(self, chain: list[c_ast.ArrayDecl]) -> int | None:
        if not chain:
            return None
        total = 1
        for array in chain:
            if array.dim is None:
                return None
            total *= self.evaluator.evaluate(array.dim)
        return total

    @staticmethod
    def _effective_align(align: int, pack: int | None, forced: int | None) -> int:
        effective = min(align, pack) if pack else align
        if forced is not None:
            effective = max(effective, forced)
        return effective


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------


def iter_leaf_fields(layout: RecordLayout, prefix: str = "", base: int = 0):
    """Yield (dotted path, FieldLayout with absolute offset) for each leaf.

    Named nested records are descended into with dotted paths; anonymous
    records are already promoted into their enclosing layout.
    """
    for f in layout.fields:
        path = f"{prefix}{f.path}"
        absolute = _shift_field(f, base)
        if f.nested is not None:
            yield from iter_leaf_fields(f.nested, path + ".", base + f.offset)
        else:
            yield path, absolute


def format_table(layouts: list[RecordLayout]) -> str:
    lines: list[str] = []
    for layout in layouts:
        header = f"{layout.kind} {layout.name}  size={layout.size} align={layout.align}"
        if layout.packed:
            header += "  packed"
        lines.append(header)
        rows = [("field", "offset", "size", "align", "bits", "type")]
        for path, f in iter_leaf_fields(layout):
            if f.bit_width is not None:
                bits = f"{f.bit_offset}:{f.bit_width}"
                size = str(f.storage_size)
            elif f.fam or f.zero_length:
                bits = "fam" if f.fam else "[0]"
                size = f"stride={f.element_stride}"
            else:
                bits = ""
                size = str(f.size)
            rows.append((path, str(f.offset), size, str(f.align), bits, f.type_name))
        widths = [max(len(row[col]) for row in rows) for col in range(6)]
        for row in rows:
            lines.append(
                "  " + "  ".join(row[col].ljust(widths[col]) for col in range(6))
            )
        for warning in layout.warnings:
            lines.append(f"  warning@byte {warning.offset}: {warning.message}")
        for path, f in iter_leaf_fields(layout):
            for warning in f.warnings:
                lines.append(
                    f"  warning@byte {warning.offset} ({path}): {warning.message}"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_json(layouts: list[RecordLayout], engine: LayoutEngine) -> str:
    payload = {
        "abi": engine.params.name,
        "endian": engine.params.endian,
        "pointer_size": engine.params.pointer_size,
        "records": [layout.as_dict() for layout in layouts],
        "warnings": [warning.as_dict() for warning in engine.warnings],
    }
    return json.dumps(payload, indent=2) + "\n"


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------


def parse_header(
    filename: str, use_cpp: bool = False, cpp_args: list[str] | None = None
) -> c_ast.FileAST:
    if use_cpp:
        return parse_file(filename, use_cpp=True, cpp_args=cpp_args or [])
    parser = CParser(dialect="c23")
    with open(filename, encoding="utf-8") as handle:
        return parser.parse(handle.read(), filename=filename)


def main() -> int:
    argparser = argparse.ArgumentParser(
        description="Generate field position tables from preprocessed C declarations"
    )
    argparser.add_argument(
        "filename",
        nargs="?",
        default="examples/c_files/struct_layout_example.c",
        help="preprocessed C header to analyze",
    )
    argparser.add_argument(
        "--abi",
        choices=sorted(ABI_PRESETS),
        default="arm64-aapcs",
        help="target ABI preset (default: arm64-aapcs)",
    )
    argparser.add_argument("--pointer-size", type=int, help="override pointer size")
    argparser.add_argument(
        "--endian", choices=("little", "big"), help="override byte order"
    )
    argparser.add_argument(
        "--type-size",
        action="append",
        default=[],
        metavar="NAME=BYTES",
        help="override a primitive type size (repeatable), e.g. long=4",
    )
    argparser.add_argument(
        "--type-align",
        action="append",
        default=[],
        metavar="NAME=BYTES",
        help="override a primitive type alignment (repeatable)",
    )
    argparser.add_argument(
        "--format", choices=("table", "json"), default="table", dest="output_format"
    )
    argparser.add_argument(
        "--cpp", action="store_true", help="run the C preprocessor first"
    )
    args = argparser.parse_args()

    params = ABI_PRESETS[args.abi]()
    if args.pointer_size:
        params.pointer_size = args.pointer_size
        params.pointer_align = args.pointer_size
    if args.endian:
        params.endian = args.endian
    for override in args.type_size:
        name, _, value = override.partition("=")
        key = tuple(name.split())
        current = params.types.get(key, (int(value), int(value)))
        params.types[key] = (int(value), current[1])
    for override in args.type_align:
        name, _, value = override.partition("=")
        key = tuple(name.split())
        current = params.types.get(key, (int(value), int(value)))
        params.types[key] = (current[0], int(value))

    ast = parse_header(args.filename, use_cpp=args.cpp)
    engine = LayoutEngine(params)
    try:
        layouts = engine.build(ast)
    except LayoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.output_format == "json":
        sys.stdout.write(format_json(layouts, engine))
    else:
        sys.stdout.write(format_table(layouts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
