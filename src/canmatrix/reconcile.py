# -*- coding: utf-8 -*-
"""Reconcile a legacy status report with a re-mapped value table.

A monthly status report may only store the *text* of the value table that was
current when it was produced, plus a count.  If two raw codes shared one text
(for example both were ``"待机"``) and the new value table classifies one of
them as ``"维护"``, the old report alone cannot say how many occurrences had
each new status.  The ambiguous total has to be distributed over the raw
codes, subject to whatever detail survived.

This module answers, exactly:

* What is the minimum and maximum count each new status can have?
* Can several proposed counts hold *in the same report*?  Per-column maxima
  can come from incompatible distributions, so each column being inside its
  own range is not enough.
* What does a retained subtotal imply?  A subtotal constrains only the
  section ("partition") it names.  Equal text appearing in two sections
  denotes two separate constraints and is never merged merely because the
  text matches.

The model
---------
For every report section ``p`` and every old raw code ``c`` there is a
non-negative integer variable ``x[p][c]``: the number of occurrences of code
``c`` in section ``p``.  Inputs become linear equalities:

* a scoped old-report row ``(p, label, n)`` sums ``x[p][c]`` over codes whose
  *old* text is ``label``;
* an unscoped row ``(None, label, n)`` sums all sections (a report-wide
  total);
* a per-code subtotal ``(p, code, n)`` pins ``x[p][code]`` (or the sum of all
  sections when unscoped);
* a per-label subtotal ``(p, label, n)`` sums old codes with that label in
  that section only.

A new status total is the sum of ``x[p][c]`` over codes whose *new* text is
that status, either in one section or in all sections.  Ranges are exact
integer ranges over all assignments satisfying the equalities.  Arithmetic is
exact (:mod:`fractions`) and no optional MILP package is required.

Public API
----------
:func:`reconcile`
    Build a model and compute ranges.
:func:`can_coexist`
    One-shot joint-feasibility question.
:class:`Scenario`
    Explicit inputs (old table, new table, report, subtotals, membership,
    declared partitions).
:class:`Reconciliation`
    Model with :meth:`~Reconciliation.ranges`,
    :meth:`~Reconciliation.minimum`, :meth:`~Reconciliation.maximum`,
    :meth:`~Reconciliation.can_coexist`,
    :meth:`~Reconciliation.witness`, and :meth:`~Reconciliation.explain`.
:data:`ALL_PARTITIONS`
    Sentinel meaning "sum over every section".
:func:`from_canmatrix`
    Read the two value tables from :class:`canmatrix.CanMatrix` objects.

Example
-------
>>> old = {0: "待机", 1: "待机", 2: "运行"}
>>> new = {0: "待机", 1: "维护", 2: "运行"}
>>> report = {"待机": 180, "运行": 20}
>>> model = reconcile(old, new, report)
>>> model.ranges()["待机"]
(0, 180)
>>> model.ranges()["维护"]
(0, 180)
>>> model.can_coexist({"维护": 100, "待机": 80})
True
>>> model.can_coexist({"维护": 100, "待机": 100})
False
"""

import collections
import collections.abc
import fractions
import typing

from canmatrix._milp import BranchLimit
from canmatrix._milp import UnboundedLP
from canmatrix._milp import feasible_point
from canmatrix._milp import maximize
from canmatrix._milp import minimize


class ReconciliationError(ValueError):
    """Base class for malformed or inconsistent reconciliation inputs."""


class InconsistentReport(ReconciliationError):
    """The report totals and subtotals have no common solution."""


class UnknownStatus(ReconciliationError):
    """A requested status is not present in the new value table."""


class _AllPartitions(object):
    """Sentinel selecting the sum over every section of the report."""

    __slots__ = ()

    def __repr__(self):
        return "ALL_PARTITIONS"


ALL_PARTITIONS = _AllPartitions()


def _normalise_partition(value):
    # type: (typing.Any) -> typing.Optional[str]
    if isinstance(value, _AllPartitions) or value is None:
        return None
    text = str(value).strip()
    return text if text != "" else None


def _normalise_code(value):
    # type: (typing.Any) -> int
    if isinstance(value, bool):
        raise ReconciliationError("a raw code must not be a boolean: %r" % (value,))
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 0)
        except ValueError:
            pass
        try:
            # Zero-padded decimal codes such as "08" are common in exports;
            # int(text, 0) rejects them, but they are still decimal.
            return int(text, 10)
        except ValueError:
            raise ReconciliationError("invalid raw code: %r" % (value,))
    raise ReconciliationError("invalid raw code: %r" % (value,))


def _normalise_count(value, description):
    # type: (typing.Any, str) -> int
    if isinstance(value, bool):
        raise ReconciliationError("%s must be an integer, not a boolean" % description)
    if isinstance(value, int):
        count = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ReconciliationError("%s must be an integer: %r" % (description, value))
        count = int(value)
    elif isinstance(value, str):
        try:
            number = fractions.Fraction(value.strip())
        except (ValueError, ZeroDivisionError):
            raise ReconciliationError("%s is not a number: %r" % (description, value))
        if number.denominator != 1:
            raise ReconciliationError("%s must be an integer: %r" % (description, value))
        count = int(number)
    else:
        raise ReconciliationError("%s must be an integer: %r" % (description, value))
    if count < 0:
        raise ReconciliationError("%s must not be negative: %d" % (description, count))
    return count


