PGN parsing and writing
=======================

Parsing
-------

.. autofunction:: chess.pgn.read_game

Writing
-------

If you want to export your game with all headers, comments and variations,
you can do it like this:

>>> import chess
>>> import chess.pgn
>>>
>>> game = chess.pgn.Game()
>>> game.headers["Event"] = "Example"
>>> node = game.add_variation(chess.Move.from_uci("e2e4"))
>>> node = node.add_variation(chess.Move.from_uci("e7e5"))
>>> node.comment = "Comment"
>>>
>>> print(game)
[Event "Example"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "?"]
[Black "?"]
[Result "*"]
<BLANKLINE>
1. e4 e5 { Comment } *

Remember that games in files should be separated with extra blank lines.

>>> print(game, file=open("/dev/null", "w"), end="\n\n")

Use the :class:`~chess.pgn.StringExporter()` or
:class:`~chess.pgn.FileExporter()` visitors if you need more control.

Game model
----------

Games are represented as a tree of moves. Conceptually each node represents a
position of the game. The tree consists of one root node
(:class:`~chess.pgn.Game`, also holding game headers) and many child
nodes (:class:`~chess.pgn.ChildNode`).
Both extend :class:`~chess.pgn.GameNode`.

.. note:: Some basic methods have complexity `O(n)` for a game with n moves.
  When following a variation, it is often more efficient to use visitors
  or incrementally update state (like board, ply counter, or turn).

.. autoclass:: chess.pgn.GameNode
    :members:

.. autoclass:: chess.pgn.Game
    :members: headers, errors, setup, accept, from_board, without_tag_roster

.. autoclass:: chess.pgn.ChildNode
    :members: parent, move, starting_comment, nags, san, uci, end

Visitors
--------

Visitors are an advanced concept for game tree traversal.

.. autoclass:: chess.pgn.BaseVisitor
    :members:

The following visitors are readily available.

.. autoclass:: chess.pgn.GameBuilder
    :members: handle_error, result

.. autoclass:: chess.pgn.HeadersBuilder

.. autoclass:: chess.pgn.BoardBuilder

.. autoclass:: chess.pgn.SkipVisitor

.. autoclass:: chess.pgn.StringExporter

.. autoclass:: chess.pgn.FileExporter

NAGs
----

Numeric anotation glyphs describe moves and positions using standardized codes
that are understood by many chess programs. During PGN parsing, annotations
like ``!``, ``?``, ``!!``, etc., are also converted to NAGs.

.. autodata:: chess.pgn.NAG_GOOD_MOVE
.. autodata:: chess.pgn.NAG_MISTAKE
.. autodata:: chess.pgn.NAG_BRILLIANT_MOVE
.. autodata:: chess.pgn.NAG_BLUNDER
.. autodata:: chess.pgn.NAG_SPECULATIVE_MOVE
.. autodata:: chess.pgn.NAG_DUBIOUS_MOVE

Position indexing
-----------------

Opening databases commonly merge game-tree nodes when the piece placement is
identical. That is not sufficient for two positions that only *look* the same:
castling rights, en passant possibilities and previous moves can all differ.

:class:`~chess.pgn.PGNIndex` builds two independent layers while importing
PGN:

* :attr:`~chess.pgn.PGNIndex.moves` (or
  :attr:`~chess.pgn.PGNIndex.by_moves`) answers queries by the set of
  currently legal moves. Positions with the same placement but different
  castling or en passant possibilities have different legal move sets and do
  not meet here.
* :attr:`~chess.pgn.PGNIndex.claims` (or
  :attr:`~chess.pgn.PGNIndex.by_claim`) answers queries by repetition identity
  and draw-claim eligibility under configurable
  :class:`~chess.pgn.DrawRules`.

>>> import io
>>> import chess.pgn
>>>
>>> pgn = io.StringIO(
...     "1. Nf3 ( 1. Na3 Nc6 2. Nb1 Nb8 ) "
...     "1... Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8 *")
>>> game = chess.pgn.read_game(pgn)
>>> index = chess.pgn.PGNIndex.from_game(game)
>>>
>>> # Both lines transpose to the same piece placement.
>>> final_board = game.end().board()
>>> all(entry.board.board_fen() == final_board.board_fen()
...     for entry in index.find(final_board))
True
>>>
>>> # The mainline allows a threefold-repetition claim, the shorter
>>> # variation does not.
>>> sorted(entry.claim.status.value for entry in index.find_claim(final_board))
['claimable', 'not_claimable', 'not_claimable', 'not_claimable']

The move layer can also be queried by an exact move set or by a move:

>>> len(index.find_by_moves(index.legal_moves(final_board)))
4
>>> len(index.find_move(chess.Move.from_uci("e2e4")))
7

If a database used only piece placement, two positions with different
castling rights would incorrectly be merged. The placement helper exposes that
coarse identity explicitly, while normal move lookups keep them apart:

>>> can_castle = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
>>> no_castling = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w - - 0 1")
>>> castling_index = chess.pgn.PositionIndex([can_castle])
>>> len(castling_index.find_by_placement(no_castling))
1
>>> len(castling_index.find(no_castling))
0

Claim assessments distinguish three outcomes:

* :data:`~chess.pgn.ClaimStatus.CLAIMABLE`: the claim is proven by the
  available move history (including a claim available on the next legal move).
* :data:`~chess.pgn.ClaimStatus.NOT_CLAIMABLE`: the claim is proven not to be
  available.
* :data:`~chess.pgn.ClaimStatus.UNKNOWN`: the available history is
  insufficient. In particular, when a game starts from a FEN with a non-zero
  half-move clock and no later irreversible move, earlier repetitions may be
  missing. The fifty-move rule remains decidable from the FEN's half-move
  clock, but repetition claims are reported as unknown unless one is already
  proven locally.

.. autoclass:: chess.pgn.PGNIndex
    :members:

.. autoclass:: chess.pgn.PositionIndex
    :members:

.. autoclass:: chess.pgn.ClaimIndex
    :members:

.. autoclass:: chess.pgn.IndexEntry
    :members:

.. autoclass:: chess.pgn.DrawRules

.. autoclass:: chess.pgn.ClaimAssessment

.. autoclass:: chess.pgn.ClaimStatus

.. autofunction:: chess.pgn.build_index

.. autofunction:: chess.pgn.assess_claim

.. autofunction:: chess.pgn.move_key

.. autofunction:: chess.pgn.position_key

The two layers are also available through dedicated, optional imports:

>>> import chess.index
>>> chess.index.PGNIndex is chess.pgn.PGNIndex
True

Skimming
--------

These functions allow for quickly skimming games without fully parsing them.

.. autofunction:: chess.pgn.read_headers

.. autofunction:: chess.pgn.skip_game
