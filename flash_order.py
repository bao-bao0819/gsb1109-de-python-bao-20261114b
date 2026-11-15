#!/usr/bin/env python3
"""NOR-flash snapshot version reconstruction.

Physics assumed (from the task statement):
  * Between two erasures of a page, programming only flips bits 1 -> 0.
  * An erasure restores a whole page to all-ones (0xFF per byte).
  * Erasure register entries carry a page number; every entry may be used
    at most once, so each page gets a fixed erasure budget.
  * At most eight snapshots per inventory.
  * Missing bytes in a HEX snapshot are unknown, never 0xFF.

Given a directory of Intel-HEX snapshots and an erasure register, enumerate
every global snapshot ordering that is explainable within the register
budgets, keep only pairwise precedence relations that hold in *every*
feasible ordering, and when nothing is feasible name the snapshots that
need at least one unregistered erasure.

Register JSON format:
  {"page_size": 256, "base": 0, "register": [1, 1, 3]}
where "register" is the list of erased page numbers (repetitions allowed,
each entry is one usable erasure).

Usage:
  python3 flash_order.py --snapshots ./snaps --register register.json
  python3 flash_order.py --demo
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intelhex import IntelHex  # noqa: E402

UNRELATED = "unrelated"          # no common byte in this page
FORWARD = "forward_clean"        # x -> y needs no erasure, strict flips
EQUAL = "equal"                  # identical on every common byte
CONFLICT = "conflict"            # flips in both directions, erase mandatory


def load_snapshot(path: Path) -> dict[int, int]:
    ih = IntelHex(str(path))
    return dict(ih.todict())


def popcount(value: int) -> int:
    return value.bit_count()


def pair_relation(x_bytes: dict[int, int], y_bytes: dict[int, int]) -> tuple[str, int]:
    """Relation of two snapshots restricted to one page's common addresses."""
    common = x_bytes.keys() & y_bytes.keys()
    if not common:
        return UNRELATED, 0
    x_to_y_ones = 0   # bits that are 1 in x but 0 in y (legal 1->0 flips)
    y_to_x_ones = 0   # bits that are 1 in y but 0 in x (would need an erase)
    for addr in common:
        xb, yb = x_bytes[addr], y_bytes[addr]
        x_to_y_ones += popcount(xb & ~yb & 0xFF)
        y_to_x_ones += popcount(yb & ~xb & 0xFF)
    if x_to_y_ones == 0 and y_to_x_ones == 0:
        return EQUAL, 0
    if y_to_x_ones == 0:
        return FORWARD, x_to_y_ones
    if x_to_y_ones == 0:
        return CONFLICT, -y_to_x_ones
    return CONFLICT, 0


def parse_register(doc, page_size):
    """Accept a page-number list, a {page: count} map, or {"register": ...}."""
    if isinstance(doc, dict) and "register" in doc:
        doc = doc["register"]
    budgets = defaultdict(int)
    if isinstance(doc, dict):
        for page, count in doc.items():
            budgets[int(page)] += int(count)
    else:
        for page in doc:
            budgets[int(page)] += 1
    return budgets


def evaluate_order(order, page_of, pages, data, budgets):
    """Return (feasible, erasures_used, extra_breaks).

    For a fixed order, a page needs one erasure per consecutive conflict
    transition; forward-clean runs chain by bitwise-subset transitivity, so
    the conflict count is the minimum erasures that explain that order.
    extra_breaks maps a page to the snapshots that begin an unsupported cycle.
    """
    used = defaultdict(int)
    extra: dict[int, list[str]] = defaultdict(list)
    for page in pages:
        active = [name for name in order
                  if any(page_of(addr) == page for addr in data[name])]
        prev = None
        for name in active:
            if prev is not None:
                rel, _ = _REL[(prev, name, page)]
                if rel == CONFLICT:
                    if used[page] < budgets.get(page, 0):
                        used[page] += 1
                    else:
                        extra[page].append(name)
            prev = name
    return not extra, dict(used), dict(extra)


_REL = {}


def build_relations(names, page_of, pages, data):
    for page in pages:
        on_page = {
            name: {a: b for a, b in data[name].items() if page_of(a) == page}
            for name in names
        }
        for x, y in itertools.permutations(names, 2):
            _REL[(x, y, page)] = pair_relation(on_page[x], on_page[y])


