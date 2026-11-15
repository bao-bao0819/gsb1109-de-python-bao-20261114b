# -----------------------------------------------------------------
# pycparser: enum_diff.py
#
# Compare enum declarations from two preprocessed C headers.
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# -----------------------------------------------------------------
"""Evaluate enum constants and build an "acquisition dictionary" diff.

Event-code enums are part of a wire protocol even though they are written as
ordinary C declarations.  Upgrades often reorder members, add aliases, or
retire an old number.  A textual diff cannot distinguish those cases from a
real protocol break, so this module evaluates every enumerator and compares
the resulting ``name -> integer`` dictionaries.

The evaluator follows the C rules that matter for these declarations:

* An explicit expression may use any enumerator declared before it (including
  an enumerator from an earlier enum in the same translation unit).
* An enumerator without ``=`` is one greater than the preceding enumerator.
  The first implicit enumerator is zero.
* Integer constant expressions support the usual arithmetic, bitwise, logical,
  ternary, cast, and ``sizeof`` forms.

Alias handling is deliberately order-independent.  Several names may share a
number; unless the caller supplies a primary name, the complete set of names
is kept for that value and merely reordering them in the header is not a
difference at all.  When a primary name is supplied (or a header carries a
``#pragma primary NAME`` marker), it identifies the value; alias additions
and removals remain visible but do not by themselves make a dictionary
incompatible.

The module can be used as a library::

    from pycparser.enum_diff import diff_files
    result = diff_files("events-old.h", "events-new.h")
    print(result.as_dict())

or from the command line::

    python -m pycparser.enum_diff events-old.h events-new.h
    python -m pycparser.enum_diff --format json old.h new.h
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import c_ast, c_parser

__all__ = [
    "EnumDiffError",
    "EnumEvaluationError",
    "EnumMember",
    "EnumSet",
    "GroupChange",
    "ValueChange",
    "EnumDiff",
    "AcquisitionDiff",
    "evaluate_enums",
    "diff_enums",
    "diff_files",
    "main",
]


class EnumDiffError(ValueError):
    """Base class for errors raised while evaluating or diffing enums."""


class EnumEvaluationError(EnumDiffError):
    """An enumerator value cannot be evaluated."""


@dataclass(frozen=True)
class EnumMember:
    """One source-level enumerator, before aliases are grouped."""

    name: str
    value: int
    explicit: bool
    file: str
    line: int
    column: int


@dataclass(frozen=True)
class EnumSet:
    """The evaluated members of one enum declaration, in source order."""

    tag: str
    file: str
    members: tuple[EnumMember, ...] = field(default_factory=tuple)
    primary: tuple[tuple[int, str], ...] = field(default_factory=tuple)

    @property
    def values(self) -> dict[str, int]:
        return {member.name: member.value for member in self.members}

    @property
    def groups(self) -> dict[int, tuple[str, ...]]:
        """Map each value to all names carrying it, in source order."""
        groups: dict[int, list[str]] = {}
        for member in self.members:
            groups.setdefault(member.value, []).append(member.name)
        return {value: tuple(names) for value, names in groups.items()}

    def primary_for(self, value: int) -> str | None:
        for candidate_value, name in self.primary:
            if candidate_value == value:
                return name
        return None

    def aliases_of(self, name: str) -> tuple[str, ...]:
        """Return every name sharing *name*'s wire value."""
        value = self.values[name]
        return self.groups[value]

    def as_dict(self) -> dict[str, object]:
        """The evaluated dictionary, retaining complete alias groups."""
        return {
            "tag": self.tag,
            "file": self.file,
            "members": [
                {
                    "name": member.name,
                    "value": member.value,
                    "explicit": member.explicit,
                    "file": member.file,
                    "line": member.line,
                    "column": member.column,
                }
                for member in self.members
            ],
            "values": dict(self.values),
            "groups": [
                {
                    "value": value,
                    "names": list(names),
                    "primary": self.primary_for(value),
                }
                for value, names in self.groups.items()
            ],
        }


@dataclass(frozen=True)
class ValueChange:
    """A name that is present on both sides with different values."""

    name: str
    old: int
    new: int
    old_file: str = ""
    old_line: int = 0
    new_file: str = ""
    new_line: int = 0


