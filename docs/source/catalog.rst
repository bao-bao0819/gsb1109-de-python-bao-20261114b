Glyph catalogs for two supplier UFOs
====================================

Two suppliers can deliver the same character under different conventions:

* one names glyphs canonically (``uni0041``, ``u1F600``), the other uses
  internal names (``A``, ``smiley``);
* a single character can arrive as several candidate designs
  (``C.alt1``/``C.alt2``);
* a design variant has no encoding at all (``A.ss01``);
* a glyph name can point at one record while an explicit stable id points at
  another.

:mod:`ufoLib2.catalog` turns the two UFOs into one deterministic, JSON-backed
catalog without letting file order, left/right position, or glyph iteration
order decide which evidence wins.

Building a catalog
------------------

.. code-block:: bash

    python -m ufoLib2.catalog build \
        --left supplier-a.ufo --left-naming unicode \
        --right supplier-b.ufo --right-naming internal \
        --stable-ids stable-ids.json \
        --manual confirmed.json \
        --output catalog.json --summary

Each supplier glyph is one :class:`~ufoLib2.catalog.model.GlyphRecord` with

* ``id`` — stable, human-readable ``source:glyphName`` (for example
  ``left:uni0041``), never derived from iteration order;
* ``stableId`` — the explicit identifier from a glyph's lib or the stable-id
  file, or ``null``;
* ``unicodes`` — the canonical Unicode set, written as ``U+XXXX`` labels;
* ``unicodeSource`` — ``glif``, ``name`` or ``none``;
* ``nameUnicodes`` — a weaker, name-derived hint kept separate from the
  identity-bearing set;
* ``unencoded`` — true when the record has no Unicode set.

Inputs
------

The stable-id file accepts either a per-source mapping or explicit entries::

    {"left": {"uni0041": "id-0041"}, "right": {"A": "id-0041"}}

    {"entries": [{"source": "left", "glyph": "uni0041", "stableId": "id-0041"}]}

The human-confirmed mapping file lists one-to-one decisions::

    {"pairs": [
        {"left": "uni0043", "right": "C.alt2", "note": "design review"}
    ]}

References may be record ids, plain glyph names (scoped to the left or right
supplier), or ``{"source": ..., "glyph": ...}`` objects. Duplicate JSON keys are
rejected rather than silently keeping the last value.

Resolution rules
----------------

1. **Human-confirmed pairs are authoritative.** A manual pair is always a
   confirmed match. Disagreeing name/stable-id/Unicode claims are retained as a
   ``manual-override`` conflict so the override is auditable.
2. **Stable id** that is unique in both sources confirms a pair.
3. **Unicode set** that is identical and unique in both sources confirms a
   pair. A stable id can narrow a multi-candidate Unicode set to one record.
4. **Name-only evidence never confirms a pair.** It is kept as evidence but
   cannot form a one-to-one correspondence, so ``A.ss01`` remains an
   independent record.
5. **Agreement is required in both directions.** Each side must independently
   select the other; a one-sided claim produces a candidate group, not a match.
6. **Disagreement is never resolved by precedence.** If evidence forms admit no
   common counterpart, the record emits a conflict listing every claim and its
   target, plus a candidate group containing all implicated records.
7. **Ambiguity is one group.** Records tied by a shared Unicode set or a
   non-unique stable id in one source form a single unsplittable candidate
   group rather than several overlapping pairs.

All ids are deterministic hashes of sorted material, so reordering either UFO
or swapping supplier sides produces the same resolution; only the source
labels change.
