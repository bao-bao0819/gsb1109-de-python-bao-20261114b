import sys
from argparse import ArgumentParser, ArgumentTypeError
from glob import iglob
from itertools import chain

from . import JUnitXml, version
from .recheck import (
    DEFAULT_WINDOW,
    RecheckError,
    format_plan,
    load_spec,
    parse_duration,
    plan_as_json,
    plan_recheck,
)


def _seconds(value: str) -> float:
    try:
        seconds = parse_duration(value)
    except RecheckError as exc:
        raise ArgumentTypeError(str(exc)) from exc
    if seconds <= 0:
        raise ArgumentTypeError(f"duration must be positive: {value!r}")
    return seconds


def _mapping_pairs(value: str, *, item: str) -> tuple[str, str]:
    if "=" not in value:
        raise ArgumentTypeError(f"expected NAME={item}: {value!r}")
    key, raw = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ArgumentTypeError(f"empty name in {value!r}")
    return key, raw.strip()


def _weight_pair(value: str) -> tuple[str, float]:
    key, raw = _mapping_pairs(value, item="WEIGHT")
    try:
        weight = float(raw)
    except ValueError as exc:
        raise ArgumentTypeError(f"invalid weight in {value!r}") from exc
    return key, weight


def _dependency_pairs(value: str) -> tuple[str, list[str]]:
    key, raw = _mapping_pairs(value, item="CASES")
    dependencies = [part for part in raw.replace(";", ",").split(",") if part.strip()]
    return key.strip(), [part.strip() for part in dependencies]


def _device_pairs(value: str) -> tuple[str, str]:
    key, device = _mapping_pairs(value, item="DEVICE")
    if not device:
        raise ArgumentTypeError(f"missing device in {value!r}")
    return key, device


def recheck(
    paths,
    *,
    window=DEFAULT_WINDOW,
    slots=1,
    weights=None,
    default_weight=1.0,
    required=(),
    depends=(),
    devices=(),
    overrides=None,
    output_format="text",
):
    """Plan a worst-case recheck table from historical JUnit reports."""
    result = plan_recheck(
        paths,
        window=window,
        slots=slots,
        weights=weights,
        default_weight=default_weight,
        required=required,
        prerequisites=depends,
        devices=devices,
        overrides=overrides,
    )
    text = plan_as_json(result) if output_format == "json" else format_plan(result)
    print(text)
    return 0 if result.feasible else 1


def merge(paths, output, suite_name=""):
    """Merge XML reports."""
    result = JUnitXml()
    for path in paths:
        result += JUnitXml.fromfile(path)

    result.update_statistics()
    if suite_name:
        result.name = suite_name
    result.write(sys.stdout if output == "-" else output)
    return 0


def verify(paths):
    """Verify if none of the testcases failed or errored."""
    # We could grab the number of failures and errors from the statistics of the root element
    # or from the test suites elements, but those attributes are not guaranteed to be present
    # or correct. So we'll just loop over all the testcases.
    for path in paths:
        xml = JUnitXml.fromfile(path)
        for suite in xml:
            for case in suite:
                if not case.is_passed and not case.is_skipped:
                    return 1
    return 0


