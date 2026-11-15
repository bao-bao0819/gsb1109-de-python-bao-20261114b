"""Command-line entry point for the requeue planner."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from .journal import JournalError, plan_jobs
from .model import IdempotencyRule


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _rule_from_document(document: Dict[str, Any]) -> IdempotencyRule:
    return IdempotencyRule(**(document.get("idempotency_rule") or {}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m requeue",
        description=(
            "Classify interrupted collection requests into resend, "
            "verify-remote and already-delivered lists."
        ),
    )
    parser.add_argument("journal", help="JSON journal path, or - for stdin")
    parser.add_argument(
        "--jobs",
        action="store_true",
        help="also print the concrete supplement jobs to enqueue",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent for output (default: 2)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = _load_json(args.journal)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read journal: {exc}", file=sys.stderr)
        return 2

    if isinstance(document, list):
        events: List[Any] = document
        rule = IdempotencyRule()
    elif isinstance(document, dict):
        events = document.get("events", [])
        rule = _rule_from_document(document)
    else:
        print("error: journal must be a list or an object", file=sys.stderr)
        return 2

    try:
        plan, jobs = plan_jobs(events, rule=rule)
    except (JournalError, KeyError, TypeError, ValueError) as exc:
        print(f"error: invalid journal: {exc}", file=sys.stderr)
        return 2

    payload: Dict[str, Any] = plan.to_dict()
    if args.jobs:
        payload["supplement_jobs"] = [job.to_dict() for job in jobs]
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=args.indent)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