def reconstruct(snapshots_dir: Path, register_path: Path, show_limit: int = 20):
    register_doc = json.loads(register_path.read_text())
    page_size = register_doc.get("page_size", 256) if isinstance(register_doc, dict) else 256
    base = register_doc.get("base", 0) if isinstance(register_doc, dict) else 0
    budgets = parse_register(register_doc, page_size)

    paths = sorted(p for p in snapshots_dir.iterdir() if p.suffix.lower() in {".hex", ".ihex", ".h86"})
    if not paths:
        raise SystemExit(f"no HEX snapshots found under {snapshots_dir}")
    if len(paths) > 8:
        raise SystemExit("at most eight snapshots per inventory are supported")
    names = [p.stem for p in paths]
    data = {p.stem: load_snapshot(p) for p in paths}

    def page_of(addr):
        return (addr - base) // page_size

    pages = sorted({page_of(addr) for blob in data.values() for addr in blob})
    build_relations(names, page_of, pages, data)

    feasible_orders = []
    failures = []
    for perm in itertools.permutations(names):
        feasible, used, extra = evaluate_order(perm, page_of, pages, data, budgets)
        if feasible:
            feasible_orders.append((perm, used))
        else:
            failures.append((sum(len(v) for v in extra.values()), perm, extra, used))

    print(f"snapshots ({len(names)}): {', '.join(names)}")
    print(f"pages seen: {pages}")
    print(f"register erasure budgets: {dict(budgets)}")
    print()

    print("per-page pairwise evidence (strict 1->0 programming edges):")
    any_edge = False
    for page in pages:
        for x, y in itertools.combinations(names, 2):
            rel, weight = _REL[(x, y, page)]
            rev, rev_weight = _REL[(y, x, page)]
            if rel == FORWARD:
                any_edge = True
                print(f"  page {page}: {x} -> {y} ({weight} bit(s) flipped 1->0)")
            elif rev == FORWARD:
                any_edge = True
                print(f"  page {page}: {y} -> {x} ({rev_weight} bit(s) flipped 1->0)")
            elif rel == CONFLICT:
                print(f"  page {page}: {x} <-> {y} conflict, an erasure must separate them")
    if not any_edge:
        print("  (no strict same-cycle progression anywhere)")
    print()

    if feasible_orders:
        fixed = set()
        for x, y in itertools.permutations(names, 2):
            if all(perm.index(x) < perm.index(y) for perm, _ in feasible_orders):
                fixed.add((x, y))
        print(f"feasible global orders: {len(feasible_orders)}")
        for perm, used in feasible_orders[:show_limit]:
            print(f"  {' < '.join(perm)}   erasures={used}")
        if len(feasible_orders) > show_limit:
            print(f"  ... {len(feasible_orders) - show_limit} more")
        print()
        print("evidenced precedence holding in EVERY feasible order:")
        for x, y in sorted(fixed):
            print(f"  {x} < {y}")
        if not fixed:
            print("  (none: the snapshots are mutually interchangeable on evidence)")
        return 0

    print("no ordering fits the registered erasures.")
    minimum = min(deficit for deficit, _, _, _ in failures)
    best_cases = [case for case in failures if case[0] == minimum]
    deficit, perm, extra, used = best_cases[0]
    unavoidable = set.intersection(*(
        {name for starters in case[2].values() for name in starters}
        for case in best_cases
    ))
    optional = set.union(*(
        {name for starters in case[2].values() for name in starters}
        for case in best_cases
    )) - unavoidable
    print(f"minimum extra unregistered erasure(s) required: {minimum}")
    print(f"representative best order: {' < '.join(perm)}")
    print(f"registered erasures consumed in it: {used}")
    for page, starters in sorted(extra.items()):
        print(f"  page {page}: unregistered erasure required before snapshot(s) {', '.join(starters)}")
    print()
    if unavoidable:
        print(f"snapshots that unavoidably need an unregistered erasure: {', '.join(sorted(unavoidable))}")
    else:
        print("no single snapshot is forced to carry the extra erasure; "
              "the conflict is symmetric across the minimum-cost orders.")
    if optional:
        print(f"candidate snapshots implicated in at least one minimum-cost order: {', '.join(sorted(optional))}")
    return 1


def _demo():
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="flash_order_demo_"))
    snaps = tmp / "snaps"
    snaps.mkdir()

    def write_hex(name, page0, page1):
        ih = IntelHex()
        base = 0
        for offset, byte in enumerate(page0):
            if byte is not None:
                ih[base + offset] = byte
        for offset, byte in enumerate(page1):
            if byte is not None:
                ih[256 + offset] = byte
        ih.write_hex_file(str(snaps / f"{name}.hex"))

    # Page 0: s1 -> s2 -> s3 pure 1->0 progression; s4 is a post-erase fresh state.
    write_hex("s1", [0xFF, 0x7F], [0xFF, 0xFF])
    write_hex("s2", [0x3F, 0x7F], [0xFE, 0xFF])
    write_hex("s3", [0x0F, 0x77], [0xFC, 0xFF])
    write_hex("s4", [0xF0, 0xFF], [0xFC, 0xF8])
    # s4 page0 (F0) conflicts with s3 (0F): bits diverge both ways, needs an erase.
    register = tmp / "register.json"
    register.write_text(json.dumps({"page_size": 256, "register": [0]}))

    print("=== demo A: one registered erasure of page 0 is enough ===")
    reconstruct(snaps, register)
    print()

    register.write_text(json.dumps({"page_size": 256, "register": []}))
    print("=== demo B: register empty, reconstruction must fail ===")
    rc_b = reconstruct(snaps, register)
    print()

    # Cross-page contradiction: page 0 only chains a -> b, page 1 only b -> a.
    # With an empty register neither direction can be paid for, so no single
    # global order survives and both snapshots are implicated.
    cross = tmp / "cross"
    cross.mkdir()
    ih_a, ih_b = IntelHex(), IntelHex()
    ih_a[0] = 0xFE      # page 0: a=FE -> b=FC is a clean 1->0 edge
    ih_b[0] = 0xFC
    ih_a[256] = 0xFC    # page 1: b=FE -> a=FC is a clean 1->0 edge
    ih_b[256] = 0xFE
    ih_a.write_hex_file(str(cross / "a.hex"))
    ih_b.write_hex_file(str(cross / "b.hex"))
    cross_register = tmp / "cross_register.json"
    cross_register.write_text(json.dumps({"page_size": 256, "register": []}))
    print("=== demo C: opposing cross-page evidence, no registered erasures ===")
    rc_c = reconstruct(cross, cross_register)
    return rc_b or rc_c


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, help="directory of Intel-HEX snapshots")
    parser.add_argument("--register", type=Path, help="JSON erasure register")
    parser.add_argument("--demo", action="store_true", help="run the built-in synthetic demo")
    args = parser.parse_args()
    if args.demo:
        sys.exit(_demo())
    if not args.snapshots or not args.register:
        parser.error("both --snapshots and --register are required outside --demo")
    sys.exit(reconstruct(args.snapshots, args.register))


if __name__ == "__main__":
    main()
