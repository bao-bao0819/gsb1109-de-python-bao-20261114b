from __future__ import annotations

import abc
import dataclasses
import enum
import itertools
import logging
import re
import typing

import chess
import chess.engine
import chess.svg

from typing import (
    Any, Callable, Dict, Generic, Iterable, Iterator, List, Literal, Mapping,
    MutableMapping, Optional, Sequence, Set, TextIO, Tuple, Type, TypeVar, Union,
)
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


@dataclasses.dataclass(frozen=True, init=False)
class BoardPhoto:
    """
    A sparse transcription of a board position, for example from a photo where
    only a few squares are legible.

    Only explicitly mentioned squares are constrained. Omitted squares are
    unknown. A square mapped to ``None`` (or ``"."``) is known to be empty.
    Piece values may be :class:`chess.Piece` objects or piece symbols like
    ``"N"`` (white knight) and ``"q"`` (black queen).

    >>> import chess
    >>> import chess.pgn
    >>>
    >>> photo = chess.pgn.BoardPhoto({"e4": "P", "e5": None, "g1": "N"})
    >>> photo[chess.E4]
    Piece.from_symbol('P')
    >>> photo[chess.E5] is None
    True
    >>> len(photo)
    3
    """

    observations: Tuple[Tuple[chess.Square, Optional[chess.Piece]], ...]
    """Sorted ``(square, piece)`` pairs. ``piece`` is ``None`` if the square is known to be empty."""

    def __init__(
            self,
            observations: Union[
                Mapping[Union[int, str], Union[str, chess.Piece, None]],
                Iterable[Tuple[Union[int, str], Union[str, chess.Piece, None]]],
            ]) -> None:
        if hasattr(observations, "items"):
            items = typing.cast(Mapping[Union[int, str], Union[str, chess.Piece, None]], observations).items()
        else:
            items = observations

        normalized: Dict[chess.Square, Optional[chess.Piece]] = {}

        for key, value in items:
            try:
                square = chess.parse_square(key) if isinstance(key, str) else int(key)
            except (ValueError, TypeError) as error:
                raise ValueError(f"invalid square: {key!r}") from error

            if not 0 <= square < 64:
                raise ValueError(f"invalid square: {key!r}")

            if square in normalized:
                raise ValueError(f"duplicate square in photo: {chess.square_name(square)}")

            normalized[square] = _parse_photo_piece(value, square)

        object.__setattr__(self, "observations", tuple(sorted(normalized.items())))

    @classmethod
    def from_symbols(
            cls,
            observations: Union[
                Mapping[Union[int, str], Union[str, chess.Piece, None]],
                Iterable[Tuple[Union[int, str], Union[str, chess.Piece, None]]],
            ]) -> BoardPhoto:
        """Creates a photo from square names and piece symbols or ``"."`` for empty squares."""
        return cls(observations)

    def __len__(self) -> int:
        return len(self.observations)

    def __iter__(self) -> Iterator[Tuple[chess.Square, Optional[chess.Piece]]]:
        return iter(self.observations)

    def __contains__(self, square: object) -> bool:
        return any(known == square for known, _ in self.observations)

    def __getitem__(self, square: Union[int, str]) -> Optional[chess.Piece]:
        square = chess.parse_square(square) if isinstance(square, str) else square
        for known, piece in self.observations:
            if known == square:
                return piece
        raise KeyError(square)

    def squares(self) -> Dict[chess.Square, Optional[chess.Piece]]:
        """Returns the known squares as a dictionary."""
        return dict(self.observations)

    def __repr__(self) -> str:
        return "{}({})".format(type(self).__name__, ", ".join(
            "{}={}".format(chess.square_name(square), "." if piece is None else piece.symbol())
            for square, piece in self.observations))