def _normalise_label(value, description="status text"):
    # type: (typing.Any, str) -> str
    if value is None:
        raise ReconciliationError("%s must not be None" % description)
    text = str(value).strip()
    if text == "":
        raise ReconciliationError("%s must not be empty" % description)
    return text


def _normalise_table(table, which):
    # type: (typing.Mapping[typing.Any, typing.Any], str) -> "collections.OrderedDict[int, str]"
    if not isinstance(table, collections.abc.Mapping):
        raise ReconciliationError("%s value table must map raw codes to text" % which)
    parsed = collections.OrderedDict()
    for raw_code, raw_text in table.items():
        code = _normalise_code(raw_code)
        text = _normalise_label(raw_text, "%s text for code %d" % (which, code))
        if code in parsed and parsed[code] != text:
            raise ReconciliationError(
                "%s value table defines code %d twice (%r and %r)" %
                (which, code, parsed[code], text))
        parsed[code] = text
    if not parsed:
        raise ReconciliationError("%s value table is empty" % which)
    return collections.OrderedDict(sorted(parsed.items()))


_COUNT_KEYS = ("count", "quantity", "total", "数量", "条数")
_LABEL_KEYS = ("label", "status", "text", "状态", "名称", "文字")
_CODE_KEYS = ("code", "raw_code", "value", "原码", "编码")
_PARTITION_KEYS = ("partition", "section", "scope", "分区", "区域", "区段")


def _row_field(entry, *names):
    # type: (typing.Mapping, *str) -> typing.Any
    for name in names:
        if name in entry:
            return entry[name]
    return None


def _looks_like_record(entry):
    # type: (typing.Any) -> bool
    if not isinstance(entry, collections.abc.Mapping):
        return False
    keys = set(str(key) for key in entry.keys())
    has_count = bool(keys & set(_COUNT_KEYS))
    has_target = bool(keys & set(_LABEL_KEYS + _CODE_KEYS))
    return has_count and has_target


class ReportRow(object):
    """An old-report row.  ``partition=None`` means report-wide scope."""

    __slots__ = ("partition", "label", "count")

    def __init__(self, partition, label, count):
        # type: (typing.Optional[str], str, int) -> None
        self.partition = partition
        self.label = label
        self.count = count

    def __repr__(self):
        return "ReportRow(partition=%r, label=%r, count=%r)" % (
            self.partition, self.label, self.count)


class Subtotal(object):
    """A retained subtotal for one raw code or one old label.

    ``partition=None`` means the subtotal is report-wide.  ``code`` and
    ``label`` are mutually exclusive.
    """

    __slots__ = ("partition", "code", "label", "count")

    def __init__(self, partition, count, code=None, label=None):
        # type: (typing.Optional[str], int, typing.Optional[int], typing.Optional[str]) -> None
        self.partition = partition
        self.code = code
        self.label = label
        self.count = count

    def __repr__(self):
        target = "code=%r" % self.code if self.code is not None else "label=%r" % self.label
        return "Subtotal(partition=%r, %s, count=%r)" % (
            self.partition, target, self.count)


def _expand_sections(document, description):
    # type: (typing.Mapping, str) -> typing.List[typing.Dict[str, typing.Any]]
    """Flatten ``{section: rows-or-{label: count}}`` into explicit rows.

    A plain ``{label: count}`` document stays untouched.  Section grouping is
    recognised only when every value is itself a mapping or a sequence, which
    cannot be confused with a count.
    """
    if not document:
        return []
    values = list(document.values())
    grouped = all(isinstance(value, (collections.abc.Mapping,
                                     collections.abc.Sequence)) and
                  not isinstance(value, (str, bytes))
                  for value in values)
    if not grouped:
        return [{"label": label, "count": count}
                for label, count in document.items()]
    rows = []
    for section, entries in document.items():
        if isinstance(entries, collections.abc.Mapping):
            if _looks_like_record(entries):
                entries = [entries]
            elif entries:
                entries = [{"label": label, "count": count}
                           for label, count in entries.items()]
            else:
                entries = []
        for entry in entries:
            if not isinstance(entry, collections.abc.Mapping):
                raise ReconciliationError(
                    "invalid %s row in section %r: %r" %
                    (description, section, entry))
            row = dict(entry)
            row.setdefault("partition", section)
            rows.append(row)
    return rows


