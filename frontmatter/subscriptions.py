# -*- coding: utf-8 -*-
"""
Subscription views over front-matter metadata.

A :class:`View` is a saved filter over a post's metadata, built from
:class:`conditions <Condition>` combined with ``all`` (AND) or ``any`` (OR).
A :class:`SubscriptionEngine` remembers each post's metadata and its
membership in every view. When a post's front matter changes, it emits a
:class:`MembershipChange` only for views whose membership actually flipped,
annotated with the smallest changed field set(s) responsible for the flip.

Replaying an already-seen revision never notifies twice.

>>> from frontmatter.subscriptions import (
...     SubscriptionEngine, View, TagCondition,
... )
>>> engine = SubscriptionEngine()
>>> engine.add_view(View("python", TagCondition("tags", "python")))
>>> engine.prime("post-1", {"tags": ["rust"]})
>>> engine.sync("post-1", {"tags": ["python"]}, revision="r1")
[MembershipChange(view_id='python', action='joined', field_sets=(('tags',),))]
>>> engine.sync("post-1", {"tags": ["python", "rust"]}, revision="r2")
[]
>>> engine.sync("post-1", {"tags": ["python"]}, revision="r1")
[]
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Mapping, Sequence

__all__ = [
    "Condition",
    "TagCondition",
    "RangeCondition",
    "MissingKeyCondition",
    "View",
    "MembershipChange",
    "SubscriptionEngine",
]

_MISSING = object()


class Condition:
    """
    Base class for view conditions.

    Every condition tests a single metadata key, available as :attr:`key`.
    Subclasses implement :meth:`matches`.
    """

    key: str

    def matches(self, metadata: Mapping[str, object]) -> bool:
        raise NotImplementedError

    @staticmethod
    def _contains(value: object, target: object) -> bool:
        if isinstance(value, (list, tuple, set, frozenset)):
            return target in value
        return value == target


@dataclass(frozen=True)
class TagCondition(Condition):
    """
    Match when metadata at ``key`` carries ``value``.

    The metadata value may be a scalar or a list of tags. With ``present=False``
    the condition matches when the tag is absent, including when the key itself
    is missing.
    """

    key: str
    value: object
    present: bool = True

    def matches(self, metadata: Mapping[str, object]) -> bool:
        if self.key not in metadata:
            return not self.present
        has_tag = self._contains(metadata[self.key], self.value)
        return has_tag if self.present else not has_tag


@dataclass(frozen=True)
class RangeCondition(Condition):
    """
    Match when a numeric metadata value falls within ``min``/``max``.

    Bounds are inclusive and either side may be omitted. A missing key, or a
    non-numeric value, never matches.
    """

    key: str
    min: float | None = None
    max: float | None = None

    def matches(self, metadata: Mapping[str, object]) -> bool:
        value = metadata.get(self.key, _MISSING)
        if value is _MISSING or isinstance(value, bool) or not isinstance(
            value, (int, float)
        ):
            return False
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True


@dataclass(frozen=True)
class MissingKeyCondition(Condition):
    """
    Match when ``key`` is absent from the metadata (``present=False``, the
    default) or present (``present=True``).
    """

    key: str
    present: bool = False

    def matches(self, metadata: Mapping[str, object]) -> bool:
        has_key = self.key in metadata
        return has_key if self.present else not has_key


@dataclass(frozen=True, init=False)
class View:
    """
    A named subscription filter.

    Conditions are combined with ``match="all"`` (AND, the default) or
    ``match="any"`` (OR).

    >>> View("news", TagCondition("tags", "news"), match="any").match
    'any'
    """

    view_id: str
    conditions: tuple[Condition, ...]
    match: str

    def __init__(
        self, view_id: str, *conditions: Condition, match: str = "all"
    ) -> None:
        if match not in ("all", "any"):
            raise ValueError("match must be 'all' or 'any'")
        if not conditions:
            raise ValueError("a view needs at least one condition")
        object.__setattr__(self, "view_id", view_id)
        object.__setattr__(self, "conditions", tuple(conditions))
        object.__setattr__(self, "match", match)

    def matches(self, metadata: Mapping[str, object]) -> bool:
        results = (condition.matches(metadata) for condition in self.conditions)
        if self.match == "all":
            return all(results)
        return any(results)


@dataclass(frozen=True)
class MembershipChange:
    """
    A membership flip for one post in one view.

    ``action`` is ``"joined"`` (was out, now in) or ``"left"`` (was in, now
    out). ``field_sets`` holds every inclusion-minimal set of changed fields
    that suffices to produce the flip; when several fields must change together
    they appear together in a single set, and unrelated changed fields are
    excluded.
    """

    view_id: str
    action: str
    field_sets: tuple[tuple[str, ...], ...]

    @property
    def fields(self) -> tuple[str, ...]:
        "The shortest minimal field set (lexicographically ordered on ties)."
        return self.field_sets[0]


def _apply_changes(
    old: Mapping[str, object],
    new: Mapping[str, object],
    keys: Sequence[str],
) -> dict[str, object]:
    "Old metadata with only the changes for ``keys`` applied (deletions honored)."
    hybrid = dict(old)
    for key in keys:
        if key in new:
            hybrid[key] = new[key]
        else:
            hybrid.pop(key, None)
    return hybrid


def _minimal_explanations(
    view: View,
    old: Mapping[str, object],
    new: Mapping[str, object],
    changed: frozenset[str],
    target_membership: bool,
) -> tuple[tuple[str, ...], ...]:
    """
    Every inclusion-minimal subset of ``changed`` that, applied to the old
    metadata on its own, already moves the view to ``target_membership``.
    """
    relevant = sorted(
        key
        for key in changed
        if any(condition.key == key for condition in view.conditions)
    )
    found: list[tuple[str, ...]] = []
    for size in range(1, len(relevant) + 1):
        for combo in combinations(relevant, size):
            combo_keys = set(combo)
            if any(set(earlier) <= combo_keys for earlier in found):
                continue
            hybrid = _apply_changes(old, new, combo)
            if view.matches(hybrid) == target_membership:
                found.append(combo)
    if not found:
        return (tuple(sorted(changed)),)
    return tuple(found)


class SubscriptionEngine:
    """
    Track post membership in subscription views and notify on real flips only.

    A post's first sighting (via :meth:`prime` or :meth:`sync`) establishes a
    silent baseline; later revisions are compared against it. Notifications are
    de-duplicated by revision, and unchanged metadata is treated as a replay.
    """

    _views: dict[str, View]
    _memberships: dict[str, dict[str, bool]]
    _snapshots: dict[str, dict[str, object]]
    _revisions: set[tuple[str, str]]

    def __init__(self) -> None:
        self._views = {}
        self._memberships = {}
        self._snapshots = {}
        self._revisions = set()

    def add_view(self, view: View) -> None:
        "Register a view, replacing any view with the same id."
        self._views[view.view_id] = view

    def get_membership(self, post_id: str, view_id: str) -> bool:
        "Return the stored membership of a post in a view (False if unknown)."
        return self._memberships.get(post_id, {}).get(view_id, False)

    def prime(self, post_id: str, metadata: Mapping[str, object]) -> None:
        """
        Establish a membership baseline for an existing post without emitting
        notifications.
        """
        snapshot = dict(metadata)
        self._snapshots[post_id] = snapshot
        self._memberships[post_id] = {
            view_id: view.matches(snapshot)
            for view_id, view in self._views.items()
        }

    def sync(
        self,
        post_id: str,
        metadata: Mapping[str, object],
        revision: str | None = None,
    ) -> list[MembershipChange]:
        """
        Record a new revision of a post's metadata.

        Returns one :class:`MembershipChange` per view whose membership flipped,
        each annotated with the minimal changed field set behind the flip.
        Replaying an already-seen revision, or metadata identical to the last
        synced revision, returns an empty list.
        """
        if revision is not None and (post_id, revision) in self._revisions:
            return []

        snapshot = dict(metadata)
        old = self._snapshots.get(post_id)
        if old is None:
            self.prime(post_id, snapshot)
            if revision is not None:
                self._revisions.add((post_id, revision))
            return []

        changed = self._diff(old, snapshot)
        if revision is not None:
            self._revisions.add((post_id, revision))
        if not changed:
            return []

        old_membership = self._memberships.get(post_id, {})
        new_membership: dict[str, bool] = {}
        changes: list[MembershipChange] = []

        for view_id in sorted(self._views):
            view = self._views[view_id]
            was_in = old_membership.get(view_id, False)
            is_in = view.matches(snapshot)
            new_membership[view_id] = is_in
            if was_in == is_in:
                continue
            field_sets = _minimal_explanations(view, old, snapshot, changed, is_in)
            changes.append(
                MembershipChange(
                    view_id=view_id,
                    action="joined" if is_in else "left",
                    field_sets=field_sets,
                )
            )

        self._snapshots[post_id] = snapshot
        self._memberships[post_id] = new_membership
        return changes

    @staticmethod
    def _diff(
        old: Mapping[str, object], new: Mapping[str, object]
    ) -> frozenset[str]:
        changed: set[str] = set()
        for key in old.keys() | new.keys():
            if old.get(key, _MISSING) != new.get(key, _MISSING):
                changed.add(key)
        return frozenset(changed)
