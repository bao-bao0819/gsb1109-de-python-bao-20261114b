# -----------------------------------------------------------------
# pycparser: status_report.py
#
# Evaluate device status names and bit masks defined as C constant
# expressions (enums and "static const" initializers) and generate a
# per-target interpretation table for a status word.
#
# The evaluator models C integer semantics for 32-bit (ILP32) and
# 64-bit (LP64) targets: signed/unsigned mixed arithmetic via the
# usual arithmetic conversions, casts, bitwise NOT and shifts.
# Expressions whose value is implementation defined or undefined are
# reported with their original source location.  When two mutually
# exclusive enum conditions can be satisfied by the same status word,
# a concrete conflict sample is printed.
#
# Usage (from the pycparser root directory):
#   python examples/status_report.py [header.h] [--bits 32|64|both]
#                                    [--status 0x...]...
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# -----------------------------------------------------------------
import argparse
import os
import re
import sys

# This is not required if you've installed pycparser into
# your site-packages/ with setup.py
sys.path.extend([".", ".."])

from pycparser import c_ast, c_parser

UNDEFINED = "undefined behavior"
IMPL_DEFINED = "implementation-defined"


class CType:
    """An integer type for a given target platform."""

    __slots__ = ("name", "width", "signed", "rank")

    def __init__(self, name, width, signed, rank):
        self.name = name
        self.width = width
        self.signed = signed
        self.rank = rank

    def __repr__(self):
        return f"CType({self.name!r}, width={self.width})"


class Value:
    """An integer value kept in range of its C type."""

    __slots__ = ("v", "t")

    def __init__(self, v, t):
        self.v = v
        self.t = t

    def __repr__(self):
        return f"Value({self.v}, {self.t.name})"


class Platform:
    """Integer type model for a target: 32-bit ILP32 or 64-bit LP64."""

    def __init__(self, bits):
        if bits == 32:
            int_w, long_w, llong_w = 32, 32, 64
        elif bits == 64:
            int_w, long_w, llong_w = 32, 64, 64
        else:
            raise ValueError(f"unsupported target width: {bits}")
        self.bits = bits
        self.types = {
            "char": CType("char", 8, True, 1),
            "signed char": CType("signed char", 8, True, 1),
            "unsigned char": CType("unsigned char", 8, False, 1),
            "short": CType("short", 16, True, 2),
            "unsigned short": CType("unsigned short", 16, False, 2),
            "int": CType("int", int_w, True, 3),
            "unsigned int": CType("unsigned int", int_w, False, 3),
            "long": CType("long", long_w, True, 4),
            "unsigned long": CType("unsigned long", long_w, False, 4),
            "long long": CType("long long", llong_w, True, 5),
            "unsigned long long": CType("unsigned long long", llong_w, False, 5),
        }
        # The status word itself is held in an unsigned long.
        self.status_type = self.types["unsigned long"]

    def describe(self):
        t = self.types
        return (
            f"{self.bits}-bit (int={t['int'].width}, "
            f"long={t['long'].width}, "
            f"long long={t['long long'].width})"
        )


def mask_of(ctype):
    return (1 << ctype.width) - 1


def fits(value, ctype):
    if ctype.signed:
        return -(1 << (ctype.width - 1)) <= value < (1 << (ctype.width - 1))
    return 0 <= value <= mask_of(ctype)


def wrap(value, ctype):
    value &= mask_of(ctype)
    if ctype.signed and value >= 1 << (ctype.width - 1):
        value -= 1 << ctype.width
    return value


