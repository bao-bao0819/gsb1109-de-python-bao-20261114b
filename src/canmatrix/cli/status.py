#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command line interface for legacy status-report reconciliation.

Examples
--------
Print the feasible count range of every new status::

    canstatus --old old.json --new new.json --report report.json

Answer whether a proposed pair of counts can occur in one report::

    canstatus --old old.json --new new.json --report report.json \
        --ask "维护=100" --ask "待机=80"

Input files are JSON or CSV (``-`` reads JSON from standard input).  A table
file is either a plain ``{raw_code: text}`` mapping, a two-column CSV, or a
canmatrix JSON export; for the latter pass ``--value-table NAME`` when more
than one table exists.  The report and subtotal files accept the same flexible
forms as :mod:`canmatrix.reconcile`.
"""

import csv
import json
import sys

import click

import canmatrix.reconcile as reconcile_module


_PARTITION_KEYS = ("partition", "section", "scope", "分区", "区域", "区段")
_LABEL_KEYS = ("label", "status", "text", "状态", "名称", "文字")
_COUNT_KEYS = ("count", "quantity", "total", "数量", "条数")
_CODE_KEYS = ("code", "raw_code", "value", "原码", "编码")
_VALUE_KEYS = ("value_tables", "enumerations", "valueTables")


def _read_document(path):
    if path == "-":
        return json.load(sys.stdin)
    if path.lower().endswith(".csv"):
        with open(path, "r", encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _find_column(fieldnames, candidates):
    normalised = {str(name).strip().lower(): name for name in fieldnames or []}
    for candidate in candidates:
        if candidate.lower() in normalised:
            return normalised[candidate.lower()]
    return None


def _table_from_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        code_column = _find_column(fieldnames, _CODE_KEYS)
        text_column = _find_column(fieldnames, _LABEL_KEYS)
        rows = list(reader)
    if code_column is None or text_column is None:
        if len(fieldnames) >= 2:
            code_column, text_column = fieldnames[0], fieldnames[1]
        else:
            raise click.ClickException(
                "%s needs columns for raw code and text" % path)
    table = {}
    for row in rows:
        raw_code = (row.get(code_column) or "").strip()
        text = (row.get(text_column) or "").strip()
        if raw_code == "" or text == "":
            continue
        if raw_code in table and table[raw_code] != text:
            raise click.ClickException(
                "%s defines code %r twice (%r and %r)" %
                (path, raw_code, table[raw_code], text))
        table[raw_code] = text
    if not table:
        raise click.ClickException("%s contains no value-table rows" % path)
    return table


def _pick_table(document, path, value_table):
    if isinstance(document, dict):
        for key in _VALUE_KEYS:
            if isinstance(document.get(key), dict) and document[key] and \
                    all(isinstance(value, dict) for value in document[key].values()):
                tables = document[key]
                if value_table is None:
                    if len(tables) == 1:
                        return next(iter(tables.values()))
                    raise click.ClickException(
                        "%s contains %d value tables; pass --value-table" %
                        (path, len(tables)))
                if value_table not in tables:
                    raise click.ClickException(
                        "%s has no value table %r" % (path, value_table))
                return tables[value_table]
    if not isinstance(document, dict):
        raise click.ClickException("%s must contain a JSON object" % path)
    return document


def _field(record, names):
    for name in names:
        if name in record:
            return record[name]
    return None


def _membership_from_document(document, path):
    if isinstance(document, dict) and _field(document, _PARTITION_KEYS) is None:
        # Either {code: section} or {section: [codes...]}.
        values = list(document.values())
        if values and all(not isinstance(value, (dict, list)) for value in values):
            return document
        if values and all(isinstance(value, list) for value in values):
            mapping = {}
            for section, codes in document.items():
                for code in codes:
                    mapping[code] = section
            return mapping
    records = document if isinstance(document, list) else [document]
    mapping = {}
    for record in records:
        if not isinstance(record, dict):
            raise click.ClickException("invalid membership row in %s: %r" % (path, record))
        code = _field(record, _CODE_KEYS)
        section = _field(record, _PARTITION_KEYS)
        if code is None or section is None:
            raise click.ClickException(
                "membership row needs a code and a partition in %s: %r" % (path, record))
        mapping[code] = section
    return mapping


def _parse_ask(values):
    counts = {}
    for value in values:
        if "=" not in value:
            raise click.BadParameter("expected STATUS=COUNT, got %r" % value)
        status, raw_count = value.rsplit("=", 1)
        status = status.strip()
        if not status:
            raise click.BadParameter("missing status in %r" % value)
        try:
            counts[status] = int(raw_count.strip(), 0)
        except ValueError:
            raise click.BadParameter("count in %r is not an integer" % value)
    return counts


@click.command()
@click.option("--old", "old_path", required=True, metavar="FILE",
              help="Old value table (JSON, or - for stdin).")
@click.option("--new", "new_path", required=True, metavar="FILE",
              help="New value table (JSON, or - for stdin).")
@click.option("--report", "report_path", required=True, metavar="FILE",
              help="Legacy report (label totals or scoped rows).")
@click.option("--subtotals", "subtotal_path", metavar="FILE",
              help="Retained per-code or per-label subtotals.")
@click.option("--membership", "membership_path", metavar="FILE",
              help="Optional code-to-section membership.")
@click.option("--partitions", "partition_text", metavar="NAMES",
              help="Comma-separated section names, including empty sections.")
@click.option("--value-table", "value_table", metavar="NAME",
              help="Value table to use when an input file contains several.")
@click.option("--section", "section", metavar="NAME",
              help="Report ranges for one section instead of the whole report.")
@click.option("--ask", "asks", multiple=True, metavar="STATUS=COUNT",
              help="Ask whether counts can coexist in one report. May repeat.")
@click.option("--ask-json", "ask_json", metavar="FILE",
              help="JSON object of status-to-count values to test together.")
@click.option("--json-output", "json_output", is_flag=True, default=False,
              help="Emit machine-readable JSON instead of text.")
def cli_status(old_path, new_path, report_path, subtotal_path, membership_path,
               partition_text, value_table, section, asks, ask_json, json_output):
    """Reconcile a legacy status report against a re-mapped value table."""
    old_table = _table_from_csv(old_path) if old_path.lower().endswith(".csv") \
        else _pick_table(_read_document(old_path), old_path, value_table)
    new_table = _table_from_csv(new_path) if new_path.lower().endswith(".csv") \
        else _pick_table(_read_document(new_path), new_path, value_table)

    report = _read_document(report_path)
    subtotals = _read_document(subtotal_path) if subtotal_path else []

    membership = None
    if membership_path:
        membership = _membership_from_document(_read_document(membership_path),
                                               membership_path)

    partitions = None
    if partition_text:
        partitions = [name.strip() for name in partition_text.split(",") if name.strip()]
    elif isinstance(report, dict):
        # Section-grouped reports declare their sections implicitly.
        if report and all(isinstance(value, dict) for value in report.values()):
            partitions = list(report.keys())

    requested = _parse_ask(asks)
    if ask_json:
        document = _read_document(ask_json)
        if not isinstance(document, dict):
            raise click.ClickException("--ask-json must contain an object")
        for key, value in document.items():
            if key in requested and requested[key] != value:
                raise click.ClickException("conflicting counts for %r" % key)
            requested[key] = value

    try:
        model = reconcile_module.Reconciliation(reconcile_module.Scenario(
            old_table, new_table, report, subtotals, membership, partitions))
        ranges = model.ranges(section if section is not None else reconcile_module.ALL_PARTITIONS)
        coexist = None
        if requested:
            coexist = model.can_coexist(
                requested,
                section if section is not None else reconcile_module.ALL_PARTITIONS)
    except reconcile_module.ReconciliationError as error:
        raise click.ClickException(str(error))

    if json_output:
        payload = {
            "ranges": {status: [minimum, None if maximum == float("inf") else maximum]
                       for status, (minimum, maximum) in ranges.items()},
            "coexist": coexist,
            "counts": requested or None,
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    for status, (minimum, maximum) in ranges.items():
        maximum_text = "unbounded" if maximum == float("inf") else str(maximum)
        click.echo("%s: %d .. %s" % (status, minimum, maximum_text))
    if coexist is not None:
        summary = ", ".join("%s=%s" % (status, count)
                            for status, count in requested.items())
        click.echo("%s: %s" % (summary, "yes" if coexist else "no"))


if __name__ == "__main__":
    sys.exit(cli_status())