def _parse_report(report):
    # type: (typing.Any) -> typing.List[ReportRow]
    if isinstance(report, collections.abc.Mapping):
        if _looks_like_record(report):
            report = [report]
        else:
            report = _expand_sections(report, "report")
    elif isinstance(report, collections.abc.Sequence) and not isinstance(report, (str, bytes)):
        report = list(report)
    else:
        raise ReconciliationError("report must be a mapping or a sequence of rows")

    rows = []
    for entry in report:
        if isinstance(entry, collections.abc.Mapping):
            label = _row_field(entry, *_LABEL_KEYS)
            count = _row_field(entry, *_COUNT_KEYS)
            partition = _row_field(entry, *_PARTITION_KEYS)
            if label is None or count is None:
                raise ReconciliationError("report row needs label and count: %r" % (entry,))
            rows.append(ReportRow(_normalise_partition(partition),
                                  _normalise_label(label, "report label"),
                                  _normalise_count(count, "report count")))
        elif isinstance(entry, (tuple, list)):
            if len(entry) == 2:
                label, count = entry
                rows.append(ReportRow(None, _normalise_label(label, "report label"),
                                      _normalise_count(count, "report count")))
            elif len(entry) == 3:
                partition, label, count = entry
                rows.append(ReportRow(_normalise_partition(partition),
                                      _normalise_label(label, "report label"),
                                      _normalise_count(count, "report count")))
            else:
                raise ReconciliationError("report row must have 2 or 3 fields: %r" % (entry,))
        else:
            raise ReconciliationError("invalid report row: %r" % (entry,))
    return rows


def _subtotal_mapping_rows(document, old_table):
    # type: (typing.Mapping, typing.Mapping[int, str]) -> typing.List[typing.Dict[str, typing.Any]]
    """Turn a section or flat subtotal mapping into rows.

    ``{"0x1": 30}`` and ``{"待机": 30}`` are both accepted; the old value
    table decides whether a key denotes a raw code or a label.  A sectioned
    document (``{"A": {"0x1": 30}}``) is flattened first.  Rows already in
    record form (``{"code": 0, "count": 30}``) pass through unchanged.
    """
    if not document:
        return []
    labels = set(old_table.values())

    def split_key(key):
        try:
            code = _normalise_code(key)
        except ReconciliationError:
            return "label", _normalise_label(key, "subtotal key")
        if str(key).strip() in labels:
            return "label", str(key).strip()
        if old_table and code not in old_table:
            raise ReconciliationError("subtotal refers to unknown old code %d" % code)
        return "code", code

    if isinstance(document, collections.abc.Mapping) and _looks_like_record(document):
        return [dict(document)]

    if isinstance(document, (list, tuple)):
        rows = []
        for entry in document:
            if isinstance(entry, collections.abc.Mapping):
                if _looks_like_record(entry):
                    rows.append(dict(entry))
                else:
                    rows.extend(_subtotal_mapping_rows(entry, old_table))
            elif isinstance(entry, (tuple, list)):
                if len(entry) == 2:
                    raw_target, count = entry
                    partition = None
                elif len(entry) == 3:
                    partition, raw_target, count = entry
                else:
                    raise ReconciliationError(
                        "subtotal row must have 2 or 3 fields: %r" % (entry,))
                kind, value = split_key(raw_target)
                row = {"count": count, kind: value}
                if partition is not None:
                    row["partition"] = partition
                rows.append(row)
            else:
                raise ReconciliationError("invalid subtotal row: %r" % (entry,))
        return rows

    grouped = all(isinstance(value, (collections.abc.Mapping, list, tuple))
                  for value in document.values())
    if grouped:
        rows = []
        for section, entries in document.items():
            if not isinstance(entries, (collections.abc.Mapping, list, tuple)):
                raise ReconciliationError("invalid subtotal section %r" % (section,))
            for row in _subtotal_mapping_rows(entries, old_table):
                row["partition"] = section
                rows.append(row)
        return rows

    rows = []
    for key, count in document.items():
        kind, value = split_key(key)
        row = {"count": count}
        row[kind] = value
        rows.append(row)
    return rows


def _parse_subtotals(subtotals, old_table=None):
    # type: (typing.Any, typing.Optional[typing.Mapping[int, str]]) -> typing.List[Subtotal]
    old_table = dict(old_table or {})
    parsed = []
    if subtotals is None:
        return parsed
    if isinstance(subtotals, collections.abc.Mapping):
        if _looks_like_record(subtotals):
            subtotals = [subtotals]
        else:
            subtotals = _subtotal_mapping_rows(subtotals, old_table)
    elif isinstance(subtotals, collections.abc.Sequence) and not isinstance(subtotals, (str, bytes)):
        subtotals = list(subtotals)
    else:
        raise ReconciliationError("subtotals must be a mapping or a sequence of rows")

    for entry in subtotals:
        if isinstance(entry, collections.abc.Mapping):
            count = _row_field(entry, *_COUNT_KEYS)
            if count is None:
                raise ReconciliationError("subtotal row needs a count: %r" % (entry,))
            partition = _normalise_partition(_row_field(entry, *_PARTITION_KEYS))
            raw_code = _row_field(entry, *_CODE_KEYS)
            raw_label = _row_field(entry, *_LABEL_KEYS)
            if (raw_code is None) == (raw_label is None):
                raise ReconciliationError(
                    "subtotal row needs exactly one of code or label: %r" % (entry,))
            count_value = _normalise_count(count, "subtotal count")
            if raw_code is not None:
                parsed.append(Subtotal(partition, count_value,
                                       code=_normalise_code(raw_code)))
            else:
                parsed.append(Subtotal(partition, count_value,
                                       label=_normalise_label(raw_label, "subtotal label")))
        elif isinstance(entry, (tuple, list)):
            if len(entry) == 2:
                raw_code, count = entry
                partition = None
            elif len(entry) == 3:
                partition, raw_code, count = entry
            else:
                raise ReconciliationError("subtotal row must have 2 or 3 fields: %r" % (entry,))
            parsed.append(Subtotal(_normalise_partition(partition),
                                   _normalise_count(count, "subtotal count"),
                                   code=_normalise_code(raw_code)))
        else:
            raise ReconciliationError("invalid subtotal row: %r" % (entry,))
    return parsed