_TYPE_NAME_MAP = {
    ("char",): "char",
    ("signed", "char"): "signed char",
    ("unsigned", "char"): "unsigned char",
    ("short",): "short",
    ("short", "int"): "short",
    ("signed", "short"): "short",
    ("signed", "short", "int"): "short",
    ("unsigned", "short"): "unsigned short",
    ("unsigned", "short", "int"): "unsigned short",
    ("int",): "int",
    ("signed",): "int",
    ("signed", "int"): "int",
    ("unsigned",): "unsigned int",
    ("unsigned", "int"): "unsigned int",
    ("long",): "long",
    ("long", "int"): "long",
    ("signed", "long"): "long",
    ("signed", "long", "int"): "long",
    ("unsigned", "long"): "unsigned long",
    ("unsigned", "long", "int"): "unsigned long",
    ("long", "long"): "long long",
    ("long", "long", "int"): "long long",
    ("signed", "long", "long"): "long long",
    ("signed", "long", "long", "int"): "long long",
    ("unsigned", "long", "long"): "unsigned long long",
    ("unsigned", "long", "long", "int"): "unsigned long long",
}

_INT_LITERAL_RE = re.compile(
    r"""
    (?P<num>0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)
    (?P<suffix>([uU](ll|LL|[lL])?|(ll|LL|[lL])[uU]?)?)$
    """,
    re.VERBOSE,
)


class Diagnostic:
    __slots__ = ("coord", "kind", "message")

    def __init__(self, coord, kind, message):
        self.coord = coord
        self.kind = kind
        self.message = message

    def __str__(self):
        return f"{self.coord}: {self.kind}: {self.message}"


