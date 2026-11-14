# -----------------------------------------------------------------
# pycparser: array_init_layout.py
#
# Demonstrates how to recover the concrete memory layout of C array
# initializers from the AST, including the tricky interactions between
# designators ([i] = ...), unmarked continuation items and compiler
# zero-filling.  The example is framed around per-instrument calibration
# tables stored as C arrays.
#
# For every array the script prints:
#   * the final value at each logical coordinate,
#   * the source text and coordinate of the last initializer that
#     wrote each cell,
#   * separate counts for explicit zeros and C zero-filled cells.
#
# Eli Bendersky [https://eli.thegreenplace.net/]
# License: BSD
# -----------------------------------------------------------------
import sys

sys.path.extend([".", ".."])

from pycparser.array_init import parse_and_extract

SOURCE = r"""
enum { CH_A = 0, CH_B, CH_C };

struct cal_point {
    int offset;
    int gain;
};

int calib[6][3] = {
    {1, 2, 3},
    [2] = { [1] = 9, 8 },
    7, 0,
    [5][0] = 50, 51,
};

struct cal_point points[3] = {
    {10, 20},
    [2].gain = 40,
};

int channel[4] = { CH_A, [2] = CH_C, [2] = 99 };
"""


def main() -> None:
    layouts = parse_and_extract(SOURCE, filename="calibration.c")

    for layout in layouts:
        print(f"=== {layout.name}  resolved dims={layout.dims}")
        print(f"    final grid: {layout.grid()}")

        for cell in layout.explicit_cells():
            origin = (
                f"designated at {cell.coord}"
                if cell.designated
                else f"continued  at {cell.coord}"
            )
            print(
                f"    {cell.index}: value={cell.value!r:<6} "
                f"source={cell.source!r:<8} {origin}"
            )

        for cell in layout.implicit_zero_cells():
            print(f"    {cell.index}: value=0      (C zero-filled, no source)")

        stats = layout.stats()
        print(
            "    totals: "
            f"{stats['explicit']} explicit "
            f"({stats['explicit_zero']} explicit zeros, "
            f"{stats['explicit_nonzero']} non-zero), "
            f"{stats['implicit_zero']} implicit zero-filled, "
            f"{stats['overwritten']} overwritten"
        )
        if layout.warnings:
            print(f"    warnings: {layout.warnings}")
        print()


if __name__ == "__main__":
    main()
