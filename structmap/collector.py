"""Scoped extraction of struct/union layouts from preprocessed C sources.

The public entry point is :func:`extract_module`. Collection runs in two
phases:

1. An indexing walk builds lexical scopes (file / function / block) mapping
   typedef spellings and struct tags to their declaring AST nodes, together
   with the scope chain visible at each definition site.
2. Records are built lazily. Every alias is resolved through the saved scope
   chain into a closed type descriptor, so downstream comparisons never rely
   on type spelling. Block-scope typedefs that shadow an outer alias are
   resolved with the inner meaning for fields collected in that block.
"""

from dataclasses import dataclass, field

from pycparser import c_ast, parse_file


@dataclass(frozen=True)
class Prim:
    names: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"type": "prim", "names": list(self.names)}


@dataclass(frozen=True)
class RecordRef:
    rid: str
    kind: str

    def to_dict(self) -> dict:
        return {"type": "record", "rid": self.rid, "kind": self.kind}


@dataclass(frozen=True)
class Ptr:
    pointee: object

    def to_dict(self) -> dict:
        return {"type": "ptr", "to": _descriptor_dict(self.pointee)}


@dataclass(frozen=True)
class Array:
    element: object
    dim: int | None

    def to_dict(self) -> dict:
        return {
            "type": "array",
            "dim": self.dim,
            "element": _descriptor_dict(self.element),
        }


@dataclass(frozen=True)
class FuncPtr:
    def to_dict(self) -> dict:
        return {"type": "funcptr"}


@dataclass(frozen=True)
class Unknown:
    spelling: str

    def to_dict(self) -> dict:
        return {"type": "unknown", "spelling": self.spelling}


TypeDescriptor = Prim | RecordRef | Ptr | Array | FuncPtr | Unknown


def _descriptor_dict(desc: TypeDescriptor) -> dict:
    return desc.to_dict()


@dataclass
class FieldInfo:
    name: str
    descriptor: TypeDescriptor
    bitsize: int | None
    coord: str | None


@dataclass
class Record:
    rid: str
    kind: str
    tag: str | None
    fields: list[FieldInfo] = field(default_factory=list)
    coord: str | None = None


@dataclass
class AliasInfo:
    name: str
    descriptor: TypeDescriptor
    coord: str | None


@dataclass
class ModuleSchema:
    source: str
    records: dict[str, Record]
    aliases: dict[str, AliasInfo]
    order: list[str]


class _Scope:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.typedefs: dict[str, c_ast.Typedef] = {}
        self.tags: dict[str, c_ast.Struct | c_ast.Union] = {}


