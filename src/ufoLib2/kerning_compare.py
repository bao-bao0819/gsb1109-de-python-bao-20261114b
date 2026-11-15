"""Effective kerning expansion and old/new UFO comparison.

UFO kerning is sparse: ``kerning.plist`` may pair glyphs with glyphs or with
kerning groups (``public.kern1.*`` on the first side, ``public.kern2.*`` on
the second).  This module resolves those sparse declarations to the value a
user actually sees for every concrete glyph pair, compares two fonts, and
clusters the per-pair differences by their shared declaration-level cause
while keeping enough provenance to reconstruct any single pair.

Resolution rules (per the UFO 3 spec):

* only ``public.kern1.*`` groups are valid on the left side and only
  ``public.kern2.*`` groups on the right side; declarations violating this,
  or referencing unknown groups, are ignored;
* glyph-glyph declarations beat glyph-group beat group-glyph beat
  group-group, regardless of file order;
* an explicit glyph-glyph value of ``0`` still wins and suppresses every
  group value for that pair.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ufoLib2.objects.font import Font

LEFT_GROUP_PREFIX = "public.kern1."
RIGHT_GROUP_PREFIX = "public.kern2."

GLYPH_GLYPH = 0
GLYPH_GROUP = 1
GROUP_GLYPH = 2
GROUP_GROUP = 3


class Source(NamedTuple):
    """One kerning declaration that can determine a concrete pair's value."""

    side: int
    index: int
    raw_left: str
    raw_right: str
    value: float

    @property
    def identity(self) -> tuple[int, str, str]:
        return (self.side, self.raw_left, self.raw_right)


class IgnoredDeclaration(NamedTuple):
    raw_left: str
    raw_right: str
    value: float
    reason: str


@dataclass(frozen=True)
class EffectivePair:
    pair: tuple[str, str]
    value: float
    source: Source
    candidates: tuple[Source, ...]


class KernModel:
    """Sparse kerning declarations expanded over a concrete glyph universe."""

    def __init__(
        self,
        glyphs: Sequence[str],
        left_members: dict[str, tuple[str, ...]],
        right_members: dict[str, tuple[str, ...]],
        declarations: Sequence[Source],
        ignored: Sequence[IgnoredDeclaration],
    ) -> None:
        self.glyphs = tuple(glyphs)
        self.left_members = dict(left_members)
        self.right_members = dict(right_members)
        self.declarations = tuple(declarations)
        self.ignored = tuple(ignored)
        self._glyph_set = frozenset(self.glyphs)
        self._pair_sources: dict[tuple[str, str], list[Source]] = defaultdict(list)
        for source in self.declarations:
            for pair in self._expand(source):
                self._pair_sources[pair].append(source)

    def _expand(self, source: Source) -> list[tuple[str, str]]:
        if source.side == GLYPH_GLYPH:
            pair = (source.raw_left, source.raw_right)
            if pair[0] in self._glyph_set and pair[1] in self._glyph_set:
                return [pair]
            return []
        if source.side == GLYPH_GROUP:
            if source.raw_left not in self._glyph_set:
                return []
            return [
                (source.raw_left, member)
                for member in self.right_members[source.raw_right]
                if member in self._glyph_set
            ]
        if source.side == GROUP_GLYPH:
            if source.raw_right not in self._glyph_set:
                return []
            return [
                (member, source.raw_right)
                for member in self.left_members[source.raw_left]
                if member in self._glyph_set
            ]
        return [
            (left_member, right_member)
            for left_member in self.left_members[source.raw_left]
            for right_member in self.right_members[source.raw_right]
            if left_member in self._glyph_set and right_member in self._glyph_set
        ]

    def resolve_pair(self, left: str, right: str) -> EffectivePair | None:
        candidates = self._pair_sources.get((left, right))
        if not candidates:
            return None
        winner = candidates[0]
        return EffectivePair((left, right), winner.value, winner, tuple(candidates))

    def effective_pairs(self) -> Iterator[EffectivePair]:
        for pair in sorted(self._pair_sources):
            resolution = self.resolve_pair(*pair)
            assert resolution is not None
            yield resolution


def _classify_side(
    raw_left: str, raw_right: str, groups: dict[str, list[str]]
) -> tuple[int, str]:
    left_is_group = raw_left.startswith(LEFT_GROUP_PREFIX) or raw_left.startswith(
        RIGHT_GROUP_PREFIX
    )
    right_is_group = raw_right.startswith(LEFT_GROUP_PREFIX) or raw_right.startswith(
        RIGHT_GROUP_PREFIX
    )
    if left_is_group and not raw_left.startswith(LEFT_GROUP_PREFIX):
        return -1, "left group uses right-side prefix (public.kern2.*)"
    if right_is_group and not raw_right.startswith(RIGHT_GROUP_PREFIX):
        return -1, "right group uses left-side prefix (public.kern1.*)"
    if left_is_group and raw_left not in groups:
        return -1, f"unknown left group {raw_left!r}"
    if right_is_group and raw_right not in groups:
        return -1, f"unknown right group {raw_right!r}"
    return (int(left_is_group) * 2 + int(right_is_group), "")


