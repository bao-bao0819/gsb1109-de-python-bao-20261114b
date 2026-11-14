import sys
from argparse import ArgumentParser
from fractions import Fraction
from glob import iglob
from itertools import chain

from . import JUnitXml, version
from .timingaudit import EXCEEDED, SHORTFALL, audit_timing, format_report


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


def timeaudit(paths, prep_min="0", prep_max="0"):
    """Audit suite timings for discrepancies beyond rounding noise.

    Prints one line per suite. Returns 1 if any suite's children cannot fit
    into the suite's own time (suspected double counting) or leave time
    unaccounted for, 0 otherwise.
    """
    prep_range = (Fraction(prep_min), Fraction(prep_max))
    exit_code = 0
    for path in paths:
        xml = JUnitXml.fromfile(path)
        findings = audit_timing(xml, prep_range=prep_range)
        report = format_report(findings)
        if report:
            print(report)
        if any(f.status in (EXCEEDED, SHORTFALL) for f in findings):
            exit_code = 1
    return exit_code


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

    # command: timeaudit
    timeaudit_parser = command_parser.add_parser(
        "timeaudit",
        help="Audit suite timings for discrepancies beyond rounding noise.",
        parents=[abstract_parser],
    )
    timeaudit_parser.add_argument(
        "--prep-min",
        help="Minimum preparation time per suite, in seconds.",
        default="0",
    )
    timeaudit_parser.add_argument(
        "--prep-max",
        help="Maximum preparation time per suite, in seconds.",
        default="0",
    )

    return parser


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
    if args.command == "timeaudit":
        return timeaudit(paths, args.prep_min, args.prep_max)
    return 255