class Evaluator:
    """Evaluates C integer constant expressions for one target platform."""

    def __init__(self, platform):
        self.platform = platform
        self.env = {}
        self.diagnostics = []

    # -- diagnostics -------------------------------------------------

    def _diag(self, node, kind, message):
        coord = node.coord if node is not None and node.coord else "<unknown>"
        self.diagnostics.append(Diagnostic(str(coord), kind, message))

    # -- type helpers --------------------------------------------------

    def type_of_names(self, names):
        key = tuple(names)
        if key not in _TYPE_NAME_MAP:
            raise ValueError(f"unsupported integer type: {' '.join(names)}")
        return self.platform.types[_TYPE_NAME_MAP[key]]

    def promote(self, val):
        """Integer promotions: anything below int becomes int."""
        int_t = self.platform.types["int"]
        if val.t.rank < int_t.rank:
            return Value(val.v, int_t)
        return val

    def common_type(self, t1, t2):
        """Usual arithmetic conversions on two promoted types."""
        if t1 is t2:
            return t1
        if t1.signed == t2.signed:
            return t1 if t1.rank >= t2.rank else t2
        unsigned_t, signed_t = (t1, t2) if not t1.signed else (t2, t1)
        if unsigned_t.rank >= signed_t.rank:
            return unsigned_t
        if signed_t.width > unsigned_t.width:
            return signed_t
        return self.platform.types["unsigned " + signed_t.name]

    def convert(self, val, ctype, node):
        """Conversion (cast/assignment), flagging out-of-range signed."""
        if fits(val.v, ctype):
            return Value(val.v, ctype)
        if ctype.signed:
            self._diag(
                node,
                IMPL_DEFINED,
                f"value {val.v} of type '{val.t.name}' is not representable "
                f"in '{ctype.name}'",
            )
        return Value(wrap(val.v, ctype), ctype)

    # -- expression evaluation -----------------------------------------

    def eval(self, node):
        method = self._dispatch.get(type(node))
        if method is None:
            self._diag(
                node, UNDEFINED, f"unsupported expression: {type(node).__name__}"
            )
            return Value(0, self.platform.types["int"])
        return method(self, node)

    def _eval_constant(self, node):
        text = node.value
        m = _INT_LITERAL_RE.match(text)
        if m is None:
            self._diag(node, UNDEFINED, f"unsupported constant: {text!r}")
            return Value(0, self.platform.types["int"])
        num, suffix = m.group("num"), m.group("suffix").lower()
        value = (
            int(num, 0)
            if num.startswith(("0x", "0X"))
            else int(num, 8)
            if (len(num) > 1 and num.startswith("0"))
            else int(num)
        )
        decimal = not num.startswith(("0x", "0X")) and not (
            len(num) > 1 and num.startswith("0")
        )
        types = self.platform.types
        if "u" in suffix:
            if "ll" in suffix:
                candidates = [types["unsigned long long"]]
            elif "l" in suffix:
                candidates = [types["unsigned long"], types["unsigned long long"]]
            else:
                candidates = [
                    types["unsigned int"],
                    types["unsigned long"],
                    types["unsigned long long"],
                ]
        elif "ll" in suffix:
            candidates = [types["long long"]]
            if not decimal:
                candidates.append(types["unsigned long long"])
        elif "l" in suffix:
            candidates = [types["long"], types["long long"]]
            if not decimal:
                candidates = [
                    types["long"],
                    types["unsigned long"],
                    types["long long"],
                    types["unsigned long long"],
                ]
        else:
            candidates = [types["int"], types["long"], types["long long"]]
            if not decimal:
                candidates = [
                    types["int"],
                    types["unsigned int"],
                    types["long"],
                    types["unsigned long"],
                    types["long long"],
                    types["unsigned long long"],
                ]
        for cand in candidates:
            if fits(value, cand):
                return Value(value, cand)
        self._diag(node, UNDEFINED, f"integer constant {text!r} too large")
        return Value(
            wrap(value, types["unsigned long long"]), types["unsigned long long"]
        )

    def _eval_id(self, node):
        if node.name in self.env:
            return self.env[node.name]
        self._diag(node, UNDEFINED, f"unknown identifier: {node.name!r}")
        return Value(0, self.platform.types["int"])

    def _eval_unaryop(self, node):
        val = self.promote(self.eval(node.expr))
        op = node.op
        if op == "+":
            return val
        if op == "-":
            if val.t.signed and val.v == -(1 << (val.t.width - 1)):
                self._diag(
                    node, UNDEFINED, f"negation of {val.t.name} minimum overflows"
                )
            return Value(wrap(-val.v, val.t), val.t)
        if op == "~":
            return Value(wrap(~val.v, val.t), val.t)
        if op == "!":
            return Value(1 if val.v == 0 else 0, self.platform.types["int"])
        self._diag(node, UNDEFINED, f"unsupported unary operator: {op!r}")
        return Value(0, self.platform.types["int"])

    def _eval_binaryop(self, node):
        op = node.op
        if op in ("<<", ">>"):
            return self._eval_shift(node)
        left = self.promote(self.eval(node.left))
        right = self.promote(self.eval(node.right))
        ctype = self.common_type(left.t, right.t)
        a = self.convert(left, ctype, node).v
        b = self.convert(right, ctype, node).v
        int_t = self.platform.types["int"]
        if op in ("<", "<=", ">", ">=", "==", "!="):
            result = {
                "<": a < b,
                "<=": a <= b,
                ">": a > b,
                ">=": a >= b,
                "==": a == b,
                "!=": a != b,
            }[op]
            return Value(1 if result else 0, int_t)
        if op in ("&", "|", "^"):
            result = {"&": a & b, "|": a | b, "^": a ^ b}[op]
            return Value(wrap(result, ctype), ctype)
        if op in ("/", "%"):
            if b == 0:
                self._diag(node, UNDEFINED, "division by zero")
                return Value(0, ctype)
            if ctype.signed and a == -(1 << (ctype.width - 1)) and b == -1:
                self._diag(
                    node, UNDEFINED, f"division of {ctype.name} minimum by -1 overflows"
                )
                return Value(wrap(a, ctype), ctype)
            quotient = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                quotient = -quotient
            result = quotient if op == "/" else a - quotient * b
            return Value(wrap(result, ctype), ctype)
        if op not in ("+", "-", "*"):
            self._diag(node, UNDEFINED, f"unsupported binary operator: {op!r}")
            return Value(0, ctype)
        result = {"+": a + b, "-": a - b, "*": a * b}[op]
        if ctype.signed and not fits(result, ctype):
            self._diag(
                node,
                UNDEFINED,
                f"signed overflow in '{op}' on type '{ctype.name}'",
            )
        return Value(wrap(result, ctype), ctype)

    def _eval_shift(self, node):
        op = node.op
        left = self.promote(self.eval(node.left))
        right = self.promote(self.eval(node.right))
        width = left.t.width
        count = right.v
        if count < 0 or count >= width:
            self._diag(
                node,
                UNDEFINED,
                f"shift count {count} is negative or >= width {width} "
                f"of '{left.t.name}'",
            )
            return Value(0, left.t)
        if op == "<<":
            if left.t.signed and left.v < 0:
                self._diag(node, UNDEFINED, "left shift of a negative value")
                return Value(0, left.t)
            result = left.v << count
            if left.t.signed and not fits(result, left.t):
                self._diag(
                    node,
                    UNDEFINED,
                    f"signed overflow in left shift on type '{left.t.name}'",
                )
            return Value(wrap(result, left.t), left.t)
        # Right shift.
        if left.t.signed and left.v < 0:
            self._diag(
                node,
                IMPL_DEFINED,
                f"right shift of negative value {left.v} of type '{left.t.name}'",
            )
            return Value(left.v >> count, left.t)
        return Value((left.v & mask_of(left.t)) >> count, left.t)

    def _eval_cast(self, node):
        val = self.eval(node.expr)
        ctype = self.type_of_names(node.to_type.type.type.names)
        return self.convert(val, ctype, node)

    _dispatch = {}


