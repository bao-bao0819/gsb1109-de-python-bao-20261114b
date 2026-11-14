# Offline column-group splitting

`tools/column_groups.py` solves the "split the offline wide table into column
groups" problem exactly for small instances.

## Model

- One row = one sampled original CAN frame.
- A **field** is a `(frame, signal)` column decoded from a canmatrix database;
  its size is the signal's bit length rounded up to whole bytes.
- A **group** may contain fields from several frames.
- A row writes a group only if at least one of the group's fields is present in
  that row. If none is present, the group writes nothing for that row.
- A written row costs the bytes of the group's present fields. A zero reading
  is present and keeps its slot; an absent field is free. Two mutually
  exclusive multiplexed signals therefore share a physical slot.
- The group header is paid once per written group file (0 if never written).
- `width_bytes = header + max(present member bytes over written rows)` and must
  be `<= width_limit`.
- A query pays each touched group once, even if it names two columns of that
  group.

Objective: `sum(frequency * bytes per run)`.

## Running

```bash
.venv/bin/python examples/column_groups/make_instance.py > examples/column_groups/aa_sdo.json
.venv/bin/python tools/column_groups.py examples/column_groups/aa_sdo.json
.venv/bin/python tools/column_groups.py examples/column_groups/aa_sdo.json --json
```

`tests/test_column_groups.py` cross-checks the DP against brute-force set
partitions:

```bash
.venv/bin/python -m pytest tests/test_column_groups.py -o addopts='' --rootdir=.
```

## Instance format

```json
{
  "header_bytes": 16,
  "width_limit": 32,
  "fields": [{"frame": "F", "signal": "s", "bytes": 2}],
  "rows": [{"frame": "F", "present": ["s"]}],
  "queries": [{"name": "q", "frequency": 10, "fields": [["F", "s"]]}]
}
```

The exact DP is exponential in fields, so it is intended for a handful of
signals per frame. For large frames the same cost table can be fed to an ILP
solver.
