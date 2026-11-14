"""Command line entry point: extract layouts and emit merge artifacts."""

import argparse
import json
import os
import sys

from .collector import extract_module
from .compare import compare_modules
from .layout import render_schema
from .merge import business_merge_table, table_to_dict


def _write_json(path: str, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _print_summary(report, table) -> None:
    print(f"== {report.source_a} vs {report.source_b} ==")
    for match in report.record_matches:
        label_a = match.rid_a or "-"
        label_b = match.rid_b or "-"
        print(
            f"  [{match.verdict}] {label_a} <-> {label_b}"
            + (f"  ({match.reason})" if match.reason else "")
        )
        for row in match.fields:
            if row.status != "same":
                detail = f" {row.reason}" if row.reason else ""
                print(f"      - field {row.index}: {row.status}{detail}")
    print(f"  mergeable business types: {len(table)}")
    print("  aliases:")
    for row in report.alias_rows:
        flag = "=" if row["layout_equal"] else "x"
        print(f"      [{flag}] {row['alias']}: {row['status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="structmap",
        description="Extract scoped struct layouts from preprocessed C modules "
        "and build a layout-based business merge table.",
    )
    parser.add_argument(
        "sources", nargs="+", help="preprocessed C source(s), e.g. module.i"
    )
    parser.add_argument(
        "--use-cpp", action="store_true", help="run the C preprocessor first"
    )
    parser.add_argument("--out-dir", help="directory for JSON artifacts")
    args = parser.parse_args(argv)

    schemas = [extract_module(path, use_cpp=args.use_cpp) for path in args.sources]
    rendered = [render_schema(schema) for schema in schemas]

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for index, payload in enumerate(rendered):
            _write_json(f"{args.out_dir}/schema_{index}.json", payload)

    if len(schemas) == 1:
        print(json.dumps(rendered[0], indent=2, ensure_ascii=False))
        return 0

    report = compare_modules(schemas[0], schemas[1])
    table = business_merge_table(schemas[0], schemas[1], report)
    if args.out_dir:
        _write_json(f"{args.out_dir}/comparison.json", report.to_dict())
        _write_json(f"{args.out_dir}/merge_table.json", table_to_dict(table))
    _print_summary(report, table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
