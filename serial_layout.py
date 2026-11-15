"""Variable-length record layout for serial port archives.

Records are extracted one at a time from a serial stream.  Some arrays
inside a record have their actual element count given by a count field
read earlier in the same record, and the whole record (as well as
nested layers inside it) may be bounded by an outer length limit.

Given preprocessed C declarations, field bindings (which count field
governs which array) and the count values of the current record, this
module computes:

* :meth:`RecordLayout.min_read_position` -- the minimum byte position
  that must be read from the record start before the complete layout
  can be determined (i.e. just past the last count field on the active
  path).  Count fields living in an inactive optional branch (union
  member) are never pre-read.
* :meth:`RecordLayout.array_ranges` -- once the layout is fully
  determined, the actual byte range of every bound array.
* Length limits per layer: when a computed length crosses an outer
  record limit, :class:`ConstraintViolation` identifies the layer whose
  constraint was breached first (innermost layer first), so the reader
  stops instead of running into the next record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pycparser import c_ast, c_parser


class SerialLayoutError(Exception):
    """Base class for layout computation failures."""


class UnknownFieldError(SerialLayoutError):
    """A binding, count or limit path does not exist in the record."""


class MissingCountError(SerialLayoutError):
    """A count value needed to determine the layout was not provided."""


class BranchSelectionError(SerialLayoutError):
    """The active member of a union (optional branch) is unknown."""


class ConstraintViolation(SerialLayoutError):
    """A layer's computed length exceeded its configured limit.

    Attributes:
        layer:  dotted path of the layer whose constraint was breached
                first (``""`` denotes the outermost record).
        limit:  the configured maximum byte length of that layer.
        actual: the computed byte length of that layer.
    """

    def __init__(self, layer: str, limit: int, actual: int):
        self.layer = layer
        self.limit = limit
        self.actual = actual
        name = layer or "<record>"
        super().__init__(
            f"layer {name!r} breached first: length {actual} exceeds limit {limit}"
        )


# ---------------------------------------------------------------------------
# Type model
# ---------------------------------------------------------------------------

DEFAULT_TYPE_SIZES = {
    "_Bool": 1,
    "char": 1,
    "short": 2,
    "int": 4,
    "long": 4,
    "long long": 8,
    "float": 4,
    "double": 8,
    "long double": 16,
}

#: Typedefs injected so that declarations using fixed-width stdint names
#: are self-contained.  Disable by passing ``prelude=""``.
DEFAULT_PRELUDE = """
typedef signed char int8_t;
typedef short int16_t;
typedef int int32_t;
typedef long long int64_t;
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;
typedef unsigned int size_t;
"""


@dataclass
class ScalarType:
    name: str
    size: int


@dataclass
class ArrayType:
    elem: object
    dim: int | None  # None: flexible array member


@dataclass
class StructType:
    name: str
    members: list  # list of (member_name, type)
    is_union: bool = False


@dataclass
class ArrayRange:
    """Actual byte range of one variable-length array in this record."""

    path: str
    offset: int
    count: int
    elem_size: int

    @property
    def end(self) -> int:
        return self.offset + self.count * self.elem_size


@dataclass
class _LayoutResult:
    size: int
    count_ends: list = field(default_factory=list)  # end offsets of count fields
    arrays: dict = field(default_factory=dict)  # path -> ArrayRange
    layers: dict = field(default_factory=dict)  # struct layer path -> size


# ---------------------------------------------------------------------------
# Declaration parsing
# ---------------------------------------------------------------------------

_INT_ALIASES = {
    ("char",): "char",
    ("signed", "char"): "char",
    ("unsigned", "char"): "char",
    ("short",): "short",
    ("signed", "short"): "short",
    ("unsigned", "short"): "short",
    ("short", "int"): "short",
    ("signed", "short", "int"): "short",
    ("unsigned", "short", "int"): "short",
    ("int",): "int",
    ("signed",): "int",
    ("unsigned",): "int",
    ("signed", "int"): "int",
    ("unsigned", "int"): "int",
    ("long",): "long",
    ("signed", "long"): "long",
    ("unsigned", "long"): "long",
    ("long", "int"): "long",
    ("signed", "long", "int"): "long",
    ("unsigned", "long", "int"): "long",
    ("long", "long"): "long long",
    ("signed", "long", "long"): "long long",
    ("unsigned", "long", "long"): "long long",
    ("long", "long", "int"): "long long",
    ("signed", "long", "long", "int"): "long long",
    ("unsigned", "long", "long", "int"): "long long",
    ("float",): "float",
    ("double",): "double",
    ("long", "double"): "long double",
    ("_Bool",): "_Bool",
}


class _TypeResolver:
    """Turns pycparser AST type nodes into the internal type model."""

    def __init__(self, type_sizes):
        self.type_sizes = type_sizes
        self.typedefs = {}  # name -> AST node (type)
        self.tags = {}  # "struct X" / "union X" -> StructType (being built)

    def collect(self, ast):
        for decl in ast.ext:
            if isinstance(decl, c_ast.Typedef):
                self.typedefs[decl.name] = decl.type
            elif isinstance(decl, c_ast.Decl) and isinstance(
                decl.type, (c_ast.Struct, c_ast.Union)
            ):
                # e.g. `struct S { ... };` declaration at file scope
                self._register_composite(decl.type)

    def _register_composite(self, node):
        key = f"{'union' if isinstance(node, c_ast.Union) else 'struct'} {node.name}"
        if key not in self.tags and node.decls is not None:
            self.tags[key] = None  # guard against infinite recursion
            self.tags[key] = self._build_composite(node)

    def _build_composite(self, node):
        members = []
        for decl in node.decls or []:
            if decl.name is None:
                raise SerialLayoutError(
                    "anonymous struct/union members are not supported"
                )
            if decl.bitsize is not None:
                raise SerialLayoutError(
                    f"bitfield {decl.name!r} is not supported in serial records"
                )
            members.append((decl.name, self.resolve(decl.type)))
        kind = "union" if isinstance(node, c_ast.Union) else "struct"
        return StructType(f"{kind} {node.name or '<anon>'}", members,
                          is_union=isinstance(node, c_ast.Union))

    def resolve(self, node):
        if isinstance(node, c_ast.TypeDecl):
            return self.resolve(node.type)
        if isinstance(node, c_ast.IdentifierType):
            names = tuple(node.names)
            if len(names) == 1 and names[0] in self.typedefs:
                return self.resolve(self.typedefs[names[0]])
            canonical = _INT_ALIASES.get(names)
            if canonical is None:
                raise SerialLayoutError(f"unknown type: {' '.join(names)!r}")
            return ScalarType(canonical, self.type_sizes[canonical])
        if isinstance(node, c_ast.ArrayDecl):
            elem = self.resolve(node.type)
            if node.dim is None:
                return ArrayType(elem, None)
            if not isinstance(node.dim, c_ast.Constant) or node.dim.type != "int":
                raise SerialLayoutError(
                    "array dimensions must be integer constants; bind "
                    "variable arrays to a count field instead"
                )
            return ArrayType(elem, int(node.dim.value, 0))
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            kind = "union" if isinstance(node, c_ast.Union) else "struct"
            if node.name is not None:
                key = f"{kind} {node.name}"
                if node.decls is not None:
                    self._register_composite(node)
                if self.tags.get(key) is None:
                    raise SerialLayoutError(f"undefined composite type: {key}")
                return self.tags[key]
            if node.decls is None:
                raise SerialLayoutError(
                    "anonymous struct/union without a body"
                )
            return self._build_composite(node)
        if isinstance(node, c_ast.Enum):
            return ScalarType("int", self.type_sizes["int"])
        if isinstance(node, c_ast.PtrDecl):
            raise SerialLayoutError(
                "pointers have no meaning in a serialised record"
            )
        raise SerialLayoutError(f"unsupported declaration node: {type(node).__name__}")


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

class RecordLayout:
    """Computes dynamic layouts of one record type.

    Args:
        source:     preprocessed C declarations (self-contained).
        root:       record type name -- a typedef name or ``"struct Tag"``.
        type_sizes: overrides for :data:`DEFAULT_TYPE_SIZES`.
        packed:     when True (default) no padding is inserted, matching
                    a byte-stream archive; when False natural alignment
                    is applied.
        prelude:    extra declarations prepended to ``source``
                    (defaults to common fixed-width typedefs).
    """

    def __init__(self, source, root, *, type_sizes=None, packed=True,
                 prelude=DEFAULT_PRELUDE):
        self.packed = packed
        sizes = dict(DEFAULT_TYPE_SIZES)
        if type_sizes:
            sizes.update(type_sizes)
        resolver = _TypeResolver(sizes)
        parser = c_parser.CParser()
        ast = parser.parse((prelude or "") + source, filename="<decls>")
        resolver.collect(ast)
        if root in resolver.typedefs:
            self.root_type = resolver.resolve(resolver.typedefs[root])
        elif f"struct {root}" in resolver.tags:
            self.root_type = resolver.tags[f"struct {root}"]
        else:
            raise SerialLayoutError(f"record type not found: {root!r}")
        if not isinstance(self.root_type, StructType) or self.root_type.is_union:
            raise SerialLayoutError(f"record type {root!r} is not a struct")

    # -- alignment helpers -------------------------------------------------

    def _align_of(self, typ):
        if self.packed:
            return 1
        if isinstance(typ, ScalarType):
            return typ.size
        if isinstance(typ, ArrayType):
            return self._align_of(typ.elem)
        if typ.is_union:
            return max((self._align_of(t) for _, t in typ.members), default=1)
        return max((self._align_of(t) for _, t in typ.members), default=1)

    @staticmethod
    def _round_up(offset, align):
        return (offset + align - 1) // align * align if align > 1 else offset

    # -- static size (no variable parts) ------------------------------------

    def _static_size(self, typ):
        if isinstance(typ, ScalarType):
            return typ.size
        if isinstance(typ, ArrayType):
            if typ.dim is None:
                raise SerialLayoutError("flexible array has no static size")
            return self._static_size(typ.elem) * typ.dim
        if typ.is_union:
            align = self._align_of(typ)
            size = max((self._static_size(t) for _, t in typ.members), default=0)
            return self._round_up(size, align)
        align = self._align_of(typ)
        offset = 0
        for _, member_type in typ.members:
            offset = self._round_up(offset, self._align_of(member_type))
            offset += self._static_size(member_type)
        return self._round_up(offset, align)

    def _has_variable_part(self, typ, bound_paths, prefix):
        if isinstance(typ, ArrayType):
            return prefix in bound_paths or (
                typ.dim is not None
                and self._has_variable_part(typ.elem, bound_paths, prefix)
            )
        if isinstance(typ, StructType):
            return any(
                self._has_variable_part(t, bound_paths, f"{prefix}.{n}")
                for n, t in typ.members
            )
        return False

    # -- public API ----------------------------------------------------------

    def min_read_position(self, bindings, counts, branches=None):
        """Minimum byte position that must be read to pin down the layout.

        Returns the offset just past the last count field on the active
        path.  Count fields in inactive union branches are ignored, so
        the reader is never asked to pre-read the other branch.  Returns
        0 when the layout is fully static.
        """
        result = self._layout(bindings, counts, branches)
        return max(result.count_ends, default=0)

    def record_length(self, bindings, counts, branches=None):
        """Total byte length of this record once counts are known."""
        return self._layout(bindings, counts, branches).size

    def array_ranges(self, bindings, counts, branches=None, limits=None):
        """Actual range of every bound array, after full layout is known.

        Args:
            limits: optional mapping of layer path (``""`` for the whole
                record) to its maximum byte length.  Layers are checked
                innermost-first; the first breach raises
                :class:`ConstraintViolation` so nothing is read into the
                next record.

        Returns:
            dict mapping each bound array path to an :class:`ArrayRange`.
        """
        result = self._layout(bindings, counts, branches)
        if limits:
            layers = dict(result.layers)
            layers[""] = result.size
            for path in limits:
                if path not in layers:
                    raise UnknownFieldError(
                        f"limit refers to unknown layer {path!r}"
                    )
            depth = lambda p: -1 if p == "" else p.count(".")
            for path in sorted(layers, key=depth, reverse=True):
                if path in limits and layers[path] > limits[path]:
                    raise ConstraintViolation(path, limits[path], layers[path])
        return result.arrays

    # -- core layout walk ----------------------------------------------------

    def _layout(self, bindings, counts, branches):
        bindings = dict(bindings)
        counts = dict(counts)
        branches = dict(branches or {})
        self._validate_paths(bindings, counts, branches)
        result = self._layout_struct(self.root_type, 0, "", bindings, counts,
                                     branches)
        return result

    def _validate_paths(self, bindings, counts, branches):
        for path in list(bindings) + list(counts) + list(branches):
            self._resolve_path(path)
        for array_path in bindings:
            typ = self._resolve_path(array_path)
            if not isinstance(typ, ArrayType):
                raise UnknownFieldError(
                    f"binding target {array_path!r} is not an array"
                )

    def _resolve_path(self, path):
        typ = self.root_type
        for component in path.split("."):
            if not isinstance(typ, StructType):
                raise UnknownFieldError(f"path {path!r} crosses a non-struct")
            members = dict(typ.members)
            if component not in members:
                raise UnknownFieldError(
                    f"no member {component!r} in path {path!r}"
                )
            typ = members[component]
        return typ

    def _active_branch(self, typ, path, bindings, counts, branches):
        """Pick the active member of a union without pre-reading others."""
        if path in branches:
            chosen = branches[path]
        else:
            referenced = set()
            prefix = f"{path}."
            for candidate in list(bindings) + list(bindings.values()) + list(counts):
                if candidate.startswith(prefix):
                    referenced.add(candidate[len(prefix):].split(".")[0])
            members = {name for name, _ in typ.members}
            referenced &= members
            variable = {
                n for n, t in typ.members
                if self._has_variable_part(t, set(bindings), f"{path}.{n}")
            }
            if len(referenced) == 1:
                chosen = referenced.pop()
            elif len(referenced) > 1:
                raise BranchSelectionError(
                    f"union at {path!r} is referenced through multiple "
                    f"branches: {sorted(referenced)}"
                )
            elif variable:
                raise BranchSelectionError(
                    f"union at {path!r} contains variable arrays; pass "
                    f"branches={{{path!r}: <member>}} to select one"
                )
            else:
                return None  # fixed-size union: static size, no branch read
        members = dict(typ.members)
        if chosen not in members:
            raise BranchSelectionError(
                f"branch {chosen!r} is not a member of union at {path!r}"
            )
        return chosen

    def _layout_struct(self, typ, base, path, bindings, counts, branches):
        result = _LayoutResult(size=0)
        offset = base
        for name, member_type in typ.members:
            member_path = f"{path}.{name}" if path else name
            offset = self._round_up(offset, self._align_of(member_type))
            sub = self._layout_member(member_type, offset, member_path,
                                      bindings, counts, branches)
            result.count_ends.extend(sub.count_ends)
            result.arrays.update(sub.arrays)
            result.layers.update(sub.layers)
            offset += sub.size
        size = self._round_up(offset - base, self._align_of(typ))
        result.size = size
        result.layers[path] = size
        return result

    def _layout_member(self, typ, offset, path, bindings, counts, branches):
        if isinstance(typ, ScalarType):
            result = _LayoutResult(size=typ.size)
            if path in bindings.values():
                result.count_ends.append(offset + typ.size)
            return result
        if isinstance(typ, ArrayType):
            return self._layout_array(typ, offset, path, bindings, counts,
                                      branches)
        if typ.is_union:
            chosen = self._active_branch(typ, path, bindings, counts, branches)
            if chosen is None:
                return _LayoutResult(size=self._static_size(typ))
            member_type = dict(typ.members)[chosen]
            sub = self._layout_member(member_type, offset, f"{path}.{chosen}",
                                      bindings, counts, branches)
            sub.size = self._round_up(sub.size, self._align_of(typ))
            return sub
        return self._layout_struct(typ, offset, path, bindings, counts,
                                   branches)

    def _layout_array(self, typ, offset, path, bindings, counts, branches):
        elem_size = self._static_size(typ.elem)
        if path in bindings:
            count_path = bindings[path]
            if count_path not in counts:
                raise MissingCountError(
                    f"count {count_path!r} for array {path!r} was not read"
                )
            count = counts[count_path]
            if not isinstance(count, int) or count < 0:
                raise SerialLayoutError(
                    f"count {count_path!r} must be a non-negative int, "
                    f"got {count!r}"
                )
            if typ.dim is not None and count > typ.dim:
                raise ConstraintViolation(path, typ.dim, count)
            result = _LayoutResult(size=elem_size * count)
            result.arrays[path] = ArrayRange(path, offset, count, elem_size)
            return result
        if typ.dim is None:
            raise SerialLayoutError(
                f"flexible array {path!r} has no count binding"
            )
        return _LayoutResult(size=elem_size * typ.dim)