def build_model(font: Font) -> KernModel:
    """Build a :class:`KernModel` from a :class:`ufoLib2.Font`."""
    groups = dict(font.groups)
    left_members: dict[str, tuple[str, ...]] = {}
    right_members: dict[str, tuple[str, ...]] = {}
    glyphs = set(font.keys())
    for group_name, members in groups.items():
        if group_name.startswith(LEFT_GROUP_PREFIX):
            left_members[group_name] = tuple(members)
        elif group_name.startswith(RIGHT_GROUP_PREFIX):
            right_members[group_name] = tuple(members)

    ignored: list[IgnoredDeclaration] = []
    classified: list[Source] = []
    for (raw_left, raw_right), value in font.kerning.items():
        side, reason = _classify_side(raw_left, raw_right, groups)
        if side < 0:
            ignored.append(IgnoredDeclaration(raw_left, raw_right, value, reason))
            continue
        classified.append(
            Source(side, -1, raw_left, raw_right, value)
        )

    classified.sort(key=lambda source: (source.side, source.raw_left, source.raw_right))
    declarations = tuple(
        source._replace(index=index) for index, source in enumerate(classified)
    )
    return KernModel(
        sorted(glyphs), left_members, right_members, declarations, ignored
    )


def effective_kerning(font: Font) -> dict[tuple[str, str], EffectivePair]:
    """Return every concrete glyph pair with the value a user actually sees."""
    model = build_model(font)
    return {resolution.pair: resolution for resolution in model.effective_pairs()}


@dataclass(frozen=True)
class PairChange:
    pair: tuple[str, str]
    old_value: float | None
    new_value: float | None
    old_source: Source | None
    new_source: Source | None
    old_candidates: tuple[Source, ...] = ()
    new_candidates: tuple[Source, ...] = ()

    @property
    def user_visible(self) -> bool:
        return (self.old_value or 0) != (self.new_value or 0)


@dataclass(frozen=True)
class ChangeCause:
    """Shared cause of a cluster; anchored on one declaration.

    ``kind`` distinguishes added coverage, removed coverage, in-place value
    edits and winner replacement.  The anchor is the declaration responsible
    for the shared cause (the new winner for additions/replacements, the old
    declaration for removals), so hundreds of pairs moved into one group end
    up in a single cluster even when their previous declarations differ.
    """

    kind: str
    anchor: Source | None


@dataclass
class ChangeGroup:
    cause: ChangeCause
    pairs: list[PairChange]

    @property
    def user_visible_count(self) -> int:
        return sum(change.user_visible for change in self.pairs)

    @property
    def declaration_only_count(self) -> int:
        return len(self.pairs) - self.user_visible_count


@dataclass
class KerningDiff:
    changes: list[ChangeGroup]
    pair_index: dict[tuple[str, str], PairChange]
    raw_kerning_added: list[tuple[str, str, float]]
    raw_kerning_removed: list[tuple[str, str, float]]
    raw_kerning_value_changed: list[tuple[str, str, float, float]]
    groups_added: list[str]
    groups_removed: list[str]
    group_membership_changes: dict[str, tuple[list[str], list[str]]]
    ignored: dict[str, tuple[IgnoredDeclaration, ...]]
    old_label: str
    new_label: str

    @property
    def pair_change_count(self) -> int:
        return len(self.pair_index)

    @property
    def user_visible_count(self) -> int:
        return sum(change.user_visible for change in self.pair_index.values())

    @property
    def declaration_only_count(self) -> int:
        return self.pair_change_count - self.user_visible_count

    @property
    def declaration_change_count(self) -> int:
        return (
            len(self.raw_kerning_added)
            + len(self.raw_kerning_removed)
            + len(self.raw_kerning_value_changed)
            + len(self.groups_added)
            + len(self.groups_removed)
            + len(self.group_membership_changes)
        )

    def reconstruct(self, pair: tuple[str, str]) -> PairChange:
        """Return the full old/new provenance recorded for one concrete pair."""
        try:
            return self.pair_index[pair]
        except KeyError as exc:
            raise KeyError(
                f"{pair!r} has no recorded change: "
                "it is unchanged or unkerned in both fonts"
            ) from exc


