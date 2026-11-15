Training data export
====================

Annotated games are trees of moves, not single lines. The helpers in this
module turn such a game into weighted ``(position, next move)`` training
samples while keeping the total weight of each game fixed, regardless of how
many comments or variations it contains.

At every fork the incoming weight is split among the allocating child moves in
proportion to their assigned relative weights. Variations that only
demonstrate bad moves (by default, moves marked with ``?``, ``??``, ``?!`` or
the "worst move" NAG) stay traversable but take no weight, and their share is
redistributed among the remaining moves. Identical positions reached through
different move orders are merged into one sample, while every contributing
path is retained.

.. autofunction:: chess.training.export_samples

.. autofunction:: chess.training.export_game_weights

.. autoclass:: chess.training.TrainingSample
    :members:

.. autoclass:: chess.training.PathOrigin
    :members:

.. autofunction:: chess.training.excluded_by_nag

.. autodata:: chess.training.DEFAULT_EXCLUDED_NAGS
