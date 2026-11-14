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

Indexing sparse board photos
----------------------------

A :class:`~chess.pgn.BoardPhoto` records only the squares that are actually
legible in a photo of a board. Omitted squares are unknown, so the same photo
can match many positions.

.. autoclass:: chess.pgn.BoardPhoto
    :members:

:func:`~chess.pgn.index_photos` indexes photos in shooting order. By default
each photo is required to lie on one variation path, so a sequence of photos
can skip moves but cannot jump between branches. The result is narrowed from
both sides: a candidate for an early photo only survives if a later photo is
still reachable from it.

Candidates are individual nodes of the game tree, never positions. If the same
position can be reached by multiple move orders, all of those paths are
retained and can be inspected separately with
:meth:`~chess.pgn.PhotoMatch.transpositions`. No candidate is merged or dropped
just because its position looks identical.

Every :class:`~chess.pgn.PhotoMatch` also carries
:class:`~chess.pgn.PhotoSquareEvidence` for each visible square, stating how
many nodes it agrees with, how many it rejects, and how many nodes it rejects
on its own. :meth:`~chess.pgn.PhotoIndex.report` renders this as a per-photo
text report.

>>> import io
>>> import chess
>>> import chess.pgn
>>>
>>> game = chess.pgn.read_game(io.StringIO("1. e4 e5 (1... c5 2. Nf3) 2. Nf3 Nc6 *"))
>>> photos = [
...     chess.pgn.BoardPhoto({"e4": "P"}),
...     chess.pgn.BoardPhoto({"c5": "p"}),
... ]
>>> index = chess.pgn.index_photos(game, photos)
>>> index.complete
True
>>> print(index.report())  # doctest: +ELLIPSIS
Photo 0: ... candidate(s), ... locally compatible
  e4=P: agrees with ..., rejects ...
  sequence narrowed: ... locally compatible node(s) removed (...)
Photo 1: ... candidate(s), ... locally compatible
  c5=p: agrees with ..., rejects ...

.. autofunction:: chess.pgn.index_photos

.. autoclass:: chess.pgn.PhotoIndex
    :members:

.. autoclass:: chess.pgn.PhotoMatch
    :members:

.. autoclass:: chess.pgn.PhotoCandidate
    :members: uci_path, visual_key

.. autoclass:: chess.pgn.PhotoSquareEvidence
    :members: square_name, expected_symbol

.. autoclass:: chess.pgn.PhotoConflict
    :members: square_name, expected_symbol, actual_symbol

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

Skimming
--------

These functions allow for quickly skimming games without fully parsing them.

.. autofunction:: chess.pgn.read_headers

.. autofunction:: chess.pgn.skip_game
