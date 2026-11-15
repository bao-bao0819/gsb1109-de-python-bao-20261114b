"""Build a stable, evidence-backed glyph catalog from two supplier UFOs.

The catalog keeps every supplier glyph as its own record.  A record is either:

* matched one-to-one with a record from the other supplier using explicit
  evidence (human-confirmed mapping, stable id, or Unicode set), or
* an independent entry (an unencoded alternate, for example), or
* part of a candidate group that cannot be split without a human decision.

When a glyph name points at one counterpart while its stable id points at
another, both claims are preserved in a conflict; no input order or left/right
priority decides the winner.
"""

from ufoLib2.catalog.input import (
    ManualPair,
    SourceSpec,
    load_json_file,
    load_manual_file,
    load_source,
    normalize_manual_mapping,
    normalize_stable_ids,
)
from ufoLib2.catalog.model import (
    Catalog,
    CatalogError,
    CandidateGroup,
    Conflict,
    ConflictClaim,
    Evidence,
    GlyphRecord,
    Match,
    SourceInfo,
    Unmatched,
    load_catalog,
    save_catalog,
    unicode_label,
    unicode_labels,
)
from ufoLib2.catalog.resolution import resolve_catalog, summarize

__all__ = [
    "Catalog",
    "CatalogError",
    "CandidateGroup",
    "Conflict",
    "ConflictClaim",
    "Evidence",
    "GlyphRecord",
    "ManualPair",
    "Match",
    "SourceInfo",
    "SourceSpec",
    "Unmatched",
    "load_catalog",
    "load_json_file",
    "load_manual_file",
    "load_source",
    "normalize_manual_mapping",
    "normalize_stable_ids",
    "resolve_catalog",
    "save_catalog",
    "summarize",
    "unicode_label",
    "unicode_labels",
]