@dataclass(frozen=True)
class GroupChange:
    """A numeric value whose set (or primary name) changed but value did not.

    ``kind`` distinguishes the two non-breaking cases the caller cares about:
    ``"alias"`` when names were added to or removed from the same value, and
    ``"rename"`` when the old group was replaced by a disjoint new group (a
    vocabulary change that kept the wire number).
    """

    value: int
    old_names: tuple[str, ...]
    new_names: tuple[str, ...]
    old_primary: str | None = None
    new_primary: str | None = None
    kind: str = "alias"

    @property
    def old_display(self) -> str:
        names = ", ".join(self.old_names) or "<none>"
        if self.old_primary is not None:
            return f"{names} (primary: {self.old_primary})"
        return names

    @property
    def new_display(self) -> str:
        names = ", ".join(self.new_names) or "<none>"
        if self.new_primary is not None:
            return f"{names} (primary: {self.new_primary})"
        return names


@dataclass(frozen=True)
class EnumDiff:
    """Difference for one enum tag (or one anonymous enum occurrence)."""

    key: str
    old: EnumSet | None
    new: EnumSet | None
    value_changes: tuple[ValueChange, ...] = ()
    renames: tuple[GroupChange, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    new_values: tuple[int, ...] = ()
    retired_values: tuple[int, ...] = ()

    @property
    def compatible(self) -> bool:
        """True when no name changed its numeric value."""
        return not self.value_changes

    def primary_for_old(self, value: int) -> str | None:
        return self.old.primary_for(value) if self.old is not None else None

    def primary_for_new(self, value: int) -> str | None:
        return self.new.primary_for(value) if self.new is not None else None

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "compatible": self.compatible,
            "old_table": self.old.as_dict() if self.old is not None else None,
            "new_table": self.new.as_dict() if self.new is not None else None,
            "value_changes": [
                {
                    "name": change.name,
                    "old": change.old,
                    "new": change.new,
                    "old_file": change.old_file,
                    "old_line": change.old_line,
                    "new_file": change.new_file,
                    "new_line": change.new_line,
                }
                for change in self.value_changes
            ],
            "aliases": [
                {
                    "value": change.value,
                    "old": list(change.old_names),
                    "new": list(change.new_names),
                    "old_primary": change.old_primary,
                    "new_primary": change.new_primary,
                }
                for change in self.renames
                if change.kind == "alias"
            ],
            "renames": [
                {
                    "value": change.value,
                    "old": list(change.old_names),
                    "new": list(change.new_names),
                    "old_primary": change.old_primary,
                    "new_primary": change.new_primary,
                }
                for change in self.renames
                if change.kind == "rename"
            ],
            "added": list(self.added),
            "removed": list(self.removed),
            "new_values": list(self.new_values),
            "retired_values": list(self.retired_values),
        }


@dataclass(frozen=True)
class AcquisitionDiff:
    """The complete two-sided comparison."""

    diffs: tuple[EnumDiff, ...]

    @property
    def compatible(self) -> bool:
        return all(diff.compatible for diff in self.diffs)

    @property
    def value_changes(self) -> tuple[dict[str, object], ...]:
        """All breaking changes, flattened across enums."""
        return tuple(
            {
                "enum": diff.key,
                "name": change.name,
                "old": change.old,
                "new": change.new,
                "old_file": change.old_file,
                "old_line": change.old_line,
                "new_file": change.new_file,
                "new_line": change.new_line,
            }
            for diff in self.diffs
            for change in diff.value_changes
        )

    @property
    def renames(self) -> tuple[dict[str, object], ...]:
        """All non-breaking name/alias changes, flattened across enums."""
        return tuple(
            {
                "enum": diff.key,
                "kind": change.kind,
                "value": change.value,
                "old": list(change.old_names),
                "new": list(change.new_names),
                "old_primary": change.old_primary,
                "new_primary": change.new_primary,
            }
            for diff in self.diffs
            for change in diff.renames
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "value_changes": list(self.value_changes),
            "renames": list(self.renames),
            "enums": [diff.as_dict() for diff in self.diffs],
        }


# ---------------------------------------------------------------------------
# Constant expression evaluation
# ---------------------------------------------------------------------------