Evaluator._dispatch = {
    c_ast.Constant: Evaluator._eval_constant,
    c_ast.ID: Evaluator._eval_id,
    c_ast.UnaryOp: Evaluator._eval_unaryop,
    c_ast.BinaryOp: Evaluator._eval_binaryop,
    c_ast.Cast: Evaluator._eval_cast,
}


class Report:
    """Everything the daily report needs for one target platform."""

    def __init__(self, platform):
        self.platform = platform
        self.constants = []  # (name, Value) for static const masks
        self.enums = []  # (enum name, [(enumerator name, Value)])
        self.diagnostics = []
        self.conflicts = []  # (enum name, [names], sample Value)

    def enum_entries(self):
        for _enum_name, entries in self.enums:
            yield from entries


def _strip_comments(text):
    """Blank out C comments, keeping newlines so coordinates stay valid."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        elif text[i] in "\"'":
            quote = text[i]
            end = i + 1
            while end < n and text[end] != quote:
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def evaluate_file(path, bits):
    """Parse a header and evaluate its constant expressions for a target."""
    parser = c_parser.CParser()
    with open(path) as f:
        ast = parser.parse(_strip_comments(f.read()), filename=path)
    platform = Platform(bits)
    ev = Evaluator(platform)
    report = Report(platform)
    int_t = platform.types["int"]

    for ext in ast.ext:
        if not isinstance(ext, c_ast.Decl):
            continue
        if isinstance(ext.type, c_ast.TypeDecl):
            inner = ext.type.type
        else:
            inner = ext.type
        if isinstance(inner, c_ast.Enum) and inner.values is not None:
            enum_name = inner.name or "<anonymous>"
            entries = []
            next_value = 0
            for enumerator in inner.values.enumerators:
                if enumerator.value is not None:
                    val = ev.eval(enumerator.value)
                    if not fits(val.v, int_t):
                        ev._diag(
                            enumerator,
                            IMPL_DEFINED,
                            f"enumerator value {val.v} is not representable in 'int'",
                        )
                    val = Value(wrap(val.v, int_t), int_t)
                    next_value = val.v
                val = Value(next_value, int_t)
                entries.append((enumerator.name, val))
                ev.env[enumerator.name] = val
                next_value += 1
            report.enums.append((enum_name, entries))
        elif isinstance(inner, c_ast.IdentifierType) and ext.init is not None:
            ctype = ev.type_of_names(inner.names)
            val = ev.convert(ev.eval(ext.init), ctype, ext.init)
            report.constants.append((ext.name, val))
            ev.env[ext.name] = val

    report.diagnostics = ev.diagnostics
    report.conflicts = _find_conflicts(report)
    return report


def _find_conflicts(report):
    """Two enumerators of one enum with the same value: a single status
    word satisfies both mutually exclusive conditions."""
    conflicts = []
    for enum_name, entries in report.enums:
        by_value = {}
        for name, val in entries:
            by_value.setdefault(val.v, []).append((name, val))
        for names_vals in by_value.values():
            if len(names_vals) > 1:
                names = [name for name, _ in names_vals]
                conflicts.append((enum_name, names, names_vals[0][1]))
    return conflicts


def interpret(report, status):
    """Interpret one status word: which flags and state conditions hit.

    Single-bit masks hit when their bit is set; combined masks hit when
    all their bits are set, so both kinds can hit at the same time.
    Enum conditions hit on equality with the status word.
    """
    status_t = report.platform.status_type
    word = status & mask_of(status_t)
    single, combined, states = [], [], []
    for name, val in report.constants:
        bits = val.v & mask_of(status_t)
        if bits == 0:
            continue
        if bits & (bits - 1) == 0:
            if word & bits:
                single.append(name)
        elif (word & bits) == bits:
            combined.append(name)
    for name, val in report.enum_entries():
        if (val.v & mask_of(status_t)) == word:
            states.append(name)
    return {"word": word, "single": single, "combined": combined, "states": states}


def _hex(val, ctype):
    width = max(ctype.width // 4, 2)
    return f"0x{val.v & mask_of(ctype):0{width}X}"


def format_report(report, status_words):
    out = []
    platform = report.platform
    status_t = platform.status_type
    out.append(f"=== Target: {platform.describe()} ===")
    out.append("")
    out.append("Named constants:")
    for enum_name, entries in report.enums:
        for name, val in entries:
            out.append(f"  {name:<24} = {_hex(val, val.t):<12} (int, enum {enum_name})")
    for name, val in report.constants:
        out.append(f"  {name:<24} = {_hex(val, val.t):<12} ({val.t.name})")
    out.append("")
    if report.diagnostics:
        out.append("Diagnostics (implementation-defined / undefined):")
        for diag in report.diagnostics:
            out.append(f"  {diag}")
        out.append("")
    if report.conflicts:
        out.append(
            "Conflicts (one status word satisfies mutually exclusive conditions):"
        )
        for enum_name, names, sample in report.conflicts:
            joined = " and ".join(names)
            out.append(f"  enum {enum_name}: {joined} all equal {sample.v}")
            out.append(f"    conflict sample: status = {_hex(sample, status_t)}")
        out.append("")
    for status in status_words:
        result = interpret(report, status)
        out.append(
            f"Interpretation of status word {_hex(Value(result['word'], status_t), status_t)}:"
        )
        hits = [f"{name} (single)" for name in result["single"]]
        hits += [f"{name} (combined)" for name in result["combined"]]
        out.append(f"  flags hit: {', '.join(hits) if hits else '(none)'}")
        states = result["states"]
        out.append(
            f"  state conditions hit: {', '.join(states) if states else '(none)'}"
        )
        out.append("")
    return "\n".join(out)


def default_status_words(report):
    """Words interpreted when --status is not given: every conflict
    sample plus the union of all flag masks."""
    status_t = report.platform.status_type
    words = [sample.v & mask_of(status_t) for _, _, sample in report.conflicts]
    union = 0
    for _name, val in report.constants:
        union |= val.v & mask_of(status_t)
    words.append(union)
    seen = set()
    unique = []
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Interpret a device status word per target platform bit width."
    )
    default_header = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "c_files", "device_status.h"
    )
    parser.add_argument("header", nargs="?", default=default_header)
    parser.add_argument("--bits", choices=["32", "64", "both"], default="both")
    parser.add_argument(
        "--status",
        action="append",
        type=lambda s: int(s, 0),
        default=None,
        help="status word to interpret (may be repeated)",
    )
    args = parser.parse_args(argv)
    widths = [32, 64] if args.bits == "both" else [int(args.bits)]
    for bits in widths:
        report = evaluate_file(args.header, bits)
        words = args.status if args.status else default_status_words(report)
        print(format_report(report, words))
    return 0


if __name__ == "__main__":
    sys.exit(main())
