"""
Compare Polyglot opening book advice with practical move choices.

The report keeps three notions separate:

* A legal move that is not mentioned in the book at all.
* A move that has an explicit book entry with weight ``0`` (the usual way
  of deleting a move from a Polyglot book without rewriting the file).
* A move with a positive book weight.

Only the mainline of each PGN game is observed. Moves from PGN variations
are deliberately ignored: they are analysis, not practical play.

Positions are identified by their Polyglot Zobrist hash, so transpositions
are merged. In ``opportunity`` counting mode every visit to a position is
one observation. In ``game`` counting mode a game contributes one vote for
each position it reaches; if that game continues with more than one distinct
move from the same position (for example after repetitions or transpositions),
that single vote is split equally between the distinct continuations.
"""

from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import json
import sys
import typing

from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, TextIO, Tuple

import chess
import chess.pgn
import chess.polyglot


CountMode = typing.Literal["game", "opportunity"]

CSV_FIELDS = [
    "stratum",
    "position_key",
    "position_fen",
    "move_san",
    "move_uci",
    "book_status",
    "book_weight",
    "book_entries",
    "book_share",
    "observed_votes",
    "observed_share",
    "delta",
    "position_book_total_weight",
    "position_observed_total",
    "position_tv_distance",
]
"""Column order used by :meth:`DeviationReport.to_csv`."""

BOOK_STATUS_PRESENT = "present"
BOOK_STATUS_ZERO = "zero_weight"
BOOK_STATUS_ABSENT = "absent"


@dataclasses.dataclass(frozen=True)
class DeviationRow:
    """One move from a position, as seen by the book and by practical play."""

    stratum: str
    """Value of the configured stratification header (usually ``Event``)."""
    position_key: int
    """Polyglot Zobrist hash identifying the position."""
    position_fen: str
    """A FEN for the position, normalized with respect to move counters."""
    move_san: str
    """SAN of the move in the position."""
    move_uci: str
    """UCI of the move in the position."""
    book_status: str
    """
    ``present`` for a positive book weight, ``zero_weight`` for an explicit
    entry with weight ``0``, or ``absent`` if the move does not occur in the
    book at all.
    """
    book_weight: Optional[int]
    """
    Summed book weight. ``None`` if the move is absent from the book. Note
    that this is deliberately different from ``0`` for an explicit
    zero-weight entry.
    """
    book_entries: int
    """Number of book entries for this move."""
    book_share: Optional[float]
    """
    Share of the position's positive book weight. ``None`` if the move is
    absent, ``0.0`` for an explicit zero-weight entry.
    """
    observed_votes: float
    """Votes according to the selected counting mode."""
    observed_share: float
    """Share of the position's total observed votes."""
    delta: float
    """
    ``observed_share - book_share``. A move absent from the book is compared
    against an implicit zero share, but remains distinguishable through
    :attr:`book_status` and :attr:`book_weight`.
    """
    position_book_total_weight: int
    """Total positive book weight for the position."""
    position_observed_total: float
    """Total observed votes for the position."""
    position_tv_distance: float
    """
    Total variation distance between the book distribution and the observed
    distribution for this position (``0.5 * sum(abs(delta))``). A position
    with no positive book weight has distance ``0.5`` when it was played,
    because the book has no active advice there.
    """

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class DeviationReport:
    """Rows and scanning statistics produced by :func:`analyze_games`."""

    rows: List[DeviationRow] = dataclasses.field(default_factory=list)
    games: int = 0
    skipped_variant_games: int = 0
    skipped_chess960_games: int = 0
    skipped_invalid_games: int = 0
    """Games excluded because the PGN contained errors or an illegal mainline."""
    positions: int = 0
    count_mode: CountMode = "game"
    stratum_headers: Tuple[str, ...] = ("Event",)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count_mode": self.count_mode,
            "stratum_headers": list(self.stratum_headers),
            "games": self.games,
            "skipped_variant_games": self.skipped_variant_games,
            "skipped_chess960_games": self.skipped_chess960_games,
            "skipped_invalid_games": self.skipped_invalid_games,
            "positions": self.positions,
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_csv(self, handle: TextIO) -> None:
        """Writes the rows as CSV, with an empty cell for absent book data."""
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({field: _csv_value(getattr(row, field)) for field in CSV_FIELDS})

    def to_json(self, handle: TextIO) -> None:
        """Writes the report as a JSON object, using ``null`` for absent data."""
        json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