_INT_SIZE = {
    "char": (8, True),
    "signed char": (8, True),
    "unsigned char": (8, False),
    "short": (16, True),
    "short int": (16, True),
    "signed short": (16, True),
    "signed short int": (16, True),
    "unsigned short": (16, False),
    "unsigned short int": (16, False),
    "_Bool": (1, False),
    "bool": (1, False),
    "int": (32, True),
    "signed": (32, True),
    "signed int": (32, True),
    "unsigned": (32, False),
    "unsigned int": (32, False),
    "long": (64, True),
    "long int": (64, True),
    "signed long": (64, True),
    "signed long int": (64, True),
    "unsigned long": (64, False),
    "unsigned long int": (64, False),
    "long long": (64, True),
    "long long int": (64, True),
    "signed long long": (64, True),
    "signed long long int": (64, True),
    "unsigned long long": (64, False),
    "unsigned long long int": (64, False),
    "__int128": (128, True),
    "signed __int128": (128, True),
    "unsigned __int128": (128, False),
}

_TYPE_QUALIFIERS = {"const", "volatile", "restrict", "register", "static", "extern"}


def _type_names(node: c_ast.Node) -> list[str]:
    if isinstance(node, c_ast.Typename):
        node = node.type
    if isinstance(node, c_ast.TypeDecl):
        node = node.type
    if isinstance(node, c_ast.IdentifierType):
        return [name for name in node.names if name not in _TYPE_QUALIFIERS]
    return []


def _integer_type(node: c_ast.Node) -> tuple[int, bool] | None:
    names = _type_names(node)
    return _INT_SIZE.get(" ".join(names)) if names else None


def _is_bool_type(node: c_ast.Node) -> bool:
    return " ".join(_type_names(node)) in {"_Bool", "bool"}


def _sizeof_type(node: c_ast.Node) -> int:
    if isinstance(node, c_ast.Typename):
        node = node.type
    if isinstance(node, c_ast.TypeDecl):
        node = node.type
    if isinstance(node, (c_ast.PtrDecl, c_ast.FuncDecl)):
        return 8
    names = _type_names(node)
    if not names:
        raise EnumEvaluationError("sizeof for this type is not supported")
    text = " ".join(names)
    size = {
        "char": 1,
        "signed char": 1,
        "unsigned char": 1,
        "_Bool": 1,
        "bool": 1,
        "short": 2,
        "short int": 2,
        "unsigned short": 2,
        "unsigned short int": 2,
        "int": 4,
        "signed": 4,
        "signed int": 4,
        "unsigned": 4,
        "unsigned int": 4,
        "float": 4,
        "long": 8,
        "long int": 8,
        "unsigned long": 8,
        "unsigned long int": 8,
        "long long": 8,
        "long long int": 8,
        "unsigned long long": 8,
        "unsigned long long int": 8,
        "double": 8,
        "__int128": 16,
        "unsigned __int128": 16,
    }.get(text)
    if size is not None:
        return size
    raise EnumEvaluationError(f"sizeof is not supported for {text!r}")


def _truncate(value: int, bits: int, signed: bool) -> int:
    value &= (1 << bits) - 1
    if signed and value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


_INT_SUFFIX = re.compile(r"(?i)(?:u(?:ll?|wb)?|ll?u?|wbu?|ll?|u)$")


def _integer_literal(token: str) -> int:
    text = token.replace("'", "")
    suffix = _INT_SUFFIX.search(text)
    if suffix:
        text = text[: suffix.start()]
    lower = text.lower()
    if lower.startswith("0x"):
        return int(text, 16)
    if lower.startswith("0b"):
        return int(text, 2)
    if len(text) > 1 and text[0] == "0" and text.isdigit():
        return int(text, 8)
    return int(text, 10)


