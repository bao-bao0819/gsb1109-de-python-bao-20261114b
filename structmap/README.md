# structmap

Layout-based struct extraction and merge tables for preprocessed C
translation units, built on top of pycparser.

## Why

Two C modules can report the same log payload while typedef-ing their own
`Payload` with different spellings (`uint32` vs `u32`) and subtly different
fields. Compatibility must therefore be decided from the *recursively
expanded layout and field relationships*, never from type spelling.

`structmap` produces one closed structural description per preprocessed
module, then a merge table of the records that are layout-equivalent.

## Concepts

- **Scope-aware collection** (`collector.py`). An indexing walk records the
  lexical scope chain (file / function / block) at every typedef and struct
  tag. Descriptors are built lazily through those saved chains, so a
  block-scope typedef that shadows an outer alias is resolved with the local
  meaning for fields collected in that block; outer fields keep the outer
  meaning.
- **Closed descriptors.** Aliases are fully expanded to `Prim`, `Array`,
  `Ptr`, `RecordRef`, `FuncPtr`, or `Unknown` nodes. No spelling survives in
  a descriptor.
- **Cycle-safe expansion** (`layout.py`). Re-entering a record on the active
  stack emits a cycle marker instead of recursing. A Tarjan SCC pass labels
  pointer edges as `self`, `mutual` (another record in the same strongly
  connected component), or `external`.
- **Structural comparison** (`compare.py`). Record equivalence walks the two
  descriptor trees with a paired active stack. Incompatible records of the
  same shape are still paired so field-level reasons are reported; they never
  enter the merge table.
- **Business merge table** (`merge.py`). Only `mergeable` record pairs
  contribute; renamed but layout-compatible fields are annotated.

## Usage

```sh
python3 -m structmap module_a.i module_b.i --out-dir artifacts
```

Outputs:

- `schema_0.json`, `schema_1.json` — per-module structural descriptions,
- `comparison.json` — record verdicts and field-level diffs,
- `merge_table.json` — mergeable business types only.

Add `--use-cpp` for sources that still contain macros; otherwise inputs are
expected to be already preprocessed (e.g. `cpp -E` output, no comments).

## Library API

```python
from structmap import extract_module, compare_modules, business_merge_table
from structmap import render_schema
from structmap.layout import expand_record, PointerClassifier

schema = extract_module("module.i")
rendered = render_schema(schema)
expanded = expand_record(schema, "Payload")

report = compare_modules(extract_module("a.i"), extract_module("b.i"))
table = business_merge_table(*schemas)
```

Tests live in `tests/test_structmap.py` and run with
`python3 -m unittest tests.test_structmap`.
