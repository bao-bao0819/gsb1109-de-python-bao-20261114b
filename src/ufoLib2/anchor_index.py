"""Aggregate anchors from multiple supplier UFOs into a path-keyed index.

When merging fonts from several suppliers, a composite glyph may carry its
own anchors (e.g. ``top``) while each of its components contributes anchors
of the same name from its source glyph. This module walks composite glyphs
across any number of supplier fonts, exports every anchor keyed by its
*component path* (e.g. ``acute@1`` or ``aacute@0/acute@1``), and applies
purchaser-supplied override rules to decide which anchors may serve as
final attachment points.

Key properties:

- Each component reference is one instance: the same base glyph used twice
  with different transformations yields two distinct paths (``acute@0`` and
  ``acute@1``).
- Every exported entry records its source chain (font, glyph, component
  index and transformation per level), so replaying the original UFO
  transformations level by level reproduces the exported coordinates. See
  :func:`replay_entry`.
- Override rules carry a level; higher levels win. If several rules at the
  winning level select different positions, the conflict is kept in the
  output instead of being silently resolved.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fontTools.misc.transform import Identity, Transform

from ufoLib2.objects.font import Font
from ufoLib2.objects.glyph import Glyph

__all__ = [
    "AnchorEntry",
    "AttachmentDecision",
    "ChainStep",
    "OverrideRule",
    "build_anchor_index",
    "collect_glyph_anchors",
    "compute_attachments",
    "decide_attachment",
    "export_index",
    "replay_entry",
]

# A font supplier is identified by a short name and provides a UFO.
FontSupplier = tuple[str, Font]


@dataclass(frozen=True)
class ChainStep:
    """One component reference along the source chain of an exported anchor."""

    font: str
    """The supplier font containing the glyph that holds the component."""

    glyph: str
    """The glyph holding the component reference (the parent at this level)."""

    component_index: int
    """The index of the component within the parent glyph."""

    base_glyph: str
    """The glyph the component points to."""

    transform: tuple[float, float, float, float, float, float]
    """The component transformation as stored in the original UFO."""


@dataclass(frozen=True)
class AnchorEntry:
    """A single exported anchor, keyed by component path."""

    glyph: str
    """The exported (composite) glyph the anchor is aggregated into."""

    path: str
    """Component path identifying the instance, e.g. ``aacute@0/acute@1``.

    The empty string denotes the exported glyph's own anchor. Each segment
    is ``<baseGlyph>@<componentIndex>``, so the same component used twice
    with different transformations produces two distinct paths.
    """

    name: str
    """The anchor name, e.g. ``top``."""

    x: float
    """The x coordinate in the exported glyph's space."""

    y: float
    """The y coordinate in the exported glyph's space."""

    source_font: str
    """The supplier font providing the glyph the anchor was read from."""

    source_glyph: str
    """The glyph the anchor was read from."""

    local: tuple[float, float]
    """The anchor coordinates in the source glyph's own space."""

    chain: tuple[ChainStep, ...]
    """The source chain, outermost component reference first."""


@dataclass(frozen=True)
class OverrideRule:
    """A purchaser rule selecting the final attachment point for an anchor.

    Exactly one of ``path`` (reuse the position of an exported anchor) or
    ``position`` (explicit coordinates in the exported glyph's space) must
    be given. Rules with a higher ``level`` win; ties at the winning level
    that select different positions are kept as conflicts.
    """

    glyph: str
    anchor: str
    level: int
    path: str | None = None
    position: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.position is None):
            raise ValueError("exactly one of 'path' and 'position' must be set")


@dataclass(frozen=True)
class AttachmentDecision:
    """The outcome of applying override rules to one (glyph, anchor) pair."""

    glyph: str
    anchor: str
    status: str
    """One of ``resolved`` (single final point), ``conflict`` (several
    same-level candidates kept) or ``none`` (no usable anchor)."""

    position: tuple[float, float] | None = None
    """The final attachment point when ``status`` is ``resolved``."""

    candidates: tuple[tuple[float, float], ...] = ()
    """All positions selected at the winning level (more than one on
    ``conflict``)."""

    level: int | None = None
    """The winning rule level, or None for the default (own anchor)."""


def _lookup(fonts: Sequence[FontSupplier], glyph_name: str) -> tuple[str, Glyph]:
    for font_id, font in fonts:
        if glyph_name in font:
            return font_id, font[glyph_name]
    raise KeyError(f"glyph {glyph_name!r} not found in any supplier font")