def _decode_escape(text: str, pos: int, token: str) -> tuple[str, int]:
    """Decode one C escape starting after the backslash at *pos*."""
    if pos >= len(text):
        raise EnumEvaluationError(f"bad escape in character constant: {token!r}")
    simple = {
        "'": "'",
        '"': '"',
        "?": "?",
        "\\": "\\",
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    ch = text[pos]
    if ch in simple:
        return simple[ch], pos + 1
    if ch == "x":
        match = re.match(r"[0-9a-fA-F]+", text[pos + 1 :])
        if not match:
            raise EnumEvaluationError(
                f"bad hex escape in character constant: {token!r}"
            )
        return chr(int(match.group(), 16)), pos + 1 + len(match.group())
    if ch in "uU":
        width = 4 if ch == "u" else 8
        digits = text[pos + 1 : pos + 1 + width]
        if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
            raise EnumEvaluationError(
                f"bad unicode escape in character constant: {token!r}"
            )
        return chr(int(digits, 16)), pos + 1 + width
    if ch.isdigit():
        match = re.match(r"[0-7]+", text[pos:])
        assert match is not None
        return chr(int(match.group(), 8)), pos + len(match.group())
    return ch, pos + 1


def _char_literal(token: str) -> int:
    prefix = ""
    for candidate in ("u8", "L", "u", "U"):
        if token.startswith(candidate):
            prefix = candidate
            token = token[len(candidate) :]
            break
    if not (token.startswith("'") and token.endswith("'")):
        raise EnumEvaluationError(f"not a character constant: {token!r}")

    body = token[1:-1]
    try:
        decoded = ast.literal_eval(token)
    except (SyntaxError, ValueError):
        decoded = None
    if isinstance(decoded, str):
        chars = list(decoded)
    else:
        chars = []
        i = 0
        while i < len(body):
            if body[i] != "\\":
                chars.append(body[i])
                i += 1
            else:
                value, i = _decode_escape(body, i + 1, token)
                chars.append(value)

    if len(chars) == 1:
        return ord(chars[0])
    if prefix:
        return sum(ord(ch) for ch in chars)
    # A multi-character integer constant is implementation-defined; the usual
    # target packs the bytes in source order.
    value = 0
    for ch in chars:
        value = (value << 8) | (ord(ch) & 0xFF)
    return value


def _c_div(left: int, right: int) -> int:
    if right == 0:
        raise EnumEvaluationError("division by zero")
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


def _c_mod(left: int, right: int) -> int:
    return left - _c_div(left, right) * right


class _Evaluator:
    def __init__(self, names: Mapping[str, int] | None = None):
        # Keep the caller's mapping: enumerators evaluated earlier in this
        # declaration must become visible to later expressions.
        self.names = names if isinstance(names, dict) else dict(names or {})

    def evaluate(self, node: c_ast.Node) -> int:
        match node:
            case c_ast.Constant(type=ctype, value=value):
                if ctype in {"char", "signed char", "unsigned char"}:
                    return _char_literal(value)
                if ctype in {"_Bool", "bool"}:
                    return int(value.lower() == "true")
                if "int" in ctype or ctype in {
                    "long",
                    "unsigned",
                    "signed",
                    "short",
                    "char",
                }:
                    return _integer_literal(value)
                raise EnumEvaluationError(f"non-integer constant: {ctype} {value}")
            case c_ast.ID(name=name):
                if name in self.names:
                    return self.names[name]
                raise EnumEvaluationError(f"unknown enumerator reference: {name}")
            case c_ast.UnaryOp(op="+", expr=expr):
                return self.evaluate(expr)
            case c_ast.UnaryOp(op="-", expr=expr):
                return -self.evaluate(expr)
            case c_ast.UnaryOp(op="~", expr=expr):
                return ~self.evaluate(expr)
            case c_ast.UnaryOp(op="!", expr=expr):
                return int(not self.evaluate(expr))
            case c_ast.UnaryOp(op="sizeof", expr=expr):
                return _sizeof_type(expr)
            case c_ast.UnaryOp(op=op, expr=_):
                raise EnumEvaluationError(f"unsupported unary operator: {op}")
            case c_ast.Cast(to_type=to_type, expr=expr):
                value = self.evaluate(expr)
                if _is_bool_type(to_type):
                    return int(bool(value))
                info = _integer_type(to_type)
                return _truncate(value, *info) if info is not None else value
            case c_ast.BinaryOp(op=op, left=left, right=right):
                return self._binary(op, left, right)
            case c_ast.TernaryOp(cond=cond, iftrue=iftrue, iffalse=iffalse):
                return self.evaluate(iftrue if self.evaluate(cond) else iffalse)
            case _:
                raise EnumEvaluationError(
                    "unsupported constant expression: " + _node_text(node)
                )

    def _binary(self, op: str, left: c_ast.Node, right: c_ast.Node) -> int:
        if op == "&&":
            return int(bool(self.evaluate(left)) and bool(self.evaluate(right)))
        if op == "||":
            return int(bool(self.evaluate(left)) or bool(self.evaluate(right)))
        lhs = self.evaluate(left)
        rhs = self.evaluate(right)
        match op:
            case "+":
                return lhs + rhs
            case "-":
                return lhs - rhs
            case "*":
                return lhs * rhs
            case "/":
                return _c_div(lhs, rhs)
            case "%":
                return _c_mod(lhs, rhs)
            case "<<":
                if rhs < 0:
                    raise EnumEvaluationError("negative shift count")
                return lhs << rhs
            case ">>":
                if rhs < 0:
                    raise EnumEvaluationError("negative shift count")
                return lhs >> rhs
            case "&":
                return lhs & rhs
            case "|":
                return lhs | rhs
            case "^":
                return lhs ^ rhs
            case "==":
                return int(lhs == rhs)
            case "!=":
                return int(lhs != rhs)
            case "<":
                return int(lhs < rhs)
            case "<=":
                return int(lhs <= rhs)
            case ">":
                return int(lhs > rhs)
            case ">=":
                return int(lhs >= rhs)
            case _:
                raise EnumEvaluationError(f"unsupported binary operator: {op}")


def _node_text(node: c_ast.Node) -> str:
    from .c_generator import CGenerator

    try:
        return CGenerator().visit(node)
    except Exception:  # pragma: no cover - only used for error messages
        return node.__class__.__name__


# ---------------------------------------------------------------------------
# Enum extraction, evaluation, and primary names
# ---------------------------------------------------------------------------


def _coerce_text(source: str | Path) -> str:
    if isinstance(source, Path):
        return source.read_text()
    if isinstance(source, str):
        return source
    raise TypeError("enum source must be C text or a path")


def _find_enums(root: c_ast.FileAST) -> list[tuple[c_ast.Enum, str]]:
    """Return each enum definition once, with an inferred name when anonymous.

    ``enum { ... } x, y;`` shares one Enum AST node across both declarators, so
    identity (not tree position) deduplicates it.  An unnamed enum whose
    declaration has a typedef name uses that name as its dictionary key; other
    unnamed declarations fall back to ``<anonymous>``.
    """
    enums: list[tuple[c_ast.Enum, str]] = []

    def typedef_name(ext: c_ast.Node) -> str:
        if isinstance(ext, c_ast.Typedef):
            return ext.name or ""
        return ""

    def walk(node: c_ast.Node, owner: str) -> None:
        if isinstance(node, c_ast.Enum):
            if node.values is not None:
                enums.append((node, node.name or owner))
            return
        for _, child in node.children():
            walk(child, owner)

    for ext in root.ext:
        walk(ext, typedef_name(ext))

    unique: dict[int, tuple[c_ast.Enum, str]] = {}
    for enum, name in enums:
        unique.setdefault(id(enum), (enum, name))
    return list(unique.values())


def _evaluate_members(
    enum: c_ast.Enum,
    *,
    start_names: Mapping[str, int],
    file: str,
) -> tuple[tuple[EnumMember, ...], dict[str, int]]:
    names = dict(start_names)
    evaluator = _Evaluator(names)
    members: list[EnumMember] = []
    previous: int | None = None

    assert enum.values is not None
    for entry in enum.values.enumerators:
        if not isinstance(entry, c_ast.Enumerator):
            continue
        if entry.value is None:
            value = 0 if previous is None else previous + 1
            explicit = False
        else:
            try:
                value = evaluator.evaluate(entry.value)
            except EnumEvaluationError as exc:
                coord = entry.coord
                where = (
                    f"{file}:{coord.line}:{coord.column}"
                    if coord is not None and coord.line
                    else (file or "<input>")
                )
                raise EnumEvaluationError(f"{where}: {entry.name}: {exc}") from exc
            explicit = True

        coord = entry.coord
        members.append(
            EnumMember(
                name=entry.name,
                value=value,
                explicit=explicit,
                file=coord.file if coord is not None else file,
                line=coord.line if coord is not None else 0,
                column=coord.column if coord is not None else 0,
            )
        )
        names[entry.name] = value
        previous = value

    return tuple(members), names


_PRIMARY_PRAGMA = re.compile(
    r"^\s*#\s*pragma\s+(?:primary|canonical)\s*:?\s*([A-Za-z_$][\w$]*)",
    re.IGNORECASE | re.MULTILINE,
)


def _source_primary_names(text: str) -> set[str]:
    """Read optional ``#pragma primary NAME`` markers.

    Standard C has no syntax for choosing one name in an alias group.  This
    small, opt-in pragma lets a header state it without changing the
    declarations.  If no marker is present, the full alias group is used.
    """
    return set(_PRIMARY_PRAGMA.findall(text))


def _normalize_primary_names(
    primary_names: Mapping[int | str, str] | Iterable[str] | None,
) -> tuple[set[str], dict[int, str]]:
    by_name: set[str] = set()
    by_value: dict[int, str] = {}
    if primary_names is None:
        return by_name, by_value
    if isinstance(primary_names, Mapping):
        for key, value in primary_names.items():
            if isinstance(key, int):
                by_value[key] = value
            else:
                if value != key:
                    raise EnumDiffError(
                        f"primary name {key!r} must designate itself, got {value!r}"
                    )
                by_name.add(key)
    else:
        by_name.update(primary_names)
    return by_name, by_value


def evaluate_enums(
    source: str | Path,
    *,
    parser: c_parser.CParser | None = None,
    primary_names: Mapping[int | str, str] | Iterable[str] | None = None,
) -> tuple[EnumSet, ...]:
    """Parse *source* and evaluate every enum declaration.

    Parameters
    ----------
    source:
        C text, or a path to a (normally preprocessed) declaration file.
    parser:
        Optional :class:`pycparser.CParser` instance, for example one using
        ``dialect="c23"``.
    primary_names:
        Optional designation of primary names.  An iterable names the
        canonical member(s); a mapping may map a member name to itself or an
        integer value to its primary name.  ``#pragma primary NAME`` is also
        recognized.  When no primary is designated, an alias group is compared
        as an unordered set.
    """
    text = _coerce_text(source)
    if parser is None:
        parser = c_parser.CParser()
    filename = str(source) if isinstance(source, Path) else ""
    root = parser.parse(text, filename=filename)

    by_name, by_value = _normalize_primary_names(primary_names)
    by_name.update(_source_primary_names(text))

    seen_names: dict[str, int] = {}
    result: list[EnumSet] = []
    for enum, inferred_name in _find_enums(root):
        coord = enum.coord
        file = coord.file if coord is not None else filename
        members, seen_names = _evaluate_members(enum, start_names=seen_names, file=file)
        groups: dict[int, set[str]] = {}
        for member in members:
            groups.setdefault(member.value, set()).add(member.name)

        primary: list[tuple[int, str]] = []
        for value, name in by_value.items():
            if name in groups.get(value, set()):
                primary.append((value, name))
        for name in by_name:
            for value, names in groups.items():
                if name in names:
                    primary.append((value, name))

        # A primary name must be unambiguous within this enum.
        seen_primary: dict[int, str] = {}
        for value, name in primary:
            previous = seen_primary.get(value)
            if previous is not None and previous != name:
                raise EnumDiffError(
                    f"conflicting primary names for {inferred_name or '<anonymous>'} "
                    f"value {value}: {previous} and {name}"
                )
            seen_primary[value] = name

        result.append(
            EnumSet(
                tag=inferred_name,
                file=file,
                members=members,
                primary=tuple(sorted(seen_primary.items())),
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _enum_identity(enum_set: EnumSet, root: str | None = None) -> tuple[str, str]:
    """Semantic identity of an enum: its originating header and tag.

    An ordinary declaration's coordinate points at the input file.  Old and
    new inputs are different paths by nature, so that root name is normalized
    away.  A ``#line`` marker, on the other hand, names the real header that
    contributed the declaration; those names must match to pair two enums.
    """
    file = enum_set.file
    if root is not None and file:
        try:
            if Path(file).resolve() == Path(root).resolve():
                file = ""
        except OSError:
            pass
    return file, enum_set.tag


def _enum_keys(
    old_sets: Sequence[EnumSet],
    new_sets: Sequence[EnumSet],
    old_root: str | None = None,
    new_root: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Match enums by tag (and originating file when a tag is duplicated).

    A unique tag identifies the same logical dictionary even when it lives in
    different old/new input paths.  When a tag occurs more than once, the
    ``#line`` header disambiguates, with occurrence order as the last resort.
    """

    def keys_for(sets: Sequence[EnumSet], root: str | None) -> list[str]:
        totals: dict[str, int] = {}
        for enum_set in sets:
            totals[enum_set.tag] = totals.get(enum_set.tag, 0) + 1
        occurrences: dict[tuple[str, str], int] = {}
        keys: list[str] = []
        for enum_set in sets:
            identity = _enum_identity(enum_set, root)
            count = occurrences.get(identity, 0)
            occurrences[identity] = count + 1
            label = enum_set.tag or "<anonymous>"
            base = f"{identity[0]}:{label}" if identity[0] else label
            if totals[enum_set.tag] == 1:
                keys.append(base)
            elif count:
                keys.append(f"{base}#{count + 1}")
            else:
                keys.append(base)
        return keys

    old_keys = keys_for(old_sets, old_root)
    new_keys = keys_for(new_sets, new_root)
    all_keys: list[str] = []
    for key in old_keys + new_keys:
        if key not in all_keys:
            all_keys.append(key)
    return old_keys, new_keys, all_keys


def _same_group(old: EnumSet, new: EnumSet, value: int) -> bool:
    old_names = set(old.groups.get(value, ()))
    new_names = set(new.groups.get(value, ()))
    return old_names == new_names


def _diff_pair(old: EnumSet | None, new: EnumSet | None, key: str) -> EnumDiff:
    old_members = old.members if old is not None else ()
    new_members = new.members if new is not None else ()
    old_values = {member.name: member.value for member in old_members}
    new_values = {member.name: member.value for member in new_members}
    old_locations = {member.name: member for member in old_members}
    new_locations = {member.name: member for member in new_members}

    value_changes = tuple(
        ValueChange(
            name,
            old_values[name],
            new_values[name],
            old_locations[name].file,
            old_locations[name].line,
            new_locations[name].file,
            new_locations[name].line,
        )
        for name in old_values.keys() & new_values.keys()
        if old_values[name] != new_values[name]
    )

    # Report a name-set change as an alias/rename only when it doesn't involve
    # removing a name that stayed present but changed value.  A name present on
    # both sides with different values is always a value change, never a
    # harmless rename, even when its new number matches another member's old
    # number.
    changed_names = {change.name for change in value_changes}
    old_groups = old.groups if old is not None else {}
    new_groups = new.groups if new is not None else {}
    renames: list[GroupChange] = []
    for value in old_groups.keys() & new_groups.keys():
        if old is None or new is None or _same_group(old, new, value):
            continue
        old_names = set(old_groups[value])
        new_names = set(new_groups[value])
        if (old_names - new_names) & changed_names:
            continue
        if (new_names - old_names) & changed_names:
            continue
        if old_names.isdisjoint(new_names):
            kind = "rename"
        else:
            kind = "alias"
        renames.append(
            GroupChange(
                value=value,
                old_names=old_groups[value],
                new_names=new_groups[value],
                old_primary=old.primary_for(value),
                new_primary=new.primary_for(value),
                kind=kind,
            )
        )

    renamed_old = {name for change in renames for name in change.old_names}
    renamed_new = {name for change in renames for name in change.new_names}
    added = tuple(
        name
        for name in new_values
        if name not in old_values and name not in renamed_new
    )
    removed = tuple(
        name
        for name in old_values
        if name not in new_values and name not in renamed_old
    )
    new_values_only = tuple(sorted(set(new_values.values()) - set(old_values.values())))
    retired_values = tuple(sorted(set(old_values.values()) - set(new_values.values())))

    return EnumDiff(
        key=key,
        old=old,
        new=new,
        value_changes=value_changes,
        renames=tuple(sorted(renames, key=lambda change: change.value)),
        added=added,
        removed=removed,
        new_values=new_values_only,
        retired_values=retired_values,
    )


def diff_enums(
    old: str | Path | Sequence[EnumSet],
    new: str | Path | Sequence[EnumSet],
    *,
    parser: c_parser.CParser | None = None,
    primary_names: Mapping[int | str, str] | Iterable[str] | None = None,
) -> AcquisitionDiff:
    """Compare two enum sources, paths, or already-evaluated sets."""
    old_root: str | None = None
    new_root: str | None = None
    if isinstance(old, (str, Path)):
        old_sets = evaluate_enums(old, parser=parser, primary_names=primary_names)
        old_root = str(old)
    else:
        old_sets = tuple(old)
    if isinstance(new, (str, Path)):
        new_sets = evaluate_enums(new, parser=parser, primary_names=primary_names)
        new_root = str(new)
    else:
        new_sets = tuple(new)

    old_keys, new_keys, all_keys = _enum_keys(old_sets, new_sets, old_root, new_root)
    old_by_key = dict(zip(old_keys, old_sets))
    new_by_key = dict(zip(new_keys, new_sets))

    return AcquisitionDiff(
        tuple(
            _diff_pair(old_by_key.get(key), new_by_key.get(key), key)
            for key in all_keys
        )
    )


def diff_files(
    old_path: str | Path,
    new_path: str | Path,
    *,
    parser: c_parser.CParser | None = None,
    primary_names: Mapping[int | str, str] | Iterable[str] | None = None,
) -> AcquisitionDiff:
    """Compare two declaration files by evaluated enum values."""
    return diff_enums(
        Path(old_path),
        Path(new_path),
        parser=parser,
        primary_names=primary_names,
    )


# ---------------------------------------------------------------------------
# Command line output
# ---------------------------------------------------------------------------


def _format_text(result: AcquisitionDiff) -> str:
    lines: list[str] = []
    for diff in result.diffs:
        lines.append(f"enum {diff.key}:")
        if diff.compatible and not diff.renames and not diff.added and not diff.removed:
            lines.append("  unchanged")
        for change in diff.value_changes:
            lines.append(f"  VALUE CHANGE {change.name}: {change.old} -> {change.new}")
        for change in diff.renames:
            label = "RENAME" if change.kind == "rename" else "ALIAS CHANGE"
            lines.append(
                f"  {label} value {change.value}: "
                f"{change.old_display} -> {change.new_display}"
            )
        if diff.added:
            lines.append("  ADDED: " + ", ".join(diff.added))
        if diff.removed:
            lines.append("  REMOVED: " + ", ".join(diff.removed))
        if diff.new_values:
            lines.append("  NEW VALUES: " + ", ".join(map(str, diff.new_values)))
        if diff.retired_values:
            lines.append(
                "  RETIRED VALUES: " + ", ".join(map(str, diff.retired_values))
            )
    if not lines:
        lines.append("no enum declarations found")
    lines.append("")
    lines.append(
        "compatible: " + ("yes" if result.compatible else "no, names changed value")
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser(
        prog="python -m pycparser.enum_diff",
        description=(
            "Compare two preprocessed C enum declarations by evaluated value. "
            "Exit status is 0 when compatible, 1 when a name changed value, "
            "and 2 on an input/evaluation error."
        ),
    )
    argument_parser.add_argument("old", help="old preprocessed declaration file")
    argument_parser.add_argument("new", help="new preprocessed declaration file")
    argument_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    argument_parser.add_argument(
        "--dialect",
        choices=("legacy", "c23"),
        default="legacy",
        help="C dialect used while parsing (default: legacy)",
    )
    argument_parser.add_argument(
        "--primary",
        action="append",
        default=[],
        metavar="NAME",
        help="canonical alias-group name (repeatable); default keeps the group",
    )
    args = argument_parser.parse_args(argv)

    try:
        parser = c_parser.CParser(dialect=args.dialect)
        result = diff_files(
            args.old,
            args.new,
            parser=parser,
            primary_names=args.primary or None,
        )
        if args.format == "json":
            json.dump(result.as_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(_format_text(result))
    except (OSError, c_parser.ParseError, EnumDiffError) as exc:
        print(f"enum_diff: {exc}", file=sys.stderr)
        return 2
    return 0 if result.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