def _parse_photo_piece(value: Union[str, chess.Piece, None], square: chess.Square) -> Optional[chess.Piece]:
    if value is None:
        return None
    if isinstance(value, chess.Piece):
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid piece at {chess.square_name(square)}: {value!r}")

    symbol = value.strip()
    if symbol in (".", "", "-", "empty"):
        return None
    if len(symbol) != 1 or symbol not in "PNBRQKpnbrqk":
        raise ValueError(f"invalid piece symbol at {chess.square_name(square)}: {value!r}")
    return chess.Piece.from_symbol(symbol)


@dataclasses.dataclass(frozen=True)
class PhotoConflict:
    """A node rejected because a known square contains something else."""

    node: GameNode
    square: chess.Square
    expected: Optional[chess.Piece]
    actual: Optional[chess.Piece]

    @property
    def square_name(self) -> str:
        return chess.square_name(self.square)

    @property
    def expected_symbol(self) -> str:
        return "." if self.expected is None else self.expected.symbol()

    @property
    def actual_symbol(self) -> str:
        return "." if self.actual is None else self.actual.symbol()

    def __repr__(self) -> str:
        return "<{} at {:#x} {} expected {}, got {} at {}>".format(
            type(self).__name__, id(self), self.square_name, self.expected_symbol,
            self.actual_symbol, _node_label(self.node))


@dataclasses.dataclass(frozen=True)
class PhotoSquareEvidence:
    """
    How one visible square discriminates between nodes.

    ``matching`` counts all nodes where the square agrees with the photo and
    ``conflicting`` counts all nodes where it does not. ``solely_conflicting``
    counts nodes that agree with every other visible square, so that this
    square alone is responsible for rejecting them. ``examples`` contains up to
    a few such nodes.

    Within the returned candidates, the square supports only those nodes that
    match it. The difference between the two counts, together with
    ``solely_conflicting``, shows whether the square adds information beyond
    the others.
    """

    square: chess.Square
    expected: Optional[chess.Piece]
    matching: int
    conflicting: int
    solely_conflicting: int
    examples: Tuple[PhotoConflict, ...] = ()

    @property
    def square_name(self) -> str:
        return chess.square_name(self.square)

    @property
    def expected_symbol(self) -> str:
        return "." if self.expected is None else self.expected.symbol()

    def __repr__(self) -> str:
        return "<{} {}={} matching={} conflicting={} solely={}>".format(
            type(self).__name__, self.square_name, self.expected_symbol,
            self.matching, self.conflicting, self.solely_conflicting)


@dataclasses.dataclass(frozen=True)
class PhotoCandidate:
    """A game node compatible with a photo."""

    node: GameNode
    ply: int
    board_fen: str
    fen: str
    matched_squares: Tuple[chess.Square, ...]

    @property
    def visual_key(self) -> str:
        """
        What the board looks like, regardless of how it was reached: piece
        placement only, ignoring side to move, castling rights, en passant,
        and move counters.
        """
        return self.board_fen

    def uci_path(self) -> Tuple[str, ...]:
        """Moves from the root of the game to this node."""
        path = []
        node = self.node
        while node.parent is not None:
            path.append(node.uci())
            node = node.parent
        return tuple(reversed(path))

    def __repr__(self) -> str:
        return "<{} at {:#x} ply={} board={}>".format(
            type(self).__name__, id(self), self.ply, self.board_fen)


@dataclasses.dataclass(frozen=True)
class PhotoMatch:
    """
    Result for one photo.

    ``local_candidates`` are all nodes compatible with the visible squares in
    this photo alone. ``candidates`` are those that additionally can be part of
    one path-consistent assignment for the whole sequence. ``path_excluded``
    are the locally compatible nodes removed by the sequence constraint.
    """

    photo: BoardPhoto
    local_candidates: Tuple[PhotoCandidate, ...]
    candidates: Tuple[PhotoCandidate, ...]
    path_excluded: Tuple[PhotoCandidate, ...]
    evidence: Tuple[PhotoSquareEvidence, ...]

    @property
    def complete(self) -> bool:
        return bool(self.candidates)

    def transpositions(self) -> Dict[str, Tuple[PhotoCandidate, ...]]:
        """
        Groups candidates with the same visual position but different game
        paths. Groups with more than one path are returned. Candidates are
        never merged or discarded based on position alone.
        """
        groups: Dict[str, List[PhotoCandidate]] = {}
        for candidate in self.candidates:
            groups.setdefault(candidate.visual_key, []).append(candidate)
        return {
            key: tuple(group)
            for key, group in groups.items()
            if len(group) > 1
        }

    def __repr__(self) -> str:
        return "<{} at {:#x} local={} candidates={} path_excluded={}>".format(
            type(self).__name__, id(self), len(self.local_candidates),
            len(self.candidates), len(self.path_excluded))