def _parse_partitions(partitions):
    # type: (typing.Any) -> typing.List[str]
    if partitions is None:
        return []
    if isinstance(partitions, str):
        partitions = [partitions]
    if not isinstance(partitions, collections.abc.Sequence):
        raise ReconciliationError("partitions must be a sequence of section names")
    result = []
    for value in partitions:
        name = _normalise_partition(value)
        if name is None:
            raise ReconciliationError("a declared partition needs a non-empty name")
        if name not in result:
            result.append(name)
    return result


def _parse_membership(membership):
    # type: (typing.Any) -> typing.Dict[int, str]
    parsed = {}
    if membership is None:
        return parsed
    if isinstance(membership, collections.abc.Mapping):
        items = list(membership.items())
    elif isinstance(membership, collections.abc.Sequence) and not isinstance(membership, (str, bytes)):
        items = []
        for entry in membership:
            if isinstance(entry, collections.abc.Mapping):
                raw_code = _row_field(entry, *_CODE_KEYS)
                partition = _row_field(entry, *_PARTITION_KEYS)
                if raw_code is None:
                    raise ReconciliationError("membership row needs a code: %r" % (entry,))
                items.append((raw_code, partition))
            elif isinstance(entry, (tuple, list)) and len(entry) == 2:
                items.append((entry[0], entry[1]))
            else:
                raise ReconciliationError("invalid membership row: %r" % (entry,))
    else:
        raise ReconciliationError("membership must be a mapping or a sequence of rows")

    for raw_code, raw_partition in items:
        code = _normalise_code(raw_code)
        partition = _normalise_partition(raw_partition)
        if partition is None:
            raise ReconciliationError("membership for code %d names no partition" % code)
        if code in parsed and parsed[code] != partition:
            raise ReconciliationError(
                "code %d is assigned to two sections (%r and %r)" %
                (code, parsed[code], partition))
        parsed[code] = partition
    return parsed


class Scenario(object):
    """Validated reconciliation inputs.

    :param old_table: ``{raw_code: old_text}``.
    :param new_table: ``{raw_code: new_status}``.
    :param report: old report.  A mapping ``{label: count}`` is report-wide;
        a sequence may contain ``(partition, label, count)`` tuples or
        mappings with ``partition``, ``label`` and ``count``.
    :param subtotals: retained subtotals.  A mapping ``{raw_code: count}`` is
        report-wide; a sequence may contain ``(partition, raw_code, count)``
        tuples or mappings with ``partition``, ``code`` (or ``label``) and
        ``count``.
    :param membership: optional ``{raw_code: partition}`` restricting a code
        to one section.  Codes without an entry may occur in any section.
    :param partitions: optional sequence declaring sections that have no rows
        (for example an empty section).
    """

    def __init__(self, old_table, new_table, report, subtotals=(),
                 membership=None, partitions=None):
        self.old_table = _normalise_table(old_table, "old")
        self.new_table = _normalise_table(new_table, "new")
        self.report = _parse_report(report)
        self.subtotals = _parse_subtotals(subtotals, self.old_table)
        self.membership = _parse_membership(membership)

        missing = [code for code in self.old_table if code not in self.new_table]
        if missing:
            raise ReconciliationError(
                "old code(s) missing from the new value table: %s" %
                ", ".join(str(code) for code in missing))

        scoped = [row.partition for row in self.report if row.partition is not None]
        scoped.extend(sub.partition for sub in self.subtotals if sub.partition is not None)
        scoped.extend(self.membership.values())
        scoped.extend(_parse_partitions(partitions))
        explicit = []
        for name in scoped:
            if name not in explicit:
                explicit.append(name)
        self.partitions = explicit if explicit else [None]  # type: typing.List[typing.Optional[str]]
        if explicit:
            known = set(explicit)
            for code, partition in self.membership.items():
                if partition not in known:
                    raise ReconciliationError(
                        "membership sends code %d to unknown section %r" % (code, partition))
            for row in self.report:
                if row.partition is not None and row.partition not in known:
                    raise ReconciliationError("unknown report section %r" % row.partition)
            for subtotal in self.subtotals:
                if subtotal.partition is not None and subtotal.partition not in known:
                    raise ReconciliationError("unknown subtotal section %r" % subtotal.partition)

        self.codes = list(self.old_table)
        self._index = {code: index for index, code in enumerate(self.codes)}
        self._validate_subtotals()

    def _validate_subtotals(self):
        for subtotal in self.subtotals:
            if subtotal.code is not None:
                if subtotal.code not in self.old_table:
                    raise ReconciliationError(
                        "subtotal refers to unknown old code %d" % subtotal.code)
                if (subtotal.partition is not None and
                        self.membership.get(subtotal.code, subtotal.partition) != subtotal.partition):
                    raise ReconciliationError(
                        "subtotal puts code %d in section %r, but its membership is %r" %
                        (subtotal.code, subtotal.partition,
                         self.membership.get(subtotal.code)))
            else:
                if not self.old_codes(subtotal.label, subtotal.partition):
                    raise ReconciliationError(
                        "subtotal for label %r matches no old code in section %r" %
                        (subtotal.label, subtotal.partition))

    def old_codes(self, label, partition=None):
        # type: (str, typing.Optional[str]) -> typing.List[int]
        result = []
        for code in self.old_table:
            if self.old_table[code] != label:
                continue
            if partition is not None:
                member = self.membership.get(code)
                if member is not None and member != partition:
                    continue
            result.append(code)
        return result

    def allows(self, code, partition):
        # type: (int, typing.Optional[str]) -> bool
        member = self.membership.get(code)
        return member is None or member == partition

    def new_status(self, code):
        # type: (int) -> str
        return self.new_table[code]

    def statuses(self):
        # type: () -> typing.List[str]
        seen = []
        for code in self.new_table:
            status = self.new_table[code]
            if status not in seen:
                seen.append(status)
        return seen


