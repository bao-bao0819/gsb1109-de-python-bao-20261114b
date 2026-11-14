"""ufoLib2 -- a package for dealing with UFO fonts."""

from __future__ import annotations

from ufoLib2.archive import (
    Archive,
    ArchiveIssue,
    ArchiveRequest,
    Character,
    Cycle,
    GlyphName,
    GlyphSource,
    LayerArchive,
    Substitution,
    build_archive,
)
from ufoLib2.objects import Font

__all__ = [
    "Archive",
    "ArchiveIssue",
    "ArchiveRequest",
    "Character",
    "Cycle",
    "Font",
    "GlyphName",
    "GlyphSource",
    "LayerArchive",
    "Substitution",
    "build_archive",
]

try:
    from ._version import version as __version__
except ImportError:
    __version__ = "0.0.0+unknown"