@dataclasses.dataclass(frozen=True)
class PhotoIndex:
    """The result of indexing a sequence of photos into a game tree."""

    game: Game
    matches: Tuple[PhotoMatch, ...]
    path_consistent: bool = True
    max_ply_gap: Optional[int] = None

    @property
    def complete(self) -> bool:
        """Whether every photo has at least one candidate."""
        return all(match.complete for match in self.matches)

    def consistent_paths(self) -> Iterator[Tuple[PhotoCandidate, ...]]:
        """
        Iterates over assignments of one candidate per photo such that all
        candidates lie on a single variation path, in shooting order. Respects
        the same ply gap limit as :func:`index_photos`.

        If the index was built with ``path_consistent=False``, all combinations
        of candidates are returned instead.
        """
        if not self.matches:
            yield ()
            return

        if not self.path_consistent:
            yield from itertools.product(*(match.candidates for match in self.matches))
            return

        def visit(index: int, path: List[PhotoCandidate]) -> Iterator[Tuple[PhotoCandidate, ...]]:
            if index == len(self.matches):
                yield tuple(path)
                return

            for candidate in self.matches[index].candidates:
                if path and not any(
                        ancestor is path[-1].node
                        for ancestor in _ancestor_nodes(candidate.node, self.max_ply_gap)):
                    continue
                path.append(candidate)
                yield from visit(index + 1, path)
                path.pop()

        yield from visit(0, [])

    def __len__(self) -> int:
        return len(self.matches)

    def __iter__(self) -> Iterator[PhotoMatch]:
        return iter(self.matches)

    def __getitem__(self, index: int) -> PhotoMatch:
        return self.matches[index]

    def report(self) -> str:
        """
        Returns a human readable per-photo report, naming the visible pieces
        that support or reject candidates.

        >>> import chess
        >>> import chess.pgn
        >>>
        >>> game = chess.pgn.Game.from_board(chess.Board())
        >>> print(chess.pgn.index_photos(game, [chess.pgn.BoardPhoto({"e2": "P"})]).report())
        Photo 0: 1 candidate(s), 1 locally compatible
          e2=P: agrees with 1 node(s), rejects 0; solely rejects 0
        """
        lines = []

        for index, match in enumerate(self.matches):
            lines.append("Photo {}: {} candidate(s), {} locally compatible".format(
                index, len(match.candidates), len(match.local_candidates)))

            for evidence in match.evidence:
                line = "  {}={}: agrees with {} node(s), rejects {}; solely rejects {}".format(
                    evidence.square_name, evidence.expected_symbol,
                    evidence.matching, evidence.conflicting, evidence.solely_conflicting)

                if evidence.examples:
                    line += " ({})".format(", ".join(
                        "{} has {}".format(_node_label(conflict.node), conflict.actual_symbol)
                        for conflict in evidence.examples))

                lines.append(line)

            if match.path_excluded:
                examples = ", ".join(
                    _node_label(candidate.node) for candidate in match.path_excluded[:3])
                if len(match.path_excluded) > 3:
                    examples += ", ..."
                lines.append("  sequence narrowed: {} locally compatible node(s) removed ({})".format(
                    len(match.path_excluded), examples))

        if not self.matches:
            lines.append("No photos.")

        return "\n".join(lines)

    def __repr__(self) -> str:
        return "<{} at {:#x} photos={} complete={}>".format(
            type(self).__name__, id(self), len(self.matches), self.complete)


