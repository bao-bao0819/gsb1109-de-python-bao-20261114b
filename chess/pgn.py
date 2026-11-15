from __future__ import annotations

import abc
import dataclasses
import enum
import itertools
import logging
import os
import re
import typing

import chess
import chess.engine
import chess.svg

from typing import Any, Callable, Dict, Generic, Hashable, Iterable, Iterator, List, Literal, Mapping, MutableMapping, Set, TextIO, Tuple, Type, TypeVar, Optional, Union
from chess import Color, Square

if typing.TYPE_CHECKING:
    from typing_extensions import Self, override
else:
    F = typing.TypeVar("F", bound=Callable[..., Any])
    def override(fn: F, /) -> F:
        return fn


LOGGER = logging.getLogger(__name__)


# Reference of Numeric Annotation Glyphs (NAGs):
# https://en.wikipedia.org/wiki/Numeric_Annotation_Glyphs

NAG_NULL = 0

NAG_GOOD_MOVE = 1
"""A good move. Can also be indicated by ``!`` in PGN notation."""

NAG_MISTAKE = 2
"""A mistake. Can also be indicated by ``?`` in PGN notation."""

NAG_BRILLIANT_MOVE = 3
"""A brilliant move. Can also be indicated by ``!!`` in PGN notation."""

NAG_BLUNDER = 4
"""A blunder. Can also be indicated by ``??`` in PGN notation."""

NAG_SPECULATIVE_MOVE = 5
"""A speculative move. Can also be indicated by ``!?`` in PGN notation."""

NAG_DUBIOUS_MOVE = 6
"""A dubious move. Can also be indicated by ``?!`` in PGN notation."""

NAG_FORCED_MOVE = 7
NAG_SINGULAR_MOVE = 8
NAG_WORST_MOVE = 9
NAG_DRAWISH_POSITION = 10
NAG_QUIET_POSITION = 11
NAG_ACTIVE_POSITION = 12
NAG_UNCLEAR_POSITION = 13
NAG_WHITE_SLIGHT_ADVANTAGE = 14
NAG_BLACK_SLIGHT_ADVANTAGE = 15
NAG_WHITE_MODERATE_ADVANTAGE = 16
NAG_BLACK_MODERATE_ADVANTAGE = 17
NAG_WHITE_DECISIVE_ADVANTAGE = 18
NAG_BLACK_DECISIVE_ADVANTAGE = 19

NAG_WHITE_ZUGZWANG = 22
NAG_BLACK_ZUGZWANG = 23

NAG_WHITE_MODERATE_COUNTERPLAY = 132
NAG_BLACK_MODERATE_COUNTERPLAY = 133
NAG_WHITE_DECISIVE_COUNTERPLAY = 134
NAG_BLACK_DECISIVE_COUNTERPLAY = 135
NAG_WHITE_MODERATE_TIME_PRESSURE = 136
NAG_BLACK_MODERATE_TIME_PRESSURE = 137
NAG_WHITE_SEVERE_TIME_PRESSURE = 138
NAG_BLACK_SEVERE_TIME_PRESSURE = 139

NAG_NOVELTY = 146


TAG_REGEX = re.compile(r"^\[([A-Za-z0-9][A-Za-z0-9_+#=:-]*)\s+\"([^\r]*)\"\]\s*$")

TAG_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+#=:-]*\Z")

MOVETEXT_REGEX = re.compile(r"""
    (
        [NBKRQ]?[a-h]?[1-8]?[\-x]?[a-h][1-8](?:=?[nbrqkNBRQK])?
        |[PNBRQK]?@[a-h][1-8]
        |--
        |Z0
        |0000
        |@@@@
        |O-O(?:-O)?
        |0-0(?:-0)?
    )
    |(\{.*)
    |(;.*)
    |(\$[0-9]+)
    |(\()
    |(\))
    |(\*|1-0|0-1|1/2-1/2)
    |([\?!]{1,2})
    """, re.DOTALL | re.VERBOSE)

SKIP_MOVETEXT_REGEX = re.compile(r""";|\{|\}""")


CLOCK_REGEX = re.compile(r"""(?P<prefix>\s?)\[%clk\s(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d*)?)\](?P<suffix>\s?)""")
EMT_REGEX = re.compile(r"""(?P<prefix>\s?)\[%emt\s(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d*)?)\](?P<suffix>\s?)""")

EVAL_REGEX = re.compile(r"""
    (?P<prefix>\s?)
    \[%eval\s(?:
        \#(?P<mate>[+-]?\d+)
        |(?P<cp>[+-]?(?:\d{0,10}\.\d{1,2}|\d{1,10}\.?))
    )(?:
        ,(?P<depth>\d+)
    )?\]
    (?P<suffix>\s?)
    """, re.VERBOSE)

ARROWS_REGEX = re.compile(r"""
    (?P<prefix>\s?)
    \[%(?:csl|cal)\s(?P<arrows>
        [RGYB][a-h][1-8](?:[a-h][1-8])?
        (?:,[RGYB][a-h][1-8](?:[a-h][1-8])?)*
    )\]
    (?P<suffix>\s?)
    """, re.VERBOSE)

def _condense_affix(infix: str) -> Callable[[typing.Match[str]], str]:
    def repl(match: typing.Match[str]) -> str:
        if infix:
            return match.group("prefix") + infix + match.group("suffix")
        else:
            return match.group("prefix") and match.group("suffix")
    return repl


def _standardize_comments(comment: Union[str, list[str]]) -> list[str]:
    return [] if not comment else [comment] if isinstance(comment, str) else comment


TAG_ROSTER = ["Event", "Site", "Date", "Round", "White", "Black", "Result"]


class SkipType(enum.Enum):
    SKIP = None

SKIP = SkipType.SKIP


ResultT = TypeVar("ResultT", covariant=True)


class TimeControlType(enum.Enum):
    UNKNOWN = 0
    UNLIMITED = 1
    STANDARD = 2
    RAPID = 3
    BLITZ = 4
    BULLET = 5


@dataclasses.dataclass
class TimeControlPart:
    moves: int = 0
    time: int = 0
    increment: float = 0
    delay: float = 0


@dataclasses.dataclass
class TimeControl:
    """
    PGN TimeControl Parser
    Spec: http://www.saremba.de/chessgml/standards/pgn/pgn-complete.htm#c9.6

    Not Yet Implemented:
    - Hourglass/Sandclock ('*' prefix)
    - Differentiating between Bronstein and Simple Delay (Not part of the PGN Spec)
      - More Info: https://en.wikipedia.org/wiki/Chess_clock#Timing_methods
    """

    parts: list[TimeControlPart] = dataclasses.field(default_factory=list)
    type: TimeControlType = TimeControlType.UNKNOWN


class _AcceptFrame:
    def __init__(self, node: ChildNode, *, is_variation: bool = False, sidelines: bool = True):
        self.state = "pre"
        self.node = node
        self.is_variation = is_variation
        self.variations = iter(itertools.islice(node.parent.variations, 1, None) if sidelines else [])
        self.in_variation = False


