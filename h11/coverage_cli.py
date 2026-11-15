"""Inspect a stored HTTP capture and reconstruct the archived object.

Usage::

    python -m h11.coverage_cli CAPTURE [--length N] [--json] [--output FILE]

``CAPTURE`` is a raw response capture (one response or several responses
recorded on one persistent connection). The command prints the coverage table
by default. With ``--output`` it writes the publishable object only when the
capture proves a complete version; gaps and unresolved conflicts are errors,
never patched over.
"""

import argparse
import json
import sys
from typing import Optional, Sequence

from .coverage import CaptureParseError, IncompleteCoverageError, analyze_capture


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m h11.coverage_cli",
        description=(
            "Reconstruct an archived object from 206/200 responses and report "
            "coverage, gaps, and conflicts."
        ),
    )
    parser.add_argument("capture", help="file holding the raw HTTP response capture")
    parser.add_argument(
        "--length",
        type=int,
        metavar="N",
        help="expected complete object length in bytes",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON"
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="write the complete object here (fails unless it is publishable)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with open(args.capture, "rb") as capture_file:
            data = capture_file.read()
    except OSError as exc:
        print("cannot read capture: {}".format(exc), file=sys.stderr)
        return 2

    try:
        report = analyze_capture(data, expected_length=args.length)
    except CaptureParseError as exc:
        print("cannot parse capture: {}".format(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_table())

    if args.output is not None:
        try:
            body = report.assemble()
        except IncompleteCoverageError as exc:
            print("cannot publish: {}".format(exc), file=sys.stderr)
            return 1
        with open(args.output, "wb") as output:
            output.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