def _node_label(node: GameNode) -> str:
    """A compact label for reports, or ``start`` for the root."""
    if node.parent is None:
        return "start"
    try:
        return "ply {} {}".format(node.ply(), node.san())
    except (ValueError, KeyError):
        return "ply {}".format(node.ply())


def _walk_game_nodes(
        game: Game,
        observations: Sequence[Sequence[Tuple[chess.Square, Optional[chess.Piece]]]],
        max_conflict_examples: int) -> _PhotoScan:
    """
    Traverses the game tree once in pre-order and evaluates every photo
    against the live board at each node.
    """
    scan = _PhotoScan(observations, max_conflict_examples)

    board = game.board()
    start_ply = game.ply()

    stack: List[Tuple[GameNode, Iterator[ChildNode], bool, int]] = [(game, iter(game.variations), False, start_ply)]

    while stack:
        node, variations, entered, ply = stack[-1]

        if not entered:
            scan.visit(node, board, ply)
            stack[-1] = (node, variations, True, ply)
            continue

        try:
            child = next(variations)
        except StopIteration:
            stack.pop()
            if stack:
                board.pop()
        else:
            board.push(child.move)
            stack.append((child, iter(child.variations), False, ply + 1))

    return scan


class _PhotoScan:
    def __init__(self, observations: Sequence[Sequence[Tuple[chess.Square, Optional[chess.Piece]]]], max_conflict_examples: int) -> None:
        self.observations = observations
        self.max_conflict_examples = max_conflict_examples

        size = len(observations)
        self.local_candidates: List[List[PhotoCandidate]] = [[] for _ in range(size)]
        self.matching: List[Dict[chess.Square, int]] = [{} for _ in range(size)]
        self.conflicting: List[Dict[chess.Square, int]] = [{} for _ in range(size)]
        self.solely_conflicting: List[Dict[chess.Square, int]] = [{} for _ in range(size)]
        self.examples: List[Dict[chess.Square, List[PhotoConflict]]] = [{} for _ in range(size)]

        for index, photo_observations in enumerate(observations):
            for square, _ in photo_observations:
                self.matching[index][square] = 0
                self.conflicting[index][square] = 0
                self.solely_conflicting[index][square] = 0
                self.examples[index][square] = []

    def visit(self, node: GameNode, board: chess.Board, ply: int) -> None:
        for photo_index, photo_observations in enumerate(self.observations):
            matches = self.matching[photo_index]
            conflicts = self.conflicting[photo_index]
            sole = self.solely_conflicting[photo_index]
            samples = self.examples[photo_index]

            mismatches: List[Tuple[chess.Square, Optional[chess.Piece], Optional[chess.Piece]]] = []

            for square, expected in photo_observations:
                actual = board.piece_at(square)
                if actual == expected:
                    matches[square] += 1
                else:
                    conflicts[square] += 1
                    mismatches.append((square, expected, actual))

            if not mismatches:
                self.local_candidates[photo_index].append(PhotoCandidate(
                    node=node,
                    ply=ply,
                    board_fen=board.board_fen(),
                    fen=board.fen(),
                    matched_squares=tuple(square for square, _ in photo_observations),
                ))
            elif len(mismatches) == 1:
                square, expected, actual = mismatches[0]
                sole[square] += 1
                if len(samples[square]) < self.max_conflict_examples:
                    samples[square].append(PhotoConflict(node, square, expected, actual))


def _ancestor_nodes(node: GameNode, max_ply_gap: Optional[int]) -> Iterator[GameNode]:
    """
    Yields *node* itself (a gap of zero plies), then its ancestors, up to
    *max_ply_gap* plies away. The node itself allows consecutive photos to
    show the same position.
    """
    steps = 0
    current: Optional[GameNode] = node
    while current is not None:
        if max_ply_gap is not None and steps > max_ply_gap:
            break
        yield current
        current = current.parent
        steps += 1