def _parser(prog_name=None):  # pragma: no cover
    """Create the CLI arg parser."""
    parser = ArgumentParser(description="Junitparser CLI helper.", prog=prog_name)

    parser.add_argument(
        "-v", "--version", action="version", version="%(prog)s " + version
    )

    command_parser = parser.add_subparsers(dest="command", help="command")
    command_parser.required = True

    # an abstract object that defines common arguments used by multiple commands
    abstract_parser = ArgumentParser(add_help=False)
    abstract_parser.add_argument(
        "--glob",
        help="Treat original XML path(s) as glob(s).",
        dest="paths_are_globs",
        action="store_true",
        default=False,
    )
    abstract_parser.add_argument("paths", help="Original XML path(s).", nargs="+")

    # command: merge
    merge_parser = command_parser.add_parser(
        "merge",
        help="Merge JUnit XML format reports with junitparser.",
        parents=[abstract_parser],
    )
    merge_parser.add_argument(
        "output", help='Merged XML Path, setting to "-" will output to the console'
    )
    merge_parser.add_argument(
        "--suite-name",
        help="Name added to <testsuites>.",
    )

    # command: verify
    verify_parser = command_parser.add_parser(  # noqa: F841
        "verify",
        help="Return a non-zero exit code if one of the testcases failed or errored.",
        parents=[abstract_parser],
    )

    # command: recheck
    recheck_parser = command_parser.add_parser(
        "recheck",
        help="Plan a worst-case recheck table from historical JUnit reports.",
        parents=[abstract_parser],
    )
    recheck_parser.add_argument(
        "--spec",
        help="JSON file supplying window, slots, weights and constraints.",
    )
    recheck_parser.add_argument(
        "--window",
        type=_seconds,
        default=None,
        help="Build window, e.g. 12m, 720s (default: 12m).",
    )
    recheck_parser.add_argument(
        "--slots",
        type=int,
        default=None,
        help="Number of parallel device slots (default: 1).",
    )
    recheck_parser.add_argument(
        "--weight",
        type=_weight_pair,
        action="append",
        default=[],
        metavar="CLASS=WEIGHT",
        help="Coverage weight of a failure class; repeatable.",
    )
    recheck_parser.add_argument(
        "--default-weight",
        type=float,
        default=None,
        help="Weight for classes not named by --weight (default: 1.0).",
    )
    recheck_parser.add_argument(
        "--required",
        action="append",
        default=[],
        metavar="CASE",
        help="Case that must run regardless of coverage; repeatable.",
    )
    recheck_parser.add_argument(
        "--depends",
        type=_dependency_pairs,
        action="append",
        default=[],
        metavar="CASE=DEP[,DEP]",
        help="Prerequisite edges; repeatable.",
    )
    recheck_parser.add_argument(
        "--device",
        type=_device_pairs,
        action="append",
        default=[],
        metavar="CASE=DEVICE",
        help="Dedicated device required by a case; repeatable.",
    )
    recheck_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )

    return parser


def _dependency_map(pairs):
    result = {}
    for key, values in pairs:
        result.setdefault(key, []).extend(values)
    return result


def main(args=None, prog_name=None):
    """CLI's main runner."""
    args = _parser(prog_name=prog_name).parse_args(args)
    paths = (
        chain.from_iterable(iglob(path) for path in args.paths)
        if args.paths_are_globs
        else args.paths
    )
    if args.command == "merge":
        return merge(paths, args.output, args.suite_name)
    if args.command == "verify":
        return verify(paths)
    if args.command == "recheck":
        try:
            spec = load_spec(args.spec) if args.spec else {}
            weights = dict(spec.get("weights", {}))
            weights.update(dict(args.weight))
            prerequisite_map = {
                key: list(value) for key, value in spec.get("prerequisites", {}).items()
            }
            for key, values in _dependency_map(args.depends).items():
                prerequisite_map.setdefault(key, []).extend(values)
            device_map = dict(spec.get("devices", {}))
            device_map.update(dict(args.device))
            required = list(spec.get("required", [])) + list(args.required)
            return recheck(
                paths,
                window=args.window
                if args.window is not None
                else spec.get("window", DEFAULT_WINDOW),
                slots=args.slots if args.slots is not None else spec.get("slots", 1),
                weights=weights,
                default_weight=args.default_weight
                if args.default_weight is not None
                else spec.get("default_weight", 1.0),
                required=required,
                depends=prerequisite_map,
                devices=device_map,
                overrides=spec.get("overrides"),
                output_format=args.format,
            )
        except (RecheckError, OSError, ValueError) as exc:
            print(f"recheck: {exc}", file=sys.stderr)
            return 2
    return 255