def collect_glyph_anchors(
    fonts: Sequence[FontSupplier], glyph_name: str
) -> list[AnchorEntry]:
    """Collect the anchors of one glyph and, recursively, of its components.

    Args:
        fonts: Supplier fonts as ``(font_id, Font)`` pairs, in priority
            order. The first font containing a glyph supplies it.
        glyph_name: The glyph to export anchors for.
    """
    entries: list[AnchorEntry] = []
    _collect(fonts, glyph_name, glyph_name, "", Identity, (), frozenset(), entries)
    return entries


def _collect(
    fonts: Sequence[FontSupplier],
    glyph_name: str,
    root_glyph: str,
    path: str,
    transform: Transform,
    chain: tuple[ChainStep, ...],
    seen: frozenset[str],
    entries: list[AnchorEntry],
) -> None:
    if glyph_name in seen:
        raise ValueError(f"component cycle detected at glyph {glyph_name!r}")
    font_id, glyph = _lookup(fonts, glyph_name)
    for anchor in glyph.anchors:
        if anchor.name is None:
            continue
        x, y = transform.transformPoint((anchor.x, anchor.y))
        entries.append(
            AnchorEntry(
                glyph=root_glyph,
                path=path,
                name=anchor.name,
                x=x,
                y=y,
                source_font=font_id,
                source_glyph=glyph_name,
                local=(anchor.x, anchor.y),
                chain=chain,
            )
        )
    seen = seen | {glyph_name}
    for index, component in enumerate(glyph.components):
        step = ChainStep(
            font=font_id,
            glyph=glyph_name,
            component_index=index,
            base_glyph=component.baseGlyph,
            transform=tuple(component.transformation),  # type: ignore[arg-type]
        )
        segment = f"{component.baseGlyph}@{index}"
        _collect(
            fonts,
            component.baseGlyph,
            root_glyph,
            f"{path}/{segment}" if path else segment,
            transform.transform(component.transformation),
            chain + (step,),
            seen,
            entries,
        )


def build_anchor_index(
    fonts: Sequence[FontSupplier], glyph_names: Sequence[str]
) -> list[AnchorEntry]:
    """Collect anchors for several exported glyphs into one index."""
    entries: list[AnchorEntry] = []
    for glyph_name in glyph_names:
        entries.extend(collect_glyph_anchors(fonts, glyph_name))
    return entries


def decide_attachment(
    entries: Sequence[AnchorEntry],
    rules: Sequence[OverrideRule],
    glyph: str,
    anchor: str,
) -> AttachmentDecision:
    """Apply the purchaser's override rules to one (glyph, anchor) pair.

    The rules at the highest level win. If they select a single position it
    becomes the final attachment point; if they select different positions
    the decision is a conflict and all candidates are kept. Without any
    matching rule, the glyph's own anchor (path ``""``) is used if present.
    """
    positions = {
        entry.path: (entry.x, entry.y)
        for entry in entries
        if entry.glyph == glyph and entry.name == anchor
    }
    matching = [r for r in rules if r.glyph == glyph and r.anchor == anchor]
    if not matching:
        if "" in positions:
            own = positions[""]
            return AttachmentDecision(glyph, anchor, "resolved", own, (own,), None)
        return AttachmentDecision(glyph, anchor, "none")
    level = max(rule.level for rule in matching)
    selected: list[tuple[float, float]] = []
    for rule in matching:
        if rule.level != level:
            continue
        if rule.path is not None:
            if rule.path not in positions:
                raise KeyError(
                    f"rule selects unknown path {rule.path!r} "
                    f"for anchor {anchor!r} of glyph {glyph!r}"
                )
            position = positions[rule.path]
        else:
            assert rule.position is not None
            position = tuple(rule.position)
        if position not in selected:
            selected.append(position)
    if len(selected) == 1:
        return AttachmentDecision(
            glyph, anchor, "resolved", selected[0], tuple(selected), level
        )
    return AttachmentDecision(glyph, anchor, "conflict", None, tuple(selected), level)