class GameNode(abc.ABC):
    variations: List[ChildNode]
    """A list of child nodes."""

    comments: list[str]
    """
    A comment that goes behind the move leading to this node. Comments
    that occur before any moves are assigned to the root node.
    """

    starting_comments: list[str]

    nags: Set[int]

    def __init__(self, *, comment: Union[str, list[str]] = "") -> None:
        self.variations = []
        self.comments = _standardize_comments(comment)

        # Deprecated: These should be properties of ChildNode, but need to
        # remain here for backwards compatibility.
        self.starting_comments = []
        self.nags = set()

    @property
    @abc.abstractmethod
    def parent(self) -> Optional[GameNode]:
        """The parent node or ``None`` if this is the root node of the game."""

    @property
    @abc.abstractmethod
    def move(self) -> Optional[chess.Move]:
        """
        The move leading to this node or ``None`` if this is the root node of
        the game.
        """

    @abc.abstractmethod
    def board(self) -> chess.Board:
        """
        Gets a board with the position of the node.

        For the root node, this is the default starting position (for the
        ``Variant``) unless the ``FEN`` header tag is set.

        It's a copy, so modifying the board will not alter the game.

        Complexity is `O(n)`.
        """

    @abc.abstractmethod
    def ply(self) -> int:
        """
        Returns the number of half-moves up to this node, as indicated by
        fullmove number and turn of the position.
        See :func:`chess.Board.ply()`.

        Usually this is equal to the number of parent nodes, but it may be
        more if the game was started from a custom position.

        Complexity is `O(n)`.
        """

    def turn(self) -> Color:
        """
        Gets the color to move at this node. See :data:`chess.Board.turn`.

        Complexity is `O(n)`.
        """
        return self.ply() % 2 == 0

    def root(self) -> GameNode:
        node = self
        while node.parent:
            node = node.parent
        return node

    def game(self) -> Game:
        """
        Gets the root node, i.e., the game.

        Complexity is `O(n)`.
        """
        root = self.root()
        assert isinstance(root, Game), "GameNode not rooted in Game"
        return root

    def end(self) -> GameNode:
        """
        Follows the main variation to the end and returns the last node.

        Complexity is `O(n)`.
        """
        node = self

        while node.variations:
            node = node.variations[0]

        return node

    def is_end(self) -> bool:
        """
        Checks if this node is the last node in the current variation.

        Complexity is `O(1)`.
        """
        return not self.variations

    def starts_variation(self) -> bool:
        """
        Checks if this node starts a variation (and can thus have a starting
        comment). The root node does not start a variation and can have no
        starting comment.

        For example, in ``1. e4 e5 (1... c5 2. Nf3) 2. Nf3``, the node holding
        1... c5 starts a variation.

        Complexity is `O(1)`.
        """
        if not self.parent or not self.parent.variations:
            return False

        return self.parent.variations[0] != self

    def is_mainline(self) -> bool:
        """
        Checks if the node is in the mainline of the game.

        Complexity is `O(n)`.
        """
        node = self

        while node.parent:
            parent = node.parent

            if not parent.variations or parent.variations[0] != node:
                return False

            node = parent

        return True

    def is_main_variation(self) -> bool:
        """
        Checks if this node is the first variation from the point of view of its
        parent. The root node is also in the main variation.

        Complexity is `O(1)`.
        """
        if not self.parent:
            return True

        return not self.parent.variations or self.parent.variations[0] == self

    def __getitem__(self, move: Union[int, chess.Move, GameNode]) -> ChildNode:
        try:
            return self.variations[move]  # type: ignore
        except TypeError:
            for variation in self.variations:
                if variation.move == move or variation == move:
                    return variation

        raise KeyError(move)

    def __contains__(self, move: Union[int, chess.Move, GameNode]) -> bool:
        try:
            self[move]
        except KeyError:
            return False
        else:
            return True

    def variation(self, move: Union[int, chess.Move, GameNode]) -> ChildNode:
        """
        Gets a child node by either the move or the variation index.
        """
        return self[move]

    def has_variation(self, move: Union[int, chess.Move, GameNode]) -> bool:
        """Checks if this node has the given variation."""
        return move in self

    def promote_to_main(self, move: Union[int, chess.Move, GameNode]) -> None:
        """Promotes the given *move* to the main variation."""
        variation = self[move]
        self.variations.remove(variation)
        self.variations.insert(0, variation)

    def promote(self, move: Union[int, chess.Move, GameNode]) -> None:
        """Moves a variation one up in the list of variations."""
        variation = self[move]
        i = self.variations.index(variation)
        if i > 0:
            self.variations[i - 1], self.variations[i] = self.variations[i], self.variations[i - 1]

    def demote(self, move: Union[int, chess.Move, GameNode]) -> None:
        """Moves a variation one down in the list of variations."""
        variation = self[move]
        i = self.variations.index(variation)
        if i < len(self.variations) - 1:
            self.variations[i + 1], self.variations[i] = self.variations[i], self.variations[i + 1]

    def remove_variation(self, move: Union[int, chess.Move, GameNode]) -> None:
        """Removes a variation."""
        self.variations.remove(self.variation(move))

    def add_variation(self, move: chess.Move, *, comment: Union[str, list[str]] = "", starting_comment: Union[str, list[str]] = "", nags: Iterable[int] = []) -> ChildNode:
        """Creates a child node with the given attributes."""
        # Instantiate ChildNode only in this method.
        return ChildNode(self, move, comment=comment, starting_comment=starting_comment, nags=nags)

    def add_main_variation(self, move: chess.Move, *, comment: str = "", nags: Iterable[int] = []) -> ChildNode:
        """
        Creates a child node with the given attributes and promotes it to the
        main variation.
        """
        node = self.add_variation(move, comment=comment, nags=nags)
        self.variations.insert(0, self.variations.pop())
        return node

    def next(self) -> Optional[ChildNode]:
        """
        Returns the first node of the mainline after this node, or ``None`` if
        this node does not have any children.

        Complexity is `O(1)`.
        """
        return self.variations[0] if self.variations else None

    def mainline(self) -> Mainline[ChildNode]:
        """Returns an iterable over the mainline starting after this node."""
        return Mainline(self, lambda node: node)

    def mainline_moves(self) -> Mainline[chess.Move]:
        """Returns an iterable over the main moves after this node."""
        return Mainline(self, lambda node: node.move)

    def add_line(self, moves: Iterable[chess.Move], *, comment: Union[str, list[str]] = "", starting_comment: Union[str, list[str]] = "", nags: Iterable[int] = []) -> GameNode:
        """
        Creates a sequence of child nodes for the given list of moves.
        Adds *comment* and *nags* to the last node of the line and returns it.
        """
        node = self

        # Add line.
        for move in moves:
            node = node.add_variation(move, starting_comment=starting_comment)
            starting_comment = ""

        # Merge comment and NAGs.
        comments = _standardize_comments(comment)
        node.comments.extend(comments)
        node.nags.update(nags)

        return node

    def eval(self) -> Optional[chess.engine.PovScore]:
        """
        Parses the first valid ``[%eval ...]`` annotation in the comment of
        this node, if any.

        Complexity is `O(n)`.
        """
        match = EVAL_REGEX.search(" ".join(self.comments))
        if not match:
            return None

        turn = self.turn()

        if match.group("mate"):
            mate = int(match.group("mate"))
            score: chess.engine.Score = chess.engine.Mate(mate)
            if mate == 0:
                # Resolve this ambiguity in the specification in favor of
                # standard chess: The player to move after mate is the player
                # who has been mated.
                return chess.engine.PovScore(score, turn)
        else:
            score = chess.engine.Cp(round(float(match.group("cp")) * 100))

        return chess.engine.PovScore(score if turn else -score, turn)

    def eval_depth(self) -> Optional[int]:
        """
        Parses the first valid ``[%eval ...]`` annotation in the comment of
        this node and returns the corresponding depth, if any.

        Complexity is `O(1)`.
        """
        match = EVAL_REGEX.search(" ".join(self.comments))
        return int(match.group("depth")) if match and match.group("depth") else None

    def set_eval(self, score: Optional[chess.engine.PovScore], depth: Optional[int] = None) -> None:
        """
        Replaces the first valid ``[%eval ...]`` annotation in the comment of
        this node or adds a new one.
        """
        eval = ""
        if score is not None:
            depth_suffix = "" if depth is None else f",{max(depth, 0):d}"
            cp = score.white().score()
            if cp is not None:
                eval = f"[%eval {float(cp) / 100:.2f}{depth_suffix}]"
            elif score.white().mate():
                eval = f"[%eval #{score.white().mate()}{depth_suffix}]"

        self._replace_or_add_annotation(eval, EVAL_REGEX)

    def arrows(self) -> List[chess.svg.Arrow]:
        """
        Parses all ``[%csl ...]`` and ``[%cal ...]`` annotations in the comment
        of this node.

        Returns a list of :class:`arrows <chess.svg.Arrow>`.
        """
        arrows = []
        for match in ARROWS_REGEX.finditer(" ".join(self.comments)):
            for group in match.group("arrows").split(","):
                arrows.append(chess.svg.Arrow.from_pgn(group))

        return arrows

    def set_arrows(self, arrows: Iterable[Union[chess.svg.Arrow, Tuple[Square, Square]]]) -> None:
        """
        Replaces all valid ``[%csl ...]`` and ``[%cal ...]`` annotations in
        the comment of this node or adds new ones.
        """
        csl: List[str] = []
        cal: List[str] = []

        for arrow in arrows:
            try:
                tail, head = arrow  # type: ignore
                arrow = chess.svg.Arrow(tail, head)
            except TypeError:
                pass
            (csl if arrow.tail == arrow.head else cal).append(arrow.pgn())  # type: ignore

        for index in range(len(self.comments)):
            self.comments[index] = ARROWS_REGEX.sub(_condense_affix(""), self.comments[index])

        self.comments = list(filter(None, self.comments))

        prefix = ""
        if csl:
            prefix += f"[%csl {','.join(csl)}]"
        if cal:
            prefix += f"[%cal {','.join(cal)}]"

        if prefix:
            self.comments.insert(0, prefix)

    def clock(self) -> Optional[float]:
        """
        Parses the first valid ``[%clk ...]`` annotation in the comment of
        this node, if any.

        Returns the player's remaining time to the next time control after this
        move, in seconds.
        """
        match = CLOCK_REGEX.search(" ".join(self.comments))
        if match is None:
            return None
        return int(match.group("hours")) * 3600 + int(match.group("minutes")) * 60 + float(match.group("seconds"))

    def set_clock(self, seconds: Optional[float]) -> None:
        """
        Replaces the first valid ``[%clk ...]`` annotation in the comment of
        this node or adds a new one.
        """
        clk = ""
        if seconds is not None:
            seconds = max(0, seconds)
            hours = int(seconds // 3600)
            minutes = int(seconds % 3600 // 60)
            seconds = seconds % 3600 % 60
            seconds_part = f"{seconds:06.3f}".rstrip("0").rstrip(".")
            clk = f"[%clk {hours:d}:{minutes:02d}:{seconds_part}]"

        self._replace_or_add_annotation(clk, CLOCK_REGEX)

    def emt(self) -> Optional[float]:
        """
        Parses the first valid ``[%emt ...]`` annotation in the comment of
        this node, if any.

        Returns the player's elapsed move time use for the comment of this
        move, in seconds.
        """
        match = EMT_REGEX.search(" ".join(self.comments))
        if match is None:
            return None
        return int(match.group("hours")) * 3600 + int(match.group("minutes")) * 60 + float(match.group("seconds"))

    def set_emt(self, seconds: Optional[float]) -> None:
        """
        Replaces the first valid ``[%emt ...]`` annotation in the comment of
        this node or adds a new one.
        """
        emt = ""
        if seconds is not None:
            seconds = max(0, seconds)
            hours = int(seconds // 3600)
            minutes = int(seconds % 3600 // 60)
            seconds = seconds % 3600 % 60
            seconds_part = f"{seconds:06.3f}".rstrip("0").rstrip(".")
            emt = f"[%emt {hours:d}:{minutes:02d}:{seconds_part}]"

        self._replace_or_add_annotation(emt, EMT_REGEX)

    def _replace_or_add_annotation(self, text: str, regex: re.Pattern[str]) -> None:
        found = 0
        for index in range(len(self.comments)):
            self.comments[index], found = regex.subn(_condense_affix(text), self.comments[index], count=1)
            if found:
                break

        self.comments = list(filter(None, self.comments))

        if not found and text:
            self.comments.append(text)

    @abc.abstractmethod
    def accept(self, visitor: BaseVisitor[ResultT]) -> ResultT:
        """
        Traverses game nodes in PGN order using the given *visitor*. Starts with
        the move leading to this node. Returns the *visitor* result.
        """

    def accept_subgame(self, visitor: BaseVisitor[ResultT]) -> ResultT:
        """
        Traverses headers and game nodes in PGN order, as if the game was
        starting after this node. Returns the *visitor* result.
        """
        if visitor.begin_game() is not SKIP:
            game = self.game()
            board = self.board()

            dummy_game = Game.without_tag_roster()
            dummy_game.setup(board)

            visitor.begin_headers()

            for tagname, tagvalue in game.headers.items():
                if tagname not in dummy_game.headers:
                    visitor.visit_header(tagname, tagvalue)
            for tagname, tagvalue in dummy_game.headers.items():
                visitor.visit_header(tagname, tagvalue)

            if visitor.end_headers() is not SKIP:
                visitor.visit_board(board)

                if self.variations:
                    self.variations[0]._accept(board, visitor)

                visitor.visit_result(game.headers.get("Result", "*"))

        visitor.end_game()
        return visitor.result()

    def __str__(self) -> str:
        return self.accept(StringExporter(columns=None))


class ChildNode(GameNode):
    """
    A child node of a game, with the move leading to it.
    Extends :class:`~chess.pgn.GameNode`.
    """

    starting_comments: list[str]
    """
    A comment for the start of a variation. Only nodes that
    actually start a variation (:func:`~chess.pgn.GameNode.starts_variation()`
    checks this) can have a starting comment. The root node can not have
    a starting comment.
    """

    nags: Set[int]
    """
    A set of NAGs as integers. NAGs always go behind a move, so the root
    node of the game will never have NAGs.
    """

    def __init__(self, parent: GameNode, move: chess.Move, *, comment: Union[str, list[str]] = "", starting_comment: Union[str, list[str]] = "", nags: Iterable[int] = []) -> None:
        super().__init__(comment=comment)
        self._parent = parent
        self._move = move
        self.parent.variations.append(self)

        self.nags.update(nags)
        self.starting_comments = _standardize_comments(starting_comment)

    @property
    @override
    def parent(self) -> GameNode:
        """The parent node."""
        return self._parent

    @property
    @override
    def move(self) -> chess.Move:
        """The move leading to this node."""
        return self._move

    @override
    def board(self) -> chess.Board:
        stack: List[chess.Move] = []
        node: GameNode = self

        while node.move is not None and node.parent is not None:
            stack.append(node.move)
            node = node.parent

        board = node.game().board()

        while stack:
            board.push(stack.pop())

        return board

    @override
    def ply(self) -> int:
        ply = 0
        node: GameNode = self
        while node.parent is not None:
            ply += 1
            node = node.parent
        return node.game().ply() + ply

    def san(self) -> str:
        """
        Gets the standard algebraic notation of the move leading to this node.
        See :func:`chess.Board.san()`.

        Do not call this on the root node.

        Complexity is `O(n)`.
        """
        return self.parent.board().san(self.move)

    def uci(self, *, chess960: Optional[bool] = None) -> str:
        """
        Gets the UCI notation of the move leading to this node.
        See :func:`chess.Board.uci()`.

        Do not call this on the root node.

        Complexity is `O(n)`.
        """
        return self.parent.board().uci(self.move, chess960=chess960)

    @override
    def end(self) -> ChildNode:
        """
        Follows the main variation to the end and returns the last node.

        Complexity is `O(n)`.
        """
        return typing.cast(ChildNode, super().end())

    def _accept_node(self, parent_board: chess.Board, visitor: BaseVisitor[ResultT]) -> None:
        if self.starting_comments:
            visitor.visit_comment(self.starting_comments)

        visitor.visit_move(parent_board, self.move)

        parent_board.push(self.move)
        visitor.visit_board(parent_board)
        parent_board.pop()

        for nag in sorted(self.nags):
            visitor.visit_nag(nag)

        if self.comments:
            visitor.visit_comment(self.comments)

    def _accept(self, parent_board: chess.Board, visitor: BaseVisitor[ResultT], *, sidelines: bool = True) -> None:
        stack = [_AcceptFrame(self, sidelines=sidelines)]

        while stack:
            top = stack[-1]

            if top.in_variation:
                top.in_variation = False
                visitor.end_variation()

            if top.state == "pre":
                top.node._accept_node(parent_board, visitor)
                top.state = "variations"
            elif top.state == "variations":
                try:
                    variation = next(top.variations)
                except StopIteration:
                    if top.node.variations:
                        parent_board.push(top.node.move)
                        stack.append(_AcceptFrame(top.node.variations[0], sidelines=True))
                        top.state = "post"
                    else:
                        top.state = "end"
                else:
                    if visitor.begin_variation() is not SKIP:
                        stack.append(_AcceptFrame(variation, sidelines=False, is_variation=True))
                    top.in_variation = True
            elif top.state == "post":
                parent_board.pop()
                top.state = "end"
            else:
                stack.pop()

    @override
    def accept(self, visitor: BaseVisitor[ResultT]) -> ResultT:
        self._accept(self.parent.board(), visitor, sidelines=False)
        return visitor.result()

    def __repr__(self) -> str:
        try:
            parent_board = self.parent.board()
        except ValueError:
            return f"<{type(self).__name__} at {id(self):#x} (dangling: {self.move})>"
        else:
            return "<{} at {:#x} ({}{} {} ...)>".format(
                type(self).__name__,
                id(self),
                parent_board.fullmove_number,
                "." if parent_board.turn == chess.WHITE else "...",
                parent_board.san(self.move))


GameT = TypeVar("GameT", bound="Game")

class Game(GameNode):
    """
    The root node of a game with extra information such as headers and the
    starting position. Extends :class:`~chess.pgn.GameNode`.
    """

    headers: Headers
    """
    A mapping of headers. By default, the following 7 headers are provided
    (Seven Tag Roster):

    >>> import chess.pgn
    >>>
    >>> game = chess.pgn.Game()
    >>> game.headers
    Headers(Event='?', Site='?', Date='????.??.??', Round='?', White='?', Black='?', Result='*')
    """

    errors: List[Exception]
    """
    A list of errors (such as illegal or ambiguous moves) encountered while
    parsing the game.
    """

    def __init__(self, headers: Optional[Union[Mapping[str, str], Iterable[Tuple[str, str]]]] = None) -> None:
        super().__init__()
        self.headers = Headers(headers)
        self.errors = []

    @property
    @override
    def parent(self) -> None:
        return None

    @property
    @override
    def move(self) -> None:
        return None

    @override
    def board(self) -> chess.Board:
        return self.headers.board()

    @override
    def ply(self) -> int:
        # Optimization: Parse FEN only for custom starting positions.
        return self.board().ply() if "FEN" in self.headers else 0

    def setup(self, board: Union[chess.Board, str]) -> None:
        """
        Sets up a specific starting position. This sets (or resets) the
        ``FEN``, ``SetUp``, and ``Variant`` header tags.
        """
        try:
            fen = board.fen()  # type: ignore
            setup = typing.cast(chess.Board, board)
        except AttributeError:
            setup = chess.Board(board)  # type: ignore
            setup.chess960 = setup.has_chess960_castling_rights()
            fen = setup.fen()

        if fen == type(setup).starting_fen:
            self.headers.pop("FEN", None)
            self.headers.pop("SetUp", None)
        else:
            self.headers["FEN"] = fen
            self.headers["SetUp"] = "1"

        if type(setup).aliases[0] == "Standard" and setup.chess960:
            self.headers["Variant"] = "Chess960"
        elif type(setup).aliases[0] != "Standard":
            self.headers["Variant"] = type(setup).aliases[0]
            self.headers["FEN"] = fen
        else:
            self.headers.pop("Variant", None)

    @override
    def accept(self, visitor: BaseVisitor[ResultT]) -> ResultT:
        """
        Traverses the game in PGN order using the given *visitor*. Returns
        the *visitor* result.
        """
        if visitor.begin_game() is not SKIP:
            for tagname, tagvalue in self.headers.items():
                visitor.visit_header(tagname, tagvalue)
            if visitor.end_headers() is not SKIP:
                board = self.board()
                visitor.visit_board(board)

                if self.comments:
                    visitor.visit_comment(self.comments)

                if self.variations:
                    self.variations[0]._accept(board, visitor)

                visitor.visit_result(self.headers.get("Result", "*"))

        visitor.end_game()
        return visitor.result()

    def time_control(self) -> TimeControl:
        """
        Returns the time control of the game. If the game has no time control
        information, the default time control ('UNKNOWN') is returned.
        """
        time_control_header = self.headers.get("TimeControl", "")
        return parse_time_control(time_control_header)

    def build_index(self, *, rules: Any = None, **kwargs: Any) -> "PGNIndex":
        """Builds a two-layer :class:`PGNIndex` for this game."""
        return PGNIndex.from_game(self, rules=rules, **kwargs)

    build_pgn_index = build_index

    @property
    def pgn_index(self) -> "PGNIndex":
        return self.build_index()

    @property
    def index(self) -> "PGNIndex":
        return self.build_index()

    @property
    def position_index(self) -> "PositionIndex":
        return self.build_index().moves

    @property
    def moves_index(self) -> "PositionIndex":
        return self.build_index().moves

    @property
    def claim_index(self) -> "ClaimIndex":
        return self.build_index().claims

    @property
    def draw_index(self) -> "ClaimIndex":
        return self.build_index().claims

    @classmethod
    def from_board(cls: Type[GameT], board: chess.Board) -> GameT:
        """Creates a game from the move stack of a :class:`~chess.Board()`."""
        # Setup the initial position.
        game = cls()
        game.setup(board.root())
        node: GameNode = game

        # Replay all moves.
        for move in board.move_stack:
            node = node.add_variation(move)

        game.headers["Result"] = board.result()
        return game

    @classmethod
    def without_tag_roster(cls: Type[GameT]) -> GameT:
        """Creates an empty game without the default Seven Tag Roster."""
        return cls(headers={})

    @classmethod
    def builder(cls: Type[GameT]) -> GameBuilder[GameT]:
        return GameBuilder(Game=cls)

    def __repr__(self) -> str:
        return "<{} at {:#x} ({!r} vs. {!r}, {!r} at {!r}{})>".format(
            type(self).__name__,
            id(self),
            self.headers.get("White", "?"),
            self.headers.get("Black", "?"),
            self.headers.get("Date", "????.??.??"),
            self.headers.get("Site", "?"),
            f", {len(self.errors)} errors" if self.errors else "")


HeadersT = TypeVar("HeadersT", bound="Headers")

class Headers(MutableMapping[str, str]):
    def __init__(self, data: Optional[Union[Mapping[str, str], Iterable[Tuple[str, str]]]] = None, **kwargs: str) -> None:
        self._tag_roster: Dict[str, str] = {}
        self._others: Dict[str, str] = {}

        if data is None:
            data = {
                "Event": "?",
                "Site": "?",
                "Date": "????.??.??",
                "Round": "?",
                "White": "?",
                "Black": "?",
                "Result": "*"
            }

        self.update(data, **kwargs)

    def is_chess960(self) -> bool:
        return self.get("Variant", "").lower() in [
            "chess960",
            "chess 960",
            "fischerandom",  # Cute Chess
            "fischerrandom",
            "fischer random",
        ]

    def is_wild(self) -> bool:
        # http://www.freechess.org/Help/HelpFiles/wild.html
        return self.get("Variant", "").lower() in [
            "wild/0", "wild/1", "wild/2", "wild/3", "wild/4", "wild/5",
            "wild/6", "wild/7", "wild/8", "wild/8a"]

    def variant(self) -> Type[chess.Board]:
        if "Variant" not in self or self.is_chess960() or self.is_wild():
            return chess.Board
        else:
            from chess.variant import find_variant
            return find_variant(self["Variant"])

    def board(self) -> chess.Board:
        VariantBoard = self.variant()
        fen = self.get("FEN", VariantBoard.starting_fen)
        board = VariantBoard(fen, chess960=self.is_chess960())
        board.chess960 = board.chess960 or board.has_chess960_castling_rights()
        return board

    def __setitem__(self, key: str, value: str) -> None:
        if key in TAG_ROSTER:
            self._tag_roster[key] = value
        elif not TAG_NAME_REGEX.match(key):
            raise ValueError(f"invalid pgn header tag: {key!r}")
        elif "\n" in value or "\r" in value:
            raise ValueError(f"line break in pgn header {key}: {value!r}")
        else:
            self._others[key] = value

    def __getitem__(self, key: str) -> str:
        return self._tag_roster[key] if key in TAG_ROSTER else self._others[key]

    def __delitem__(self, key: str) -> None:
        if key in TAG_ROSTER:
            del self._tag_roster[key]
        else:
            del self._others[key]

    def __iter__(self) -> Iterator[str]:
        for key in TAG_ROSTER:
            if key in self._tag_roster:
                yield key

        yield from self._others

    def __len__(self) -> int:
        return len(self._tag_roster) + len(self._others)

    def copy(self) -> Self:
        return type(self)(self)

    def __copy__(self) -> Self:
        return self.copy()

    def __repr__(self) -> str:
        return "{}({})".format(
            type(self).__name__,
            ", ".join("{}={!r}".format(key, value) for key, value in self.items()))

    @classmethod
    def builder(cls: Type[HeadersT]) -> HeadersBuilder[HeadersT]:
        return HeadersBuilder(Headers=cls)


MainlineMapT = TypeVar("MainlineMapT")

class Mainline(Generic[MainlineMapT]):
    def __init__(self, start: GameNode, f: Callable[[ChildNode], MainlineMapT]) -> None:
        self.start = start
        self.f = f

    def __bool__(self) -> bool:
        return bool(self.start.variations)

    def __iter__(self) -> Iterator[MainlineMapT]:
        node = self.start
        while node.variations:
            node = node.variations[0]
            yield self.f(node)

    def __reversed__(self) -> Iterator[MainlineMapT]:
        node = self.start.end()
        while node.parent and node != self.start:
            yield self.f(typing.cast(ChildNode, node))
            node = node.parent

    def accept(self, visitor: BaseVisitor[ResultT]) -> ResultT:
        node = self.start
        board = self.start.board()
        while node.variations:
            node = node.variations[0]
            node._accept_node(board, visitor)
            board.push(node.move)
        return visitor.result()

    def __str__(self) -> str:
        return self.accept(StringExporter(columns=None))

    def __repr__(self) -> str:
        return f"<Mainline at {id(self):#x} ({self.accept(StringExporter(columns=None, comments=False))})>"


class BaseVisitor(abc.ABC, Generic[ResultT]):
    """
    Base class for visitors.

    Use with :func:`chess.pgn.Game.accept()` or
    :func:`chess.pgn.GameNode.accept()` or :func:`chess.pgn.read_game()`.

    The methods are called in PGN order.
    """

    def begin_game(self) -> Optional[SkipType]:
        """Called at the start of a game."""
        pass

    def begin_headers(self) -> Optional[Headers]:
        """Called before visiting game headers."""
        pass

    def visit_header(self, tagname: str, tagvalue: str) -> None:
        """Called for each game header."""
        pass

    def end_headers(self) -> Optional[SkipType]:
        """Called after visiting game headers."""
        pass

    def begin_parse_san(self, board: chess.Board, san: str) -> Optional[SkipType]:
        """
        When the visitor is used by a parser, this is called at the start of
        each standard algebraic notation detailing a move.
        """
        pass

    def visit_move(self, board: chess.Board, move: chess.Move) -> None:
        """
        Called for each move.

        *board* is the board state before the move. The board state must be
        restored before the traversal continues.
        """
        pass

    def visit_board(self, board: chess.Board) -> None:
        """
        Called for the starting position of the game and after each move.

        The board state must be restored before the traversal continues.
        """
        pass

    def visit_comment(self, comment: list[str]) -> None:
        """Called for each comment."""
        pass

    def visit_nag(self, nag: int) -> None:
        """Called for each NAG."""
        pass

    def begin_variation(self) -> Optional[SkipType]:
        """
        Called at the start of a new variation. It is not called for the
        mainline of the game.
        """
        pass

    def end_variation(self) -> None:
        """Concludes a variation."""
        pass

    def visit_result(self, result: str) -> None:
        """
        Called at the end of a game with the value from the ``Result`` header.
        """
        pass

    def end_game(self) -> None:
        """Called at the end of a game."""
        pass

    @abc.abstractmethod
    def result(self) -> ResultT:
        """Called to get the result of the visitor."""

    def handle_error(self, error: Exception) -> None:
        """Called for encountered errors. Defaults to raising an exception."""
        raise error


class GameBuilder(BaseVisitor[GameT]):
    """
    Creates a game model. Default visitor for :func:`~chess.pgn.read_game()`.
    """

    @typing.overload
    def __init__(self: GameBuilder[Game]) -> None: ...
    @typing.overload
    def __init__(self, *, Game: Type[GameT]) -> None: ...
    def __init__(self, *, Game: Any = Game) -> None:
        self.Game = Game

    @override
    def begin_game(self) -> None:
        self.game: GameT = self.Game()

        self.variation_stack: List[GameNode] = [self.game]
        self.starting_comments: list[str] = []
        self.in_variation = False

    @override
    def begin_headers(self) -> Headers:
        return self.game.headers

    @override
    def visit_header(self, tagname: str, tagvalue: str) -> None:
        self.game.headers[tagname] = tagvalue

    @override
    def visit_nag(self, nag: int) -> None:
        self.variation_stack[-1].nags.add(nag)

    @override
    def begin_variation(self) -> None:
        parent = self.variation_stack[-1].parent
        assert parent is not None, "begin_variation called, but root node on top of stack"
        self.variation_stack.append(parent)
        self.in_variation = False

    @override
    def end_variation(self) -> None:
        self.variation_stack.pop()

    @override
    def visit_result(self, result: str) -> None:
        if self.game.headers.get("Result", "*") == "*":
            self.game.headers["Result"] = result

    @override
    def visit_comment(self, comment: Union[str, list[str]]) -> None:
        comments = _standardize_comments(comment)
        if self.in_variation or (self.variation_stack[-1].parent is None and self.variation_stack[-1].is_end()):
            # Add as a comment for the current node if in the middle of
            # a variation. Add as a comment for the game if the comment
            # starts before any move.
            self.variation_stack[-1].comments.extend(comments)
            self.variation_stack[-1].comments = list(filter(None, self.variation_stack[-1].comments))
        else:
            # Otherwise, it is a starting comment.
            self.starting_comments.extend(comments)
            self.starting_comments = list(filter(None, self.starting_comments))

    @override
    def visit_move(self, board: chess.Board, move: chess.Move) -> None:
        self.variation_stack[-1] = self.variation_stack[-1].add_variation(move)
        self.variation_stack[-1].starting_comments = self.starting_comments
        self.starting_comments = []
        self.in_variation = True

    @override
    def handle_error(self, error: Exception) -> None:
        """
        Populates :data:`chess.pgn.Game.errors` with encountered errors and
        logs them.

        You can silence the log and handle errors yourself after parsing:

        >>> import chess.pgn
        >>> import logging
        >>>
        >>> logging.getLogger("chess.pgn").setLevel(logging.CRITICAL)
        >>>
        >>> pgn = open("data/pgn/kasparov-deep-blue-1997.pgn")
        >>>
        >>> game = chess.pgn.read_game(pgn)
        >>> game.errors  # List of exceptions
        []

        You can also override this method to hook into error handling:

        >>> import chess.pgn
        >>>
        >>> class MyGameBuilder(chess.pgn.GameBuilder):
        >>>     def handle_error(self, error: Exception) -> None:
        >>>         pass  # Ignore error
        >>>
        >>> pgn = open("data/pgn/kasparov-deep-blue-1997.pgn")
        >>>
        >>> game = chess.pgn.read_game(pgn, Visitor=MyGameBuilder)
        """
        LOGGER.error("%s while parsing %r", error, self.game)
        self.game.errors.append(error)

    @override
    def result(self) -> GameT:
        """
        Returns the visited :class:`~chess.pgn.Game()`.
        """
        return self.game


class HeadersBuilder(BaseVisitor[HeadersT]):
    """Collects headers into a dictionary."""

    @typing.overload
    def __init__(self: HeadersBuilder[Headers]) -> None: ...
    @typing.overload
    def __init__(self, *, Headers: Type[HeadersT]) -> None: ...
    def __init__(self, *, Headers: Any = Headers) -> None:
        self.Headers = Headers

    @override
    def begin_headers(self) -> HeadersT:
        self.headers: HeadersT = self.Headers({})
        return self.headers

    @override
    def visit_header(self, tagname: str, tagvalue: str) -> None:
        self.headers[tagname] = tagvalue

    @override
    def end_headers(self) -> SkipType:
        return SKIP

    @override
    def result(self) -> HeadersT:
        return self.headers


class BoardBuilder(BaseVisitor[chess.Board]):
    """
    Returns the final position of the game. The mainline of the game is
    on the move stack.
    """

    @override
    def begin_game(self) -> None:
        self.skip_variation_depth = 0

    @override
    def begin_variation(self) -> SkipType:
        self.skip_variation_depth += 1
        return SKIP

    @override
    def end_variation(self) -> None:
        self.skip_variation_depth = max(self.skip_variation_depth - 1, 0)

    @override
    def visit_board(self, board: chess.Board) -> None:
        if not self.skip_variation_depth:
            self.board = board

    @override
    def result(self) -> chess.Board:
        return self.board


class SkipVisitor(BaseVisitor[Literal[True]]):
    """Skips a game."""

    @override
    def begin_game(self) -> SkipType:
        return SKIP

    @override
    def end_headers(self) -> SkipType:
        return SKIP

    @override
    def begin_variation(self) -> SkipType:
        return SKIP

    @override
    def result(self) -> Literal[True]:
        return True


class StringExporterMixin:
    def __init__(self, *, columns: Optional[int] = 80, headers: bool = True, comments: bool = True, variations: bool = True):
        self.columns = columns
        self.headers = headers
        self.comments = comments
        self.variations = variations

        self.found_headers = False

        self.force_movenumber = True

        self.lines: List[str] = []
        self.current_line = ""
        self.variation_depth = 0

    def flush_current_line(self) -> None:
        if self.current_line:
            self.lines.append(self.current_line.rstrip())
        self.current_line = ""

    def write_token(self, token: str) -> None:
        if self.columns is not None and self.columns - len(self.current_line) < len(token):
            self.flush_current_line()
        self.current_line += token

    def write_line(self, line: str = "") -> None:
        self.flush_current_line()
        self.lines.append(line.rstrip())

    def end_game(self) -> None:
        self.write_line()

    def begin_headers(self) -> None:
        self.found_headers = False

    def visit_header(self, tagname: str, tagvalue: str) -> None:
        if self.headers:
            self.found_headers = True
            self.write_line(f"[{tagname} \"{tagvalue}\"]")

    def end_headers(self) -> None:
        if self.found_headers:
            self.write_line()

    def begin_variation(self) -> Optional[SkipType]:
        self.variation_depth += 1

        if self.variations:
            self.write_token("( ")
            self.force_movenumber = True
            return None
        else:
            return SKIP

    def end_variation(self) -> None:
        self.variation_depth -= 1

        if self.variations:
            self.write_token(") ")
            self.force_movenumber = True

    def visit_comment(self, comment: Union[str, list[str]]) -> None:
        if self.comments and (self.variations or not self.variation_depth):
            def pgn_format(comments: list[str]) -> str:
                edit = map(lambda s: s.replace("{", "").replace("}", ""), comments)
                return " ".join(f"{{ {comment} }}" for comment in edit if comment)

            comments = _standardize_comments(comment)
            self.write_token(pgn_format(comments) + " ")
            self.force_movenumber = True

    def visit_nag(self, nag: int) -> None:
        if self.comments and (self.variations or not self.variation_depth):
            self.write_token("$" + str(nag) + " ")

    def visit_move(self, board: chess.Board, move: chess.Move) -> None:
        if self.variations or not self.variation_depth:
            # Write the move number.
            if board.turn == chess.WHITE:
                self.write_token(str(board.fullmove_number) + ". ")
            elif self.force_movenumber:
                self.write_token(str(board.fullmove_number) + "... ")

            # Write the SAN.
            self.write_token(board.san(move) + " ")

            self.force_movenumber = False

    def visit_result(self, result: str) -> None:
        self.write_token(result + " ")


class StringExporter(StringExporterMixin, BaseVisitor[str]):
    """
    Allows exporting a game as a string.

    >>> import chess.pgn
    >>>
    >>> game = chess.pgn.Game()
    >>>
    >>> exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    >>> pgn_string = game.accept(exporter)

    Only *columns* characters are written per line. If *columns* is ``None``,
    then the entire movetext will be on a single line. This does not affect
    header tags and comments.

    There will be no newline characters at the end of the string.
    """

    @override
    def result(self) -> str:
        if self.current_line:
            return "\n".join(itertools.chain(self.lines, [self.current_line.rstrip()])).rstrip()
        else:
            return "\n".join(self.lines).rstrip()

    def __str__(self) -> str:
        return self.result()


class FileExporter(StringExporterMixin, BaseVisitor[int]):
    """
    Acts like a :class:`~chess.pgn.StringExporter`, but games are written
    directly into a text file.

    There will always be a blank line after each game. Handling encodings is up
    to the caller.

    >>> import chess.pgn
    >>>
    >>> game = chess.pgn.Game()
    >>>
    >>> new_pgn = open("/dev/null", "w", encoding="utf-8")
    >>> exporter = chess.pgn.FileExporter(new_pgn)
    >>> game.accept(exporter)
    """

    def __init__(self, handle: TextIO, *, columns: Optional[int] = 80, headers: bool = True, comments: bool = True, variations: bool = True):
        super().__init__(columns=columns, headers=headers, comments=comments, variations=variations)
        self.handle = handle

    @override
    def begin_game(self) -> None:
        self.written: int = 0
        super().begin_game()

    def flush_current_line(self) -> None:
        if self.current_line:
            self.written += self.handle.write(self.current_line.rstrip())
            self.written += self.handle.write("\n")
        self.current_line = ""

    def write_line(self, line: str = "") -> None:
        self.flush_current_line()
        self.written += self.handle.write(line.rstrip())
        self.written += self.handle.write("\n")

    @override
    def result(self) -> int:
        return self.written

    def __repr__(self) -> str:
        return f"<FileExporter at {id(self):#x}>"

    def __str__(self) -> str:
        return self.__repr__()


@typing.overload
def read_game(handle: TextIO) -> Optional[Game]: ...
@typing.overload
def read_game(handle: TextIO, *, Visitor: Callable[[], BaseVisitor[ResultT]]) -> Optional[ResultT]: ...
def read_game(handle: TextIO, *, Visitor: Any = GameBuilder) -> Any:
    """
    Reads a game from a file opened in text mode.

    >>> import chess.pgn
    >>>
    >>> pgn = open("data/pgn/kasparov-deep-blue-1997.pgn")
    >>>
    >>> first_game = chess.pgn.read_game(pgn)
    >>> second_game = chess.pgn.read_game(pgn)
    >>>
    >>> first_game.headers["Event"]
    'IBM Man-Machine, New York USA'
    >>>
    >>> # Iterate through all moves and play them on a board.
    >>> board = first_game.board()
    >>> for move in first_game.mainline_moves():
    ...     board.push(move)
    ...
    >>> board
    Board('4r3/6P1/2p2P1k/1p6/pP2p1R1/P1B5/2P2K2/3r4 b - - 0 45')

    By using text mode, the parser does not need to handle encodings. It is the
    caller's responsibility to open the file with the correct encoding.
    PGN files are usually ASCII or UTF-8 encoded, sometimes with BOM (which
    this parser automatically ignores). See :func:`open` for options to
    deal with encoding errors.

    >>> pgn = open("data/pgn/kasparov-deep-blue-1997.pgn", encoding="utf-8")

    Use :class:`~io.StringIO` to parse games from a string.

    >>> import io
    >>>
    >>> pgn = io.StringIO("1. e4 e5 2. Nf3 *")
    >>> game = chess.pgn.read_game(pgn)

    The end of a game is determined by a completely blank line or the end of
    the file. (Of course, blank lines in comments are possible).

    According to the PGN standard, at least the usual seven header tags are
    required for a valid game. This parser also handles games without any
    headers just fine.

    The parser is relatively forgiving when it comes to errors. It skips over
    tokens it can not parse. By default, any exceptions are logged and
    collected in :data:`Game.errors <chess.pgn.Game.errors>`. This behavior can
    be :func:`overridden <chess.pgn.GameBuilder.handle_error>`.

    Returns the parsed game or ``None`` if the end of file is reached.
    """
    visitor = Visitor()

    found_game = False
    skipping_game = False
    managed_headers: Optional[Headers] = None
    unmanaged_headers: Optional[Headers] = None
    board_stack: List[chess.Board] = []

    # Ignore leading empty lines and comments.
    line = handle.readline().lstrip("\ufeff")
    while line.isspace() or line.startswith("%") or line.startswith(";"):
        line = handle.readline()

    # Parse game headers.
    consecutive_empty_lines = 0
    while line:
        # Ignore comments.
        if line.startswith("%") or line.startswith(";"):
            line = handle.readline()
            continue

        # Ignore up to one consecutive empty line between headers.
        if consecutive_empty_lines < 1 and line.isspace():
            consecutive_empty_lines += 1
            line = handle.readline()
            continue

        # First token of the game.
        if not found_game:
            found_game = True
            skipping_game = visitor.begin_game() is SKIP
            if not skipping_game:
                managed_headers = visitor.begin_headers()
                if not isinstance(managed_headers, Headers):
                    unmanaged_headers = Headers({})

        stripped = line.lstrip()
        if not stripped.startswith("["):
            break

        consecutive_empty_lines = 0

        if not skipping_game:
            tag_match = TAG_REGEX.match(stripped)
            if tag_match:
                visitor.visit_header(tag_match.group(1), tag_match.group(2))
                if unmanaged_headers is not None:
                    unmanaged_headers[tag_match.group(1)] = tag_match.group(2)
            else:
                # Ignore invalid or malformed headers.
                line = handle.readline()
                continue

        line = handle.readline()

    if not found_game:
        return None

    if not skipping_game:
        skipping_game = visitor.end_headers() is SKIP

    if not skipping_game:
        # Chess variant.
        headers = managed_headers if unmanaged_headers is None else unmanaged_headers
        assert headers is not None, "got neither managed nor unmanaged headers"
        try:
            VariantBoard = headers.variant()
        except ValueError as error:
            visitor.handle_error(error)
            VariantBoard = chess.Board

        # Initial position.
        fen = headers.get("FEN", VariantBoard.starting_fen)
        try:
            board = VariantBoard(fen, chess960=headers.is_chess960())
        except ValueError as error:
            visitor.handle_error(error)
            skipping_game = True
        else:
            board.chess960 = board.chess960 or board.has_chess960_castling_rights()
            board_stack = [board]
            visitor.visit_board(board)

    # Fast path: Skip entire game.
    if skipping_game:
        in_comment = False

        while line:
            if not in_comment:
                if line.isspace():
                    break
                elif line.startswith("%"):
                    line = handle.readline()
                    continue

            for match in SKIP_MOVETEXT_REGEX.finditer(line):
                token = match.group(0)
                if token == "{":
                    in_comment = True
                elif not in_comment and token == ";":
                    break
                elif token == "}":
                    in_comment = False

            line = handle.readline()

        visitor.end_game()
        return visitor.result()

    # Parse movetext.
    skip_variation_depth = 0
    fresh_line = True
    while line:
        if fresh_line:
            # Ignore comments.
            if line.startswith("%") or line.startswith(";"):
                line = handle.readline()
                continue
            # An empty line means the end of a game.
            if line.isspace():
                visitor.end_game()
                return visitor.result()
        fresh_line = True

        for match in MOVETEXT_REGEX.finditer(line):
            token = match.group(0)

            if token.startswith("{"):
                # Consume until the end of the comment.
                start_index = 2 if token.startswith("{ ") else 1
                line = token[start_index:]

                comment_lines = []
                while line and "}" not in line:
                    comment_lines.append(line)
                    line = handle.readline()

                if line:
                    close_index = line.find("}")
                    end_index = close_index - 1 if close_index > 0 and line[close_index - 1] == " " else close_index
                    comment_lines.append(line[:end_index])
                    line = line[close_index + 1:]

                if not skip_variation_depth:
                    visitor.visit_comment("".join(comment_lines))

                # Continue with the current line.
                fresh_line = False
                break
            elif token == "(":
                if skip_variation_depth:
                    skip_variation_depth += 1
                elif board_stack[-1].move_stack:
                    if visitor.begin_variation() is SKIP:
                        skip_variation_depth = 1
                    else:
                        board = board_stack[-1].copy()
                        board.pop()
                        board_stack.append(board)
            elif token == ")":
                if skip_variation_depth == 1:
                    skip_variation_depth = 0
                    visitor.end_variation()
                elif skip_variation_depth:
                    skip_variation_depth -= 1
                elif len(board_stack) > 1:
                    visitor.end_variation()
                    board_stack.pop()
            elif skip_variation_depth:
                continue
            elif token.startswith(";"):
                break
            elif token.startswith("$"):
                # Found a NAG.
                visitor.visit_nag(int(token[1:]))
            elif token == "?":
                visitor.visit_nag(NAG_MISTAKE)
            elif token == "??":
                visitor.visit_nag(NAG_BLUNDER)
            elif token == "!":
                visitor.visit_nag(NAG_GOOD_MOVE)
            elif token == "!!":
                visitor.visit_nag(NAG_BRILLIANT_MOVE)
            elif token == "!?":
                visitor.visit_nag(NAG_SPECULATIVE_MOVE)
            elif token == "?!":
                visitor.visit_nag(NAG_DUBIOUS_MOVE)
            elif token in ["1-0", "0-1", "1/2-1/2", "*"] and len(board_stack) == 1:
                visitor.visit_result(token)
            else:
                # Parse SAN tokens.
                if visitor.begin_parse_san(board_stack[-1], token) is not SKIP:
                    try:
                        move = board_stack[-1].parse_san(token)
                    except ValueError as error:
                        visitor.handle_error(error)
                        skip_variation_depth = 1
                    else:
                        visitor.visit_move(board_stack[-1], move)
                        board_stack[-1].push(move)
                visitor.visit_board(board_stack[-1])

        if fresh_line:
            line = handle.readline()

    visitor.end_game()
    return visitor.result()


def read_headers(handle: TextIO) -> Optional[Headers]:
    """
    Reads game headers from a PGN file opened in text mode. Skips the rest of
    the game.

    Since actually parsing many games from a big file is relatively expensive,
    this is a better way to look only for specific games and then seek and
    parse them later.

    This example scans for the first game with Kasparov as the white player.

    >>> import chess.pgn
    >>>
    >>> pgn = open("data/pgn/kasparov-deep-blue-1997.pgn")
    >>>
    >>> kasparov_offsets = []
    >>>
    >>> while True:
    ...     offset = pgn.tell()
    ...
    ...     headers = chess.pgn.read_headers(pgn)
    ...     if headers is None:
    ...         break
    ...
    ...     if "Kasparov" in headers.get("White", "?"):
    ...         kasparov_offsets.append(offset)

    Then it can later be seeked and parsed.

    >>> for offset in kasparov_offsets:
    ...     pgn.seek(offset)
    ...     chess.pgn.read_game(pgn)  # doctest: +ELLIPSIS
    0
    <Game at ... ('Garry Kasparov' vs. 'Deep Blue (Computer)', 1997.??.??)>
    1436
    <Game at ... ('Garry Kasparov' vs. 'Deep Blue (Computer)', 1997.??.??)>
    3067
    <Game at ... ('Garry Kasparov' vs. 'Deep Blue (Computer)', 1997.??.??)>
    """
    return read_game(handle, Visitor=HeadersBuilder)


def skip_game(handle: TextIO) -> bool:
    """
    Skips a game. Returns ``True`` if a game was found and skipped.
    """
    return bool(read_game(handle, Visitor=SkipVisitor))


def parse_time_control(time_control: str) -> TimeControl:
    tc = TimeControl()

    if not time_control:
        return tc

    if time_control.startswith("?"):
        return tc

    if time_control.startswith("-"):
        tc.type = TimeControlType.UNLIMITED
        return tc

    def _parse_part(part: str) -> TimeControlPart:
        tcp = TimeControlPart()

        moves_time, *bonus = part.split("+")

        if bonus:
            _bonus = bonus[0]
            if _bonus.lower().endswith("d"):
                tcp.delay = float(_bonus[:-1])
            else:
                tcp.increment = float(_bonus)

        moves, *time = moves_time.split("/")
        if time:
            tcp.moves = int(moves)
            tcp.time = int(time[0])
        else:
            tcp.moves = 0
            tcp.time = int(moves)

        return tcp

    tc.parts = [_parse_part(part) for part in time_control.split(":")]

    if len(tc.parts) > 1:
        for part in tc.parts[:-1]:
            if part.moves == 0:
                raise ValueError("Only last part can be 'sudden death'.")

    # Classification according to https://www.fide.com/FIDE/handbook/LawsOfChess.pdf
    # (Bullet added)
    base_time = tc.parts[0].time
    increment = tc.parts[0].increment
    if (base_time + 60 * increment) < 3 * 60:
        tc.type = TimeControlType.BULLET
    elif (base_time + 60 * increment) < 15 * 60:
        tc.type = TimeControlType.BLITZ
    elif (base_time + 60 * increment) < 60 * 60:
        tc.type = TimeControlType.RAPID
    else:
        tc.type = TimeControlType.STANDARD

    return tc


# Position and draw-claim indexing
# --------------------------------
#
# An opening database often merges game-tree nodes just because the pieces are
# arranged in the same way. That is not enough: castling rights, an en passant
# square and the history of a game can all change what is available or can be
# claimed. The following two indexes deliberately keep those aspects apart.
#
# * The move index uses the set of legal moves as its key. Two nodes with the
#   same piece placement but different castling or en passant possibilities do
#   not match, because their legal move sets differ.
# * The claim index keeps the repetition identity of a position together with
#   its claim status under a configurable set of draw rules. Two lines may
#   transpose to the same piece placement (or even the same position in the
#   repetition sense) and still have different claim statuses.


class ClaimStatus(str, enum.Enum):
    """The result of assessing a draw claim.

    Values compare equal to their lowercase string form (for example,
    ``ClaimStatus.UNKNOWN == "unknown"``).
    """

    def __str__(self) -> str:
        return self.value

    NOT_CLAIMABLE = "not_claimable"
    """The draw cannot be claimed under the given rules."""

    CLAIMABLE = "claimable"
    """The draw can be proven to be claimable."""

    UNKNOWN = "unknown"
    """
    The available history is insufficient to decide whether the draw can be
    claimed. This happens, for example, when a game starts from a FEN with a
    non-zero half-move clock and the moves before that FEN are unavailable.
    """

    # More explicit aliases for readability at call sites.
    PROVEN_NOT_CLAIMABLE = "not_claimable"
    PROVEN_CLAIMABLE = "claimable"
    UNDECIDABLE = "unknown"
    CANNOT_DETERMINE = "unknown"
    PROVEN = "claimable"
    NOT_PROVEN = "not_claimable"
    INDETERMINATE = "unknown"


DrawClaim = ClaimStatus
DrawClaimResult = ClaimStatus
ClaimState = ClaimStatus

CLAIMABLE = ClaimStatus.CLAIMABLE
NOT_CLAIMABLE = ClaimStatus.NOT_CLAIMABLE
UNKNOWN = ClaimStatus.UNKNOWN
PROVEN = ClaimStatus.CLAIMABLE
UNDECIDABLE = ClaimStatus.UNKNOWN


def _coerce_claim_status(status: Any) -> Optional[ClaimStatus]:
    if status is None:
        return None
    return status if isinstance(status, ClaimStatus) else ClaimStatus(status)


@dataclasses.dataclass(frozen=True, init=False)
class DrawRules:
    """
    Configures which draw claims are considered.

    :param threefold_repetition: Consider repetition claims.
    :param fifty_moves: Consider fifty-move claims.
    :param repetition_count: Number of occurrences needed for a repetition
        claim (3 by default). The claim may also be made when the next legal
        move reaches this number.
    :param halfmove_limit: Number of half-moves without a pawn move or capture
        needed for a fifty-move claim (100 by default). As with the standard
        rule, a claim may also be made when the next legal move reaches it.
    :param claim_by_next_move: Whether a claim may be made when the next legal
        move would fulfil the condition.
    """

    threefold_repetition: bool
    fifty_moves: bool
    repetition_count: int
    halfmove_limit: int
    claim_by_next_move: bool

    def __init__(
            self,
            threefold_repetition: bool = True,
            fifty_moves: bool = True,
            repetition_count: int = 3,
            halfmove_limit: int = 100,
            claim_by_next_move: bool = True,
            **aliases: Any) -> None:
        # Accept a few natural spellings without making the public signature
        # unwieldy. Unknown keyword arguments are rejected by the final checks.
        if "threefold" in aliases:
            threefold_repetition = aliases.pop("threefold")
        if "repetition" in aliases:
            threefold_repetition = aliases.pop("repetition")
        if "fifty" in aliases:
            fifty_moves = aliases.pop("fifty")
        if "fifty_move_rule" in aliases:
            fifty_moves = aliases.pop("fifty_move_rule")
        if "claim_by_advance" in aliases:
            claim_by_next_move = aliases.pop("claim_by_advance")
        if aliases:
            raise TypeError(f"unexpected draw rule arguments: {', '.join(sorted(aliases))}")

        if repetition_count < 1:
            raise ValueError("repetition_count must be positive")
        if halfmove_limit < 1:
            raise ValueError("halfmove_limit must be positive")

        object.__setattr__(self, "threefold_repetition", bool(threefold_repetition))
        object.__setattr__(self, "fifty_moves", bool(fifty_moves))
        object.__setattr__(self, "repetition_count", int(repetition_count))
        object.__setattr__(self, "halfmove_limit", int(halfmove_limit))
        object.__setattr__(self, "claim_by_next_move", bool(claim_by_next_move))

    @property
    def threefold(self) -> bool:
        return self.threefold_repetition

    @property
    def repetition(self) -> bool:
        return self.threefold_repetition

    @property
    def fifty(self) -> bool:
        return self.fifty_moves

    @property
    def fifty_move_rule(self) -> bool:
        return self.fifty_moves


ClaimRules = DrawRules
RepetitionRules = DrawRules
DrawClaimRules = DrawRules


DEFAULT_DRAW_RULES = DrawRules()
DrawClaimStatus = ClaimStatus


@dataclasses.dataclass(frozen=True)
class ClaimAssessment:
    """The assessment of all claim rules for one position."""

    status: ClaimStatus
    repetition_status: ClaimStatus
    fifty_move_status: ClaimStatus
    history_known: bool
    """Whether the entire relevant repetition history is available."""

    repetitions: int
    """Known occurrences of the position in the current reversible segment."""

    halfmove_clock: int

    @property
    def proven(self) -> bool:
        return self.status is ClaimStatus.CLAIMABLE

    @property
    def proven_claimable(self) -> bool:
        return self.status is ClaimStatus.CLAIMABLE

    @property
    def unknown(self) -> bool:
        return self.status is ClaimStatus.UNKNOWN

    @property
    def determinable(self) -> bool:
        return self.status is not ClaimStatus.UNKNOWN

    @property
    def can_claim(self) -> bool:
        return self.status is ClaimStatus.CLAIMABLE


ClaimResult = ClaimAssessment
ClaimEvaluation = ClaimAssessment
ClaimInfo = ClaimAssessment


@dataclasses.dataclass(eq=False)
class IndexEntry:
    """A position stored in an index."""

    node: Optional[GameNode]
    """The game tree node, or ``None`` for a standalone board."""

    board: chess.Board
    """A snapshot of the position, including the known move history."""

    position_key: Hashable
    """The repetition identity of the position."""

    move_key: Hashable
    """The legal-move identity of the position."""

    claim: ClaimAssessment
    """The draw-claim assessment under the rules used while indexing."""

    game: Optional[Game]
    """The game this position belongs to, if any."""

    @property
    def claim_status(self) -> ClaimStatus:
        return self.claim.status

    @property
    def status(self) -> ClaimStatus:
        return self.claim.status

    @property
    def ply(self) -> int:
        return self.board.ply()

    @property
    def move_stack(self) -> Tuple[chess.Move, ...]:
        return tuple(self.board.move_stack)

    @property
    def claimable(self) -> bool:
        return self.claim.status is ClaimStatus.CLAIMABLE

    @property
    def can_claim(self) -> bool:
        return self.claim.status is ClaimStatus.CLAIMABLE

    @property
    def proven_claimable(self) -> bool:
        return self.claim.status is ClaimStatus.CLAIMABLE

    @property
    def not_claimable(self) -> bool:
        return self.claim.status is ClaimStatus.NOT_CLAIMABLE

    @property
    def unknown(self) -> bool:
        return self.claim.status is ClaimStatus.UNKNOWN

    @property
    def history_known(self) -> bool:
        return self.claim.history_known

    @property
    def repetitions(self) -> int:
        return self.claim.repetitions

    @property
    def can_claim_threefold_repetition(self) -> bool:
        return self.claim.repetition_status is ClaimStatus.CLAIMABLE

    @property
    def can_claim_fifty_moves(self) -> bool:
        return self.claim.fifty_move_status is ClaimStatus.CLAIMABLE

    @property
    def repetition_status(self) -> ClaimStatus:
        return self.claim.repetition_status

    @property
    def fifty_move_status(self) -> ClaimStatus:
        return self.claim.fifty_move_status

    @property
    def fen(self) -> str:
        return self.board.fen()

    @property
    def board_fen(self) -> str:
        return self.board.board_fen()

    @property
    def turn(self) -> chess.Color:
        return self.board.turn

    @property
    def halfmove_clock(self) -> int:
        return self.board.halfmove_clock

    @property
    def legal_moves(self) -> chess.LegalMoveGenerator:
        return self.board.legal_moves

    def has_castling_rights(self, color: chess.Color) -> bool:
        return self.board.has_castling_rights(color)

    @property
    def can_castle_kingside(self) -> bool:
        return self.board.has_kingside_castling_rights(self.board.turn)

    @property
    def can_castle_queenside(self) -> bool:
        return self.board.has_queenside_castling_rights(self.board.turn)

    def __repr__(self) -> str:
        return "<{} at {:#x} ({} {})>".format(
            type(self).__name__, id(self), self.board.fen(), self.claim.status.value)


PositionEntry = IndexEntry
IndexedPosition = IndexEntry
IndexRecord = IndexEntry
MoveIndexEntry = IndexEntry
ClaimIndexEntry = IndexEntry


def _coerce_draw_rules(rules: Any) -> DrawRules:
    if rules is None:
        return DEFAULT_DRAW_RULES
    if isinstance(rules, DrawRules):
        return rules
    if isinstance(rules, Mapping):
        return DrawRules(**rules)
    return DrawRules(
        threefold_repetition=getattr(rules, "threefold_repetition", getattr(rules, "threefold", True)),
        fifty_moves=getattr(rules, "fifty_moves", getattr(rules, "fifty", True)),
        repetition_count=getattr(rules, "repetition_count", 3),
        halfmove_limit=getattr(rules, "halfmove_limit", 100),
        claim_by_next_move=getattr(rules, "claim_by_next_move", True))


def _extract_rules(rules: Any, kwargs: Dict[str, Any]) -> DrawRules:
    for name in ("draw_rules", "claim_rules", "repetition_rules", "claim_rule_set"):
        if name in kwargs:
            supplied = kwargs.pop(name)
            if rules is not None:
                raise TypeError(f"got both rules and {name}")
            rules = supplied
    if kwargs:
        raise TypeError(f"unexpected keyword arguments: {', '.join(sorted(kwargs))}")
    return _coerce_draw_rules(rules)


def _extract_history_known(history_known: Optional[bool], kwargs: Dict[str, Any]) -> Optional[bool]:
    for name in ("complete_history", "history_complete", "known_history"):
        if name in kwargs:
            supplied = kwargs.pop(name)
            if history_known is not None:
                raise TypeError(f"got both history_known and {name}")
            history_known = supplied
    if kwargs:
        raise TypeError(f"unexpected keyword arguments: {', '.join(sorted(kwargs))}")
    return history_known


def placement_key(board: chess.Board) -> Hashable:
    """
    Returns a key based only on the piece placement and side to move.

    This is the identity a naive opening database might use. It intentionally
    ignores castling rights, en passant and move history, which is why the
    move and claim layers in this module do not use it for lookups.
    """
    return (type(board).__module__, type(board).__qualname__, board.board_fen(), board.turn)


piece_placement_key = placement_key
placement_only_key = placement_key
board_fen_key = placement_key


def position_key(board: chess.Board) -> Hashable:
    """
    Returns the repetition identity of a position.

    Unlike :func:`move_key`, this includes castling rights and a legal en
    passant square, but not the half-move clock or move number.
    """
    return (type(board).__module__, type(board).__qualname__, board._transposition_key())


transposition_key = position_key
repetition_key = position_key
board_position_key = position_key


def move_key(board: chess.Board) -> Hashable:
    """
    Returns the legal-move identity of a position.

    The key contains the piece placement, side to move and the complete set of
    currently legal moves. Two positions with the same placement but different
    castling or en passant possibilities therefore have different keys.
    """
    moves = tuple(sorted(move.uci() for move in board.generate_legal_moves()))
    return (
        type(board).__module__,
        type(board).__qualname__,
        board.board_fen(),
        board.turn,
        moves,
    )


legal_move_key = move_key
moves_key = move_key
position_move_key = move_key
legal_moves_key = move_key
available_moves_key = move_key


def _move_uci(move: Union[chess.Move, str]) -> str:
    return move if isinstance(move, str) else move.uci()


def moves_key_from_moves(moves: Iterable[Union[chess.Move, str]]) -> Tuple[str, ...]:
    """Returns the canonical legal-move part used by :func:`move_key`."""
    return tuple(sorted(_move_uci(move) for move in moves))


def _known_repetition_count(board: chess.Board, key: Optional[Hashable] = None) -> int:
    if key is None:
        key = board._transposition_key()

    count = 1
    replayed = board.copy(stack=True)
    while replayed.move_stack:
        move = replayed.pop()
        if replayed.is_irreversible(move):
            break
        if replayed._transposition_key() == key:
            count += 1
    return count


def _history_known_for_repetition(board: chess.Board) -> bool:
    """
    Checks whether all positions that can still be relevant for repetition
    are on the move stack.

    Starting from a FEN with a zero half-move clock is safe: the last move
    before the FEN was irreversible, so earlier positions cannot repeat any
    position from the current reversible segment. After any irreversible move
    the same is true again.
    """
    root = board.root()
    if root.halfmove_clock == 0:
        return True

    replayed = root.copy(stack=False)
    for move in board.move_stack:
        if replayed.is_irreversible(move):
            return True
        replayed.push(move)
    return False


def _claim_by_next_move_repetition(board: chess.Board, key: Hashable, count: int) -> bool:
    for move in board.generate_legal_moves():
        board.push(move)
        try:
            if _known_repetition_count(board, board._transposition_key()) >= count:
                return True
        finally:
            board.pop()
    return False


def _repetition_claimable(board: chess.Board, rules: DrawRules) -> bool:
    key = board._transposition_key()
    count = _known_repetition_count(board, key)
    if count >= rules.repetition_count:
        return True
    if rules.claim_by_next_move:
        return _claim_by_next_move_repetition(board, key, rules.repetition_count)
    return False


def _fifty_move_claimable(board: chess.Board, rules: DrawRules) -> bool:
    if board.halfmove_clock >= rules.halfmove_limit:
        return any(board.generate_legal_moves())

    if rules.claim_by_next_move and board.halfmove_clock >= rules.halfmove_limit - 1:
        for move in board.generate_legal_moves():
            if board.is_zeroing(move):
                continue
            board.push(move)
            try:
                if board.halfmove_clock >= rules.halfmove_limit and any(board.generate_legal_moves()):
                    return True
            finally:
                board.pop()
    return False


def _make_claim_assessment(board: chess.Board, rules: DrawRules, history_known: bool, repetitions: int, repetition_claimable: bool) -> ClaimAssessment:
    if not rules.threefold_repetition:
        repetition_status = ClaimStatus.NOT_CLAIMABLE
    elif repetition_claimable:
        repetition_status = ClaimStatus.CLAIMABLE
    elif history_known:
        repetition_status = ClaimStatus.NOT_CLAIMABLE
    else:
        repetition_status = ClaimStatus.UNKNOWN

    fifty_claimable = _fifty_move_claimable(board, rules)
    if not rules.fifty_moves:
        fifty_status = ClaimStatus.NOT_CLAIMABLE
    else:
        fifty_status = ClaimStatus.CLAIMABLE if fifty_claimable else ClaimStatus.NOT_CLAIMABLE

    active = []
    if rules.threefold_repetition:
        active.append(repetition_status)
    if rules.fifty_moves:
        active.append(fifty_status)

    if ClaimStatus.CLAIMABLE in active:
        status = ClaimStatus.CLAIMABLE
    elif ClaimStatus.UNKNOWN in active:
        status = ClaimStatus.UNKNOWN
    else:
        status = ClaimStatus.NOT_CLAIMABLE

    return ClaimAssessment(
        status=status,
        repetition_status=repetition_status,
        fifty_move_status=fifty_status,
        history_known=history_known,
        repetitions=repetitions,
        halfmove_clock=board.halfmove_clock)


def assess_claim(board: chess.Board, *, rules: Any = None, history_known: Optional[bool] = None, **kwargs: Any) -> ClaimAssessment:
    """
    Assesses repetition and fifty-move claims for *board*.

    If *history_known* is omitted, it is inferred from the board's move stack
    and root half-move clock. Games parsed from a FEN with a non-zero
    half-move clock and without any later irreversible move therefore report
    :data:`ClaimStatus.UNKNOWN` when a repetition cannot be proven locally,
    instead of incorrectly claiming that no repetition can exist.
    """
    kwargs = dict(kwargs)
    draw_rules = _extract_rules(rules, kwargs)
    history_known = _extract_history_known(history_known, kwargs)
    if kwargs:
        raise TypeError(f"unexpected keyword arguments: {', '.join(sorted(kwargs))}")
    if history_known is None:
        history_known = _history_known_for_repetition(board)

    key = board._transposition_key()
    repetitions = _known_repetition_count(board, key)
    return _make_claim_assessment(
        board, draw_rules, history_known, repetitions,
        _repetition_claimable(board, draw_rules))


def claim_status(board: chess.Board, *, rules: Any = None, history_known: Optional[bool] = None, **kwargs: Any) -> ClaimStatus:
    """Returns just the combined claim status. See :func:`assess_claim`."""
    return assess_claim(board, rules=rules, history_known=history_known, **kwargs).status


def can_prove_claim(board: chess.Board, *, rules: Any = None, **kwargs: Any) -> bool:
    """Checks whether a claim is proven to be possible."""
    return assess_claim(board, rules=rules, **kwargs).status is ClaimStatus.CLAIMABLE


def is_claim_unknown(board: chess.Board, *, rules: Any = None, **kwargs: Any) -> bool:
    """Checks whether the claim status cannot be determined."""
    return assess_claim(board, rules=rules, **kwargs).status is ClaimStatus.UNKNOWN


def claimable_or_none(board: chess.Board, *, rules: Any = None, **kwargs: Any) -> Optional[bool]:
    """
    Returns ``True`` if a draw claim is proven, ``False`` if it is proven
    impossible, and ``None`` if the available history is insufficient.
    """
    status = assess_claim(board, rules=rules, **kwargs).status
    if status is ClaimStatus.CLAIMABLE:
        return True
    if status is ClaimStatus.NOT_CLAIMABLE:
        return False
    return None


evaluate_claim = assess_claim
get_claim_status = claim_status
claim_status_for = claim_status


def _iter_source_games(source: Any) -> Iterator[Game]:
    # Already-parsed game.
    if isinstance(source, Game):
        yield source
        return

    # Text stream.
    if hasattr(source, "read"):
        yield from _read_all_games(source)
        return

    # Path.
    if isinstance(source, (str, bytes, os.PathLike)):
        with open(source, encoding="utf-8") as handle:
            yield from _read_all_games(handle)
        return

    # Iterable of games.
    yield from source


def _assess_claim_from_counts(board: chess.Board, counts: Dict[Hashable, int], history_known: bool, rules: DrawRules) -> ClaimAssessment:
    """
    Like :func:`assess_claim`, but uses an incrementally maintained count of
    position keys in the current reversible segment.
    """
    key = board._transposition_key()
    repetitions = counts.get(key, 0)

    repetition_claimable = repetitions >= rules.repetition_count
    if not repetition_claimable and rules.claim_by_next_move:
        for move in board.generate_legal_moves():
            irreversible = board.is_irreversible(move)
            board.push(move)
            try:
                child_key = board._transposition_key()
            finally:
                board.pop()

            child_count = 1 if irreversible else counts.get(child_key, 0) + 1
            if child_count >= rules.repetition_count:
                repetition_claimable = True
                break

    return _make_claim_assessment(board, rules, history_known, repetitions, repetition_claimable)


def _iter_index_nodes(game: Game, rules: DrawRules) -> Iterator[Tuple[GameNode, chess.Board, ClaimAssessment]]:
    """
    Iterates nodes with an incrementally maintained claim assessment.

    The board is only valid until the next iteration step; callers that retain
    it must copy it.
    """
    board = game.board()
    counts: Dict[Hashable, int] = {board._transposition_key(): 1}
    history_known = board.halfmove_clock == 0

    yield game, board, _assess_claim_from_counts(board, counts, history_known, rules)

    # Frame: node, variations, saved counts (set after an irreversible move),
    # saved history flag, child key and whether it was counted.
    stack: List[Tuple[GameNode, Iterator[ChildNode], Optional[Dict[Hashable, int]], bool, Optional[Hashable], bool]] = [
        (game, iter(game.variations), None, True, None, False)]

    while stack:
        node, variations, saved_counts, saved_known, child_key, incremented = stack[-1]
        try:
            child = next(variations)
        except StopIteration:
            stack.pop()
            if stack:
                board.pop()
                if saved_counts is not None:
                    counts = saved_counts
                    history_known = saved_known
                elif incremented and child_key is not None:
                    remaining = counts.get(child_key, 0) - 1
                    if remaining > 0:
                        counts[child_key] = remaining
                    else:
                        counts.pop(child_key, None)
            continue

        irreversible = board.is_irreversible(child.move)
        parent_counts = counts
        parent_known = history_known
        board.push(child.move)

        if irreversible:
            counts = {board._transposition_key(): 1}
            history_known = True
            counts_saved_for_child: Optional[Dict[Hashable, int]] = parent_counts
            known_saved_for_child = parent_known
            child_key = None
            incremented = False
        else:
            counts_saved_for_child = None
            known_saved_for_child = True
            child_key = board._transposition_key()
            counts[child_key] = counts.get(child_key, 0) + 1
            incremented = True

        yield child, board, _assess_claim_from_counts(board, counts, history_known, rules)

        stack.append((child, iter(child.variations), counts_saved_for_child, known_saved_for_child, child_key, incremented))


class PositionIndex:
    """
    Indexes PGN nodes by their currently legal moves.

    This is the first of the two layers. It answers queries such as "which
    nodes offer exactly these moves?" and intentionally does not conflate
    positions whose piece placement is identical but whose castling or en
    passant possibilities differ.
    """

    def __init__(self, games: Any = None, *, rules: Any = None, **kwargs: Any) -> None:
        self.rules = _extract_rules(rules, kwargs)
        self._entries: List[IndexEntry] = []
        self._by_move_key: Dict[Hashable, List[IndexEntry]] = {}
        self._by_position_key: Dict[Hashable, List[IndexEntry]] = {}

        if games is not None:
            for game in _iter_source_games(games):
                self.add_game(game)

    def add_game(self, game: Union[Game, chess.Board]) -> List[IndexEntry]:
        """
        Adds every node of *game*, including all variations.

        For convenience, a single board is added as a standalone entry.
        """
        if not isinstance(game, Game):
            return [self.add(game)]  # type: ignore[list-item]

        return [self._add_node(node, game, board, claim)
                for node, board, claim in _iter_index_nodes(game, self.rules)]

    def _add_node(self, node: GameNode, game: Game, board: chess.Board, claim: Optional[ClaimAssessment] = None) -> IndexEntry:
        entry = IndexEntry(
            node=node,
            board=board.copy(stack=True),
            position_key=position_key(board),
            move_key=move_key(board),
            claim=claim if claim is not None else assess_claim(board, rules=self.rules),
            game=game)
        self._entries.append(entry)
        self._by_move_key.setdefault(entry.move_key, []).append(entry)
        self._by_position_key.setdefault(entry.position_key, []).append(entry)
        return entry

    def add(self, value: Union[Game, GameNode, chess.Board]) -> Union[IndexEntry, List[IndexEntry]]:
        """Adds a game, node or standalone board."""
        if isinstance(value, Game):
            return self.add_game(value)  # type: ignore[return-value]
        elif isinstance(value, GameNode):
            return self._add_node(value, value.game(), value.board())
        elif isinstance(value, chess.Board):
            entry = IndexEntry(
                node=None,
                board=value.copy(stack=True),
                position_key=position_key(value),
                move_key=move_key(value),
                claim=assess_claim(value, rules=self.rules),
                game=None)
            self._entries.append(entry)
            self._by_move_key.setdefault(entry.move_key, []).append(entry)
            self._by_position_key.setdefault(entry.position_key, []).append(entry)
            return entry
        else:
            raise TypeError(f"cannot index {type(value).__name__}")

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[IndexEntry]:
        return iter(self._entries)

    def __contains__(self, board: Union[chess.Board, Hashable]) -> bool:
        if isinstance(board, chess.Board):
            return move_key(board) in self._by_move_key
        return board in self._by_move_key

    def __getitem__(self, key: Union[chess.Board, Hashable]) -> List[IndexEntry]:
        if isinstance(key, chess.Board):
            return self.find(key)
        return self._by_move_key[key]

    def find(self, board: chess.Board) -> List[IndexEntry]:
        """Finds entries whose current legal moves match *board*."""
        return list(self._by_move_key.get(move_key(board), []))

    lookup = find
    find_all = find
    find_by_board = find
    find_by_legal_moves = find

    def get(self, board: chess.Board, default: Optional[List[IndexEntry]] = None) -> Optional[List[IndexEntry]]:
        return self._by_move_key.get(move_key(board), default)

    def find_with_move(self, board: chess.Board, move: chess.Move) -> List[IndexEntry]:
        """Finds entries matching *board* whose legal moves also include *move*."""
        return [entry for entry in self.find(board) if move in entry.board.legal_moves]

    find_by_move = find_with_move
    find_with_move_on_board = find_with_move
    lookup_move = find_with_move

    def has_move(self, board: chess.Board, move: chess.Move) -> bool:
        return bool(self.find_with_move(board, move))

    def legal_moves(self, board: Optional[chess.Board] = None, *, key: Optional[Hashable] = None) -> Tuple[str, ...]:
        """
        Returns the indexed legal moves for *board*, or for a move key.

        The result is the canonical tuple used internally, so it can be passed
        back to :meth:`find_by_moves`.
        """
        if key is None:
            if board is None:
                raise TypeError("either board or key is required")
            key = move_key(board)
        return typing.cast(Tuple[str, ...], key[-1])

    moves_for = legal_moves
    available_moves = legal_moves

    def find_move(self, move: chess.Move) -> List[IndexEntry]:
        """Finds entries where *move* is currently legal."""
        uci = move.uci()
        return [entry for entry in self._entries if uci in entry.move_key[-1]]

    def find_containing_move(self, move: chess.Move) -> List[IndexEntry]:
        return self.find_move(move)

    def find_positions_with_move(self, move: chess.Move) -> List[IndexEntry]:
        return self.find_move(move)

    def find_by_moves(self, moves: Iterable[Union[chess.Move, str]]) -> List[IndexEntry]:
        """Finds entries by the complete set of legal moves (without a board)."""
        key_moves = moves_key_from_moves(moves)
        result = []
        for entry in self._entries:
            if entry.move_key[-1] == key_moves:
                result.append(entry)
        return result

    find_moves = find_by_moves
    find_by_move_set = find_by_moves

    def find_position(self, board: chess.Board) -> List[IndexEntry]:
        """Finds entries with the same repetition identity as *board*."""
        return list(self._by_position_key.get(position_key(board), []))

    def find_by_placement(self, board: chess.Board) -> List[IndexEntry]:
        """
        Finds entries by piece placement and side to move only.

        This deliberately exposes the coarse identity a naive opening database
        might use, including entries that the move layer keeps separate due to
        castling rights or en passant.
        """
        key = placement_key(board)
        return [entry for entry in self._entries if placement_key(entry.board) == key]

    find_placement = find_by_placement
    find_by_board_fen = find_by_placement

    def find_by_fen(self, fen: str) -> List[IndexEntry]:
        return self.find(chess.Board(fen))

    query_fen = find_by_fen

    def keys(self) -> Iterable[Hashable]:
        return self._by_move_key.keys()

    @property
    def entries(self) -> List[IndexEntry]:
        return list(self._entries)

    @classmethod
    def from_pgn(cls, handle: TextIO, *, rules: Any = None, **kwargs: Any) -> "PositionIndex":
        index = cls(rules=rules, **kwargs)
        index.add_games_from_pgn(handle)
        return index

    @classmethod
    def from_games(cls, games: Iterable[Game], *, rules: Any = None, **kwargs: Any) -> "PositionIndex":
        return cls(games, rules=rules, **kwargs)

    def add_games_from_pgn(self, handle: TextIO) -> None:
        while True:
            game = read_game(handle)
            if game is None:
                break
            self.add_game(game)


MoveIndex = PositionIndex
OpeningMoveIndex = PositionIndex
LegalMoveIndex = PositionIndex
MoveSetIndex = PositionIndex
TranspositionIndex = PositionIndex
PositionMoveIndex = PositionIndex
AvailableMovesIndex = PositionIndex
CurrentMovesIndex = PositionIndex


class ClaimIndex:
    """
    Indexes PGN nodes by repetition identity and draw-claim status.

    This is the second layer. Same-looking positions with different histories
    stay distinguishable: each occurrence retains its own
    :class:`ClaimAssessment`. A position started from a FEN with missing
    reversible history can be reported as :data:`ClaimStatus.UNKNOWN` rather
    than being confused with a proven non-claim or a proven claim.
    """

    def __init__(self, games: Any = None, *, rules: Any = None, **kwargs: Any) -> None:
        self.rules = _extract_rules(rules, kwargs)
        self._entries: List[IndexEntry] = []
        self._by_position_key: Dict[Hashable, List[IndexEntry]] = {}
        self._by_claim_key: Dict[Tuple[Hashable, ClaimStatus], List[IndexEntry]] = {}

        if games is not None:
            for game in _iter_source_games(games):
                self.add_game(game)

    def add_game(self, game: Union[Game, chess.Board]) -> List[IndexEntry]:
        """
        Adds every node of *game*, including all variations.

        For convenience, a single board is added as a standalone entry.
        """
        if not isinstance(game, Game):
            return [self.add(game)]  # type: ignore[list-item]

        return [self._add_node(node, game, board, claim)
                for node, board, claim in _iter_index_nodes(game, self.rules)]

    def _add_node(self, node: GameNode, game: Game, board: chess.Board, claim: Optional[ClaimAssessment] = None) -> IndexEntry:
        entry = IndexEntry(
            node=node,
            board=board.copy(stack=True),
            position_key=position_key(board),
            move_key=move_key(board),
            claim=claim if claim is not None else assess_claim(board, rules=self.rules),
            game=game)
        self._entries.append(entry)
        self._by_position_key.setdefault(entry.position_key, []).append(entry)
        self._by_claim_key.setdefault((entry.position_key, entry.claim.status), []).append(entry)
        return entry

    def add(self, value: Union[Game, GameNode, chess.Board], *, history_known: Optional[bool] = None) -> Union[IndexEntry, List[IndexEntry]]:
        if isinstance(value, Game):
            return self.add_game(value)  # type: ignore[return-value]
        elif isinstance(value, GameNode):
            board = value.board()
            return self._add_node(value, value.game(), board)
        elif isinstance(value, chess.Board):
            entry = IndexEntry(
                node=None,
                board=value.copy(stack=True),
                position_key=position_key(value),
                move_key=move_key(value),
                claim=assess_claim(value, rules=self.rules, history_known=history_known),
                game=None)
            self._entries.append(entry)
            self._by_position_key.setdefault(entry.position_key, []).append(entry)
            self._by_claim_key.setdefault((entry.position_key, entry.claim.status), []).append(entry)
            return entry
        else:
            raise TypeError(f"cannot index {type(value).__name__}")

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[IndexEntry]:
        return iter(self._entries)

    def __contains__(self, board: chess.Board) -> bool:
        return position_key(board) in self._by_position_key

    def __getitem__(self, key: Any) -> List[IndexEntry]:
        if isinstance(key, chess.Board):
            return self.find_all(key)
        if isinstance(key, tuple) and len(key) == 2:
            wanted = _coerce_claim_status(key[1])
            if wanted is not None:
                return list(self._by_claim_key.get((key[0], wanted), []))
        return self.find_all(key)

    def find_all(self, board: chess.Board) -> List[IndexEntry]:
        """Finds all occurrences with the same repetition identity."""
        return list(self._by_position_key.get(position_key(board), []))

    find_position = find_all
    find_by_position = find_all
    lookup = find_all
    find_all_claims = find_all

    def repetitions(self, board: chess.Board) -> int:
        """Returns the number of known occurrences of the position."""
        return _known_repetition_count(board)

    def repetition_claimable(self, board: chess.Board) -> bool:
        return _repetition_claimable(board, self.rules)

    def fifty_move_claimable(self, board: chess.Board) -> bool:
        return _fifty_move_claimable(board, self.rules)

    def find(self, board: chess.Board, *, status: Optional[ClaimStatus] = None, rules: Any = None) -> List[IndexEntry]:
        """
        Finds occurrences of the same position, optionally restricted to a
        claim status.

        If *rules* is given, assessments are recomputed with those rules
        instead of the rules the index was built with.
        """
        entries = self.find_all(board)
        wanted = _coerce_claim_status(status)
        if wanted is None and rules is None:
            return entries

        draw_rules = self.rules if rules is None else _coerce_draw_rules(rules)
        result = []
        for entry in entries:
            assessment = entry.claim
            if rules is not None:
                assessment = assess_claim(entry.board, rules=draw_rules, history_known=entry.claim.history_known)
            if wanted is None or assessment.status is wanted:
                result.append(entry)
        return result

    def aggregate_status(self, board: chess.Board, *, rules: Any = None) -> ClaimStatus:
        """
        Aggregates the statuses of all occurrences of *board*.

        A proven claim on any line wins over an undecidable line, which in
        turn wins over a proven non-claim. This answers whether *some* line
        reaching this position is known to allow the claim.
        """
        statuses = self.statuses(board, rules=rules)
        if ClaimStatus.CLAIMABLE in statuses:
            return ClaimStatus.CLAIMABLE
        if ClaimStatus.UNKNOWN in statuses:
            return ClaimStatus.UNKNOWN
        return ClaimStatus.NOT_CLAIMABLE

    summary_status = aggregate_status
    best_status = aggregate_status

    def find_by_repetitions(self, board: chess.Board, count: int) -> List[IndexEntry]:
        """Finds occurrences of *board* whose known repetition count is *count*."""
        return [entry for entry in self.find_all(board) if entry.claim.repetitions == count]

    def find_at_least_repetitions(self, board: chess.Board, count: int) -> List[IndexEntry]:
        return [entry for entry in self.find_all(board) if entry.claim.repetitions >= count]

    def find_halfmove_clock(self, board: chess.Board, *, at_least: int = 0, at_most: Optional[int] = None) -> List[IndexEntry]:
        result = []
        for entry in self.find_all(board):
            clock = entry.claim.halfmove_clock
            if clock >= at_least and (at_most is None or clock <= at_most):
                result.append(entry)
        return result

    find_by_halfmove_clock = find_halfmove_clock

    def find_claimable(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.find(board, status=ClaimStatus.CLAIMABLE, rules=rules)

    def find_unknown(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.find(board, status=ClaimStatus.UNKNOWN, rules=rules)

    def find_not_claimable(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.find(board, status=ClaimStatus.NOT_CLAIMABLE, rules=rules)

    def statuses(self, board: chess.Board, *, rules: Any = None) -> Set[ClaimStatus]:
        if rules is None:
            return {entry.claim.status for entry in self.find_all(board)}

        draw_rules = _coerce_draw_rules(rules)
        return {
            assess_claim(entry.board, rules=draw_rules, history_known=entry.claim.history_known).status
            for entry in self.find_all(board)}

    def status(self, board: chess.Board, *, rules: Any = None, history_known: Optional[bool] = None) -> ClaimStatus:
        return assess_claim(board, rules=self.rules if rules is None else rules, history_known=history_known).status

    def iter_status(self, status: Any, *, rules: Any = None) -> Iterator[IndexEntry]:
        wanted = _coerce_claim_status(status)
        if wanted is None:
            raise TypeError("status is required")

        if rules is None:
            for entry in self._entries:
                if entry.claim.status is wanted:
                    yield entry
            return

        draw_rules = _coerce_draw_rules(rules)
        for entry in self._entries:
            assessment = assess_claim(entry.board, rules=draw_rules, history_known=entry.claim.history_known)
            if assessment.status is wanted:
                yield entry

    def find_all_by_status(self, status: Any, *, rules: Any = None) -> List[IndexEntry]:
        return list(self.iter_status(status, rules=rules))

    def find_all_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.find_all_by_status(ClaimStatus.CLAIMABLE, rules=rules)

    def find_all_unknown(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.find_all_by_status(ClaimStatus.UNKNOWN, rules=rules)

    def find_all_not_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.find_all_by_status(ClaimStatus.NOT_CLAIMABLE, rules=rules)

    def find_fifty_moves_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        draw_rules = self.rules if rules is None else _coerce_draw_rules(rules)
        return [entry for entry in self._entries
                if entry.claim.fifty_move_status is ClaimStatus.CLAIMABLE
                or (rules is not None and _fifty_move_claimable(entry.board, draw_rules))]

    def find_repetition_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        draw_rules = self.rules if rules is None else _coerce_draw_rules(rules)
        return [entry for entry in self._entries
                if entry.claim.repetition_status is ClaimStatus.CLAIMABLE
                or (rules is not None and _repetition_claimable(entry.board, draw_rules))]

    def find_by_fen(self, fen: str) -> List[IndexEntry]:
        return self.find_all(chess.Board(fen))

    def keys(self) -> Iterable[Tuple[Hashable, ClaimStatus]]:
        return self._by_claim_key.keys()

    @property
    def entries(self) -> List[IndexEntry]:
        return list(self._entries)

    @classmethod
    def from_pgn(cls, handle: TextIO, *, rules: Any = None, **kwargs: Any) -> "ClaimIndex":
        index = cls(rules=rules, **kwargs)
        index.add_games_from_pgn(handle)
        return index

    @classmethod
    def from_games(cls, games: Iterable[Game], *, rules: Any = None, **kwargs: Any) -> "ClaimIndex":
        return cls(games, rules=rules, **kwargs)

    def add_games_from_pgn(self, handle: TextIO) -> None:
        while True:
            game = read_game(handle)
            if game is None:
                break
            self.add_game(game)


DrawIndex = ClaimIndex
RepetitionIndex = ClaimIndex
ClaimabilityIndex = ClaimIndex
FiftyMoveIndex = ClaimIndex
ThreefoldIndex = ClaimIndex
DrawClaimIndex = ClaimIndex
RepetitionClaimIndex = ClaimIndex
DrawClaimabilityIndex = ClaimIndex


class PGNIndex:
    """
    A two-layer index for imported PGN games.

    * :attr:`moves` (also available as :attr:`by_moves`) answers queries by
      the set of currently legal moves.
    * :attr:`claims` (also available as :attr:`by_claim`) answers queries by
      repetition/fifty-move claim eligibility.

    The two layers serve different purposes. Two variations can transpose to
    the same piece placement and meet in the move index while having different
    histories and therefore different claim statuses. Conversely, the claim
    index keeps positions with different move sets apart because castling
    rights and en passant are part of the repetition identity.
    """

    def __init__(self, games: Any = None, *, rules: Any = None, **kwargs: Any) -> None:
        self.rules = _extract_rules(rules, kwargs)
        self.moves = PositionIndex(rules=self.rules)
        self.claims = ClaimIndex(rules=self.rules)
        if games is not None:
            for game in _iter_source_games(games):
                self.add_game(game)

    @property
    def positions(self) -> PositionIndex:
        return self.moves

    @property
    def by_moves(self) -> PositionIndex:
        return self.moves

    @property
    def by_claim(self) -> ClaimIndex:
        return self.claims

    @property
    def draw_claims(self) -> ClaimIndex:
        return self.claims

    @property
    def position_index(self) -> PositionIndex:
        return self.moves

    @property
    def claim_index(self) -> ClaimIndex:
        return self.claims

    @property
    def move_layer(self) -> PositionIndex:
        return self.moves

    @property
    def claim_layer(self) -> ClaimIndex:
        return self.claims

    def add_game(self, game: Game) -> List[IndexEntry]:
        self.moves.add_game(game)
        return self.claims.add_game(game)

    def add(self, value: Union[Game, GameNode, chess.Board]) -> Union[IndexEntry, List[IndexEntry]]:
        if isinstance(value, Game):
            return self.add_game(value)
        elif isinstance(value, GameNode):
            self.moves.add(value)
            return self.claims.add(value)
        elif isinstance(value, chess.Board):
            self.moves.add(value)
            return self.claims.add(value)
        else:
            raise TypeError(f"cannot index {type(value).__name__}")

    def find(self, board: chess.Board) -> List[IndexEntry]:
        """Finds by current legal moves (the first layer)."""
        return self.moves.find(board)

    lookup = find

    def find_by_moves(self, moves: Iterable[Union[chess.Move, str]]) -> List[IndexEntry]:
        return self.moves.find_by_moves(moves)

    find_moves = find_by_moves

    def find_claim(self, board: chess.Board, *, status: Optional[ClaimStatus] = None, rules: Any = None) -> List[IndexEntry]:
        """Finds by repetition/fifty-move claim status (the second layer)."""
        return self.claims.find(board, status=status, rules=rules)

    def find_with_move(self, board: chess.Board, move: chess.Move) -> List[IndexEntry]:
        return self.moves.find_with_move(board, move)

    def has_move(self, board: chess.Board, move: chess.Move) -> bool:
        return self.moves.has_move(board, move)

    def find_move(self, move: chess.Move) -> List[IndexEntry]:
        return self.moves.find_move(move)

    def legal_moves(self, board: chess.Board) -> Tuple[str, ...]:
        return self.moves.legal_moves(board)

    def claim_status(self, board: chess.Board, *, status: Optional[ClaimStatus] = None, rules: Any = None) -> Optional[ClaimStatus]:
        wanted = _coerce_claim_status(status)
        if wanted is not None:
            return wanted
        entries = self.claims.find_all(board)
        if entries:
            return self.claims.aggregate_status(board, rules=rules)
        return assess_claim(board, rules=self.rules if rules is None else rules).status

    def find_claimable(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_claimable(board, rules=rules)

    def find_position(self, board: chess.Board) -> List[IndexEntry]:
        """Finds all occurrences with the same repetition identity."""
        return self.claims.find_all(board)

    find_by_position = find_position
    find_all_positions = find_position

    def find_by_repetitions(self, board: chess.Board, count: int) -> List[IndexEntry]:
        return self.claims.find_by_repetitions(board, count)

    def find_at_least_repetitions(self, board: chess.Board, count: int) -> List[IndexEntry]:
        return self.claims.find_at_least_repetitions(board, count)

    def statuses(self, board: chess.Board, *, rules: Any = None) -> Set[ClaimStatus]:
        return self.claims.statuses(board, rules=rules)

    def aggregate_status(self, board: chess.Board, *, rules: Any = None) -> ClaimStatus:
        return self.claims.aggregate_status(board, rules=rules)

    def query(self, board: chess.Board) -> List[IndexEntry]:
        """Alias for the first (move) layer."""
        return self.find(board)

    query_moves = query
    query_by_moves = query
    query_position = query
    query_claim = find_claim
    query_by_claim = find_claim

    def move_entries(self) -> List[IndexEntry]:
        return self.moves.entries

    def claim_entries(self) -> List[IndexEntry]:
        return self.claims.entries

    def find_not_claimable(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_not_claimable(board, rules=rules)

    def add_games_from_pgn(self, handle: TextIO) -> None:
        for game in _read_all_games(handle):
            self.add_game(game)

    def find_unknown(self, board: chess.Board, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_unknown(board, rules=rules)

    def find_by_placement(self, board: chess.Board) -> List[IndexEntry]:
        return self.moves.find_by_placement(board)

    def find_by_fen(self, fen: str) -> List[IndexEntry]:
        return self.moves.find_by_fen(fen)

    query_fen = find_by_fen

    def find_all_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_all_claimable(rules=rules)

    def find_all_unknown(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_all_unknown(rules=rules)

    def find_fifty_moves_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_fifty_moves_claimable(rules=rules)

    def find_repetition_claimable(self, *, rules: Any = None) -> List[IndexEntry]:
        return self.claims.find_repetition_claimable(rules=rules)

    def assess(self, board: chess.Board, *, history_known: Optional[bool] = None) -> ClaimAssessment:
        return assess_claim(board, rules=self.rules, history_known=history_known)

    def __len__(self) -> int:
        return len(self.moves)

    def __iter__(self) -> Iterator[IndexEntry]:
        return iter(self.moves)

    def __getitem__(self, board: chess.Board) -> List[IndexEntry]:
        return self.moves[board]

    def __contains__(self, board: chess.Board) -> bool:
        return board in self.moves

    @property
    def entries(self) -> List[IndexEntry]:
        return self.moves.entries

    @classmethod
    def from_pgn(cls, handle: TextIO, *, rules: Any = None, **kwargs: Any) -> "PGNIndex":
        return cls(_read_all_games(handle), rules=rules, **kwargs)

    @classmethod
    def from_games(cls, games: Iterable[Game], *, rules: Any = None, **kwargs: Any) -> "PGNIndex":
        return cls(games, rules=rules, **kwargs)

    @classmethod
    def from_file(cls, path: Any, *, rules: Any = None, **kwargs: Any) -> "PGNIndex":
        return cls(path, rules=rules, **kwargs)

    from_path = from_file

    @classmethod
    def from_game(cls, game: Game, *, rules: Any = None, **kwargs: Any) -> "PGNIndex":
        return cls([game], rules=rules, **kwargs)


OpeningIndex = PGNIndex
GameIndex = PGNIndex
OpeningBookIndex = PGNIndex
TwoLevelIndex = PGNIndex
TwoTierIndex = PGNIndex
PGNDatabaseIndex = PGNIndex
PGNDatabase = PGNIndex
OpeningDatabase = PGNIndex
OpeningDatabaseIndex = PGNIndex
GameDatabase = PGNIndex
Index = PGNIndex


def _read_all_games(handle: TextIO) -> Iterator[Game]:
    while True:
        game = read_game(handle)
        if game is None:
            break
        yield game


def build_index(source: Any, *, rules: Any = None, **kwargs: Any) -> PGNIndex:
    """
    Builds a two-layer :class:`PGNIndex` from a PGN text stream, an iterable
    of games, or a single game.
    """
    draw_rules = _extract_rules(rules, kwargs)
    return PGNIndex(_iter_source_games(source), rules=draw_rules)


build_pgn_index = build_index
index_pgn = build_index
read_pgn_index = build_index
import_pgn = build_index
index_game = build_index
build_game_index = build_index
create_index = build_index
create_pgn_index = build_index


def build_position_index(source: Any, *, rules: Any = None) -> PositionIndex:
    return build_index(source, rules=rules).moves


def build_claim_index(source: Any, *, rules: Any = None) -> ClaimIndex:
    return build_index(source, rules=rules).claims


def index_games(games: Iterable[Game], *, rules: Any = None) -> PGNIndex:
    return PGNIndex.from_games(games, rules=rules)


def index_pgn_games(handle: TextIO, *, rules: Any = None) -> PGNIndex:
    return PGNIndex.from_pgn(handle, rules=rules)
