# ------------------------------------------------------------------------------
# pycparser: declaration_catalog.py
#
# A catalog of the declarations found in a parsed translation unit.
#
# A collection plugin receives one concatenated, preprocessed file whose
# #line markers keep switching the logical (vendor source) location. The same
# vendor file name and line number can therefore appear many times, and many
# declarations can be packed onto a single logical line.
#
# Each catalog entry therefore keeps both:
# - the logical location decoded from the #line markers (where the vendor
#   claims the declaration lives), and
# - the physical location in the concatenated input the parser actually read.
#
# The catalog supports reverse lookup: given a logical location it returns
# every expanded instance found there. When several declarations share one
# logical line they are distinguished by their owning type and field path.
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# ------------------------------------------------------------------------------
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from . import c_ast
from .c_parser import Coord


@dataclass(frozen=True, slots=True)
class LogicalLocation:
    """A vendor source location decoded from #line markers.

    Only the file and line are identity keys: several declarations and several
    expanded instances can legitimately share the same logical line.
    """

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True, slots=True)
class PhysicalLocation:
    """Where an element physically sits in the concatenated input.

    This is the input handed to the parser, not any of the vendor files named
    by #line markers. The file is the physical input name (often empty when
    text was parsed directly), and line/column count raw input lines.
    """

    file: str
    line: int
    column: int | None = None

    def __str__(self) -> str:
        text = f"{self.file}:{self.line}"
        if self.column is not None:
            text += f":{self.column}"
        return text


@dataclass(frozen=True, slots=True)
class DeclarationEntry:
    """One declaration occurrence recorded in the catalog.

    node:
        The AST node for the declaration (Decl, Typedef, FuncDef, ...).
    kind:
        The owning declaration type, e.g. "Decl", "Typedef", "FuncDef",
        "Enumerator". This disambiguates entries packed on one logical line.
    field_path:
        The dotted path of the declared field from the enclosing type, e.g.
        "config.flags" or the plain name for a translation-unit declaration.
    logical:
        The vendor source location from #line markers.
    physical:
        The location inside the concatenated, preprocessed input.
    """

    node: c_ast.Node
    kind: str
    field_path: str
    logical: LogicalLocation
    physical: PhysicalLocation

    @property
    def name(self) -> str | None:
        """The declared name, or None for anonymous declarations."""
        node = self.node
        if isinstance(node, c_ast.FuncDef):
            return node.decl.name if node.decl is not None else None
        return getattr(node, "name", None)


