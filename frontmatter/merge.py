# -*- coding: utf-8 -*-
"""
Merge parallel metadata edits back onto a common base draft.

Two suppliers file parallel updates against a shared old draft of a
post's metadata. Each actor may only touch the fields granted to them,
and the editor-in-chief may freeze a whole group of fields at a given
version. :func:`merge_metadata` works out which changes are actually
adopted and reports why the rest were rejected:

    >>> base = {
    ...     "source": "old-source",
    ...     "category": "uncategorized",
    ...     "meta": {"id": 42, "note": "keep"},
    ... }
    >>> permissions = {
    ...     "supplier-a": ["source"],
    ...     "supplier-b": ["category"],
    ...     "editor": ["meta"],
    ... }
    >>> freezes = [Freeze(["meta"], version=5)]
    >>> changes = [
    ...     Change("supplier-a", "source", "new-source", effective_version=6),
    ...     Change("supplier-a", "category", "essay", effective_version=6),
    ...     Change("supplier-b", "category", "review", effective_version=6),
    ...     Change("editor", "meta.note", "updated", effective_version=6),
    ...     Change("editor", "meta.id", 43, effective_version=4),
    ... ]
    >>> result = merge_metadata(base, changes, permissions, freezes)

The supplier's own field is adopted, while the category edit from
``supplier-a`` is rejected for lack of permission. The editor's late
``meta.note`` change is rejected because ``meta`` is frozen at version
5 -- but the ``meta.id`` change declares an effective version *before*
the freeze, so it is honored even though it was delivered afterwards:

    >>> result.metadata
    {'source': 'new-source', 'category': 'review', 'meta': {'id': 43, 'note': 'keep'}}
    >>> for rejection in result.rejected:
    ...     print(rejection.change.actor, rejection.change.path, rejection.reason)
    supplier-a ('category',) 'supplier-a' may not modify 'category'
    editor ('meta', 'note') 'meta.note' is frozen since version 5

Competing changes to the same leaf keep both candidates when neither
has a provable order, and the base value is left in place:

    >>> result = merge_metadata(
    ...     {"source": "old"},
    ...     [Change("a", "source", "x"), Change("b", "source", "y")],
    ...     {"a": ["source"], "b": ["source"]},
    ... )
    >>> result.metadata
    {'source': 'old'}
    >>> result.conflicts[0].values
    ['x', 'y']

With a provable order on every competitor, the later change wins:

    >>> result = merge_metadata(
    ...     {"source": "old"},
    ...     [Change("a", "source", "x", order=1), Change("b", "source", "y", order=2)],
    ...     {"a": ["source"], "b": ["source"]},
    ... )
    >>> result.metadata
    {'source': 'y'}
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from . import Post

__all__ = [
    "Change",
    "Conflict",
    "Freeze",
    "MergeResult",
    "Rejection",
    "merge_metadata",
    "merge_posts",
]


Path = tuple[str, ...]
PathSpec = Union[str, Sequence[str]]


def _as_path(path: PathSpec) -> Path:
    "Normalize a dotted string or key sequence to a path tuple."
    if isinstance(path, str):
        return tuple(part for part in path.split(".") if part)
    return tuple(str(part) for part in path)


def _is_prefix(prefix: Path, path: Path) -> bool:
    return len(prefix) <= len(path) and path[: len(prefix)] == prefix


def _dotted(path: Path) -> str:
    return ".".join(path) or "<root>"


def _flatten(path: Path, value: Any) -> Iterator[tuple[Path, Any]]:
    "Yield (leaf path, scalar value) pairs; lists are treated as atomic."
    if isinstance(value, dict) and value:
        for key, sub in value.items():
            yield from _flatten(path + (str(key),), sub)
    else:
        yield path, value


def _set_path(metadata: dict[str, Any], path: Path, value: Any) -> None:
    node = metadata
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


@dataclass(frozen=True)
class Change:
    """
    A single metadata edit filed by an actor.

    ``path`` is the key being changed, either a dotted string
    (``"meta.id"``) or a tuple of keys. A dict value is applied
    leaf-by-leaf, so changing a parent object never smuggles in edits
    to a frozen child key. ``effective_version`` is the declared
    version the change was approved for; ``order`` (sequence number or
    timestamp) proves ordering against competing changes, and may be
    left as ``None`` when no such proof exists.
    """

    actor: str
    path: PathSpec
    value: Any
    effective_version: int = 0
    order: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _as_path(self.path))


@dataclass(frozen=True)
class Freeze:
    """
    A freeze placed by the editor-in-chief on a group of fields.

    Every key at or below one of ``paths`` is frozen starting at
    ``version``: changes declaring an ``effective_version`` at or after
    it are rejected, while changes approved earlier (smaller effective
    version) are still honored even if delivered late.
    """

    paths: Iterable[PathSpec]
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", tuple(_as_path(p) for p in self.paths))


@dataclass
class Rejection:
    "A change that was not adopted, with the reason why."

    change: Change
    reason: str


@dataclass
class Conflict:
    "Competing changes with no provable order; both values are kept."

    path: Path
    values: list[Any]
    changes: list[Change]


@dataclass
class MergeResult:
    """
    Outcome of :func:`merge_metadata`.

    ``metadata`` is the merged mapping. ``adopted`` and ``rejected``
    hold the leaf-level changes that were applied or turned away (with
    reasons). ``conflicts`` lists the paths where competing changes
    could not be ordered; both candidate values are preserved there and
    the base value is left untouched in ``metadata``.
    """

    metadata: dict[str, Any]
    adopted: list[Change] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)


def _active_freeze(
    path: Path, freezes: list[Freeze], effective_version: int
) -> Freeze | None:
    "Return the earliest freeze covering ``path`` at ``effective_version``, if any."
    earliest: Freeze | None = None
    for freeze in freezes:
        covered = any(
            _is_prefix(_as_path(freeze_path), path) for freeze_path in freeze.paths
        )
        if covered and effective_version >= freeze.version:
            if earliest is None or freeze.version < earliest.version:
                earliest = freeze
    return earliest


def merge_metadata(
    base: Mapping[str, Any],
    changes: Iterable[Change],
    permissions: Mapping[str, Iterable[PathSpec]],
    freezes: Iterable[Freeze] = (),
) -> MergeResult:
    """
    Merge parallel metadata changes onto a common ``base`` mapping.

    ``permissions`` maps each actor to the key paths they may modify;
    a grant on a parent object covers its children, but never overrides
    a freeze on a child key:

        >>> result = merge_metadata(
        ...     {"meta": {"id": 1, "note": "old"}},
        ...     [Change("editor", "meta", {"id": 2, "note": "new"}, effective_version=6)],
        ...     {"editor": ["meta"]},
        ...     [Freeze(["meta.id"], version=5)],
        ... )
        >>> result.metadata
        {'meta': {'id': 1, 'note': 'new'}}
        >>> result.rejected[0].reason
        "'meta.id' is frozen since version 5"

    Each change is flattened to leaf-level assignments, so the parts of
    a parent-object edit that are permitted and unfrozen are adopted
    while the rest are rejected individually.
    """
    metadata = copy.deepcopy(dict(base))
    perms = {
        actor: [_as_path(path) for path in paths]
        for actor, paths in permissions.items()
    }
    freeze_list = list(freezes)

    candidates: dict[Path, list[Change]] = {}
    adopted: list[Change] = []
    rejected: list[Rejection] = []
    conflicts: list[Conflict] = []

    for change in changes:
        for leaf_path, leaf_value in _flatten(_as_path(change.path), change.value):
            leaf = Change(
                change.actor,
                leaf_path,
                leaf_value,
                effective_version=change.effective_version,
                order=change.order,
            )
            if not leaf_path:
                rejected.append(
                    Rejection(leaf, "cannot replace the metadata root with a scalar")
                )
                continue
            allowed = perms.get(change.actor, [])
            if not any(_is_prefix(grant, leaf_path) for grant in allowed):
                rejected.append(
                    Rejection(
                        leaf,
                        f"{change.actor!r} may not modify {_dotted(leaf_path)!r}",
                    )
                )
                continue
            blocker = _active_freeze(leaf_path, freeze_list, change.effective_version)
            if blocker is not None:
                rejected.append(
                    Rejection(
                        leaf,
                        f"{_dotted(leaf_path)!r} is frozen since version {blocker.version}",
                    )
                )
                continue
            candidates.setdefault(leaf_path, []).append(leaf)

    for path, group in candidates.items():
        distinct: list[Any] = []
        for leaf in group:
            if leaf.value not in distinct:
                distinct.append(leaf.value)

        if len(distinct) == 1:
            _set_path(metadata, path, copy.deepcopy(distinct[0]))
            adopted.extend(group)
            continue

        orders = [leaf.order for leaf in group]
        provable = all(order is not None for order in orders) and len(set(orders)) == len(
            orders
        )
        if provable:
            winner = max(
                group,
                key=lambda leaf: leaf.order if leaf.order is not None else float("-inf"),
            )
            _set_path(metadata, path, copy.deepcopy(winner.value))
            adopted.append(winner)
            for leaf in group:
                if leaf is not winner:
                    rejected.append(
                        Rejection(
                            leaf,
                            f"superseded by {winner.actor!r} under provable order",
                        )
                    )
        else:
            # no provable order: keep both candidates, leave base value
            conflicts.append(
                Conflict(path=path, values=distinct, changes=list(group))
            )

    return MergeResult(
        metadata=metadata, adopted=adopted, rejected=rejected, conflicts=conflicts
    )


def merge_posts(
    base: Post,
    changes: Iterable[Change],
    permissions: Mapping[str, Iterable[PathSpec]],
    freezes: Iterable[Freeze] = (),
) -> tuple[Post, MergeResult]:
    """
    Merge parallel metadata changes onto a base :py:class:`Post <frontmatter.Post>`.

    Returns a new post (content and handler taken from ``base``) together
    with the :py:class:`MergeResult` describing what was adopted,
    rejected, or left in conflict.
    """
    result = merge_metadata(base.metadata, changes, permissions, freezes)
    post = Post(base.content, handler=getattr(base, "handler", None), **result.metadata)
    return post, result
