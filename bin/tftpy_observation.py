#!/usr/bin/env python
# vim: ts=4 sw=4 et ai:
# -*- coding: utf-8 -*-

"""Analyze a timestamped TFTP observation log.

Reads log lines such as::

    100.050 DAT block=1 len=512

from a file or standard input, and reports unique payload, first-delivery
timing (exact or bracketed), retransmit overhead and undeterminable bytes.
The log may cover only the middle of a transfer.
"""

import argparse
import sys

import tftpy


def main():
    parser = argparse.ArgumentParser(
        description="Analyze timestamped TFTP observations.")
    parser.add_argument(
        "logfile",
        nargs="?",
        default="-",
        help="observation log file, or - for standard input (default)",
    )
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        default=False,
        help="emit machine-readable JSON instead of a text report",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        default=False,
        help="skip DAT lines without a payload length instead of failing",
    )
    options = parser.parse_args()

    strict = not options.lenient
    if options.logfile == "-":
        observations = tftpy.parse_observations(sys.stdin, strict=strict)
    else:
        observations = tftpy.load_observations(
            options.logfile, strict=strict)

    analysis = tftpy.analyze_observations(observations)
    if options.json:
        sys.stdout.write(tftpy.analysis_to_json(analysis))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(tftpy.render_report(analysis))
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
