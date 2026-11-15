from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from ufoLib2.catalog.input import (
    SourceSpec,
    load_json_file,
    load_manual_file,
    load_source,
    normalize_stable_ids,
)
from ufoLib2.catalog.model import (
    CatalogError,
    load_catalog,
    save_catalog,
)
from ufoLib2.catalog.resolution import resolve_catalog, summarize


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    for side in ("left", "right"):
        parser.add_argument(
            f"--{side}",
            required=True,
            metavar="UFO",
            help=f"path to the {side} supplier's UFO",
        )
        parser.add_argument(
            f"--{side}-label",
            default=side,
            metavar="NAME",
            help=f"stable record prefix for the {side} supplier (default: {side})",
        )
        parser.add_argument(
            f"--{side}-naming",
            choices=("auto", "unicode", "internal"),
            default="auto",
            help=(
                f"how the {side} supplier names glyphs; 'unicode' permits "
                "uniXXXX/uXXXXX names to supply a missing glif Unicode value, "
                "'internal' never does (default: auto)"
            ),
        )
        parser.add_argument(
            f"--{side}-stable-id-key",
            default=None,
            metavar="LIB_KEY",
            help=(
                f"glyph lib key holding an explicit stable id for the {side} "
                "supplier"
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ufoLib2.catalog",
        description=(
            "Build a stable glyph catalog from two supplier UFOs. Records keep "
            "explicit ids; one-to-one matches require explicit evidence, and "
            "unsplittable sets are reported as candidate groups."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a catalog")
    _add_source_arguments(build)
    build.add_argument(
        "--stable-ids",
        type=Path,
        default=None,
        metavar="JSON",
        help="explicit stable-id file keyed by source glyph or catalog record id",
    )
    build.add_argument(
        "--manual",
        type=Path,
        default=None,
        metavar="JSON",
        help="human-confirmed one-to-one mapping file",
    )
    build.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="JSON",
        help="where to write the catalog",
    )
    build.add_argument(
        "--summary",
        action="store_true",
        help="also print the human-readable summary to stdout",
    )

    show = subparsers.add_parser("show", help="print a catalog summary")
    show.add_argument("catalog", type=Path, metavar="JSON")
    show.add_argument(
        "--json",
        action="store_true",
        help="emit canonical JSON instead of a text summary",
    )

    validate = subparsers.add_parser(
        "validate", help="parse and cross-check a catalog file"
    )
    validate.add_argument("catalog", type=Path, metavar="JSON")

    return parser


def _build(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    specs = [
        SourceSpec(
            label=args.left_label,
            path=args.left,
            naming=args.left_naming,
            stable_id_key=args.left_stable_id_key,
        ),
        SourceSpec(
            label=args.right_label,
            path=args.right,
            naming=args.right_naming,
            stable_id_key=args.right_stable_id_key,
        ),
    ]
    if specs[0].label == specs[1].label:
        parser.error("--left-label and --right-label must differ")

    stable_id_file = None
    if args.stable_ids is not None:
        stable_id_file = load_json_file(args.stable_ids)

    sources = []
    records = []
    for spec in specs:
        info, source_records = load_source(spec)
        sources.append(info)
        records.extend(source_records)

    stable_ids = (
        normalize_stable_ids(stable_id_file, specs, records)
        if stable_id_file is not None
        else {}
    )
    if stable_ids:
        records = [
            replace(record, stable_id=stable_ids.get(record.id, record.stable_id))
            for record in records
        ]

    manual_pairs = (
        load_manual_file(args.manual, specs, records)
        if args.manual is not None
        else []
    )
    catalog = resolve_catalog(sources, records, manual_pairs)
    save_catalog(catalog, args.output)
    if args.summary:
        sys.stdout.write(summarize(catalog))


def _show(args: argparse.Namespace) -> None:
    catalog = load_catalog(args.catalog)
    if args.json:
        sys.stdout.write(catalog.to_json_string())
    else:
        sys.stdout.write(summarize(catalog))


def _validate(args: argparse.Namespace) -> None:
    catalog = load_catalog(args.catalog)
    sys.stdout.write(
        f"{len(catalog.records)} records, {len(catalog.matches)} confirmed "
        f"pairs, {len(catalog.candidate_groups)} candidate groups, "
        f"{len(catalog.conflicts)} conflicts\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            _build(args, parser)
        elif args.command == "show":
            _show(args)
        elif args.command == "validate":
            _validate(args)
        else:
            parser.error(f"unknown command {args.command!r}")
    except CatalogError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
