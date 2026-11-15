#!/usr/bin/env python3
"""Build a weekend recompute plan from two CAN dictionaries.

The warehouse keeps per-partition statistics (read message ids, observed
multiplexer values, source signals of every derived column).  This example
fakes those statistics; in production they come from the partition catalog.
"""
import copy

from canmatrix import CanMatrix, Frame, Signal
from canmatrix.recompute import PartitionStats, build_recompute_plan, format_plan


def build_matrix(selector_start_bit=0):
    db = CanMatrix()
    frame = Frame("Climate", arbitration_id=0x100, size=8)
    frame.add_signal(Signal("Mux", start_bit=selector_start_bit, size=4,
                            multiplex="Multiplexor"))
    frame.add_signal(Signal("Temperature", start_bit=8, size=8, multiplex=1))
    frame.add_signal(Signal("Pressure", start_bit=16, size=8, multiplex=2))
    db.add_frame(frame)
    return db


old_db = build_matrix()
new_db = build_matrix(selector_start_bit=4)  # multiplexer moved to other bits

partitions = [
    # Complete statistics: only branch 1 was ever seen.
    PartitionStats(
        partition_id="2026-W45-Sat",
        message_ids={0x100},
        multiplexer_values={0x100: {1}},
        derived_signal_fields={
            "engine_temperature": ["Temperature"],
            "oil_pressure": ["Pressure"],
        },
    ),
    # Partition that never read the changed frame.
    PartitionStats(
        partition_id="2026-W45-Sun",
        message_ids={0x200},
        derived_signal_fields={"some_column": ["SomeSignal"]},
    ),
    # Partition without complete statistics: raw frames have to be replayed.
    PartitionStats(
        partition_id="2026-W44-Sun",
        message_ids={0x100},
        complete=False,
    ),
]

plan = build_recompute_plan(old_db, new_db, partitions)
print(format_plan(plan))