def from_canmatrix(old_matrix, new_matrix, report, subtotals=(),
                   membership=None, partitions=None, value_table=None):
    # type: (typing.Any, typing.Any, typing.Any, typing.Any, typing.Any, typing.Any, typing.Optional[str]) -> Scenario
    """Build a :class:`Scenario` from CAN matrices.

    Both arguments may be :class:`canmatrix.CanMatrix` instances or mappings
    of value-table name to ``{raw_code: text}``.  When ``value_table`` is
    omitted and each matrix has exactly one value table, that table is used.
    """
    return Scenario(
        _pick_value_table(old_matrix, "old", value_table),
        _pick_value_table(new_matrix, "new", value_table),
        report, subtotals, membership, partitions)


def _pick_value_table(matrix, which, value_table=None):
    # type: (typing.Any, str, typing.Optional[str]) -> typing.Mapping
    tables = getattr(matrix, "value_tables", None)
    if tables is None:
        if not isinstance(matrix, collections.abc.Mapping):
            raise TypeError("%s must be a CanMatrix or a value-table mapping" % which)
        tables = matrix
        if "value_tables" in tables and isinstance(tables["value_tables"], collections.abc.Mapping):
            tables = tables["value_tables"]
        elif "enumerations" in tables and isinstance(tables["enumerations"], collections.abc.Mapping):
            tables = tables["enumerations"]
        elif not all(isinstance(value, collections.abc.Mapping)
                     for value in tables.values()):
            # A plain ``{raw_code: text}`` table, not a mapping of tables.
            return tables
    if not isinstance(tables, collections.abc.Mapping):
        raise TypeError("%s value tables must be mappings" % which)
    if value_table is not None:
        if value_table not in tables:
            raise ReconciliationError(
                "%s matrix has no value table %r" % (which, value_table))
        return tables[value_table]
    if len(tables) == 1:
        return next(iter(tables.values()))
    if len(tables) == 0:
        raise ReconciliationError("%s matrix has no value tables" % which)
    raise ReconciliationError(
        "%s matrix has %d value tables; pass value_table explicitly" %
        (which, len(tables)))


class _Constraint(object):
    __slots__ = ("coefficients", "count", "description")

    def __init__(self, coefficients, count, description):
        # type: (typing.Dict[int, int], int, str) -> None
        self.coefficients = coefficients
        self.count = count
        self.description = description


class _Component(object):
    __slots__ = ("variables", "constraints")

    def __init__(self, variables, constraints):
        self.variables = variables
        self.constraints = constraints