class _Collector:
    def __init__(self, source: str) -> None:
        self.source = source
        self._stack: list[_Scope] = [_Scope("file")]
        self._all_scopes: list[_Scope] = list(self._stack)
        self._node_chain: dict[int, tuple[_Scope, ...]] = {}
        self._preferred_names: dict[int, str] = {}
        self.records: dict[str, Record] = {}
        self._node_rids: dict[int, str] = {}
        self._used_rids: set[str] = set()
        self.aliases: dict[str, AliasInfo] = {}
        self.order: list[str] = []

    def collect(self, ast: c_ast.FileAST) -> ModuleSchema:
        self._index(ast)
        for scope in self._all_scopes:
            for node in scope.tags.values():
                if node.decls is not None:
                    self._build_record(node)
            for typedef in scope.typedefs.values():
                core = _chain_core(typedef.type)
                if (
                    isinstance(core, (c_ast.Struct, c_ast.Union))
                    and core.decls is not None
                ):
                    self._build_record(core)
        for typedef in self._all_scopes[0].typedefs.values():
            self._build_alias(typedef)
        return ModuleSchema(self.source, self.records, self.aliases, list(self.order))

    # ------------------------------------------------------------------
    # Phase 1: scope-aware index of typedefs and struct/union tags.
    # ------------------------------------------------------------------

    def _index(self, node: c_ast.Node) -> None:
        if isinstance(node, c_ast.FuncDef):
            scope = _Scope("func")
            self._stack.append(scope)
            self._all_scopes.append(scope)
            func_decl = node.decl.type
            if isinstance(func_decl, c_ast.FuncDecl) and isinstance(
                func_decl.args, c_ast.ParamList
            ):
                for param in func_decl.args.params:
                    self._index(param)
            self._index(node.body)
            self._stack.pop()
            return
        if isinstance(node, c_ast.Compound):
            self._stack.append(_Scope("block"))
            self._all_scopes.append(self._stack[-1])
            for item in node.block_items or []:
                self._index(item)
            self._stack.pop()
            return
        if isinstance(node, c_ast.Typedef):
            scope = self._stack[-1]
            scope.typedefs[node.name] = node
            self._node_chain[id(node)] = tuple(self._stack)
            core = _chain_core(node.type)
            if isinstance(core, (c_ast.Struct, c_ast.Union)):
                self._preferred_names.setdefault(id(core), node.name)
                if core.name is not None:
                    self._register_tag(core)
        if isinstance(node, c_ast.Decl):
            core = _chain_core(node.type)
            if isinstance(core, (c_ast.Struct, c_ast.Union)) and core.name is not None:
                self._register_tag(core)
        for _, child in node.children() or []:
            self._index(child)

    def _register_tag(self, node: c_ast.Struct | c_ast.Union) -> None:
        scope = self._stack[-1]
        existing = scope.tags.get(node.name)
        if existing is None or (existing.decls is None and node.decls is not None):
            scope.tags[node.name] = node
        self._node_chain.setdefault(id(node), tuple(self._stack))

    # ------------------------------------------------------------------
    # Phase 2: build closed descriptors using the saved scope chains.
    # ------------------------------------------------------------------

    def _build_alias(self, node: c_ast.Typedef) -> None:
        chain = self._node_chain[id(node)]
        descriptor = self._describe(node.type, chain)
        self.aliases[node.name] = AliasInfo(
            node.name, descriptor, str(node.coord) if node.coord else None
        )

    def _build_record(self, node: c_ast.Struct | c_ast.Union) -> str:
        existing = self._node_rids.get(id(node))
        if existing is not None:
            return existing
        kind = "union" if isinstance(node, c_ast.Union) else "struct"
        rid = self._mint_rid(node)
        record = Record(
            rid=rid,
            kind=kind,
            tag=node.name,
            fields=[],
            coord=str(node.coord) if node.coord else None,
        )
        self._node_rids[id(node)] = rid
        self.records[rid] = record
        self.order.append(rid)
        chain = self._node_chain.get(id(node), tuple(self._stack))
        for member in node.decls or []:
            if not isinstance(member, c_ast.Decl):
                continue
            descriptor = self._describe(member.type, chain)
            bitsize = None
            if isinstance(member.bitsize, c_ast.Constant):
                bitsize = int(member.bitsize.value)
            record.fields.append(
                FieldInfo(
                    name=member.name or "",
                    descriptor=descriptor,
                    bitsize=bitsize,
                    coord=str(member.coord) if member.coord else None,
                )
            )
        return rid

    def _mint_rid(self, node: c_ast.Struct | c_ast.Union) -> str:
        base = node.name or self._preferred_names.get(id(node))
        if base is None:
            base = (
                f"anon@{node.coord.line}"
                if node.coord
                else f"anon#{len(self.order) + 1}"
            )
        rid = base
        suffix = 2
        while rid in self._used_rids:
            rid = f"{base}#{suffix}"
            suffix += 1
        self._used_rids.add(rid)
        return rid

    def _resolve_tag(
        self, name: str, chain: tuple[_Scope, ...]
    ) -> c_ast.Struct | c_ast.Union | None:
        for scope in reversed(chain):
            if name in scope.tags:
                return scope.tags[name]
        return None

    def _resolve_typedef(
        self, name: str, chain: tuple[_Scope, ...]
    ) -> c_ast.Typedef | None:
        for scope in reversed(chain):
            if name in scope.typedefs:
                return scope.typedefs[name]
        return None

    def _describe(
        self, chain_node: c_ast.Node, chain: tuple[_Scope, ...]
    ) -> TypeDescriptor:
        node = chain_node
        pointer_depth = 0
        arrays: list[int | None] = []
        while isinstance(node, (c_ast.PtrDecl, c_ast.ArrayDecl)):
            if isinstance(node, c_ast.PtrDecl):
                pointer_depth += 1
                node = node.type
            else:
                arrays.append(_array_dim(node))
                node = node.type
        if isinstance(node, c_ast.FuncDecl):
            descriptor: TypeDescriptor = FuncPtr()
        else:
            descriptor = self._describe_core(node, chain)
        for dim in reversed(arrays):
            descriptor = Array(descriptor, dim)
        for _ in range(pointer_depth):
            descriptor = Ptr(descriptor)
        return descriptor

    def _describe_core(
        self, node: c_ast.Node, chain: tuple[_Scope, ...]
    ) -> TypeDescriptor:
        if isinstance(node, c_ast.TypeDecl):
            return self._describe_core(node.type, chain)
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            kind = "union" if isinstance(node, c_ast.Union) else "struct"
            target = node
            if node.decls is None and node.name is not None:
                resolved = self._resolve_tag(node.name, chain)
                if resolved is None:
                    return Unknown(f"{kind} {node.name}")
                target = resolved
            if target.decls is None:
                return Unknown(f"{kind} {target.name}".strip())
            rid = self._build_record(target)
            return RecordRef(rid, kind)
        if isinstance(node, c_ast.Enum):
            label = node.name or "anon"
            return Prim(("enum", label))
        if isinstance(node, c_ast.IdentifierType):
            if len(node.names) == 1:
                typedef = self._resolve_typedef(node.names[0], chain)
                if typedef is not None:
                    return self._describe(typedef.type, self._node_chain[id(typedef)])
            return Prim(tuple(node.names))
        return Unknown(type(node).__name__)


def _chain_core(node: c_ast.Node) -> c_ast.Node:
    while isinstance(node, (c_ast.PtrDecl, c_ast.ArrayDecl, c_ast.TypeDecl)):
        node = node.type
    return node


def _array_dim(node: c_ast.ArrayDecl) -> int | None:
    if isinstance(node.dim, c_ast.Constant):
        try:
            return int(node.dim.value)
        except ValueError:
            return None
    return None


def extract_module(path: str, *, use_cpp: bool = False) -> ModuleSchema:
    """Parse a (preprocessed) C source and collect its record layouts."""
    ast = parse_file(path, use_cpp=use_cpp)
    return _Collector(path).collect(ast)
