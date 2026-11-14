# -----------------------------------------------------------------
# pycparser: sampling_reachability.py
#
# Find the channel/field/scale combinations that can reach the single
# database-write call in a sampling pipeline, using the pycparser AST.
#
# The analysis is path-sensitive and inlines wrapper calls, so arguments
# chosen by the same branch stay correlated.  Recursive and unknown
# callees truncate only the affected path; other calls are still listed.
#
# Usage:
#   python examples/sampling_reachability.py tests/c_files/sampling_pipeline.c
#
# Entry points are the functions that are not called by any other
# function in the file.  Pass --entry to restrict them, and --endpoint
# plus --payload to name the write function and its fields.
#
# License: BSD
# -----------------------------------------------------------------
import argparse
import json
import sys

# This is not required if you've installed pycparser into
# your site-packages/ with setup.py
sys.path.extend([".", ".."])

from pycparser import c_ast, c_parser
from pycparser.sampling_analysis import (
    Endpoint,
    EntryPoint,
    ReachabilityAnalyzer,
    prepare_source,
)


def _called_functions(ast: c_ast.FileAST) -> set[str]:
    called: set[str] = set()

    class CallCollector(c_ast.NodeVisitor):
        def visit_FuncCall(self, node: c_ast.FuncCall) -> None:
            if isinstance(node.name, c_ast.ID):
                called.add(node.name.name)
            self.generic_visit(node)

    CallCollector().visit(ast)
    return called


def _defined_functions(ast: c_ast.FileAST) -> list[str]:
    names: list[str] = []
    for ext in ast.ext:
        if isinstance(ext, c_ast.FuncDef) and ext.decl.name is not None:
            names.append(ext.decl.name)
    return names


def _default_entries(ast: c_ast.FileAST, endpoint: str) -> list[str]:
    called = _called_functions(ast)
    return [
        name
        for name in _defined_functions(ast)
        if name not in called and name != endpoint
    ]


def _render_value(value) -> str:
    return "?" if value is None else repr(value)


def _print_text(results, truncations) -> None:
    for reach in results:
        payload = ", ".join(
            f"{name}={tracked.render()}"
            for name, tracked in zip(reach.payload_names, reach.values)
        )
        print(f"{reach.entry}: {payload}")
        for frame in reach.path:
            args = ", ".join(argument.render() for argument in frame.arguments)
            print(f"    {frame.function}({args})  at {frame.coord or '?'}")
        print()
    if truncations:
        print("truncated paths:")
        for truncation in truncations:
            print(f"    {truncation.render()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filename")
    parser.add_argument(
        "--endpoint",
        default=None,
        help="write function; default: db_write when present",
    )
    parser.add_argument(
        "--payload",
        default="channel,field,scale",
        help="comma-separated payload parameter names (default: channel,field,scale)",
    )
    parser.add_argument(
        "--entry",
        action="append",
        default=[],
        help="entry function; repeatable. Default: functions nobody calls",
    )
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="entry arguments as key=value,a,b; repeatable for multiple entries",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--use-cpp",
        action="store_true",
        help="run the real preprocessor instead of the built-in comment/macro pre-pass",
    )
    args = parser.parse_args()

    with open(args.filename, encoding="utf-8") as stream:
        source = stream.read()
    text = source if args.use_cpp else prepare_source(source)
    ast = c_parser.CParser().parse(text, filename=args.filename)

    endpoint = args.endpoint
    if endpoint is None:
        if "db_write" not in _defined_functions(ast):
            parser.error("no --endpoint given and db_write is not defined")
        endpoint = "db_write"

    entries: list[EntryPoint]
    if args.arg:
        entries = []
        for specification in args.arg:
            function, _, rest = specification.partition("=")
            values = tuple(_parse_argument(value) for value in rest.split(",") if value)
            entries.append(EntryPoint(function.strip(), values))
    else:
        entries = [EntryPoint(name) for name in _default_entries(ast, endpoint)]

    analyzer = ReachabilityAnalyzer(
        ast,
        Endpoint(endpoint, tuple(args.payload.split(","))),
    )
    results = analyzer.analyze(entries)

    if args.json:
        print(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "results": [reach.as_dict() for reach in results],
                    "truncations": [
                        {
                            "reason": item.reason,
                            "function": item.function,
                            "coord": item.coord,
                            "detail": item.detail,
                            "call_path": list(item.call_path),
                        }
                        for item in analyzer.truncations
                    ],
                },
                indent=2,
            )
        )
    else:
        _print_text(results, analyzer.truncations)


def _parse_argument(text: str):
    text = text.strip()
    try:
        return int(text, 0)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


if __name__ == "__main__":
    main()