class Reconciliation(object):
    """Exact ranges and joint-feasibility queries for a :class:`Scenario`."""

    def __init__(self, scenario):
        # type: (Scenario) -> None
        self.scenario = scenario
        self._variables = self._build_variables()
        self._position = {variable: index
                          for index, variable in enumerate(self._variables)}
        self._constraints = self._build_constraints()
        self._components, self._free = self._build_components()
        self._check_feasible()
        self._range_cache = {}

    def _build_variables(self):
        # type: () -> typing.List[typing.Tuple[typing.Optional[str], int]]
        variables = []
        for partition in self.scenario.partitions:
            for code in self.scenario.codes:
                if self.scenario.allows(code, partition):
                    variables.append((partition, code))
        return variables

    def _terms(self, partitions, codes):
        # type: (typing.Iterable[typing.Optional[str],], typing.Iterable[int]) -> typing.Dict[int, int]
        terms = {}
        codes = set(codes)
        for partition in partitions:
            for code in codes:
                variable = (partition, code)
                index = self._position.get(variable)
                if index is not None:
                    terms[index] = 1
        return terms

    def _build_constraints(self):
        # type: () -> typing.List[_Constraint]
        scenario = self.scenario
        constraints = []
        seen = {}

        # Report rows.
        for row in scenario.report:
            partitions = (scenario.partitions if row.partition is None
                          else (row.partition,))
            codes = scenario.old_codes(row.label, row.partition)
            terms = self._terms(partitions, codes)
            scope = ("all sections" if row.partition is None
                     else "section %r" % row.partition)
            if not terms:
                raise ReconciliationError(
                    "reported label %r has no usable old code in %s" %
                    (row.label, scope))
            key = ("report", row.partition, row.label)
            if key in seen and seen[key] != row.count:
                raise ReconciliationError(
                    "report gives conflicting counts for %r in %s: %r and %r" %
                    (row.label, scope, seen[key], row.count))
            seen[key] = row.count
            constraints.append(_Constraint(
                terms, row.count,
                "report row %r (%s)" % (row.label, scope)))

        # Retained subtotals.
        for subtotal in scenario.subtotals:
            partitions = (scenario.partitions if subtotal.partition is None
                          else (subtotal.partition,))
            if subtotal.code is not None:
                terms = self._terms(partitions, [subtotal.code])
                description = "subtotal for code %d" % subtotal.code
                key = ("subtotal", subtotal.partition, "code", subtotal.code)
            else:
                codes = scenario.old_codes(subtotal.label, subtotal.partition)
                terms = self._terms(partitions, codes)
                description = "subtotal for label %r in %s" % (
                    subtotal.label,
                    "all sections" if subtotal.partition is None
                    else "section %r" % subtotal.partition)
                key = ("subtotal", subtotal.partition, "label", subtotal.label)
            if not terms:
                raise ReconciliationError("%s matches no report occurrence" % description)
            if key in seen and seen[key] != subtotal.count:
                raise ReconciliationError(
                    "conflicting counts in %s: %r and %r" %
                    (description, seen[key], subtotal.count))
            seen[key] = subtotal.count
            constraints.append(_Constraint(terms, subtotal.count, description))
        return constraints

    def _build_components(self):
        # type: () -> typing.Tuple[typing.List[_Component], typing.List[int]]
        variable_count = len(self._variables)
        parent = list(range(variable_count))

        def find(index):
            # type: (int) -> int
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            # type: (int, int) -> None
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        touched = set()
        for constraint in self._constraints:
            indices = sorted(constraint.coefficients)
            touched.update(indices)
            for index in indices[1:]:
                union(indices[0], index)

        groups = collections.OrderedDict()
        for constraint in self._constraints:
            root = find(next(iter(constraint.coefficients)))
            groups.setdefault(root, _Component([], []))
            groups[root].constraints.append(constraint)
        for index in sorted(touched):
            root = find(index)
            groups.setdefault(root, _Component([], []))
            groups[root].variables.append(index)
        for component in groups.values():
            component.variables.sort()

        free = [index for index in range(variable_count) if index not in touched]
        return list(groups.values()), free

    def _local_equalities(self, component):
        # type: (_Component) -> typing.Tuple[typing.List[int], typing.List[typing.List[int]]]
        positions = {global_index: local_index
                     for local_index, global_index in enumerate(component.variables)}
        equalities = []
        for constraint in component.constraints:
            row = [0] * len(component.variables)
            for global_index, coefficient in constraint.coefficients.items():
                row[positions[global_index]] = coefficient
            equalities.append(row)
        return component.variables, equalities

    def _constraint_rhs(self, component):
        # type: (_Component) -> typing.List[int]
        return [constraint.count for constraint in component.constraints]

    def _check_feasible(self):
        # type: () -> None
        for component in self._components:
            variables, equalities = self._local_equalities(component)
            try:
                point = feasible_point(equalities, self._constraint_rhs(component))
            except (BranchLimit, UnboundedLP) as error:
                raise InconsistentReport(
                    "could not verify the report exactly: %s" % error)
            if point is None:
                involved = sorted(set(
                    code for index in variables
                    for code in (self._variables[index][1],)))
                raise InconsistentReport(
                    "report totals and subtotals cannot all hold; conflicting "
                    "constraints involve raw code(s): %s" %
                    ", ".join(str(code) for code in involved))

    def statuses(self):
        # type: () -> typing.List[str]
        return self.scenario.statuses()

    def status_codes(self, status):
        # type: (str) -> typing.List[int]
        if status not in self.scenario.new_table.values():
            raise UnknownStatus("unknown new status %r; known statuses: %s" %
                                (status, ", ".join(self.statuses())))
        return [code for code in self.scenario.codes
                if self.scenario.new_status(code) == status]

    def _validate_partition(self, partition):
        # type: (typing.Any) -> typing.Union[typing.Optional[str], _AllPartitions]
        if isinstance(partition, _AllPartitions):
            return ALL_PARTITIONS
        if partition is None and self.scenario.partitions != [None]:
            # With explicit sections, None means "whole report" rather than a
            # section name.  The default section only exists when no explicit
            # sections were supplied.
            return ALL_PARTITIONS
        name = _normalise_partition(partition)
        if name is None and self.scenario.partitions == [None]:
            return None
        if name not in self.scenario.partitions:
            raise ReconciliationError(
                "unknown section %r; sections: %s" %
                (name, ", ".join(repr(p) for p in self.scenario.partitions)))
        return name

    def _includes(self, variable, status, partition):
        # type: (typing.Tuple[typing.Optional[str], int], str, typing.Any) -> bool
        variable_partition, code = variable
        if self.scenario.new_status(code) != status:
            return False
        if partition is ALL_PARTITIONS:
            return True
        return variable_partition == partition

    def ranges(self, partition=ALL_PARTITIONS):
        # type: (typing.Any) -> "collections.OrderedDict[str, typing.Tuple[int, typing.Union[int, float]]]"
        """Return ``{status: (minimum, maximum)}``.

        By default the range is for the whole report (all sections added
        together).  Pass a section name for that section only.  ``maximum`` is
        ``float("inf")`` when the inputs do not bound the status from above.
        """
        partition = self._validate_partition(partition)
        key = (partition,) if partition is not ALL_PARTITIONS else ("all",)
        if key in self._range_cache:
            return self._range_cache[key]
        result = collections.OrderedDict()
        for status in self.statuses():
            result[status] = self._range_for(status, partition)
        self._range_cache[key] = result
        return result

    def overall_ranges(self):
        # type: () -> "collections.OrderedDict[str, typing.Tuple[int, typing.Union[int, float]]]"
        """Alias for the default :meth:`ranges`."""
        return self.ranges(ALL_PARTITIONS)

    def _range_for(self, status, partition):
        # type: (str, typing.Any) -> typing.Tuple[int, typing.Union[int, float]]
        minimum = 0
        maximum = 0
        unbounded = False

        for component in self._components:
            variables = [self._variables[index] for index in component.variables]
            objective = [1 if self._includes(variable, status, partition) else 0
                         for variable in variables]
            if not any(objective):
                continue
            _indices, equalities = self._local_equalities(component)
            rhs = self._constraint_rhs(component)
            try:
                low = minimize(objective, equalities, rhs)
                high = maximize(objective, equalities, rhs)
            except BranchLimit as error:
                raise InconsistentReport(
                    "could not compute the range for status %r exactly: %s" %
                    (status, error))
            except UnboundedLP:
                # A feasible but upward-unbounded component still has a
                # well-defined minimum; report an infinite maximum.
                low = minimize(objective, equalities, rhs)
                high = None
            if low is None or (high is None and not unbounded):
                if high is None:
                    unbounded = True
                else:
                    raise InconsistentReport(
                        "the report has no solution while computing status %r" % status)
            if low is not None:
                minimum += int(low[0])
            if high is not None:
                maximum += int(high[0])

        for index in self._free:
            if self._includes(self._variables[index], status, partition):
                unbounded = True

        return minimum, float("inf") if unbounded else maximum

    def minimum(self, status, partition=ALL_PARTITIONS):
        # type: (str, typing.Any) -> int
        """Minimum count of ``status`` in ``partition``."""
        if status not in self.scenario.new_table.values():
            raise UnknownStatus("unknown new status %r; known statuses: %s" %
                                (status, ", ".join(self.statuses())))
        return self.ranges(partition)[status][0]

    def maximum(self, status, partition=ALL_PARTITIONS):
        # type: (str, typing.Any) -> typing.Union[int, float]
        """Maximum count of ``status`` in ``partition`` (may be infinite)."""
        if status not in self.scenario.new_table.values():
            raise UnknownStatus("unknown new status %r; known statuses: %s" %
                                (status, ", ".join(self.statuses())))
        return self.ranges(partition)[status][1]

    def _constraint_rows(self):
        # type: () -> typing.Tuple[typing.List[typing.List[int]], typing.List[int]]
        equalities = []
        rhs = []
        for constraint in self._constraints:
            row = [0] * len(self._variables)
            for index, coefficient in constraint.coefficients.items():
                row[index] = coefficient
            equalities.append(row)
            rhs.append(constraint.count)
        return equalities, rhs

    def _query_constraints(self, counts, partition):
        # type: (typing.Mapping[str, typing.Any], typing.Any) -> typing.Tuple[typing.List[typing.List[int]], typing.List[int]]
        if not isinstance(counts, collections.abc.Mapping):
            raise ReconciliationError("counts must be a mapping of status to count")
        equalities, rhs = self._constraint_rows()
        seen = set()
        for raw_status, raw_value in counts.items():
            status = _normalise_label(raw_status, "query status")
            if status in seen:
                raise ReconciliationError("status %r requested twice" % status)
            seen.add(status)
            if status not in self.scenario.new_table.values():
                raise UnknownStatus("unknown new status %r; known statuses: %s" %
                                    (status, ", ".join(self.statuses())))
            if isinstance(raw_value, collections.abc.Mapping):
                for raw_section, raw_count in raw_value.items():
                    section = self._validate_partition(raw_section)
                    if section is ALL_PARTITIONS:
                        raise ReconciliationError(
                            "a per-section count cannot use ALL_PARTITIONS")
                    count = _normalise_count(raw_count, "count for %r in %r" %
                                             (status, section))
                    row = [1 if self._includes(variable, status, section) else 0
                           for variable in self._variables]
                    equalities.append(row)
                    rhs.append(count)
            else:
                count = _normalise_count(raw_value, "count for %r" % status)
                row = [1 if self._includes(variable, status, partition) else 0
                       for variable in self._variables]
                equalities.append(row)
                rhs.append(count)
        return equalities, rhs

    def can_coexist(self, counts, partition=ALL_PARTITIONS):
        # type: (typing.Mapping[str, typing.Any], typing.Any) -> bool
        """Whether all ``{status: count}`` values can occur in one report.

        Every entry is an exact equality, so this is stricter than checking
        each count against its own individual range.  For example, if one old
        total of 180 has to supply both ``maintenance`` and ``standby``, each
        status can individually reach 180, but ``maintenance=100`` and
        ``standby=100`` cannot hold together.

        A value may also be a ``{section: count}`` mapping to constrain one
        status separately in several sections.
        """
        partition = self._validate_partition(partition)
        equalities, rhs = self._query_constraints(counts, partition)
        try:
            point = feasible_point(equalities, rhs)
        except (BranchLimit, UnboundedLP) as error:
            raise InconsistentReport(
                "could not answer the coexistence query exactly: %s" % error)
        return point is not None

    def witness(self, counts=None, partition=ALL_PARTITIONS):
        # type: (typing.Optional[typing.Mapping[str, typing.Any]], typing.Any) -> typing.Optional["collections.OrderedDict"]
        """Return one satisfying assignment ``{section: {code: count}}``.

        With ``counts`` supplied, the assignment also matches those status
        totals.  ``None`` means infeasible.
        """
        partition = self._validate_partition(partition)
        equalities, rhs = self._query_constraints(counts or {}, partition)
        try:
            point = feasible_point(equalities, rhs)
        except (BranchLimit, UnboundedLP) as error:
            raise InconsistentReport(
                "could not construct a witness exactly: %s" % error)
        if point is None:
            return None
        assignment = collections.OrderedDict(
            (section, collections.OrderedDict()) for section in self.scenario.partitions)
        for variable, value in zip(self._variables, point):
            section, code = variable
            assignment[section][code] = int(value)
        return assignment

    def status_total(self, assignment, status, partition=ALL_PARTITIONS):
        # type: (typing.Mapping, str, typing.Any) -> int
        """Count ``status`` in a witness assignment."""
        if status not in self.scenario.new_table.values():
            raise UnknownStatus("unknown new status %r" % status)
        partition = self._validate_partition(partition)
        total = 0
        for section, counts in assignment.items():
            if partition is not ALL_PARTITIONS and section != partition:
                continue
            for code, count in counts.items():
                code = _normalise_code(code)
                if self.scenario.new_status(code) == status:
                    total += _normalise_count(count, "count for code %d" % code)
        return total

    def explain(self):
        # type: () -> typing.List[str]
        """Return human-readable range lines for every status."""
        lines = []
        for status, (minimum, maximum) in self.ranges().items():
            maximum_text = "unbounded" if maximum == float("inf") else str(maximum)
            lines.append("%s: %d .. %s" % (status, minimum, maximum_text))
        return lines


def reconcile(old_table, new_table, report, subtotals=(), membership=None,
              partitions=None):
    # type: (typing.Mapping, typing.Mapping, typing.Any, typing.Any, typing.Any, typing.Any) -> Reconciliation
    """Build a :class:`Reconciliation` from plain mappings and rows."""
    return Reconciliation(Scenario(old_table, new_table, report, subtotals,
                                   membership, partitions))


def can_coexist(old_table, new_table, report, counts, subtotals=(),
                membership=None, partitions=None, partition=ALL_PARTITIONS):
    # type: (typing.Mapping, typing.Mapping, typing.Any, typing.Mapping, typing.Any, typing.Any, typing.Any, typing.Any) -> bool
    """One-shot joint-feasibility question."""
    return reconcile(old_table, new_table, report, subtotals, membership,
                     partitions).can_coexist(counts, partition)


__all__ = [
    "ALL_PARTITIONS",
    "InconsistentReport",
    "Reconciliation",
    "ReconciliationError",
    "ReportRow",
    "Scenario",
    "Subtotal",
    "UnknownStatus",
    "can_coexist",
    "from_canmatrix",
    "reconcile",
]
