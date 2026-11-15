"""Command-line interface for the gateway traffic estimator.

Typical use::

    python -m gateway_traffic --path-archive path/ --status-archive status/ \
        --plan sampling.json

The sampling plan is a JSON object with path and status rate schedules::

    {
      "path": {
        "default": 0.20,
        "rules": [
          {"match": "prefix:/api/", "probability": 0.10},
          {"match": "/health", "probability": 1}
        ]
      },
      "status": {
        "default": 0.0,
        "rules": [
          {"match": "2xx", "probability": 0.50},
          {"match": "5xx", "probability": 1}
        ]
      },
      "joint": {"default": null}
    }

``joint.default`` of ``null`` means the two routes select independently, so
``p_both = p_path * p_status``.  A number fixes the joint probability, and
``joint.rules`` can override it for particular paths and statuses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .archives import load_and_merge
from .report import render_csv, render_json, render_text, render_transactions_csv
from .sampling import SamplingError, SamplingPlan, estimate


def _read_plan(path: str) -> Dict[str, Any]:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SamplingError(f"invalid sampling plan JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SamplingError("sampling plan must be a JSON object")
    return value


def _rate(value: str) -> Any:
    """Parse a command-line probability, keeping exact fractions exact."""

    text = value.strip()
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        if int(denominator) == 0:
            raise argparse.ArgumentTypeError("probability denominator cannot be zero")
        return f"{int(numerator)}/{int(denominator)}"
    return text


def _add_probability_options(parser: argparse.ArgumentParser, prefix: str) -> None:
    parser.add_argument(
        f"--{prefix}-default",
        type=_rate,
        default=None,
        help="default inclusion probability for unmatched values",
    )
    parser.add_argument(
        f"--{prefix}-rule",
        action="append",
        nargs=2,
        metavar=("MATCH", "PROBABILITY"),
        default=[],
        help="first-match rule; repeatable (e.g. --path-rule 'prefix:/api/' 0.1)",
    )


def build_plan(args: argparse.Namespace) -> SamplingPlan:
    if args.plan:
        raw = _read_plan(args.plan)
        if args.path_default is not None or args.path_rule or args.status_default is not None or args.status_rule:
            raise SamplingError("use either --plan or individual rate options, not both")
        return SamplingPlan.from_dict(raw)

    if args.path_default is None and args.status_default is None and not args.path_rule and not args.status_rule:
        raise SamplingError(
            "a sampling plan is required: pass --plan FILE, or --path-default/--path-rule "
            "and --status-default/--status-rule.  Without the design the archives cannot "
            "be expanded to population totals."
        )

    path_rules = [
        {"match": match, "probability": probability}
        for match, probability in args.path_rule
    ]
    status_rules = [
        {"match": match, "probability": probability}
        for match, probability in args.status_rule
    ]
    plan_data: Dict[str, Any] = {
        "path": {"default": args.path_default if args.path_default is not None else 0, "rules": path_rules},
        "status": {
            "default": args.status_default if args.status_default is not None else 0,
            "rules": status_rules,
        },
        "independent_routes": not args.dependent_routes,
    }
    if args.joint_default is not None:
        plan_data["joint"] = {"default": args.joint_default}
    elif args.joint_rule:
        plan_data["joint"] = {
            "rules": [
                {"path": path, "status": status, "probability": probability}
                for path, status, probability in args.joint_rule
            ]
        }
    return SamplingPlan.from_dict(plan_data)


def _write_output(text: str, path: Optional[str]) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway_traffic",
        description=(
            "Estimate original gateway response-body traffic from two independently "
            "sampled HTTP archives using h11-parsed final status and received body bytes."
        ),
    )
    parser.add_argument("--path-archive", required=True, help="directory or file of path-sampled transactions")
    parser.add_argument("--status-archive", required=True, help="directory or file of status-sampled transactions")
    parser.add_argument("--plan", help="JSON sampling plan; '-' reads it from stdin")
    _add_probability_options(parser, "path")
    _add_probability_options(parser, "status")
    parser.add_argument(
        "--joint-default",
        type=_rate,
        default=None,
        help="fixed joint inclusion probability; omitted means p_both = p_path * p_status",
    )
    parser.add_argument(
        "--joint-rule",
        action="append",
        nargs=3,
        metavar=("PATH", "STATUS", "PROBABILITY"),
        default=[],
        help="joint-inclusion override for a path/status pair; repeatable",
    )
    parser.add_argument(
        "--dependent-routes",
        action="store_true",
        help="routes are not independent; requires a joint schedule",
    )
    parser.add_argument(
        "--group-by",
        choices=("status_class", "status"),
        default="status_class",
        help="report response-body totals by 1xx..5xx (default) or exact status",
    )
    parser.add_argument(
        "--known-group",
        action="append",
        default=[],
        help="additional status group to report even if unsampled (e.g. 418 or 5xx)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="summary format (default: text)",
    )
    parser.add_argument("--output", help="write the summary here instead of stdout")
    parser.add_argument("--transactions-output", help="write the per-transaction table (CSV) here")
    parser.add_argument(
        "--include-empty-classes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="report the five standard status classes even when unsampled (default: on)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args)
        merged = load_and_merge(Path(args.path_archive), Path(args.status_archive))
        report = estimate(
            merged.transactions,
            plan,
            group_by=args.group_by,
            known_groups=args.known_group,
            include_empty_classes=args.include_empty_classes,
        )
    except SamplingError as exc:
        parser.error(str(exc))
        return 2

    counts = {
        "path_only": merged.path_only,
        "status_only": merged.status_only,
        "both": merged.both,
        "union": len(merged.transactions),
    }
    if args.format == "json":
        text = render_json(report, merged_counts=counts)
    elif args.format == "csv":
        text = render_csv(report, merged_counts=counts)
    else:
        text = render_text(report, merged_counts=counts)
    _write_output(text, args.output)
    if args.transactions_output:
        Path(args.transactions_output).write_text(
            render_transactions_csv(report), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
