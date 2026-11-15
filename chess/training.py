"""
Weighted training data from annotated game trees.

An annotated game (:class:`chess.pgn.Game`) is a tree of moves, not a single
line. A game that carries many explanatory variations must not end up with a
much larger training weight than a bare game, simply because it has more
comments. The helpers in this module therefore keep the **total weight of each
game fixed** (by default ``1.0``) and distribute that fixed weight down the
tree.

At a fork, the weight of a position is split among its child moves in
proportion to the relative weights assigned to those moves (equal weight when
the moves are unannotated). Variations that only demonstrate bad moves can be
marked as *excluded*: they remain fully traversable and searchable in the
original game tree, but receive no training weight, and the share they would
have taken is redistributed among the remaining moves.

The same position reached through different move orders (a transposition) is
exported as a single merged sample, while the individual paths that reached it
are retained as :class:`PathOrigin` records.

At any depth cut, the weights that rest on ended or cut-off branches still
add up to the fixed share of the whole game::

    >>> import chess
    >>> import chess.pgn
    >>> import chess.training

    >>> game = chess.pgn.Game()
    >>> e4 = game.add_variation(chess.Move.from_uci("e2e4"))
    >>> e5 = e4.add_variation(chess.Move.from_uci("e7e5"))
    >>> _ = e5.add_variation(chess.Move.from_uci("g1f3"))
    >>> d5 = e4.add_variation(chess.Move.from_uci("d7d5"))
    >>> d5.nags.add(chess.pgn.NAG_BLUNDER)  # a warning, not a move to learn

    >>> samples = chess.training.export_samples(game)
    >>> e4_sample = next(s for s in samples if s.fen == e4.board().fen())
    >>> e4_sample.weight
    1.0
    >>> e4_sample.next_moves  # the blunder takes no share; everything stays on e5
    {'e7e5': 1.0}

    >>> truncated = chess.training.export_samples(game, max_depth=1)
    >>> sum(sample.resting_weight for sample in truncated)
    1.0

See :func:`export_samples` for the full entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple

import chess
import chess.pgn


# NAGs that mark a move as bad. Such moves are typically shown only to explain
# why they should be avoided, so variations carrying them are excluded from
# weight allocation by default.
DEFAULT_EXCLUDED_NAGS = frozenset({
    chess.pgn.NAG_MISTAKE,        # ?
    chess.pgn.NAG_BLUNDER,        # ??
    chess.pgn.NAG_DUBIOUS_MOVE,   # ?!
    chess.pgn.NAG_WORST_MOVE,     # worst move
})

ExclusionPredicate = Callable[[chess.pgn.ChildNode], bool]
MoveWeightFn = Callable[[chess.pgn.ChildNode], float]


def excluded_by_nag(node: chess.pgn.ChildNode) -> bool:
    """
    Returns ``True`` if the move leading to *node* is marked with a NAG that
    describes a bad move (``?``, ``??``, ``?!``, or the "worst move" glyph).
    This is the default exclusion rule.
    """
    return any(nag in DEFAULT_EXCLUDED_NAGS for nag in node.nags)


@dataclass
class PathOrigin:
    """
    One path through the game tree that contributed to a training sample.

    A position can be reached by more than one move order. Each distinct path
    is recorded separately so that a merged sample still remembers every path
    (and the weight each one carried).
    """

    path: Tuple[str, ...]
    """The UCI moves from the root of the game to this position."""

    weight: float
    """The weight carried along :attr:`path` before merging."""

    excluded: bool
    """Whether this path runs through an excluded, weightless line."""


@dataclass
class TrainingSample:
    """
    A training example: a position together with the weight of each candidate
    next move.

    The weight reaching a position is either forwarded to a child (recorded in
    :attr:`next_moves`) or *rests* here because the line ended naturally or was
    cut off (see :attr:`resting_weight`). At any depth cut, the resting weights
    of all samples add up to the fixed total weight of the game. Excluded
    (illustrative bad-move) lines carry no weight and are only returned when
    ``include_excluded=True``.
    """

    fen: str
    """The position, in FEN notation."""

    board: chess.Board
    """The position as a :class:`chess.Board`."""

    next_moves: Dict[str, float]
    """Mapping of UCI next moves to the share of :attr:`weight` forwarded to
    each one. Excluded moves do not appear."""

    weight: float
    """Total weight that reached this position (across every merged path)."""

    resting_weight: float
    """Share of :attr:`weight` that is not forwarded, because the line ended
    here or was truncated. The resting weights of all samples sum to the fixed
    total weight of the game."""

    depth: int
    """Number of plies from the root of the game to this position."""

    truncated: bool
    """Whether at least one path was cut off here by a depth limit."""

    excluded: bool
    """Whether every contributing path is an excluded, weightless line."""

    origins: List[PathOrigin] = field(default_factory=list)
    """The distinct tree paths that reached this position and their weights."""

    @property
    def is_terminal(self) -> bool:
        """Whether this position carries no policy distribution, so all of its
        weight rests here."""
        return not self.next_moves


@dataclass
class _Frame:
    """A node pending expansion in the iterative depth-first walk."""

    node: chess.pgn.GameNode
    depth: int
    mass: float
    path: Tuple[str, ...]
    excluded: bool


def _default_move_weight(node: chess.pgn.ChildNode) -> float:
    return 1.0


def export_samples(
    game: chess.pgn.Game,
    *,
    total_weight: float = 1.0,
    max_depth: Optional[int] = None,
    move_weight: MoveWeightFn = _default_move_weight,
    is_excluded: ExclusionPredicate = excluded_by_nag,
    include_excluded: bool = False,
) -> List[TrainingSample]:
    """
    Exports weighted ``(position, next move)`` training samples from *game*.

    The whole game receives a fixed *total_weight* (default ``1.0``), so a
    heavily annotated game and a bare game weigh the same. At each fork the
    incoming weight is split among the allocating child moves in proportion to
    the relative weights returned by *move_weight* (equal weight by default).

    A child move for which *is_excluded* returns ``True`` (by default: moves
    marked with a bad-move NAG) stays traversable but takes no weight. Its share
    is redistributed among the remaining moves and the excluded subtree keeps a
    weight of zero. Excluded positions are only reported when
    *include_excluded* is set.

    Positions reached through different move orders are merged into one sample
    keyed by their position (the board transposition key); every contributing
    path is kept in :attr:`TrainingSample.origins`.

    *max_depth*, if given, cuts every line after that many plies. Whether a
    line ends naturally or is truncated, the resting weights of all samples
    still add up to *total_weight*. Each sample satisfies
    ``weight == resting_weight + sum(next_moves.values())``.

    Two unannotated replies split a position equally:

    >>> import chess.pgn
    >>> game = chess.pgn.Game()
    >>> e4 = game.add_variation(chess.Move.from_uci("e2e4"))
    >>> e5 = e4.add_variation(chess.Move.from_uci("e7e5"))
    >>> c5 = e4.add_variation(chess.Move.from_uci("c7c5"))
    >>> sample = next(s for s in chess.training.export_samples(game)
    ...               if s.fen == e4.board().fen())
    >>> sample.next_moves == {"e7e5": 0.5, "c7c5": 0.5}
    True
    """
    if total_weight < 0:
        raise ValueError("total_weight must be non-negative")
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be non-negative")

    if not game.variations:
        return []

    # Keyed by transposition key so identical positions are merged.
    merged: Dict[Hashable, TrainingSample] = {}
    # Stable, tree-order list of every record, including weightless ones.
    records: List[TrainingSample] = []

    stack = [_Frame(game, 0, float(total_weight), (), False)]

    while stack:
        frame = stack.pop()
        node = frame.node

        sample = _record(
            merged,
            records,
            board=node.board(),
            depth=frame.depth,
            mass=frame.mass,
            excluded=frame.excluded,
            path=frame.path,
        )

        children = list(node.variations)
        cut_off = max_depth is not None and frame.depth >= max_depth
        if cut_off or not children:
            # The line ends here (naturally, or because of the depth cut), so
            # its weight rests on this position instead of being forwarded.
            if not frame.excluded:
                sample.resting_weight += frame.mass
            if cut_off:
                sample.truncated = True
            continue

        allocating = [child for child in children if not is_excluded(child)]
        raw_weights = {child: max(0.0, float(move_weight(child))) for child in allocating}
        weight_sum = sum(raw_weights.values())

        if weight_sum <= 0.0 and not frame.excluded:
            # Every child is excluded (or carries zero weight): nothing can be
            # forwarded, so the parent keeps its mass as resting weight.
            sample.resting_weight += frame.mass

        # Children are pushed in reverse so the LIFO stack processes them in
        # tree order.
        for child in reversed(children):
            child_path = frame.path + (child.uci(),)
            child_excluded = frame.excluded or is_excluded(child)

            if child_excluded or weight_sum <= 0.0:
                # Excluded moves take no weight. If nothing can be allocated,
                # the parent keeps its mass and no child receives a share.
                child_mass = 0.0
            else:
                child_mass = frame.mass * (raw_weights[child] / weight_sum)
                sample.next_moves[child.uci()] = (
                    sample.next_moves.get(child.uci(), 0.0) + child_mass
                )

            stack.append(_Frame(child, frame.depth + 1, child_mass, child_path, child_excluded))

    if include_excluded:
        return records
    return [sample for sample in records if not sample.excluded and sample.weight > 0.0]


def _record(
    merged: Dict[Hashable, TrainingSample],
    records: List[TrainingSample],
    *,
    board: chess.Board,
    depth: int,
    mass: float,
    excluded: bool,
    path: Tuple[str, ...],
) -> TrainingSample:
    key = board._transposition_key()
    sample = merged.get(key)

    if sample is None:
        sample = TrainingSample(
            fen=board.fen(),
            board=board,
            next_moves={},
            weight=mass,
            resting_weight=0.0,
            depth=depth,
            truncated=False,
            excluded=excluded,
            origins=[PathOrigin(path, mass, excluded)],
        )
        merged[key] = sample
        records.append(sample)
        return sample

    # Merge a transposition into the existing sample for the same position.
    sample.weight += mass
    sample.excluded = sample.excluded and excluded
    sample.origins.append(PathOrigin(path, mass, excluded))
    return sample


def export_game_weights(game: chess.pgn.Game, **kwargs: Any) -> List[TrainingSample]:
    """Alias for :func:`export_samples`."""
    return export_samples(game, **kwargs)
