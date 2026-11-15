"""
Reconcile a legacy status report with a re-mapped value table.

The report was produced with ``old-table.json``, where codes 0 and 1 were both
called ``待机``.  The current dictionary calls code 1 ``维护``.  The report has
two sections, each with its own ``待机`` total; the surviving subtotal pins
code 0 in section ``一区``.

Run from the repository root::

    python examples/status/reconcile.py
"""

import canmatrix.reconcile


OLD = {0: "待机", 1: "待机", 2: "运行", 3: "停机"}
NEW = {0: "待机", 1: "维护", 2: "运行", 3: "停机"}
REPORT = {
    "一区": {"待机": 100, "运行": 10, "停机": 0},
    "二区": {"待机": 80, "运行": 20, "停机": 0},
}
SUBTOTALS = {"一区": {"0": 30}}


def print_ranges(title, ranges):
    print(title)
    for status, (low, high) in ranges.items():
        print("  %s: %d .. %s" % (status, low, high))


def main():
    model = canmatrix.reconcile.reconcile(OLD, NEW, REPORT, SUBTOTALS)

    print_ranges("whole report", model.ranges())
    print()
    print_ranges("section 一区", model.ranges("一区"))
    print()
    print_ranges("section 二区", model.ranges("二区"))

    print()
    print("joint scenarios for the whole report")
    for scenario in (
            {"维护": 100, "待机": 80},
            {"维护": 150, "待机": 30},
    ):
        verdict = "possible" if model.can_coexist(scenario) else "impossible"
        description = ", ".join("%s=%d" % item for item in sorted(scenario.items()))
        print("  %s -> %s" % (description, verdict))

    print()
    print("one satisfying report")
    witness = model.witness({"维护": 100})
    for section, counts in witness.items():
        for code, count in counts.items():
            if count:
                print("  %s / code %d: %d" % (section, code, count))


if __name__ == "__main__":
    main()