@dataclasses.dataclass
class _Position:
    key: int
    board: chess.Board
    fen: str
    occurrences: int = 0
    opportunity_moves: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    game_moves: Dict[int, Set[str]] = dataclasses.field(default_factory=dict)

    def observed(self, count_mode: CountMode) -> Tuple[Dict[str, float], float]:
        if count_mode == "opportunity":
            return dict(self.opportunity_moves), float(self.occurrences)

        moves: Dict[str, float] = collections.defaultdict(float)
        for continuations in self.game_moves.values():
            if continuations:
                weight = 1.0 / len(continuations)
                for uci in continuations:
                    moves[uci] += weight
        return dict(moves), float(len(self.game_moves))


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return "{:.6f}".format(value)
    return value


def _normalized_fen(board: chess.Board) -> str:
    # Polyglot ignores the move counters and only hashes a legal (or
    # pseudo-legal, depending on the book) en passant square.
    return "{} 0 1".format(board.epd(en_passant="xfen"))


def _stratum(headers: Mapping[str, str], stratum_headers: Sequence[str]) -> str:
    for header in stratum_headers:
        value = headers.get(header, "?")
        if value and value != "?":
            return value
    return "?"


def _book_cache_entry(reader: chess.polyglot.MemoryMappedReader, board: chess.Board) -> Dict[str, Tuple[int, int]]:
    entries: Dict[str, Tuple[int, int]] = collections.defaultdict(lambda: (0, 0))
    for entry in reader.find_all(board, minimum_weight=0):
        uci = entry.move.uci()
        weight, count = entries[uci]
        entries[uci] = (weight + max(entry.weight, 0), count + 1)
    return dict(entries)


def _legal_ucis(board: chess.Board) -> Set[str]:
    return {board.uci(move) for move in board.legal_moves}


def analyze_games(
        games: Iterable[chess.pgn.Game],
        book: chess.polyglot.MemoryMappedReader,
        *,
        count_mode: CountMode = "game",
        stratum_headers: Sequence[str] = ("Event", "Site"),
        include_all_legal: bool = False,
        include_positions_without_book: bool = False) -> DeviationReport:
    """
    Compares mainline PGN moves with a Polyglot book.

    *games* must yield :class:`chess.pgn.Game` objects. *book* must remain
    open for the duration of the call.

    :param count_mode: ``"game"`` counts a position at most once per game and
        splits that game's vote between distinct continuations. ``"opportunity"``
        counts every visit.
    :param stratum_headers: Header names used for stratification, in fallback
        order. Defaults to ``("Event", "Site")``.
    :param include_all_legal: Also emit legal moves that are absent from the
        book and were never played. By default only book entries and observed
        moves are emitted.
    :param include_positions_without_book: Also emit positions that do not
        occur in the book (with every observed move marked ``absent``).
    """
    if count_mode not in ("game", "opportunity"):
        raise ValueError("count_mode must be 'game' or 'opportunity'")

    report = DeviationReport(count_mode=count_mode, stratum_headers=tuple(stratum_headers))
    positions: Dict[Tuple[str, int], _Position] = {}
    book_cache: Dict[int, Dict[str, Tuple[int, int]]] = {}

    game_id = 0
    for game in games:
        report.games += 1

        # A parser error usually means part of the mainline was dropped.
        # Counting the remainder would silently bias the adoption rates.
        if game.errors:
            report.skipped_invalid_games += 1
            continue

        try:
            board = game.board()
        except ValueError:
            report.skipped_invalid_games += 1
            continue

        # Polyglot books use standard chess move and castling encodings.
        if type(board) is not chess.Board:
            report.skipped_variant_games += 1
            continue
        if board.chess960:
            report.skipped_chess960_games += 1
            continue

        stratum = _stratum(game.headers, stratum_headers)
        observations: List[Tuple[int, str]] = []
        first_seen: Dict[int, Tuple[chess.Board, str]] = {}

        try:
            # Deliberately follow variations[0] only. PGN sidelines are
            # analysis, not practical play.
            node = game
            while node.variations:
                node = node.variations[0]
                key = chess.polyglot.zobrist_hash(board)
                if key not in first_seen and (stratum, key) not in positions:
                    first_seen[key] = (board.copy(stack=False), _normalized_fen(board))
                observations.append((key, board.uci(node.move)))
                board.push(node.move)
        except (ValueError, AssertionError):
            # A malformed mainline is not useful for practical statistics,
            # and must not contribute partial observations.
            report.skipped_invalid_games += 1
            continue

        game_id += 1
        for key, uci in observations:
            position = positions.get((stratum, key))
            if position is None:
                board_copy, fen = first_seen[key]
                position = _Position(key=key, board=board_copy, fen=fen)
                positions[(stratum, key)] = position

            position.occurrences += 1
            position.opportunity_moves[uci] += 1
            position.game_moves.setdefault(game_id, set()).add(uci)

    report.positions = len(positions)

    for (stratum, key), position in sorted(positions.items(), key=lambda item: (item[0][0], item[0][1])):
        book_moves = book_cache.get(key)
        if book_moves is None:
            book_moves = _book_cache_entry(book, position.board)
            book_cache[key] = book_moves

        if not book_moves and not include_positions_without_book:
            continue

        observed_moves, observed_total = position.observed(count_mode)

        union = set(book_moves) | set(observed_moves)
        if include_all_legal:
            union.update(_legal_ucis(position.board))

        total_weight = sum(weight for weight, _ in book_moves.values())
        book_shares: Dict[str, float] = {}
        for uci, (weight, count) in book_moves.items():
            if total_weight:
                book_shares[uci] = weight / total_weight
            else:
                # Explicit zero-weight entries get an explicit zero share,
                # unlike moves that are absent from the book.
                book_shares[uci] = 0.0

        observed_shares = {
            uci: votes / observed_total if observed_total else 0.0
            for uci, votes in observed_moves.items()
        }

        tv_distance = 0.5 * sum(
            abs(observed_shares.get(uci, 0.0) - book_shares.get(uci, 0.0))
            for uci in union
        )

        for uci in sorted(union):
            move = chess.Move.from_uci(uci)
            try:
                san = position.board.san(move)
            except ValueError:
                san = uci

            if uci in book_moves:
                weight, count = book_moves[uci]
                if weight > 0:
                    status = BOOK_STATUS_PRESENT
                    book_weight: Optional[int] = weight
                else:
                    status = BOOK_STATUS_ZERO
                    book_weight = 0
                book_share: Optional[float] = book_shares[uci]
            else:
                weight, count = None, 0
                status = BOOK_STATUS_ABSENT
                book_weight = None
                book_share = None

            observed_votes = observed_moves.get(uci, 0.0)
            observed_share = observed_shares.get(uci, 0.0)

            # Moves absent from the book are compared with an implicit zero
            # recommendation, while retaining their distinct status and null
            # book_weight in the export.
            delta = observed_share - (book_share if book_share is not None else 0.0)

            report.rows.append(DeviationRow(
                stratum=stratum,
                position_key=key,
                position_fen=position.fen,
                move_san=san,
                move_uci=uci,
                book_status=status,
                book_weight=book_weight,
                book_entries=count,
                book_share=book_share,
                observed_votes=observed_votes,
                observed_share=observed_share,
                delta=delta,
                position_book_total_weight=total_weight,
                position_observed_total=observed_total,
                position_tv_distance=tv_distance))

    return report