def _cause_kind(old_source: Source | None, new_source: Source | None) -> str:
    if old_source is None:
        return "added"
    if new_source is None:
        return "removed"
    if old_source.identity == new_source.identity:
        return "value_changed"
    return "winner_changed"


def _make_cause(
    kind: str, old_source: Source | None, new_source: Source | None
) -> ChangeCause:
    anchor = new_source if kind in ("added", "winner_changed") else old_source
    return ChangeCause(kind, anchor)


def _raw_kerning_diff(
    old_font: Font, new_font: Font
) -> tuple[
    list[tuple[str, str, float]],
    list[tuple[str, str, float]],
    list[tuple[str, str, float, float]],
]:
    old_items = dict(old_font.kerning)
    new_items = dict(new_font.kerning)
    added: list[tuple[str, str, float]] = []
    removed: list[tuple[str, str, float]] = []
    value_changed: list[tuple[str, str, float, float]] = []
    for key in sorted(set(old_items) | set(new_items)):
        if key not in old_items:
            added.append((key[0], key[1], new_items[key]))
        elif key not in new_items:
            removed.append((key[0], key[1], old_items[key]))
        elif old_items[key] != new_items[key]:
            value_changed.append((key[0], key[1], old_items[key], new_items[key]))
    return added, removed, value_changed


def _group_membership_diff(
    old_model: KernModel, new_model: KernModel
) -> tuple[list[str], list[str], dict[str, tuple[list[str], list[str]]]]:
    old_groups = set(old_model.left_members) | set(old_model.right_members)
    new_groups = set(new_model.left_members) | set(new_model.right_members)
    groups_added = sorted(new_groups - old_groups)
    groups_removed = sorted(old_groups - new_groups)
    membership_changes: dict[str, tuple[list[str], list[str]]] = {}
    for group_name in sorted(old_groups & new_groups):
        old_fallback = old_model.right_members.get(group_name, ())
        new_fallback = new_model.right_members.get(group_name, ())
        old_members = set(old_model.left_members.get(group_name, old_fallback))
        new_members = set(new_model.left_members.get(group_name, new_fallback))
        added_members = sorted(new_members - old_members)
        removed_members = sorted(old_members - new_members)
        if added_members or removed_members:
            membership_changes[group_name] = (added_members, removed_members)
    return groups_added, groups_removed, membership_changes


def compare_fonts(
    old_font: Font,
    new_font: Font,
    old_label: str = "旧版",
    new_label: str = "新版",
) -> KerningDiff:
    """Compare two fonts' effective kerning and cluster changes by cause."""
    old_model = build_model(old_font)
    new_model = build_model(new_font)

    grouped: dict[ChangeCause, list[PairChange]] = defaultdict(list)
    pair_index: dict[tuple[str, str], PairChange] = {}
    all_pairs = set(old_model._pair_sources) | set(new_model._pair_sources)
    for pair in sorted(all_pairs):
        old_resolution = old_model.resolve_pair(*pair)
        new_resolution = new_model.resolve_pair(*pair)
        old_source = old_resolution.source if old_resolution else None
        new_source = new_resolution.source if new_resolution else None
        old_value = old_resolution.value if old_resolution else None
        new_value = new_resolution.value if new_resolution else None
        same_identity = (
            old_source is not None
            and new_source is not None
            and old_source.identity == new_source.identity
        ) or (old_source is None and new_source is None)
        if (old_value or 0) == (new_value or 0) and same_identity:
            continue
        kind = _cause_kind(old_source, new_source)
        change = PairChange(
            pair,
            old_value,
            new_value,
            old_source,
            new_source,
            old_resolution.candidates if old_resolution else (),
            new_resolution.candidates if new_resolution else (),
        )
        grouped[_make_cause(kind, old_source, new_source)].append(change)
        pair_index[pair] = change

    def cause_order(cause: ChangeCause) -> tuple[str, int, str, str]:
        anchor = cause.anchor
        if anchor is None:
            return (cause.kind, -1, "", "")
        return (cause.kind, anchor.side, anchor.raw_left, anchor.raw_right)

    changes = [
        ChangeGroup(cause, grouped[cause])
        for cause in sorted(grouped, key=cause_order)
    ]
    for group in changes:
        group.pairs.sort(key=lambda change: change.pair)

    added, removed, value_changed = _raw_kerning_diff(old_font, new_font)
    groups_added, groups_removed, membership = _group_membership_diff(
        old_model, new_model
    )
    return KerningDiff(
        changes=changes,
        pair_index=pair_index,
        raw_kerning_added=added,
        raw_kerning_removed=removed,
        raw_kerning_value_changed=value_changed,
        groups_added=groups_added,
        groups_removed=groups_removed,
        group_membership_changes=membership,
        ignored={
            old_label: old_model.ignored,
            new_label: new_model.ignored,
        },
        old_label=old_label,
        new_label=new_label,
    )


