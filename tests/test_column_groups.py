"""Tests for tools/column_groups.py (stdlib only)."""

import itertools
import random
import unittest

from tools.column_groups import Field, Instance, Query, Row, solve


def partitions(items):
    if not items:
        yield ()
        return
    first, rest = items[0], items[1:]
    for k in range(len(rest) + 1):
        for block in itertools.combinations(rest, k):
            block = (first,) + block
            remaining = [x for x in rest if x not in block]
            for tail in partitions(remaining):
                yield (block,) + tail


def brute_force(inst):
    fields = list(inst.fields)
    best = None
    for groups in partitions(list(range(len(fields)))):
        total = 0
        legal = True
        for block in groups:
            members = {fields[i].key for i in block}
            widths = []
            size = inst.header_bytes
            written = False
            for row in inst.rows:
                hit = [fields[i] for i in block if fields[i].frame == row.frame
                       and fields[i].name in row.present]
                if not hit:
                    continue
                written = True
                size += sum(f.bytes for f in hit)
                widths.append(sum(f.bytes for f in hit))
            if not written:
                continue
            if inst.header_bytes + max(widths) > inst.width_limit:
                legal = False
                break
            for q in inst.queries:
                if any(k in members for k in q.fields):
                    total += q.frequency * size
        if legal and (best is None or total < best):
            best = total
    return best


class SolverTest(unittest.TestCase):
    def test_dp_matches_brute_force(self):
        rng = random.Random(11)
        for _ in range(80):
            n = rng.randint(1, 6)
            fields = [Field("F", f"s{i}", rng.randint(1, 4)) for i in range(n)]
            rows = []
            for _ in range(rng.randint(1, 4)):
                rows.append(Row("F", frozenset(
                    f.name for f in fields if rng.random() < 0.6)))
            queries = []
            for qi in range(rng.randint(1, 3)):
                pick = rng.sample(fields, rng.randint(1, min(2, n)))
                queries.append(Query(f"q{qi}", tuple(f.key for f in pick),
                                     rng.randint(1, 5)))
            header = rng.randint(0, 3)
            width = rng.randint(header + max(f.bytes for f in fields),
                               header + sum(f.bytes for f in fields))
            inst = Instance(fields, rows, queries, header, width)
            got, _, _ = solve(inst)
            self.assertEqual(got, brute_force(inst), (fields, rows, queries, header, width))

    def test_group_skipped_when_row_has_none_of_its_fields(self):
        fields = [Field("A", "x", 2), Field("A", "y", 2)]
        rows = [Row("A", frozenset({"x"})), Row("A", frozenset({"y"}))]
        queries = [Query("qx", (("A", "x"),), 1)]
        total, groups, _ = solve(Instance(fields, rows, queries, 1, 10))
        # x alone: header once + one written row of 2 bytes.
        self.assertEqual(total, 3)

    def test_zero_reading_keeps_its_slot(self):
        fields = [Field("A", "x", 2)]
        rows = [Row("A", frozenset({"x"})), Row("A", frozenset({"x"}))]
        queries = [Query("qx", (("A", "x"),), 1)]
        total, _, _ = solve(Instance(fields, rows, queries, 1, 10))
        self.assertEqual(total, 1 + 2 * 2)

    def test_two_columns_of_one_group_read_once(self):
        fields = [Field("A", "x", 2), Field("A", "y", 3)]
        rows = [Row("A", frozenset({"x", "y"}))]
        queries = [Query("q", (("A", "x"), ("A", "y")), 1)]
        total, groups, per_query = solve(Instance(fields, rows, queries, 1, 10))
        self.assertEqual(total, 1 + 2 + 3)
        self.assertEqual(len(groups), 1)
        self.assertEqual(per_query[0][1], 6)

    def test_mutually_exclusive_signals_fit_one_slot(self):
        # x and y never appear in the same row, so one group's row width is
        # header+4 rather than header+8.  Splitting is still cheaper here
        # because each read then pays a smaller file; the point is only that
        # the merged group is legal at width 8.
        fields = [Field("A", "x", 4), Field("A", "y", 4)]
        rows = [Row("A", frozenset({"x"})), Row("A", frozenset({"y"}))]
        queries = [
            Query("qx", (("A", "x"),), 1),
            Query("qy", (("A", "y"),), 1),
        ]
        total, groups, _ = solve(Instance(fields, rows, queries, 4, 8))
        self.assertEqual(total, 16)
        self.assertEqual(len(groups), 2)

    def test_merging_wins_when_a_query_reads_both_columns(self):
        # Same slots, but one hot query needs both columns: merging pays the
        # header once instead of twice.
        fields = [Field("A", "x", 4), Field("A", "y", 4)]
        rows = [Row("A", frozenset({"x"})), Row("A", frozenset({"y"}))]
        queries = [Query("both", (("A", "x"), ("A", "y")), 1)]
        merged = solve(Instance(fields, rows, queries, 4, 12))
        self.assertEqual(len(merged[1]), 1)
        self.assertEqual(merged[0], 12)  # header 4 + one 4B payload per row

    def test_width_cap_forces_split(self):
        fields = [Field("A", "x", 4), Field("A", "y", 4)]
        rows = [Row("A", frozenset({"x", "y"}))]
        queries = [Query("q", (("A", "x"), ("A", "y")), 1)]
        total, groups, _ = solve(Instance(fields, rows, queries, 4, 9))
        self.assertEqual(len(groups), 2)


if __name__ == "__main__":
    unittest.main()