def compute_attachments(
    entries: Sequence[AnchorEntry], rules: Sequence[OverrideRule]
) -> list[AttachmentDecision]:
    """Compute final attachment points for every (glyph, anchor) in play."""
    keys = {(entry.glyph, entry.name) for entry in entries}
    keys |= {(rule.glyph, rule.anchor) for rule in rules}
    return [
        decide_attachment(entries, rules, glyph, anchor)
        for glyph, anchor in sorted(keys)
    ]


def replay_entry(
    fonts: Mapping[str, Font], entry: AnchorEntry
) -> tuple[float, float]:
    """Recompute an exported point from the original UFOs along its chain.

    Reads the anchor back from the source glyph in the source font and
    applies each level's component transformation as stored in the original
    UFOs, innermost first. The result must equal ``(entry.x, entry.y)``.
    """
    source = fonts[entry.source_font][entry.source_glyph]
    point: tuple[float, float] | None = None
    for anchor in source.anchors:
        if anchor.name == entry.name:
            point = (anchor.x, anchor.y)
            break
    if point is None:
        raise KeyError(
            f"anchor {entry.name!r} not found in glyph {entry.source_glyph!r} "
            f"of font {entry.source_font!r}"
        )
    for step in reversed(entry.chain):
        parent = fonts[step.font][step.glyph]
        component = parent.components[step.component_index]
        if component.baseGlyph != step.base_glyph:
            raise ValueError(
                f"component {step.component_index} of glyph {step.glyph!r} "
                f"in font {step.font!r} no longer points to {step.base_glyph!r}"
            )
        point = component.transformation.transformPoint(point)
    return point


def _entry_to_dict(entry: AnchorEntry) -> dict[str, Any]:
    return {
        "path": entry.path,
        "name": entry.name,
        "x": entry.x,
        "y": entry.y,
        "source": {
            "font": entry.source_font,
            "glyph": entry.source_glyph,
            "local": list(entry.local),
            "chain": [
                {
                    "font": step.font,
                    "glyph": step.glyph,
                    "component": step.component_index,
                    "baseGlyph": step.base_glyph,
                    "transform": list(step.transform),
                }
                for step in entry.chain
            ],
        },
    }


def export_index(
    entries: Sequence[AnchorEntry],
    decisions: Sequence[AttachmentDecision] = (),
) -> dict[str, Any]:
    """Serialize the anchor index and attachment decisions to a JSON-able dict."""
    glyphs: dict[str, Any] = {}
    for entry in entries:
        glyphs.setdefault(entry.glyph, {"anchors": [], "attachments": {}})[
            "anchors"
        ].append(_entry_to_dict(entry))
    for decision in decisions:
        bucket = glyphs.setdefault(decision.glyph, {"anchors": [], "attachments": {}})
        attachment: dict[str, Any] = {"status": decision.status}
        if decision.position is not None:
            attachment["position"] = list(decision.position)
        if decision.candidates:
            attachment["candidates"] = [list(p) for p in decision.candidates]
        if decision.level is not None:
            attachment["level"] = decision.level
        bucket["attachments"][decision.anchor] = attachment
    return {"glyphs": glyphs}


def main(argv: Sequence[str] | None = None) -> int:
    """Command line interface: export an anchor index from supplier UFOs."""
    parser = argparse.ArgumentParser(
        prog="anchor-index",
        description=(
            "Aggregate anchors from supplier UFOs into a path-keyed index and "
            "apply override rules to compute final attachment points."
        ),
    )
    parser.add_argument(
        "fonts",
        nargs="+",
        type=Path,
        metavar="FONT.ufo",
        help="supplier UFOs, in priority order",
    )
    parser.add_argument(
        "--glyph",
        action="append",
        dest="glyphs",
        metavar="NAME",
        help="glyph to export (repeatable); defaults to all glyphs of the "
        "first font",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        metavar="RULES.json",
        help="JSON list of override rules with keys glyph, anchor, level and "
        "either path or position",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="OUT.json",
        help="output file (default: stdout)",
    )
    args = parser.parse_args(argv)

    fonts: list[FontSupplier] = [
        (path.stem, Font.open(path)) for path in args.fonts
    ]
    glyph_names: list[str] = args.glyphs or sorted(fonts[0][1].keys())
    rules: list[OverrideRule] = []
    if args.rules is not None:
        rules = [OverrideRule(**item) for item in json.loads(args.rules.read_text())]

    entries = build_anchor_index(fonts, glyph_names)
    decisions = compute_attachments(entries, rules)
    data = export_index(entries, decisions)
    text = json.dumps(data, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