class DeclarationCatalog:
    """Collection of declaration occurrences with dual coordinates.

    Build one with :meth:`from_ast`. Entries can be enumerated directly or
    reverse-searched by the logical vendor location with
    :meth:`instances_at` / :meth:`lookup`.
    """

    def __init__(self) -> None:
        self._entries: list[DeclarationEntry] = []
        self._by_logical: dict[LogicalLocation, list[DeclarationEntry]] = {}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_ast(cls, ast: c_ast.FileAST) -> "DeclarationCatalog":
        catalog = cls()
        collector = _DeclarationCollector(catalog)
        collector.visit(ast)
        return catalog

    def _add(
        self,
        node: c_ast.Node,
        kind: str,
        field_path: str,
        coord: Coord | None,
    ) -> None:
        if coord is None:
            # Without coordinates the entry cannot be located; skip it rather
            # than fabricating a location.
            return
        logical = LogicalLocation(file=coord.file, line=coord.line)
        physical = PhysicalLocation(
            file=coord.phys_file if coord.phys_file is not None else "",
            line=coord.phys_line if coord.phys_line is not None else coord.line,
            column=coord.phys_column,
        )
        entry = DeclarationEntry(
            node=node,
            kind=kind,
            field_path=field_path,
            logical=logical,
            physical=physical,
        )
        self._entries.append(entry)
        self._by_logical.setdefault(logical, []).append(entry)

    # ------------------------------------------------------------------
    # Access and reverse lookup
    # ------------------------------------------------------------------
    def __iter__(self) -> Iterator[DeclarationEntry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[DeclarationEntry, ...]:
        return tuple(self._entries)

    def logical_locations(self) -> Iterable[LogicalLocation]:
        """All distinct logical locations that have catalog entries."""
        return self._by_logical.keys()

    def instances_at(self, file: str, line: int) -> tuple[DeclarationEntry, ...]:
        """Return every expanded instance at one logical vendor location.

        Multiple entries are returned when the same vendor line was expanded
        into the concatenated input more than once (repeated #line markers),
        or when several declarations were packed onto the same logical line.
        """
        return tuple(self._by_logical.get(LogicalLocation(file=file, line=line), ()))

    def lookup(
        self, file: str, line: int, *, name: str | None = None
    ) -> tuple[DeclarationEntry, ...]:
        """Reverse lookup with an optional declared-name filter.

        When a logical line holds several declarations, pass name (or combine
        with the entry's kind/field_path) to select a specific field. Entries
        sharing the logical line but differing in owning type and field path
        are all returned when name is omitted.
        """
        entries = self.instances_at(file, line)
        if name is None:
            return entries
        return tuple(entry for entry in entries if entry.name == name)


class _DeclarationCollector(c_ast.NodeVisitor):
    """Walks an AST and feeds named declarations into a catalog.

    The visitor tracks the enclosing aggregate (struct/union/enum) so each
    member gets a dotted field path. Translation-unit declarations use their
    bare name. Function definitions are recorded as a single entry and are not
    descended into, so locals and the nested declarator do not create
    duplicate or synthetic field paths.
    """

    def __init__(self, catalog: DeclarationCatalog) -> None:
        self._catalog = catalog
        # Path components of named scopes currently entered.
        self._scope_path: list[str] = []

    def _qualified(self, name: str) -> str:
        if self._scope_path:
            return ".".join([*self._scope_path, name])
        return name

    def _record(self, node: c_ast.Node, name: str | None) -> None:
        if not name:
            # Anonymous/unnamed declarations have no addressable field path.
            # Skip them rather than inventing a name or a macro expansion
            # stack; their named members are still collected on recursion.
            return
        self._catalog._add(
            node=node,
            kind=type(node).__name__,
            field_path=self._qualified(name),
            coord=getattr(node, "coord", None),
        )

    # Top-level / general named declarations.
    def visit_Decl(self, node: c_ast.Decl) -> None:
        self._record(node, node.name)
        self.generic_visit(node)

    def visit_Typedef(self, node: c_ast.Typedef) -> None:
        self._record(node, node.name)
        self.generic_visit(node)

    def visit_FuncDef(self, node: c_ast.FuncDef) -> None:
        name = node.decl.name if node.decl is not None else None
        self._record(node, name)
        # A function definition already represents the function as one entry.
        # Do not descend into its body/parameters: local variables and the
        # nested declarator are not vendor field declarations, and descending
        # would duplicate the function under a synthetic path.

    def visit_Enumerator(self, node: c_ast.Enumerator) -> None:
        self._record(node, node.name)
        self.generic_visit(node)

    # Aggregates: prefix member field paths with the aggregate name.
    def _visit_aggregate(self, node: c_ast.Struct | c_ast.Union) -> None:
        name = node.name
        if name:
            self._scope_path.append(name)
        self.generic_visit(node)
        if name:
            self._scope_path.pop()

    def visit_Struct(self, node: c_ast.Struct) -> None:
        self._visit_aggregate(node)

    def visit_Union(self, node: c_ast.Union) -> None:
        self._visit_aggregate(node)

    def visit_Enum(self, node: c_ast.Enum) -> None:
        name = node.name
        if name:
            self._scope_path.append(name)
        self.generic_visit(node)
        if name:
            self._scope_path.pop()
