Polyglot opening book reading
=============================

.. autofunction:: chess.polyglot.open_reader

.. autoclass:: chess.polyglot.Entry
    :members:


.. autoclass:: chess.polyglot.MemoryMappedReader
    :members:

.. py:data:: chess.polyglot.POLYGLOT_RANDOM_ARRAY
    :value: [0x9D39247E33776D41, ..., 0xF8D626AAAF278509]

    Array of 781 polyglot compatible pseudo random values for Zobrist hashing.

.. autofunction:: chess.polyglot.zobrist_hash


Practical deviations from the book
----------------------------------

:mod:`chess.book_report` compares the weights in a Polyglot book with the
moves actually chosen in a collection of PGN games. Only the mainline of each
game is counted; PGN sidelines are analysis, not practical play.

Positions are identified by their Polyglot Zobrist hash, so transpositions
are merged. Two counting modes are available:

``opportunity``
    Every visit to a position is one observation.

``game``
    Each game contributes one vote per position it reaches. If the same game
    continues with more than one distinct move from that position (for example
    after a repetition or transposition), the vote is split equally between
    the distinct continuations.

The report distinguishes a move that is absent from the book from a move with
an explicit weight of ``0``. Polyglot books commonly use ``0`` to delete a
move without rewriting the file, which is not the same as never having had an
entry. In CSV exports an absent move has an empty ``book_weight`` and
``book_share``; an explicit zero-weight entry has ``book_weight`` ``0``. JSON
uses ``null`` for absent data. The ``book_status`` column contains
``present``, ``zero_weight``, or ``absent``.

On the command line::

    python -m chess.book_report games.pgn --book book.bin \
        --count-mode game --stratum Event -o deviations.csv

.. autoclass:: chess.book_report.DeviationRow
    :members:

.. autoclass:: chess.book_report.DeviationReport
    :members:

.. autofunction:: chess.book_report.analyze_games
