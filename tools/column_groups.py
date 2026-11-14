"""Exact solver for splitting an offline wide table into column groups.

Model
-----
The wide table has one row per sampled original CAN frame.  Columns are the
(frame, signal) fields decoded by canmatrix.  A *column group* is an arbitrary
set of fields; it may contain fields from several frames.

Storage of one group:

* The group is written for a row only if at least one of its fields is present
  in that row.  If none is present, the whole group is skipped for that row.
* A written row costs the bytes of that group's fields which are present in the
  row.  A legitimate zero reading is present and keeps its slot; only a truly
  absent field is free.  Thus two mutually exclusive multiplexed signals can
  share the same physical slot.
* The group header costs ``header_bytes`` once per written group (zero if the
  group is never written).
* ``width_bytes`` is ``header_bytes + max(present member bytes over written
  rows)`` and must not exceed ``width_limit``.  Rows are sparse, so two signals
  that never co-occur share the same physical slot and do not add up.
* A query reads every group it touches exactly once, even when it names two
  columns of that group.

Objective: ``sum(query.frequency * bytes_read_by_query)``.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import math
from typing import Dict, FrozenSet, Iterable, List, Sequence, Tuple


Key = Tuple[str, str]


@dataclass(frozen=True)
class Field:
    frame: str
    name: str
    bytes: int

    @property
    def key(self) -> Key:
        return (self.frame, self.name)


@dataclass(frozen=True)
class Query:
    name: str
    fields: Tuple[Key, ...]
    frequency: int


@dataclass(frozen=True)
class Row:
    frame: str
    present: FrozenSet[str]


@dataclass
class Group:
    members: Tuple[Key, ...]
    header_bytes: int
    payload_bytes: int
    records: int
    width_bytes: int

    @property
    def id(self) -> str:
        return " + ".join(f"{frame}.{name}" for frame, name in self.members)

    def read_bytes(self) -> int:
        return self.header_bytes + self.payload_bytes


@dataclass
class Instance:
    fields: Sequence[Field]
    rows: Sequence[Row]
    queries: Sequence[Query]
    header_bytes: int
    width_limit: int


class InfeasibleGrouping(ValueError):
    pass


def _row_masks(instance: Instance) -> List[int]:
    index = {f.key: i for i, f in enumerate(instance.fields)}
    masks = []
    for row in instance.rows:
        mask = 0
        for i, f in enumerate(instance.fields):
            if f.frame == row.frame and f.name in row.present:
                mask |= 1 << i
        masks.append(mask)
    return masks


def solve(instance: Instance) -> Tuple[int, List[Group], List[Tuple[Query, int]]]:
    """Return (weighted total bytes, groups, per-query read bytes)."""
    fields = list(instance.fields)
    n = len(fields)
    if n == 0:
        return 0, [], [(q, 0) for q in instance.queries]
    if n > 16:
        raise ValueError(
            f"exact set-partition DP handles up to 16 fields, got {n}; "
            "split the instance or add an ILP backend"
        )

    header = instance.header_bytes
    width_limit = instance.width_limit
    full = (1 << n) - 1
    index = {f.key: i for i, f in enumerate(fields)}

    for i, f in enumerate(fields):
        if header + f.bytes > width_limit:
            raise InfeasibleGrouping(
                f"field {f.frame}.{f.name} needs {header + f.bytes}B, "
                f"over width limit {width_limit}B"
            )

    present_masks = _row_masks(instance)

    # Per-group metrics.  A group's payload is the sum over written rows of the
    # member bytes actually present; its width is the largest such row.
    written = [0] * (full + 1)
    payload = [0] * (full + 1)
    widest = [0] * (full + 1)
    for mask in range(1, full + 1):
        records = 0
        total = 0
        row_max = 0
        for row in present_masks:
            hit = row & mask
            if not hit:
                continue
            row_bytes = 0
            bits = hit
            while bits:
                low = bits & -bits
                row_bytes += fields[low.bit_length() - 1].bytes
                bits ^= low
            records += 1
            total += row_bytes
            row_max = max(row_max, row_bytes)
        written[mask] = records
        payload[mask] = total
        widest[mask] = row_max

    size = [0] * (full + 1)
    legal = [False] * (full + 1)
    for mask in range(1, full + 1):
        size[mask] = header + payload[mask] if written[mask] else 0
        # A group with no written row costs nothing and is always allowed.
        legal[mask] = written[mask] == 0 or header + widest[mask] <= width_limit

    qmask = []
    for q in instance.queries:
        mask = 0
        for key in q.fields:
            if key not in index:
                raise KeyError(f"query {q.name!r} references unknown field {key}")
            mask |= 1 << index[key]
        qmask.append(mask)

    # Query cost is additive over groups: a query pays each touched group once.
    group_cost = [0] * (full + 1)
    for mask in range(1, full + 1):
        if not legal[mask]:
            continue
        group_cost[mask] = size[mask] * sum(
            q.frequency for qi, q in enumerate(instance.queries) if qmask[qi] & mask
        )

    best_cost = [math.inf] * (full + 1)
    best_groups = [math.inf] * (full + 1)
    choice = [0] * (full + 1)
    best_cost[0] = 0
    best_groups[0] = 0

    for subset in range(1, full + 1):
        low = subset & -subset
        group = subset
        while group:
            if group & low and legal[group]:
                rest = subset ^ group
                if not math.isinf(best_cost[rest]):
                    cand_cost = best_cost[rest] + group_cost[group]
                    cand_groups = best_groups[rest] + 1
                    if (cand_cost, cand_groups) < (best_cost[subset], best_groups[subset]):
                        best_cost[subset] = cand_cost
                        best_groups[subset] = cand_groups
                        choice[subset] = group
            group = (group - 1) & subset

    if math.isinf(best_cost[full]):
        raise InfeasibleGrouping(f"no grouping fits width limit {width_limit}B")

    groups: List[Group] = []
    subset = full
    while subset:
        mask = choice[subset]
        members = tuple(fields[i].key for i in range(n) if mask >> i & 1)
        groups.append(Group(
            members=members,
            header_bytes=header if written[mask] else 0,
            payload_bytes=payload[mask],
            records=written[mask],
            width_bytes=header + widest[mask] if written[mask] else 0,
        ))
        subset ^= mask
    groups.sort(key=lambda g: (g.members[0][0], g.members[0][1], g.id))

    per_query: List[Tuple[Query, int]] = []
    for qi, q in enumerate(instance.queries):
        total = 0
        for g in groups:
            gm = 0
            for key in g.members:
                gm |= 1 << index[key]
            if qmask[qi] & gm:
                total += g.read_bytes()
        per_query.append((q, total))

    return int(best_cost[full]), groups, per_query


def load_instance(path: str) -> Instance:
    with open(path) as fh:
        cfg = json.load(fh)
    fields = [Field(x["frame"], x["signal"], int(x["bytes"])) for x in cfg["fields"]]
    raw_rows = cfg.get("rows", [])
    if isinstance(raw_rows, dict):
        raise ValueError("rows must be a list of {frame, present}; dict form is ambiguous")
    rows = [Row(r["frame"], frozenset(r["present"])) for r in raw_rows]
    queries = [
        Query(q["name"], tuple((f, s) for f, s in q["fields"]), int(q["frequency"]))
        for q in cfg["queries"]
    ]
    return Instance(fields, rows, queries, int(cfg["header_bytes"]), int(cfg["width_limit"]))


def format_report(instance: Instance, total: int, groups: List[Group],
                  per_query: List[Tuple[Query, int]]) -> str:
    lines = [f"total weighted bytes: {total}", "groups:"]
    for g in groups:
        lines.append(
            f"  [{g.id}] width={g.width_bytes}B records={g.records} "
            f"header={g.header_bytes}B payload={g.payload_bytes}B read={g.read_bytes()}B"
        )
    lines.append("queries:")
    for q, b in per_query:
        lines.append(
            f"  {q.name:26s} freq={q.frequency:6d} bytes/run={b:7d} "
            f"weighted={b * q.frequency:9d}"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exact offline column-group solver")
    ap.add_argument("instance", help="instance JSON")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    instance = load_instance(args.instance)
    total, groups, per_query = solve(instance)
    if args.json:
        print(json.dumps({
            "total_bytes": total,
            "groups": [
                {
                    "members": [[f, s] for f, s in g.members],
                    "records": g.records,
                    "header_bytes": g.header_bytes,
                    "payload_bytes": g.payload_bytes,
                    "read_bytes": g.read_bytes(),
                    "width_bytes": g.width_bytes,
                }
                for g in groups
            ],
            "queries": [
                {"name": q.name, "frequency": q.frequency, "bytes": b,
                 "weighted_bytes": b * q.frequency}
                for q, b in per_query
            ],
        }, indent=2))
    else:
        print(format_report(instance, total, groups, per_query))


if __name__ == "__main__":
    main()
