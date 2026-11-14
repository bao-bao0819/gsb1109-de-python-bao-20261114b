#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command line interface for the J1939 per-device operating-day ledger."""

import json
import logging
import sys

import click

import canmatrix.j1939_ledger

logger = logging.getLogger(__name__)


@click.command()
@click.argument("batches", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="write the JSON ledger report here instead of stdout",
)
def cli_j1939_ledger(batches, output):
    """Reconcile J1939 recorder batches into a per-device operating-day ledger.

    Each BATCH is a JSON file describing one recorder's receive window:

    \b
    {
      "recorder": "rec-1",
      "window": {"start": "2026-11-14T07:00:00", "end": "2026-11-14T12:00:00"},
      "frames": [
        {"timestamp": "2026-11-14T08:00:00",
         "can_id": "0x18EEFF21", "data": "0011223344556677"}
      ]
    }

    Address-claimed frames (PGN 0x00EE00) carry the 64 bit device NAME, the
    stable identity used for the ledger; every other frame is a measurement
    attributed by source address, claim windows and the J1939 claim priority
    rules.  Batches may be listed in any order -- the merge follows the
    frame timestamps, not the file order.
    """
    if not batches:
        raise click.UsageError("at least one recorder batch file is required")
    ledger = canmatrix.j1939_ledger.reconcile_files(list(batches))
    report = json.dumps(ledger.to_dict(), indent=2, sort_keys=True)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        logger.info("ledger report written to %s", output)
    else:
        sys.stdout.write(report + "\n")


if __name__ == "__main__":
    cli_j1939_ledger()