def compare_paths(
    old_path: str | Path,
    new_path: str | Path,
    old_label: str = "旧版",
    new_label: str = "新版",
) -> KerningDiff:
    import ufoLib2

    old_font = ufoLib2.Font.open(old_path)
    new_font = ufoLib2.Font.open(new_path)
    return compare_fonts(old_font, new_font, old_label, new_label)


def describe_source(source: Source) -> str:
    def label(name: str) -> str:
        if name.startswith(LEFT_GROUP_PREFIX):
            return f"左组 {name}"
        if name.startswith(RIGHT_GROUP_PREFIX):
            return f"右组 {name}"
        return f"单字 {name}"

    return f"{label(source.raw_left)} × {label(source.raw_right)} = {source.value}"


def _cause_heading(cause: ChangeCause) -> str:
    anchor = cause.anchor
    if cause.kind == "added":
        assert anchor is not None
        return f"新增覆盖：{describe_source(anchor)}"
    if cause.kind == "removed":
        assert anchor is not None
        return f"失去覆盖（原声明 {describe_source(anchor)}）"
    if cause.kind == "value_changed":
        assert anchor is not None
        return f"同一声明改值：{describe_source(anchor)}"
    assert anchor is not None
    return f"生效声明替换，新声明：{describe_source(anchor)}"


def render_report(diff: KerningDiff, sample: int = 5) -> str:
    """Render a human-readable report separating declarations from effects."""
    lines: list[str] = []
    lines.append("一、声明层变化（kerning.plist / groups.plist 原文）")
    for left, right, value in diff.raw_kerning_added:
        lines.append(f"  + 字距声明 ({left}, {right}) = {value}")
    for left, right, value in diff.raw_kerning_removed:
        lines.append(f"  - 字距声明 ({left}, {right}) = {value}")
    for left, right, old_value, new_value in diff.raw_kerning_value_changed:
        lines.append(
            f"  ~ 字距声明 ({left}, {right}): {old_value} -> {new_value}"
        )
    for group_name in diff.groups_added:
        lines.append(f"  + 新增组 {group_name}")
    for group_name in diff.groups_removed:
        lines.append(f"  - 删除组 {group_name}")
    for group_name, (
        added_members,
        removed_members,
    ) in diff.group_membership_changes.items():
        if added_members:
            lines.append(f"  ~ 组 {group_name} 加入成员：{', '.join(added_members)}")
        if removed_members:
            lines.append(f"  ~ 组 {group_name} 移出成员：{', '.join(removed_members)}")
    if diff.declaration_change_count == 0:
        lines.append("  （无）")

    lines.append("")
    lines.append("二、用户实际看到的有效字对变化（按共同原因汇总）")
    lines.append(
        f"  发生变化的字对 {diff.pair_change_count} 个；"
        f"其中真实可见变化 {diff.user_visible_count} 个，"
        f"仅声明形式改写 {diff.declaration_only_count} 个。"
    )
    for index, group in enumerate(diff.changes, start=1):
        visible = group.user_visible_count
        total = len(group.pairs)
        tag = "真实变化" if visible else "形式改写"
        lines.append(
            f"  [{index}] {_cause_heading(group.cause)}"
        )
        lines.append(
            f"      影响 {total} 对（可见 {visible}，形式改写 {total - visible}）；{tag}"
        )
        examples = group.pairs[:sample]
        for change in examples:
            left, right = change.pair
            lines.append(
                f"      例：({left}, {right}) {change.old_value or 0} -> "
                f"{change.new_value or 0}"
            )
        if total > len(examples):
            lines.append(f"      ……其余 {total - len(examples)} 对可用 reconstruct() 还原")

    ignored_pairs = [
        (label, declaration)
        for label, declarations in diff.ignored.items()
        for declaration in declarations
    ]
    if ignored_pairs:
        lines.append("")
        lines.append("三、被侧别规则忽略的非法声明")
        for label, declaration in ignored_pairs:
            lines.append(
                f"  {label}: ({declaration.raw_left}, {declaration.raw_right}) "
                f"= {declaration.value}（{declaration.reason}）"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare effective kerning of two UFOs."
    )
    parser.add_argument("old_ufo", type=Path)
    parser.add_argument("new_ufo", type=Path)
    parser.add_argument("--sample", type=int, default=5, help="example pairs per cause")
    args = parser.parse_args(argv)
    diff = compare_paths(args.old_ufo, args.new_ufo)
    print(render_report(diff, sample=args.sample))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