def index_photos(
        game: Game,
        photos: Iterable[BoardPhoto],
        *,
        path_consistent: bool = True,
        max_ply_gap: Optional[int] = None,
        max_conflict_examples: int = 3) -> PhotoIndex:
    """
    Indexes a sequence of sparse board photos into *game*.

    Photos must be given in shooting order. A photo is represented by
    :class:`BoardPhoto`, which only constrains the squares that are legible.

    By default the photos are additionally required to lie on one variation
    path, in order. Consecutive photos may skip moves, including across
    variation boundaries, as long as one legal root-to-leaf path contains all
    of them. Set *path_consistent* to ``False`` to consider each photo
    independently. *max_ply_gap* optionally limits how many plies apart two
    consecutive photos may be.

    The result is narrowed both forwards and backwards: a candidate for an
    early photo survives only if a later photo can still be reached from it.
    Candidates are game nodes, not positions, so the same position reached by
    different move orders remains visible as distinct candidates.

    >>> import chess
    >>> import chess.pgn
    >>>
    >>> game = chess.pgn.Game.from_board(chess.Board())
    >>> index = chess.pgn.index_photos(game, [chess.pgn.BoardPhoto({"e2": "P"})])
    >>> len(index[0].candidates)
    1

    Complexity is `O(n * p)` for n game nodes and p photos, plus the path
    consistency passes.
    """
    if max_ply_gap is not None and max_ply_gap < 1:
        raise ValueError("max_ply_gap must be at least 1")

    photos = tuple(photos)
    if not photos:
        return PhotoIndex(game, (), path_consistent, max_ply_gap)

    observations = [photo.observations for photo in photos]

    scan = _walk_game_nodes(game, observations, max_conflict_examples)

    local_candidates = scan.local_candidates
    matching = scan.matching
    conflicting = scan.conflicting
    solely_conflicting = scan.solely_conflicting
    examples = scan.examples

    final_candidates: List[List[PhotoCandidate]] = [list(candidates) for candidates in local_candidates]

    if path_consistent and len(photos) > 1:
        forward: List[Set[GameNode]] = [set() for _ in photos]
        backward: List[Set[GameNode]] = [set() for _ in photos]

        forward[0] = {candidate.node for candidate in local_candidates[0]}
        for i in range(1, len(photos)):
            allowed: Set[GameNode] = set()
            previous = forward[i - 1]
            if previous:
                for candidate in local_candidates[i]:
                    if any(ancestor in previous for ancestor in _ancestor_nodes(candidate.node, max_ply_gap)):
                        allowed.add(candidate.node)
            forward[i] = allowed

        backward[-1] = {candidate.node for candidate in local_candidates[-1]}
        for i in range(len(photos) - 2, -1, -1):
            reachable_ancestors: Set[GameNode] = set()
            for candidate in local_candidates[i + 1]:
                if candidate.node in backward[i + 1]:
                    reachable_ancestors.update(_ancestor_nodes(candidate.node, max_ply_gap))
            backward[i] = {
                candidate.node
                for candidate in local_candidates[i]
                if candidate.node in reachable_ancestors
            }

        for i in range(len(photos)):
            final_candidates[i] = [
                candidate
                for candidate in local_candidates[i]
                if candidate.node in forward[i] and candidate.node in backward[i]
            ]

    matches_result: List[PhotoMatch] = []

    for i, photo in enumerate(photos):
        evidence = tuple(
            PhotoSquareEvidence(
                square=square,
                expected=expected,
                matching=matching[i][square],
                conflicting=conflicting[i][square],
                solely_conflicting=solely_conflicting[i][square],
                examples=tuple(examples[i][square]),
            )
            for square, expected in photo.observations
        )

        final_nodes = {candidate.node for candidate in final_candidates[i]}
        path_excluded = tuple(
            candidate for candidate in local_candidates[i]
            if candidate.node not in final_nodes
        )

        matches_result.append(PhotoMatch(
            photo=photo,
            local_candidates=tuple(local_candidates[i]),
            candidates=tuple(final_candidates[i]),
            path_excluded=path_excluded,
            evidence=evidence,
        ))

    return PhotoIndex(game, tuple(matches_result), path_consistent, max_ply_gap)


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