def _write_report(report: DeviationReport, handle: TextIO, file_format: str) -> None:
    if file_format == "csv":
        report.to_csv(handle)
    elif file_format == "json":
        report.to_json(handle)
    else:
        raise ValueError("format must be 'csv' or 'json'")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Polyglot opening book proportions with practical adoption in PGN mainlines.")
    parser.add_argument("pgn", nargs="+", type=argparse.FileType("r", encoding="utf-8"),
                        help="PGN file(s) with practical games")
    parser.add_argument("--book", required=True, help="Polyglot opening book")
    parser.add_argument("--count-mode", choices=["game", "opportunity"], default="game",
                        help="count a repeated position once per game (splitting votes between "
                             "distinct continuations) or once per visit (default: game)")
    parser.add_argument("--stratum", action="append", default=None, metavar="HEADER",
                        help="header used to stratify the report, may be repeated for fallbacks "
                             "(default: Event, then Site)")
    parser.add_argument("--include-all-legal", action="store_true",
                        help="include legal moves that are absent from the book and were not played")
    parser.add_argument("--include-positions-without-book", action="store_true",
                        help="include positions that do not occur in the book at all")
    parser.add_argument("--format", choices=["csv", "json"], default=None,
                        help="output format (default: inferred from --output, otherwise csv)")
    parser.add_argument("-o", "--output", type=argparse.FileType("w", encoding="utf-8"),
                        default=sys.stdout, help="output file (default: stdout)")
    args = parser.parse_args(argv)

    file_format = args.format
    if file_format is None:
        file_format = "json" if getattr(args.output, "name", "").endswith(".json") else "csv"

    stratum_headers = tuple(args.stratum or ["Event", "Site"])

    def all_games() -> Iterator[chess.pgn.Game]:
        for handle in args.pgn:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                yield game

    with chess.polyglot.open_reader(args.book) as book:
        report = analyze_games(
            all_games(),
            book,
            count_mode=args.count_mode,
            stratum_headers=stratum_headers,
            include_all_legal=args.include_all_legal,
            include_positions_without_book=args.include_positions_without_book)

    _write_report(report, args.output, file_format)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Allow piping into head/less without a traceback.
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
